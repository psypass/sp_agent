"""Agent 核心逻辑测试：用假客户端跑完整回合，不联网。

重点验证：
1. 「模型 → 工具 → 模型」循环正常，工具真的被执行；
2. 消息序列符合 OpenAI 协议（每个 assistant.tool_calls 后面都有配对的 tool 结果）；
3. 中断时不会留下悬空的 tool_calls；
4. 上下文裁剪与摘要压缩链路能跑通。

运行：python3 tests/test_session.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import agent  # noqa: E402
from frontend import ui as ui_module  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'✓' if condition else '✗'} {name}" + (f"   ← {detail}" if detail and not condition else ""))


# ---------------- 假的流式响应 ----------------
def text_chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))])


def tool_chunk(index, call_id=None, name=None, arguments=None):
    tc = SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[tc]))])


def empty_chunk():
    return SimpleNamespace(choices=[])


class FakeClient:
    """按脚本依次返回若干次「流式响应」，并记录每次请求。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return iter(self.scripts.pop(0))


class RecordingUI(ui_module.UI):
    def __init__(self):
        self.events = []
        self.on_tool_end = None

    def notice(self, text, kind="info"):
        self.events.append(("notice", kind, text))

    def delta(self, text):
        self.events.append(("delta", text))

    def assistant_end(self, text):
        self.events.append(("assistant_end", text))

    def tool_start(self, name, args):
        self.events.append(("tool_start", name, args))

    def tool_end(self, name, result, ok=True, seconds=0.0):
        self.events.append(("tool_end", name, ok))
        if self.on_tool_end:
            self.on_tool_end()

    def status(self, **fields):
        self.events.append(("status", fields))

    def turn_end(self):
        self.events.append(("turn_end",))

    def kinds(self):
        return [e[0] for e in self.events]


def protocol_ok(messages):
    """检查每个 assistant.tool_calls 后面是否紧跟了数量匹配的 tool 结果。"""
    for i, msg in enumerate(messages):
        calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if not calls:
            continue
        following = sorted(
            m.get("tool_call_id") for m in messages[i + 1:i + 1 + len(calls)] if m.get("role") == "tool"
        )
        expected = sorted(c["id"] for c in calls)
        if following != expected:
            return False, f"第 {i} 条消息的 tool_calls 未配对：期望 {expected}，实际 {following}"
    return True, ""


