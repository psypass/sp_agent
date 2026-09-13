"""斜杠命令表测试：解析、前缀匹配、帮助文本。纯逻辑，不依赖界面。

运行：python3 tests/test_commands.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import commands  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'✓' if condition else '✗'} {name}" + (f"   ← {detail}" if detail and not condition else ""))


def main():
    print("【命令表自洽性】")
    names = [c.name for c in commands.COMMANDS]
    check("命令名不重复", len(names) == len(set(names)), str(names))
    aliases = [a for c in commands.COMMANDS for a in c.aliases]
    check("别名不重复且不与命令名冲突",
          len(aliases) == len(set(aliases)) and not (set(aliases) & set(names)),
          f"别名={aliases}")
    check("where 取值合法", all(c.where in {"ui", "agent"} for c in commands.COMMANDS))
    check("每条命令都有说明", all(c.summary.strip() for c in commands.COMMANDS))

    print("\n【识别命令行】")
    check("/ 开头识别为命令", commands.is_command_line("/help"))
    check("普通文字不是命令", not commands.is_command_line("看看代码"))
    check("空串不是命令", not commands.is_command_line(""))

    print("\n【解析】")
    p = commands.parse("帮我改个 bug")
    check("普通文本原样返回", p.kind == "text" and p.text == "帮我改个 bug", f"{p.kind}")

    p = commands.parse("/status")
    check("命令被识别", p.kind == "command" and p.command is commands._INDEX["status"], str(p.command))

    p = commands.parse("/help status")
    check("命令参数被切出来", p.command.name == "help" and p.arg == "status", f"arg={p.arg!r}")

    p = commands.parse("/QUIT")
    check("命令名大小写不敏感且支持别名",
          p.kind == "command" and p.command.name == "exit", str(p.command))

    p = commands.parse("/nope")
    check("未知命令 command 为 None", p.kind == "command" and p.command is None)

    p = commands.parse("/")
    check("单独一个斜杠当作未知命令", p.kind == "command" and p.command is None)

    p = commands.parse("//Users/me/notes.txt")
    check("双斜杠转义为普通消息",
          p.kind == "escape" and p.text == "/Users/me/notes.txt", f"{p.kind} {p.text!r}")

    print("\n【前缀匹配（补全菜单用）】")
    got = [c.name for c in commands.matching("/st")]
    check("带斜杠前缀命中 status", got == ["status"], str(got))

    got = [c.name for c in commands.matching("st")]
    check("裸前缀同样命中", got == ["status"], str(got))

    got = commands.matching("/")
    check("空前缀返回全部命令", len(got) == len(commands.COMMANDS))

    got = [c.name for c in commands.matching("/c")]
    check("多个命中按定义顺序", got == ["compact", "clear"], str(got))

    got = [c.name for c in commands.matching("q")]
    check("别名也能命中（q → exit）", got == ["exit"], str(got))

    check("无匹配返回空", commands.matching("/zzz") == [])

    got = [c.name for c in commands.matching("/h")]
    check("名字命中排在别名命中之前（h 同时是 help 与别名）",
          got and got[0] == "help", str(got))

    print("\n【帮助文本】")
    text = commands.help_text()
    check("帮助里列出全部命令", all(c.display in text for c in commands.COMMANDS))
    check("帮助里含转义说明", "//" in text)
    check("帮助里标注了别名", "/quit" in text, text[:200])

    detail = commands.detail_text("help")
    check("详情含用法", "用法：/help" in detail, detail)
    check("详情对未知命令给提示", "未知命令" in commands.detail_text("nope"))

    check("未知命令提示含命令名与指引",
          "/nope" in commands.unknown_text("nope") and "/help" in commands.unknown_text("nope"))

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
