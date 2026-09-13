"""Agent 主程序：对话循环、流式输出与工具调用编排（后端 / backend）。

分层：
- AgentSession  一次长驻对话的全部状态与单轮处理逻辑，不依赖任何终端呈现。
- stream_chat   流式调用模型，逐片交给 UI，并把分片拼回完整消息。
- main / run    两种入口：纯文本模式、以及 TTY 下的全屏 TUI。

目录划分：
- 后端 backend/：本文件、context.py（上下文管理）、tools.py（工具实现）。
- 前端 frontend/：ui.py（UI 事件接口）、tui.py（Textual 全屏界面）。
本文件只依赖 frontend.ui 的抽象接口，不依赖任何具体界面实现。
"""

import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from openai import OpenAI
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from backend import commands, context, tools
from frontend.ui import PlainUI

ROOT = Path.cwd().resolve()
MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"

SELF_PATH = Path(__file__).resolve()
TOOLS_PATH = Path(tools.__file__).resolve()


class MissingAPIKey(SystemExit):
    pass


def require_api_key() -> None:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise MissingAPIKey("请先设置：export DEEPSEEK_API_KEY='你的 API Key'")


_client = None


def get_client():
    """惰性创建模型客户端。

    放在函数里而不是模块顶层，是为了让 import agent 不再强制要求 API Key 已设置，
    否则连跑测试、看帮助都会直接崩掉。
    """
    global _client
    if _client is None:
        require_api_key()
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=BASE_URL)
    return _client


# ---------------- 热重载 ----------------
_tools_changed = threading.Event()
_self_changed = threading.Event()


class _ReloadHandler(FileSystemEventHandler):
    """监视 tools.py（热重载）与 agent.py（提示重启）。"""

    def _handle(self, src_path):
        p = Path(src_path).resolve()
        if p == TOOLS_PATH:
            _tools_changed.set()
        elif p == SELF_PATH:
            _self_changed.set()

    def on_modified(self, event):
        self._handle(event.src_path)

    def on_created(self, event):
        self._handle(event.src_path)


def start_watcher():
    """启动后台线程监视后端目录的两个源文件。重复调用只保留最后一个。

    后端文件都在 backend/ 下，所以只监视这一个目录即可（非递归）。
    """
    observer = Observer()
    observer.schedule(_ReloadHandler(), str(SELF_PATH.parent), recursive=False)
    observer.daemon = True
    observer.start()
    return observer


def format_args(args, limit: int = 160) -> str:
    """把工具参数字典压成一行摘要，过长则截断。"""
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", "⏎")
        if len(text) > limit:
            text = text[:limit] + f"…(+{len(text) - limit})"
        parts.append(f"{key}={text}")
    return " ".join(parts) or "（无参数）"


def stream_chat(messages, tool_schemas, ui, model=MODEL, should_stop=None):
    """流式调用模型：逐片推给 UI，并把分片拼成一条完整 assistant 消息。

    返回 SimpleNamespace(content, tool_calls, interrupted)，结构与非流式返回的
    message 一致，便于后续统一处理。should_stop 返回 True 时会提前收尾：
    丢弃半截的 tool_calls（避免拼出非法调用），只保留已生成的正文。
    """
    stream = get_client().chat.completions.create(
        model=model, messages=messages, tools=tool_schemas, stream=True,
    )

    content_parts = []
    tool_calls = {}  # index -> {id, name, arguments}
    interrupted = False

    for chunk in stream:
        if should_stop and should_stop():
            interrupted = True
            break
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta

        if delta.content:
            ui.delta(delta.content)
            content_parts.append(delta.content)

        if delta.tool_calls:
            for tc in delta.tool_calls:
                slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] += tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments

    text = "".join(content_parts)
    ui.assistant_end(text)

    calls = []
    if not interrupted:
        for idx in sorted(tool_calls):
            slot = tool_calls[idx]
            calls.append(SimpleNamespace(
                id=slot["id"],
                type="function",
                function=SimpleNamespace(name=slot["name"], arguments=slot["arguments"]),
            ))

    return SimpleNamespace(content=text or None, tool_calls=calls or None, interrupted=interrupted)


