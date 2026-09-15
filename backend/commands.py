"""斜杠命令表：单点定义，前端只负责路由。

设计要点：
- 命令是**数据**（Command 表），不是散落的 if-else。加一条命令只改这里。
- 每条命令标注 `where`，决定它在哪个线程执行：
    "ui"    界面线程直接处理（清屏、退出等，不碰会话状态）
    "agent" 必须交给 Agent 线程（会话状态只在那条线程里）
- 本模块是纯逻辑，不导入界面、不导入 agent，方便单独测试。

命令永不写入对话历史，避免污染上下文、白烧 token。

输入约定：
- `/help`  → 命令
- `//abc`  → 转义：原样当普通消息发送，内容为 `/abc`（用于绝对路径等）
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    """一条斜杠命令的定义。"""

    name: str                      # 命令名，不含斜杠
    summary: str                   # 一句话说明，用于 /help 与补全菜单
    where: str                     # "ui" 或 "agent"
    aliases: tuple = ()            # 别名，如 exit 的 quit
    usage: str = ""                # 参数用法，如 "/model <名称>"

    @property
    def display(self) -> str:
        """补全菜单里显示的名字（带斜杠）。"""
        return "/" + self.name


# 命令表。顺序即 /help 与菜单里的展示顺序。
COMMANDS = (
    Command("help", "显示全部命令", "ui", aliases=("h", "?"), usage="/help [命令名]"),
    Command("model", "切换模型供应商与模型（不带参数弹出多级菜单）", "agent",
            usage="/model [供应商] [模型]"),
    Command("models", "在线拉取并列出模型清单（默认当前供应商）", "agent",
            usage="/models [供应商]"),
    Command("status", "查看轮次、上下文占用、压缩情况", "agent"),
    Command("tools", "列出当前加载的工具", "agent"),
    Command("compact", "手动压缩较早的历史为摘要", "agent"),
    Command("reload", "重新加载工具（等价 Ctrl+R）", "agent"),
    Command("clear", "清屏，对话历史不受影响（等价 Ctrl+L）", "ui", aliases=("cls",)),
    Command("exit", "退出程序（等价 Ctrl+Q）", "ui", aliases=("quit", "q")),
)

# 名字（含别名）→ Command，供 O(1) 查找
_INDEX = {}
for _cmd in COMMANDS:
    for _key in (_cmd.name,) + _cmd.aliases:
        _INDEX[_key] = _cmd


class Parsed:
    """解析结果：是命令、是转义消息，还是普通消息。"""

    def __init__(self, kind, command=None, arg="", text=""):
        self.kind = kind          # "command" | "escape" | "text"
        self.command = command    # kind=="command" 时有效
        self.arg = arg            # 命令参数（原文，未做二次切分）
        self.text = text          # kind=="escape" 时，转义后的原文


def is_command_line(text: str) -> bool:
    """判断一行输入是否要走命令解析（以 / 开头即算，未知命令也走）。"""
    return text.startswith("/")


def parse(text: str) -> Parsed:
    """解析一行输入。非命令输入返回 kind="text" 原样返回。

    - `//xxx`  → kind="escape"，text="/xxx"（去掉一个斜杠后当普通消息发）
    - `/xxx`   → kind="command"，command=None 表示未知命令
    """
    if not is_command_line(text):
        return Parsed("text", text=text)

    if text.startswith("//"):
        return Parsed("escape", text=text[1:])

    body = text[1:].strip()
    if not body:
        # 只输入一个 "/"：当作未知命令，交给上层提示
        return Parsed("command", command=None, arg="")

    name, _, arg = body.partition(" ")
    return Parsed("command", command=_INDEX.get(name.lower()), arg=arg.strip())


def matching(prefix: str):
    """按前缀匹配命令，供补全菜单使用。

    prefix 可以带前导斜杠（如 "/st"）也可以是裸名字。返回去重后的命令列表，
    名字前缀命中优先，别名命中排后面。
    """
    p = prefix[1:] if prefix.startswith("/") else prefix
    p = p.lower()
    if not p:
        return list(COMMANDS)

    primary = [c for c in COMMANDS if c.name.startswith(p)]
    secondary = [
        c for c in COMMANDS
        if c not in primary and any(a.startswith(p) for a in c.aliases)
    ]
    return primary + secondary


def help_text() -> str:
    """生成 /help 的多行文本。"""
    width = max(len(c.display) for c in COMMANDS)
    lines = ["可用命令："]
    for c in COMMANDS:
        extra = f"（别名 {', '.join('/' + a for a in c.aliases)}）" if c.aliases else ""
        lines.append(f"  {c.display.ljust(width)}  {c.summary}{extra}")
    lines.append("")
    lines.append("输入 // 开头可原样发送以斜杠开头的普通消息。")
    return "\n".join(lines)


def get(name: str):
    """按名字或别名取命令，找不到返回 None。"""
    return _INDEX.get((name or "").lstrip("/").lower())


def detail_text(name: str) -> str:
    """`/help <命令名>` 的详细说明。命令不存在时返回提示。"""
    cmd = get(name)
    if cmd is None:
        return unknown_text(name)
    lines = [f"{cmd.display}  {cmd.summary}"]
    if cmd.usage:
        lines.append(f"用法：{cmd.usage}")
    if cmd.aliases:
        lines.append("别名：" + "、".join("/" + a for a in cmd.aliases))
    lines.append("执行位置：" + ("界面（立即生效）" if cmd.where == "ui" else "会话线程"))
    return "\n".join(lines)


def unknown_text(name: str) -> str:
    """未知命令的提示文本。"""
    label = f"/{name}" if name else "/"
    return f"未知命令：{label}。输入 /help 查看全部命令。"
