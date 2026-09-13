"""TUI 冒烟测试：用 Textual 的 pilot 无头驱动界面，验证渲染与交互不报错。

不联网、不启动 Agent 线程（start_worker=False），只把 UI 事件直接灌进队列，
检查 widget 是否正确生成、状态是否正确流转。

运行：python3 tests/test_smoke.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual.widgets import Input, Markdown, Static  # noqa: E402

from frontend.tui import AgentTUI, ToolCard  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'✓' if condition else '✗'} {name}" + (f"   ← {detail}" if detail and not condition else ""))


def text_of(widget):
    """取出 Static 的文本内容（Text 或 str 都兼容）。"""
    content = widget.content
    return content.plain if hasattr(content, "plain") else str(content)


def status_text(app):
    return text_of(app.query_one("#status", Static))


async def main():
    app = AgentTUI("/tmp/demo", "deepseek-v4-flash", start_worker=False)

    async with app.run_test() as pilot:
        print("布局")
        check("Header 存在", len(app.query("Header")) == 1)
        check("对话区 #conv 存在", len(app.query("#conv")) == 1)
        check("状态栏 #status 存在", len(app.query("#status")) == 1)
        check("输入框 #prompt 存在", len(app.query("#prompt")) == 1)
        check("Footer 存在", len(app.query("Footer")) == 1)
        check("输入框默认获得焦点", app.focused is app.query_one("#prompt", Input))
        check("启动提示已上屏", len(app.query(".notice")) >= 1)

        print("流式正文")
        app._ui.delta("我先看一下")
        app._ui.delta("当前实现。")
        app._pump()
        await pilot.pause()
        streaming = app.query(".assistant")
        check("流式期间生成正文 widget", len(streaming) == 1)
        check("多个分片被拼接", text_of(streaming.first()) == "我先看一下当前实现。", text_of(streaming.first()))
        check("流式期间是纯文本（未解析 Markdown）", not isinstance(streaming.first(), Markdown))

        app._ui.assistant_end("我先看一下当前实现。")
        app._pump()
        await pilot.pause()
        check("结束后换成 Markdown widget", len(app.query(Markdown)) == 1)
        check("旧的流式 widget 已移除", all(isinstance(w, Markdown) for w in app.query(".assistant")))
        check("Markdown 内容正确", "我先看一下当前实现。" in app.query_one(Markdown).source)

        print("工具卡片")
        app._ui.tool_start("read_file", "path=tools.py offset=90")
        app._pump()
        await pilot.pause()
        check("生成工具卡片", len(app.query(ToolCard)) == 1)
        card = app.query_one(ToolCard)
        check("标题含工具名", "read_file" in card.title, card.title)
        check("默认折叠", card.collapsed is True)

        app._ui.tool_end("read_file", "90\tdef grep(...):", ok=True, seconds=0.02)
        app._pump()
        await pilot.pause()
        check("成功后标题带 ✓", "✓" in card.title, card.title)
        check("标题带耗时", "0.02s" in card.title, card.title)
        check("正文含输出", "def grep" in text_of(card._body))
        check("成功时不额外弹提示", len(app.query(".notice")) == 1)

        app._ui.tool_start("bash", "command=exit 1")
        app._pump()
        app._ui.tool_end("bash", "退出码：1", ok=False, seconds=1.5)
        app._pump()
        await pilot.pause()
        failed = app.query(ToolCard).last()
        check("失败标题带 ✗", "✗" in failed.title, failed.title)
        check("失败时自动展开便于查错", failed.collapsed is False)
        check("失败额外提示", len(app.query(".notice")) == 2)

        print("超长输出截断")
        app._ui.tool_start("bash", "command=yes")
        app._pump()
        app._ui.tool_end("bash", "x" * 9999, ok=True, seconds=3.0)
        app._pump()
        await pilot.pause()
        body = text_of(app.query(ToolCard).last()._body)
        check("输出被截断（未撑爆界面）", len(body) < 3400, f"实际 {len(body)} 字符")

        print("状态栏")
        app._ui.status(turns=3, chars=12345, limit=80000, compressed=2, tools=7,
                       model="deepseek-v4-flash")
        app._pump()
        await pilot.pause()
        text = status_text(app)
        check("显示轮次", "轮次 3" in text, text)
        check("显示工具数", "工具 7" in text, text)
        check("显示上下文占用与百分比", "12.3k/80k" in text and "15%" in text, text)
        check("显示已压缩轮次", "已压缩 2 轮" in text, text)
        check("空闲时显示就绪", "就绪" in text, text)

        print("输入交互")
        prompt = app.query_one("#prompt", Input)
        await pilot.press("enter")
        await pilot.pause()
        check("空输入被忽略", app._busy is False)

        prompt.value = "把 grep 改成支持多模式"
        await pilot.press("enter")
        await pilot.pause()
        check("用户消息已上屏", "把 grep 改成支持多模式" in text_of(app.query(".user").last()))
        check("输入框被清空", prompt.value == "")
        check("任务已投递到 Agent 队列",
              app._to_agent.get_nowait() == ("__ask__", "把 grep 改成支持多模式"))
        check("状态栏显示处理中", "处理中" in status_text(app), status_text(app))

        print("忙碌时再次回车")
        before = len(app.query(".notice"))
        prompt.value = "第二条指令"
        await pilot.press("enter")
        await pilot.pause()
        check("被拦截并提示", len(app.query(".notice")) == before + 1,
              f"{before} -> {len(app.query('.notice'))}")
        check("已输入的文字被保留（不会被吞掉）", prompt.value == "第二条指令", prompt.value)
        check("未投递第二条任务", app._to_agent.empty() is True)
        prompt.value = ""

        print("中断")
        before = len(app.query(".notice"))
        app.action_cancel()
        await pilot.pause()
        check("Esc 触发中断标志", app._cancel.is_set() is True)
        check("中断有提示", len(app.query(".notice")) == before + 1)

        print("回合结束")
        app._ui.turn_end()
        app._pump()
        await pilot.pause()
        check("取消中断标志", app._cancel.is_set() is False)
        check("状态回到就绪", "就绪" in status_text(app), status_text(app))

        before = len(app.query(".notice"))
        app.action_cancel()
        await pilot.pause()
        check("空闲时 Esc 只提示不报错", len(app.query(".notice")) == before + 1)

        print("清屏")
        before = len(app.query("#conv > *"))
        app.action_clear_log()
        await pilot.pause()
        after = len(app.query("#conv > *"))
        check("对话区被清空", after < before, f"{before} -> {after}")

        print("热重载命令")
        app.action_reload()
        check("Ctrl+R 投递重载命令", app._to_agent.get_nowait() == ("__reload__", None))

        print("TuiUI 事件接口（回归：notice 的 kind 曾与 _push 形参重名，导致 Agent 线程一启动就崩）")
        api = app._ui
        start_qsize = api.events.qsize()
        api.notice("提示A", kind="warn")
        api.notice("提示B")  # 走默认 kind，确保默认值分支也被覆盖
        api.delta("分片")
        api.assistant_end("完整正文")
        api.tool_start("demo", "a=1")
        api.tool_end("demo", "结果", ok=True, seconds=0.1)
        api.status(turns=1, chars=10, limit=100, compressed=0, tools=1, model="m")
        api.turn_end()
        check("8 个事件全部入队", api.events.qsize() == start_qsize + 8, str(api.events.qsize()))
        payloads = list(api.events.queue)[start_qsize:]
        check("notice 的 kind 被正确保留",
              payloads[0] == ("notice", {"text": "提示A", "kind": "warn"})
              and payloads[1] == ("notice", {"text": "提示B", "kind": "info"}),
              str(payloads[:2]))
        app._pump()
        await pilot.pause()
        check("经队列的正文渲染成 Markdown",
              any("完整正文" in m.source for m in app.query(Markdown)))
        check("经队列的工具卡片出现", len(app.query(ToolCard)) >= 1)

        print("exit 指令")
        prompt.value = "exit"
        await pilot.press("enter")
        await pilot.pause()
        check("exit 不进入处理流程", app._busy is False)

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