class AgentSession:
    """一次长驻对话的全部状态，以及「处理一轮用户输入」的完整逻辑。"""

    def __init__(self, ui, root=ROOT, model=MODEL):
        self.ui = ui
        self.root = Path(root).resolve()
        self.model = model
        tools.set_root(self.root)
        self.tool_schemas = tools.TOOLS
        self.functions = tools.FUNCTIONS
        self.system_prompt = tools.build_system_prompt(self.root)
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.history_summary = ""
        self.turns = 0
        self.tool_count = 0
        self.compressed_rounds = 0

    # ---------- 热重载 ----------
    def reload_tools(self):
        """重新加载 tools.py，返回最新工具名列表。

        注意：importlib.reload 会把模块级变量 ROOT 重置回 Path.cwd()，
        因此必须重新注入工作目录，否则从非项目目录启动时工具会操作错目录。
        """
        fresh = importlib.reload(tools)
        fresh.set_root(self.root)
        self.tool_schemas = fresh.TOOLS
        self.functions = fresh.FUNCTIONS
        self.system_prompt = fresh.build_system_prompt(self.root)
        self.messages[0] = {"role": "system", "content": self.system_prompt}
        if self.history_summary:
            self._place_summary()
        return [t["function"]["name"] for t in self.tool_schemas]

    # ---------- 上下文 ----------
    def _place_summary(self):
        """保证摘要消息紧随原始 system prompt，且只有一条。"""
        message = context.summary_message(self.history_summary)
        if len(self.messages) >= 2 and "历史摘要" in str(self.messages[1].get("content", "")):
            self.messages[1] = message
        else:
            self.messages.insert(1, message)

    def compress(self, force: bool = False):
        """裁掉最老的轮次，并把它们合并进历史摘要。

        force=False（默认）：仅当超过阈值时才裁剪，即自动压缩。
        force=True：无视阈值，压到只剩最近 KEEP_RECENT_ROUNDS 轮，供 /compact 手动触发。
        """
        if force:
            self.messages, dropped = context.trim(self.messages, max_chars=0)
            if not dropped:
                self.ui.notice("没有可压缩的较早轮次", kind="context")
                return
        else:
            self.messages, dropped = context.trim(self.messages)
            if not dropped:
                return
        before = len(self.history_summary)
        self.history_summary = context.summarize(dropped, get_client(), self.model, self.history_summary)
        self._place_summary()
        self.compressed_rounds += len(dropped)
        self.ui.notice(
            f"已压缩 {len(dropped)} 个较早轮次为摘要（摘要长度 {before}→{len(self.history_summary)} 字符）",
            kind="context",
        )

    def emit_status(self):
        self.ui.status(
            turns=self.turns,
            chars=context.total_chars(self.messages),
            limit=context.DEFAULT_MAX_CHARS,
            compressed=self.compressed_rounds,
            tools=self.tool_count,
            model=self.model,
        )

    # ---------- 供斜杠命令查询的只读视图 ----------
    def tool_names(self):
        """当前加载的工具名列表。"""
        return [t["function"]["name"] for t in self.tool_schemas]

    def status_detail(self) -> str:
        """多行状态说明，供 /status 使用。"""
        chars = context.total_chars(self.messages)
        limit = context.DEFAULT_MAX_CHARS
        pct = chars / limit * 100 if limit else 0.0
        return "\n".join([
            f"模型：{self.model}",
            f"工作目录：{self.root}",
            f"轮次：{self.turns}　工具调用：{self.tool_count}　工具数：{len(self.tool_schemas)}",
            f"上下文：{chars} / {limit} 字符（{pct:.0f}%）",
            f"已压缩：{self.compressed_rounds} 轮　摘要：{len(self.history_summary)} 字符",
            f"消息条数：{len(self.messages)}",
        ])

    # ---------- 单步执行 ----------
    def _run_tool(self, call):
        """执行一个工具调用，并把结果作为 tool 消息写回历史。"""
        name = call.function.name
        raw_args = call.function.arguments or "{}"
        self.tool_count += 1

        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as error:
            self.ui.tool_start(name, raw_args)
            self.ui.tool_end(name, f"工具参数解析失败：{error}", ok=False)
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": f"工具参数解析失败：{error}"})
            return

        self.ui.tool_start(name, format_args(args))
        started = time.time()
        try:
            result = self.functions[name](**args)
            ok = True
        except Exception as error:
            result = f"工具执行失败：{error}"
            ok = False
        result = str(result)
        self.ui.tool_end(name, result, ok=ok, seconds=time.time() - started)
        self.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    def handle(self, question, should_stop=None):
        """处理一轮用户输入：反复「模型 → 工具 → 模型」直到模型不再调用工具。"""
        self.messages.append({"role": "user", "content": question})
        self.turns += 1

        while True:
            self.compress()
            self.emit_status()

            message = stream_chat(
                self.messages, self.tool_schemas, self.ui, self.model, should_stop=should_stop,
            )

            if message.interrupted:
                # 半截的 tool_calls 已在 stream_chat 里丢弃，这里只留正文，避免非法历史。
                self.messages.append({"role": "assistant", "content": message.content or "（已中断）"})
                self.ui.notice("本轮生成已中断", kind="warn")
                break

            assistant_message = {"role": "assistant", "content": message.content}
            if message.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": call.type,
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in message.tool_calls
                ]
            self.messages.append(assistant_message)

            if not message.tool_calls:
                break

            interrupted = False
            for index, call in enumerate(message.tool_calls):
                if should_stop and should_stop():
                    # 协议要求每个 tool_call 都必须有对应的 tool 结果，缺一条都会报错。
                    for rest in message.tool_calls[index:]:
                        self.messages.append({
                            "role": "tool", "tool_call_id": rest.id, "content": "（已中断，未执行）",
                        })
                    self.ui.notice("剩余工具调用已中断", kind="warn")
                    interrupted = True
                    break
                self._run_tool(call)

            if interrupted:
                break

        self.emit_status()
        self.ui.turn_end()


