"""全屏 TUI：基于 Textual 的对话界面（前端 / frontend）。

本文件只负责「怎么呈现」：把 backend 的 Agent 播报事件渲染到界面，把用户输入回传。

线程模型（这是本文件最关键的部分）：
- UI 线程：只负责渲染与输入，Textual 的事件循环，绝不阻塞。
- Agent 线程：执行模型调用与工具，可能一跑几十秒，绝不直接碰 widget。
- Agent → UI：TuiUI 把事件推进 queue，UI 侧 50ms 定时器批量取出后一次性更新。
  这样既线程安全，又天然把上百个流式分片合并成每帧一次重绘，不会卡。
- UI → Agent：用户输入同样走 queue，Agent 线程阻塞等待，与改造前的 input() 等价。

按键：Ctrl+Q 退出，Esc 中断当前回合，Ctrl+L 清屏，Ctrl+R 手动热重载 tools.py。
"""

import queue
import sys
import threading
import traceback

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Footer, Header, Input, Markdown, Static

import backend.agent as agent_module
import frontend.ui as ui_module
from backend.agent import AgentSession

TICK = 0.05            # 事件泵间隔（秒）
MAX_TOOL_CHARS = 3000  # 工具输出在卡片里最多展示的字符数


class TuiUI(ui_module.UI):
    """把 Agent 核心的播报转成队列事件。所有方法都是线程安全的。"""

    def __init__(self):
        self.events = queue.Queue()

    def _push(self, event, **payload):
        # 形参名不能叫 kind：notice() 会传 kind=... 关键字，重名会撞成
        # "got multiple values for argument" 直接把 Agent 线程打挂。
        self.events.put((event, payload))

    def notice(self, text, kind="info"):
        self._push("notice", text=text, kind=kind)

    def delta(self, text):
        self._push("delta", text=text)

    def assistant_end(self, text):
        self._push("assistant_end", text=text)

    def tool_start(self, name, args):
        self._push("tool_start", name=name, args=args)

    def tool_end(self, name, result, ok=True, seconds=0.0):
        self._push("tool_end", name=name, result=result, ok=ok, seconds=seconds)

    def status(self, **fields):
        self._push("status", **fields)

    def turn_end(self):
        self._push("turn_end")

    # banner 由 App 的 Header 呈现，这里不需要额外输出。


class ToolCard(Collapsible):
    """一次工具调用的折叠卡片：标题带名字与耗时，正文放参数和输出。"""

    def __init__(self, name, args):
        body = Static(f"参数：{args}", markup=False)
        super().__init__(body, title=f"🔧 {name} …", collapsed=True, classes="tool")
        self._args = args
        self._body = body
        self._name = name

    def finish(self, result, ok, seconds):
        self._body.update(
            f"参数：{self._args}\n\n输出（{len(str(result))} 字符）：\n"
            f"{ui_module.truncate(result, MAX_TOOL_CHARS)}"
        )
        mark = "✓" if ok else "✗"
        self.title = f"{mark} {self._name}  {seconds:.2f}s"
        if not ok:
            self.collapsed = False  # 出错时自动展开，省得手动点开找原因


