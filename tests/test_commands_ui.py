"""斜杠命令的界面行为测试：补全菜单、按键、界面/会话命令路由。

用 Textual pilot 无头驱动，不联网、不起 Agent 线程。
菜单交互是真实按键（pilot.press）驱动的，尽量贴近真人操作。

运行：python3 tests/test_commands_ui.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual.widgets import Input, OptionList, Static  # noqa: E402

from backend import commands  # noqa: E402
from frontend.tui import AgentTUI  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'✓' if condition else '✗'} {name}" + (f"   ← {detail}" if detail and not condition else ""))


def text_of(widget):
    content = widget.content
    return content.plain if hasattr(content, "plain") else str(content)


def notice_text(app):
    return "\n".join(text_of(w) for w in app.query(".notice"))


def drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


async def main():
    app = AgentTUI("/tmp/demo", "deepseek-v4-flash", start_worker=False)

    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", Input)
        menu = app.query_one("#cmd-menu", OptionList)

        print("菜单默认收起")
        check("启动时菜单不可见", not app._menu_visible())
        check("补全菜单控件已就位", menu.option_count == 0)

        print("输入 / 展开菜单")
        await pilot.press("/")
        await pilot.pause()
        check("菜单展开", app._menu_visible(), repr(prompt.value))
        check("列出全部命令", menu.option_count == len(commands.COMMANDS), str(menu.option_count))
        check("默认高亮第一条", menu.highlighted == 0, str(menu.highlighted))

        print("继续输入按前缀过滤")
        await pilot.press("s", "t")
        await pilot.pause()
        check("过滤到唯一命中", menu.option_count == 1, str(menu.option_count))
        check("命中的是 status", menu.highlighted_option.id == "status")

        print("Tab 补全")
        await pilot.press("tab")
        await pilot.pause()
        check("命令填进输入框并留空格", prompt.value == "/status ", repr(prompt.value))
        check("补全后菜单收起", not app._menu_visible())
        check("补全后光标在末尾", prompt.cursor_position == len(prompt.value), str(prompt.cursor_position))

        print("上下键切换高亮")
        prompt.value = "/"
        await pilot.pause()
        check("重新展开", app._menu_visible())
        menu.highlighted = 0
        await pilot.press("down")
        await pilot.pause()
        check("down 下移高亮", menu.highlighted == 1, str(menu.highlighted))
        await pilot.press("up")
        await pilot.pause()
        check("up 上移高亮", menu.highlighted == 0, str(menu.highlighted))

        print("菜单开着时 Esc 只收菜单")
        await pilot.press("escape")
        await pilot.pause()
        check("菜单被收起", not app._menu_visible())
        check("没有触发中断", app._cancel.is_set() is False)

        print("已输入空格（进入参数）时菜单不展开")
        prompt.value = "/help "
        await pilot.pause()
        check("带空格的命令不再弹菜单", not app._menu_visible())

        print("会话命令路由到 Agent 线程")
        drain(app._to_agent)
        prompt.value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        sent = drain(app._to_agent)
        check("投递 __cmd__ 与命令对象",
              len(sent) == 1 and sent[0][0] == "__cmd__" and sent[0][1][0].name == "status", str(sent))
        check("命令期间置忙", app._busy is True)
        check("状态栏显示处理中", "处理中" in text_of(app.query_one("#status", Static)))
        check("命令上屏（带斜杠）", "/status" in text_of(app.query(".user").last()))

        print("忙碌时拦截会话命令")
        before = len(app.query(".notice"))
        prompt.value = "/tools"
        await pilot.press("enter")
        await pilot.pause()
        check("忙碌时不再投递", drain(app._to_agent) == [])
        check("忙碌时有提示", len(app.query(".notice")) == before + 1)

        # worker 结束后会发 turn_end，这里手动模拟
        app._ui.turn_end()
        app._pump()
        await pilot.pause()
        check("turn_end 后解除置忙", app._busy is False)

        print("界面命令当场执行")
        drain(app._to_agent)
        before = len(app.query(".notice"))
        prompt.value = "/help"
        await pilot.press("enter")
        await pilot.pause()
        check("/help 有输出", len(app.query(".notice")) == before + 1)
        check("/help 不投递给 Agent", drain(app._to_agent) == [])
        check("/help 不置忙", app._busy is False)
        check("帮助文本列出全部命令",
              all(c.display in notice_text(app) for c in commands.COMMANDS))
        check("输入框被清空", prompt.value == "")

        print("/help <命令名> 显示详情")
        prompt.value = "/help status"
        await pilot.press("enter")
        await pilot.pause()
        check("详情含用法说明", "执行位置" in notice_text(app), notice_text(app)[-120:])

        print("/clear 清屏")
        drained_before = len(app.query("#conv > *"))
        prompt.value = "/clear"
        await pilot.press("enter")
        await pilot.pause()
        check("对话区被清空", len(app.query("#conv > *")) < drained_before,
              f"{drained_before} -> {len(app.query('#conv > *'))}")

        print("未知命令")
        drain(app._to_agent)
        prompt.value = "/nope"
        await pilot.press("enter")
        await pilot.pause()
        check("提示未知命令", "未知命令" in text_of(app.query(".notice").last()),
              text_of(app.query(".notice").last()))
        check("未知命令不发给模型", drain(app._to_agent) == [])

        print("// 转义为普通消息")
        prompt.value = "//Users/me/notes.txt"
        await pilot.press("enter")
        await pilot.pause()
        sent = drain(app._to_agent)
        check("去掉一个斜杠后当普通消息发送",
              sent == [("__ask__", "/Users/me/notes.txt")], str(sent))
        app._ui.turn_end()
        app._pump()
        await pilot.pause()

        print("菜单在转义输入上不展开")
        prompt.value = "//"
        await pilot.pause()
        check("// 不弹菜单", not app._menu_visible())

        print("裸 exit 仍然可用（向后兼容）")
        prompt.value = "exit"
        await pilot.press("enter")
        await pilot.pause()
        check("裸 exit 不进入处理流程", app._busy is False and drain(app._to_agent) == [])

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