def run_session_command(session, ui, cmd, arg=""):
    """执行一条 where=="agent" 的斜杠命令。

    会话状态只能在这条线程里安全访问，所以界面侧必须把命令交过来执行，
    纯文本模式则直接调用。界面相关命令（help/clear/exit）由调用方各自处理。
    """
    if cmd.name == "status":
        ui.notice(session.status_detail(), kind="info")
    elif cmd.name == "tools":
        names = session.tool_names()
        ui.notice(f"当前工具（{len(names)} 个）：" + "、".join(names), kind="info")
    elif cmd.name == "compact":
        session.compress(force=True)
    elif cmd.name == "reload":
        names = session.reload_tools()
        ui.notice(f"已重新加载 {len(names)} 个工具：{', '.join(names)}", kind="reload")
    else:
        ui.notice(f"命令 /{cmd.name} 暂未实现", kind="warn")
    session.emit_status()


def main():
    """纯文本模式（非 TTY 或不装 textual 时使用）。"""
    require_api_key()

    ui = PlainUI()
    session = AgentSession(ui, ROOT, MODEL)
    start_watcher()
    ui.banner(session.root, session.model)

    while True:
        if _tools_changed.is_set():
            _tools_changed.clear()
            time.sleep(0.2)  # 等文件写完
            try:
                names = session.reload_tools()
                ui.notice(f"tools.py 已更新，加载了 {len(names)} 个工具：{', '.join(names)}", kind="reload")
            except Exception as error:
                ui.notice(f"tools.py 加载失败，继续使用旧版本：{error}", kind="error")

        if _self_changed.is_set():
            _self_changed.clear()
            ui.notice("agent.py 已改动，主逻辑需重启后生效（tools.py 的改动会自动热重载）", kind="warn")

        try:
            raw = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue

        # 斜杠命令：// 开头转义为普通消息，其余按命令表路由。
        if commands.is_command_line(raw):
            parsed = commands.parse(raw)
            if parsed.kind == "command":
                if parsed.command is None:
                    ui.notice(commands.unknown_text(raw[1:].split(" ")[0]), kind="warn")
                elif parsed.command.name == "exit":
                    return
                elif parsed.command.name == "help":
                    ui.notice(commands.detail_text(parsed.arg) if parsed.arg else commands.help_text(), kind="info")
                elif parsed.command.name == "clear":
                    ui.notice("纯文本模式没有界面可清，对话历史不受影响", kind="info")
                else:
                    try:
                        run_session_command(session, ui, parsed.command, parsed.arg)
                    except Exception as error:
                        ui.notice(f"/{parsed.command.name} 执行失败：{error}", kind="error")
                continue
            raw = parsed.text  # escape：去掉一个斜杠，按普通消息发送

        if raw.lower() in {"exit", "quit", "退出"}:
            return
        try:
            session.handle(raw)
        except Exception as error:
            # 与 TUI 侧保持一致：单轮失败不该带走整个程序。
            ui.notice(f"本轮处理异常：{error}", kind="error")


def run():
    """统一入口：TTY 下启动全屏 TUI，否则退回纯文本模式。"""
    require_api_key()

    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            from frontend.tui import run_tui
        except ImportError:
            pass  # 没装 textual，退回纯文本
        else:
            run_tui(ROOT, MODEL)
            return

    main()


if __name__ == "__main__":
    run()