class AgentTUI(App):
    TITLE = "DeepSeek 编程 Agent"
    CSS = """
    #conv { height: 1fr; padding: 0 1; }
    #status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    #prompt { border-top: solid $primary; }
    .user { margin: 1 0 0 0; }
    .assistant { margin: 0 0 1 0; }
    .notice { margin: 0 0 1 0; color: $text-muted; text-style: italic; }
    .tool { margin: 0 0 1 0; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("escape", "cancel", "中断当前回合"),
        Binding("ctrl+l", "clear_log", "清屏"),
        Binding("ctrl+r", "reload", "重载 tools.py"),
    ]

    def __init__(self, root, model, start_worker=True):
        super().__init__()
        self._root = root
        self._model = model
        self._start_worker = start_worker
        # 注意：App.__init__ 里已经用 SUB_TITLE 初始化过 sub_title 这个 Reactive，
        # 之后再改 SUB_TITLE 不会生效，必须直接给 sub_title 赋值。
        self.sub_title = f"{model} · {root}"

        self._ui = TuiUI()
        self._to_agent = queue.Queue()
        self._cancel = threading.Event()
        self._busy = False

        self._stream_widget = None
        self._stream_buf = []
        self._open_card = None
        self._status_fields = {}

    # ---------------- 布局 ----------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="conv")
        yield Static("", id="status")
        yield Input(placeholder="输入指令，回车发送；输入 exit 退出", id="prompt")
        yield Footer()

    def on_mount(self):
        self.query_one("#prompt", Input).focus()
        self._render_status()
        self.set_interval(TICK, self._pump)
        if self._start_worker:
            threading.Thread(target=self._agent_worker, daemon=True).start()
        self._notice("已就绪。Ctrl+Q 退出，Esc 中断，Ctrl+R 重载 tools.py。")

    # ---------------- 渲染helpers ----------------
    def _conversation(self) -> VerticalScroll:
        return self.query_one("#conv", VerticalScroll)

    def _at_bottom(self) -> bool:
        conv = self._conversation()
        try:
            return conv.scroll_offset.y >= conv.max_scroll_y - 3
        except Exception:
            return True

    def _add(self, widget, stick=None):
        """挂载一个 widget。stick 为 True 时滚到底部（默认沿用当前位置）。"""
        conv = self._conversation()
        was_at_bottom = self._at_bottom() if stick is None else stick
        conv.mount(widget)
        if was_at_bottom:
            conv.scroll_end(animate=False)

    def _notice(self, text, kind="info"):
        style = {"warn": "yellow", "error": "red", "reload": "cyan", "context": "magenta"}.get(kind, "dim")
        self._add(Static(Text(f"· {text}", style=style), classes="notice"))

    def _render_status(self):
        f = self._status_fields
        if not f:
            text = Text("等待状态…", style="dim")
        else:
            chars = f.get("chars", 0)
            limit = max(f.get("limit", 1), 1)
            pct = chars / limit * 100
            state = "⏳ 处理中" if self._busy else "● 就绪"
            text = Text()
            text.append(f"{state}  ", style="yellow" if self._busy else "green")
            text.append(f"轮次 {f.get('turns', 0)} · 工具 {f.get('tools', 0)} · ", style="")
            text.append(f"上下文 {chars / 1000:.1f}k/{limit / 1000:.0f}k ({pct:.0f}%)")
            text.append(f" · 已压缩 {f.get('compressed', 0)} 轮")
            text.append(f" · {f.get('model', self._model)}", style="dim")
        self.query_one("#status", Static).update(text)

    # ---------------- 事件泵（UI 线程） ----------------
    def _pump(self):
        drained = False
        while True:
            try:
                kind, payload = self._ui.events.get_nowait()
            except queue.Empty:
                break
            drained = True
            self._apply(kind, payload)
        if drained:
            self._render_status()

        # 热重载：tools.py 交给 Agent 线程处理（它会重置工作目录），agent.py 只提示。
        if agent_module._tools_changed.is_set():
            agent_module._tools_changed.clear()
            self._to_agent.put(("__reload__", None))
        if agent_module._self_changed.is_set():
            agent_module._self_changed.clear()
            self._notice("agent.py 已改动，主逻辑需重启后生效（tools.py 会自动热重载）", kind="warn")

    def _apply(self, kind, payload):
        if kind == "delta":
            self._on_delta(payload["text"])
        elif kind == "assistant_end":
            self._on_assistant_end(payload["text"])
        elif kind == "tool_start":
            self._on_tool_start(payload["name"], payload["args"])
        elif kind == "tool_end":
            self._on_tool_end(payload["name"], payload["result"], payload["ok"], payload["seconds"])
        elif kind == "notice":
            self._notice(payload["text"], payload.get("kind", "info"))
        elif kind == "status":
            self._status_fields = payload
        elif kind == "turn_end":
            self._busy = False
            self._cancel.clear()
            self.query_one("#prompt", Input).focus()

    # ---------------- 流式正文 ----------------
    def _on_delta(self, text):
        if self._stream_widget is None:
            self._stream_widget = Static("", classes="assistant", markup=False)
            self._add(self._stream_widget)
        self._stream_buf.append(text)
        stick = self._at_bottom()
        # 注意：流式期间用纯文本更新（不解析 Markdown），避免每个分片都重排，
        # 本轮结束后再整体换成 Markdown widget，兼顾流畅与最终排版。
        self._stream_widget.update("".join(self._stream_buf))
        if stick:
            self._conversation().scroll_end(animate=False)

    def _on_assistant_end(self, text):
        if self._stream_widget is not None:
            self._stream_widget.remove()
            self._stream_widget = None
        self._stream_buf = []
        if text and text.strip():
            self._add(Markdown(text, classes="assistant"))

    def _on_tool_start(self, name, args):
        card = ToolCard(name, args)
        self._open_card = card
        self._add(card)

    def _on_tool_end(self, name, result, ok, seconds):
        if self._open_card is not None:
            self._open_card.finish(result, ok, seconds)
            self._open_card = None
        if not ok:
            self._notice(f"{name} 执行失败", kind="error")

    # ---------------- 输入 ----------------
    def on_input_submitted(self, event: Input.Submitted):
        question = event.value.strip()
        if not question:
            return
        if question.lower() in {"exit", "quit", "退出"}:
            self.exit()
            return
        if self._busy:
            # 注意：这里不清空输入框，保留用户已经打好的文字，避免白打一遍。
            self._notice("上一轮仍在处理中，请等待结束或按 Esc 中断", kind="warn")
            return

        event.input.value = ""
        self._busy = True
        self._cancel.clear()
        self._add(Static(Text(f"你 ❯ {question}", style="bold cyan"), classes="user"))
        self._render_status()
        self._to_agent.put(("__ask__", question))

    def action_cancel(self):
        if self._busy:
            self._cancel.set()
            self._notice("已请求中断，将在当前步骤结束后停止", kind="warn")
        else:
            self._notice("当前没有进行中的回合", kind="info")

    def action_clear_log(self):
        conv = self._conversation()
        for child in list(conv.children):
            child.remove()
        self._stream_widget = None
        self._stream_buf = []
        self._notice("已清屏（对话历史不受影响）")

    def action_reload(self):
        self._to_agent.put(("__reload__", None))
        self._notice("已请求重新加载 tools.py")

    # ---------------- Agent 线程 ----------------
    def _agent_worker(self):
        """Agent 线程主循环。

        整个函数体包在 try 里：线程一旦静默死亡，界面看起来还活着却永远不再响应，
        这种失败最难排查，所以必须把异常摆到用户眼前。
        """
        try:
            self._worker_loop()
        except Exception as error:
            self._ui.notice(f"Agent 线程异常退出：{error}", kind="error")
            self._ui.notice("详情见终端 stderr，请重启程序。", kind="error")
            print(traceback.format_exc(), file=sys.stderr)

    def _worker_loop(self):
        session = AgentSession(self._ui, self._root, self._model)
        agent_module.start_watcher()
        self._ui.notice(f"加载了 {len(session.tool_schemas)} 个工具，工作目录 {self._root}", kind="info")
        session.emit_status()

        while True:
            command, payload = self._to_agent.get()
            if command == "__reload__":
                try:
                    names = session.reload_tools()
                except Exception as error:
                    self._ui.notice(f"tools.py 加载失败，继续使用旧版本：{error}", kind="error")
                else:
                    self._ui.notice(
                        f"tools.py 已更新，加载了 {len(names)} 个工具：{', '.join(names)}", kind="reload",
                    )
                session.emit_status()
            elif command == "__ask__":
                try:
                    session.handle(payload, should_stop=self._cancel.is_set)
                except Exception as error:
                    self._ui.notice(f"本轮处理异常：{error}", kind="error")
                    self._ui.turn_end()


def run_tui(root, model):
    """全屏 TUI 入口。"""
    AgentTUI(root, model).run()