def main():
    print("【场景一】模型调用一次工具，再给出结论")
    fake = FakeClient([
        [text_chunk("我先读一下文件。"), empty_chunk(), tool_chunk(0, "call_1", "read_file", ""),
         tool_chunk(0, arguments='{"path": "backend/tools.py", "offset": 90, "limit": 3}')],
        [text_chunk("读完了，grep 在"), text_chunk("第 90 行。")],
    ])
    agent.get_client = lambda: fake

    recorder = RecordingUI()
    session = agent.AgentSession(recorder, root=Path(__file__).resolve().parent.parent, model="fake-model")
    session.handle("grep 在哪儿")

    kinds = recorder.kinds()
    check("事件顺序：状态→正文→工具→正文→收尾",
          kinds == ["status", "delta", "assistant_end", "tool_start", "tool_end",
                    "status", "delta", "delta", "assistant_end", "status", "turn_end"], str(kinds))
    check("发了两次模型请求", len(fake.requests) == 2)
    check("工具真的被执行了（读到 grep 定义）",
          "def grep" in session.messages[3]["content"], session.messages[3]["content"][:60])
    check("工具参数被格式化上屏", "path=backend/tools.py" in recorder.events[3][2], recorder.events[3][2])

    ok, why = protocol_ok(session.messages)
    check("消息序列符合协议", ok, why)
    check("历史结构：[system, user, assistant, tool, assistant]",
          [m["role"] for m in session.messages] == ["system", "user", "assistant", "tool", "assistant"],
          str([m["role"] for m in session.messages]))

    print("\n【场景二】模型一次并发调用多个工具")
    fake2 = FakeClient([
        [tool_chunk(0, "c0", "list_dir", '{"path": "."}'),
         tool_chunk(1, "c1", "read_file", '{"path": "backend/context.py", "limit": 2}')],
        [text_chunk("看完了。")],
    ])
    agent.get_client = lambda: fake2
    recorder2 = RecordingUI()
    session2 = agent.AgentSession(recorder2, root=Path(__file__).resolve().parent.parent, model="fake-model")
    session2.handle("看看目录和 context.py")
    ok, why = protocol_ok(session2.messages)
    check("多工具并发后协议仍合法", ok, why)
    check("两个工具都被执行", len([e for e in recorder2.events if e[0] == "tool_end"]) == 2)

    print("\n【场景三】中途中断：不留悬空 tool_calls")
    fake3 = FakeClient([
        [tool_chunk(0, "d0", "list_dir", '{"path": "."}'),
         tool_chunk(1, "d1", "list_dir", '{"path": "."}'),
         tool_chunk(2, "d2", "list_dir", '{"path": "."}')],
    ])
    agent.get_client = lambda: fake3
    recorder3 = RecordingUI()
    session3 = agent.AgentSession(recorder3, root=Path(__file__).resolve().parent.parent, model="fake-model")

    # should_stop 是在每个流式分片上轮询的，所以用「第一个工具执行完」作为触发点最稳：
    # 这样流式阶段不中断（tool_calls 能完整拼出来），进入工具循环后才中断。
    stop_flag = {"stop": False}
    recorder3.on_tool_end = lambda: stop_flag.__setitem__("stop", True)

    session3.handle("跑三个工具", should_stop=lambda: stop_flag["stop"])
    ok, why = protocol_ok(session3.messages)
    check("中断后协议仍合法（无悬空 tool_calls）", ok, why)
    tool_msgs = [m for m in session3.messages if m["role"] == "tool"]
    check("三个 tool_call 都有结果", len(tool_msgs) == 3, f"实际 {len(tool_msgs)} 条")
    check("未执行的被标记为中断",
          sum("已中断" in m["content"] for m in tool_msgs) == 2,
          str([m["content"][:12] for m in tool_msgs]))
    check("只真正执行了一个工具", len([e for e in recorder3.events if e[0] == "tool_end"]) == 1)
    check("有中断提示", any(e[0] == "notice" and e[1] == "warn" for e in recorder3.events))

    print("\n【场景四】生成中断：丢弃半截 tool_calls")
    # 第三个分片到达前中断，此时 tool_call 只拼了一半，必须丢弃，否则历史非法。
    fake4 = FakeClient([
        [text_chunk("我正在想"), tool_chunk(0, "e0", "list_dir", '{"path": ".'),
         text_chunk("继续想")],
    ])
    agent.get_client = lambda: fake4
    recorder4 = RecordingUI()
    session4 = agent.AgentSession(recorder4, root=Path(__file__).resolve().parent.parent, model="fake-model")
    stop = {"n": 0}

    def stop_immediately():
        stop["n"] += 1
        return stop["n"] > 2  # 处理完前两个分片后立即中断

    session4.handle("问一句", should_stop=stop_immediately)
    ok, why = protocol_ok(session4.messages)
    check("中断后协议合法", ok, why)
    check("半截的 tool_calls 未被写入历史",
          "tool_calls" not in session4.messages[-1], str(session4.messages[-1])[:80])
    check("保住了已生成的部分正文", session4.messages[-1]["content"] == "我正在想",
          session4.messages[-1]["content"])
    check("未执行任何工具", not any(e[0] == "tool_start" for e in recorder4.events))

    print("\n【场景五】工具不存在 / 参数非法时不崩")
    fake5 = FakeClient([
        [tool_chunk(0, "f0", "no_such_tool", "{}")],
        [text_chunk("抱歉，换个方式。")],
    ])
    agent.get_client = lambda: fake5
    recorder5 = RecordingUI()
    session5 = agent.AgentSession(recorder5, root=Path(__file__).resolve().parent.parent, model="fake-model")
    session5.handle("调个不存在的工具")
    failures = [e for e in recorder5.events if e[0] == "tool_end" and e[2] is False]
    check("不存在的工具被标记失败", len(failures) == 1, str(recorder5.kinds()))
    ok, why = protocol_ok(session5.messages)
    check("失败后协议仍合法", ok, why)

    fake6 = FakeClient([
        [tool_chunk(0, "g0", "read_file", "{不是合法 json")],
        [text_chunk("参数写错了。")],
    ])
    agent.get_client = lambda: fake6
    recorder6 = RecordingUI()
    session6 = agent.AgentSession(recorder6, root=Path(__file__).resolve().parent.parent, model="fake-model")
    session6.handle("参数写错")
    bad = [m for m in session6.messages if m["role"] == "tool"]
    check("JSON 解析失败被友好处理", bad and "参数解析失败" in bad[0]["content"], str(bad)[:80])

    print("\n【场景六】上下文超限触发裁剪与摘要")
    summaries = {"n": 0}

    def fake_summarize(dropped, client, model, existing=""):
        summaries["n"] += 1
        return existing + f"（第{len(dropped)}轮）"

    from backend import context
    original = context.summarize
    context.summarize = fake_summarize
    original_trim = context.trim

    def fake_trim(messages, max_chars=None, keep_recent=None):
        if len(messages) > 5:
            dropped = messages[2:4]
            return messages[:2] + messages[4:], [dropped]
        return messages, []

    context.trim = fake_trim
    try:
        agent_recorder = RecordingUI()
        session7 = agent.AgentSession(agent_recorder, root=Path(__file__).resolve().parent.parent, model="fake-model")
        session7.messages = [{"role": "system", "content": "s"}] + [
            {"role": "user", "content": f"问题{i}"} for i in range(6)
        ]
        session7.compress()
        check("裁剪被调用并生成了摘要", summaries["n"] == 1)
        check("摘要消息插在 system 之后",
              "历史摘要" in session7.messages[1]["content"], str(session7.messages[1])[:60])
        check("摘要以 system 角色存在", session7.messages[1]["role"] == "system")
        check("压缩轮次被计数", session7.compressed_rounds == 1)
        check("有压缩提示", any(e[0] == "notice" and e[1] == "context" for e in agent_recorder.events))

        session7.compress()
        summaries["n"] = 0
        session7.compress()
        check("重复压缩不会插入第二条摘要",
              sum("历史摘要" in str(m.get("content", "")) for m in session7.messages) == 1)
    finally:
        context.summarize = original
        context.trim = original_trim

    print("\n【场景七】工具热重载后工作目录不丢")
    session8 = agent.AgentSession(RecordingUI(), root=Path(__file__).resolve().parent.parent, model="fake-model")
    from backend import tools
    tools.set_root("/tmp/__hijacked__")
    names = session8.reload_tools()
    check("重载后工具数正常", len(names) == 7, str(names))
    check("重载后工作目录被重新注入", str(tools.ROOT) == str(session8.root), str(tools.ROOT))
    check("system prompt 同步刷新", str(session8.root) in session8.messages[0]["content"])

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
