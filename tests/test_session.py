"""Agent 核心逻辑测试：用假客户端跑完整回合，不联网。

重点验证：
1. 「模型 → 工具 → 模型」循环正常，工具真的被执行；
2. 消息序列符合 OpenAI 协议（每个 assistant.tool_calls 后面都有配对的 tool 结果）；
3. 中断时不会留下悬空的 tool_calls；
4. 上下文裁剪与摘要压缩链路能跑通。

运行：python3 tests/test_session.py
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import agent  # noqa: E402
from frontend import ui as ui_module  # noqa: E402

# 场景一~六会用假客户端覆盖 agent.get_client，这里先留一份真函数，
# 供场景九验证「切模型后客户端重建」的真实逻辑。
_REAL_GET_CLIENT = agent.get_client

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

    print("\n【场景八】/compact 强制压缩与只读视图（供斜杠命令使用）")
    from backend import context as context_module
    saved_summarize = context_module.summarize
    seen = []

    def fake_summarize(dropped, client, model, existing=""):
        seen.append(len(dropped))
        return (existing + " 强制摘要").strip()

    context_module.summarize = fake_summarize
    try:
        recorder8 = RecordingUI()
        session9 = agent.AgentSession(recorder8, root=Path(__file__).resolve().parent.parent, model="fake-model")

        check("tool_names 与 schema 对齐",
              session9.tool_names() == [t["function"]["name"] for t in session9.tool_schemas],
              str(session9.tool_names()))
        detail = session9.status_detail()
        check("status_detail 含模型与目录",
              "fake-model" in detail and str(session9.root) in detail, detail)

        # 造 8 轮历史（超过 KEEP_RECENT_ROUNDS=6），强制压缩应丢掉多出的 2 轮
        for i in range(8):
            session9.messages.append({"role": "user", "content": f"问题 {i}"})
            session9.messages.append({"role": "assistant", "content": f"回答 {i}"})

        session9.compress(force=True)
        check("强制压缩丢弃多余轮次（8-6=2）", seen == [2], str(seen))
        remaining = sum(1 for m in session9.messages if m["role"] == "user")
        check("压缩后只留最近 N 轮", remaining == context_module.KEEP_RECENT_ROUNDS, str(remaining))
        check("压缩轮次被计数", session9.compressed_rounds == 2, str(session9.compressed_rounds))
        check("摘要以 system 消息插在 system 之后",
              session9.messages[0]["role"] == "system"
              and session9.messages[1]["role"] == "system"
              and "历史摘要" in session9.messages[1]["content"],
              str([m["role"] for m in session9.messages[:3]]))
        check("压缩有 context 提示",
              any(e[0] == "notice" and e[1] == "context" for e in recorder8.events))

        before_events = len(recorder8.events)
        session9.compress(force=True)
        check("无可压缩时给出提示而不报错",
              any(e[0] == "notice" and e[1] == "context" and "没有可压缩" in e[2]
                  for e in recorder8.events[before_events:]),
              str(recorder8.events[before_events:]))
        check("无可压缩时不调用摘要模型", seen == [2], str(seen))
    finally:
        context_module.summarize = saved_summarize

    print("\n【场景九】供应商网关：/model 切换会重建客户端、刷新状态栏")
    from backend import providers
    saved_env = {k: os.environ.pop(k, None) for k in (
        "SP_AGENT_PROVIDER", "SP_AGENT_MODEL", "SP_AGENT_BASE_URL", "SP_AGENT_API_KEY_ENV",
    )}
    os.environ["DASHSCOPE_API_KEY"] = "sk-dash"
    os.environ["DEEPSEEK_API_KEY"] = "sk-ds"
    try:
        recorder9 = RecordingUI()
        session10 = agent.AgentSession(recorder9, root=Path(__file__).resolve().parent.parent)
        session10.selection = providers.make_selection(provider="dashscope", model="qwen-plus")
        session10.model = session10.selection.model
        agent._SELECTION = session10.selection

        check("初始 spec 为 dashscope/qwen-plus", session10.current_spec() == "dashscope/qwen-plus",
              session10.current_spec())
        check("provider_list 标出当前供应商",
              [p.key for p, cur in session10.provider_list() if cur] == ["dashscope"],
              str(session10.provider_list()))

        # 造一个假客户端，验证切换后 get_client 会重建（base_url 跟着模型走）。
        built = []
        real_openai = agent.OpenAI

        class FakeOpenAI:
            def __init__(self, api_key=None, base_url=None):
                built.append(base_url)
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: iter([])))

        agent.OpenAI = FakeOpenAI
        agent._client = None
        agent._client_signature = None
        agent.get_client = _REAL_GET_CLIENT
        try:
            agent.get_client()
            check("首个客户端用 dashscope 网关", built[-1] == providers.get("dashscope").base_url, str(built))

            selection = session10.switch_model(provider="deepseek", model="deepseek-chat")
            check("切换后 model 更新", session10.model == "deepseek-chat", session10.model)
            check("切换后 selection 更新", session10.selection.provider.key == "deepseek")
            check("切换给出 model 类提示",
                  any(e[0] == "notice" and e[1] == "model" for e in recorder9.events),
                  str(recorder9.kinds()))

            agent.get_client()
            check("客户端按新网关重建", built[-1] == providers.get("deepseek").base_url, str(built))

            # 用别名 + 裸模型名切换
            session10.switch_model_from_arg("kimi moonshot-v1-32k")
            check("别名+模型切换生效",
                  session10.current_spec() == "moonshot/moonshot-v1-32k", session10.current_spec())

            # 缺 Key：切换仍生效，但给出 warn
            recorder9.events.clear()
            session10.switch_model(provider="openai", model="gpt-4o")
            check("缺 Key 时给 warn 提示",
                  any(e[0] == "notice" and e[1] == "warn" for e in recorder9.events),
                  str(recorder9.kinds()))
            check("缺 Key 不回滚选择（状态栏如实显示）",
                  session10.selection.provider.key == "openai", session10.selection.provider.key)
            check("缺 Key 时 get_client 抛 MissingAPIKey",
                  _raises(agent.MissingAPIKey, agent.get_client))

            # 清单外模型给 custom 提示
            recorder9.events.clear()
            session10.switch_model(provider="zhipu", model="glm-9-preview-xyz")
            check("自定义模型被标记 custom", session10.selection.custom)
            check("自定义模型有提醒",
                  any(e[0] == "notice" and e[1] == "warn" and "推荐清单" in e[2] for e in recorder9.events),
                  str(recorder9.events[-2:]))

            detail = session10.status_detail()
            check("status_detail 含供应商与网关",
                  "智谱" in detail and "open.bigmodel.cn" in detail, detail)
            check("状态事件带上 provider 字段",
                  any(e[0] == "status" and e[1].get("provider") == "zhipu" for e in recorder9.events),
                  str([e for e in recorder9.events if e[0] == "status"][-1:]))
        finally:
            agent.OpenAI = real_openai
            agent._client = None
            agent._client_signature = None
            agent._SELECTION = providers.resolve_config()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n【场景十】/model 命令：无参数列菜单、未知供应商给提示")
    recorder10 = RecordingUI()
    session11 = agent.AgentSession(recorder10, root=Path(__file__).resolve().parent.parent)

    cmd = agent.commands.get("model")
    check("命令表里注册了 /model", cmd is not None and cmd.where == "agent", str(cmd))
    check("命令表里注册了 /model 的用法", cmd is not None and "供应商" in cmd.usage, str(cmd))

    recorder10.events.clear()
    agent.run_session_command(session11, recorder10, cmd, "")
    listing = [e for e in recorder10.events if e[0] == "notice" and e[1] == "info"]
    check("无参数时打印供应商清单",
          listing and all(p.label in listing[-1][2] for p in providers.PROVIDERS),
          str(listing[-1:])[:200])
    check("清单不触发模型切换", not any(e[0] == "notice" and e[1] == "model" for e in recorder10.events))

    recorder10.events.clear()
    agent.run_session_command(session11, recorder10, cmd, "no_such_provider")
    check("单 token 的未知名字当自定义模型（不报错）",
          session11.current_spec() == "dashscope/no_such_provider", session11.current_spec())

    recorder10.events.clear()
    before_spec = session11.current_spec()
    agent.run_session_command(session11, recorder10, cmd, "no_such_provider some-model")
    check("双 token 时首个供应商拼错会报错",
          any(e[0] == "notice" and "未知供应商" in e[2] for e in recorder10.events),
          str(recorder10.events[:2]))
    check("拼错时不改动当前选择", session11.current_spec() == before_spec, session11.current_spec())

    recorder10.events.clear()
    agent.run_session_command(session11, recorder10, cmd, "deepseek")
    check("带参数直接切换",
          session11.current_spec() == "deepseek/deepseek-chat", session11.current_spec())

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
