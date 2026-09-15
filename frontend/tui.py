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
from textual.widgets import Collapsible, Footer, Header, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

import backend.agent as agent_module
import frontend.ui as ui_module
from backend import commands, providers
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

    def model_options(self, provider_key, items, source):
        self._push("model_options", provider_key=provider_key, items=items, source=source)

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
    TITLE = "编程 Agent（多供应商网关）"
    CSS = """
    #conv { height: 1fr; padding: 0 1; }
    #status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    /* 命令补全 / 模型多级菜单共用：默认隐藏，需要时展开，贴在输入框上方。
   max-height 要留得下当前命令数（8 条 + 边框 2 行），否则末尾命令会被裁掉。 */
    #cmd-menu { display: none; height: auto; max-height: 12; margin: 0 1; border: round $primary; }
    #cmd-menu.visible { display: block; }
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
        # 补全菜单：Tab 用 priority 抢在输入框之前（Textual 默认拿 Tab 切换焦点）
        Binding("tab", "complete", "补全命令", show=False, priority=True),
        Binding("up", "menu_up", "上一条", show=False, priority=True),
        Binding("down", "menu_down", "下一条", show=False, priority=True),
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

        # /model 的多级选择菜单状态：None 表示不在选模型流程里。
        # "provider" = 一级（选供应商）；"model" = 二级（选模型）。
        self._menu_flow = None
        self._menu_provider = None

    # ---------------- 布局 ----------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="conv")
        yield Static("", id="status")
        yield OptionList(id="cmd-menu")
        yield Input(placeholder="输入指令，回车发送；输入 / 查看命令", id="prompt")
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
        style = {"warn": "yellow", "error": "red", "reload": "cyan",
                 "context": "magenta", "model": "bold green"}.get(kind, "dim")
        self._add(Static(Text(f"· {text}", style=style), classes="notice"))

    def _block(self, text, kind="info"):
        """多行文本块（如 /help 输出），不加「·」前缀。"""
        style = {"warn": "yellow", "error": "red", "reload": "cyan",
                 "context": "magenta", "model": "bold green"}.get(kind, "")
        self._add(Static(Text(text, style=style), classes="notice"))

    def _render_status(self):
        f = self._status_fields
        # 还没有状态数据时显示占位；但正在处理中就必须如实显示忙碌，不能被占位盖掉。
        if not f and not self._busy:
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
            provider = f.get("provider")
            label = f"{provider}/" if provider else ""
            text.append(f" · {label}{f.get('model', self._model)}", style="dim")
        self.query_one("#status", Static).update(text)

    # ---------------- 命令补全菜单 ----------------
    def _menu(self) -> OptionList:
        return self.query_one("#cmd-menu", OptionList)

    def _menu_visible(self) -> bool:
        return self._menu().has_class("visible")

    def _hide_menu(self):
        menu = self._menu()
        if menu.has_class("visible"):
            menu.remove_class("visible")

    # ---------------- /model 多级选择菜单 ----------------
    def _current_provider(self):
        return (self._status_fields or {}).get("provider")

    def _current_model(self):
        return (self._status_fields or {}).get("model")

    def _close_flow(self):
        """退出 /model 选择流程（收菜单并清状态）。"""
        self._menu_flow = None
        self._menu_provider = None
        self._hide_menu()

    def open_model_menu(self):
        """打开 /model 一级菜单：列出全部供应商。"""
        self._menu_flow = "provider"
        self._menu_provider = None
        current = self._current_provider()
        menu = self._menu()
        options, highlight = [], 0
        for index, (provider, subtitle) in enumerate(providers.provider_menu_items()):
            is_current = provider.key == current
            text = Text()
            text.append("● " if is_current else "○ ", style="green" if is_current else "dim")
            text.append(provider.label.ljust(18), style="bold cyan")
            text.append(f" {provider.key}", style="yellow")
            text.append(f"  {subtitle}", style="dim")
            options.append(Option(text, id=provider.key))
            if is_current:
                highlight = index
        menu.set_options(options)
        menu.highlighted = highlight
        menu.add_class("visible")
        self._notice("选择模型供应商（↑↓ 选择，回车进入下一步，Esc 取消）", kind="info")

    def open_model_choice(self, provider):
        """打开二级菜单：先用离线推荐清单渲染，再让 Agent 线程拉在线清单刷新。"""
        self._menu_provider = provider
        self._menu_flow = "model"
        self._render_model_options(provider, providers.model_menu_items(provider), source="offline")

    def _render_model_options(self, provider, items, source="offline"):
        """按给定 [(模型名, 说明)] 渲染二级菜单，尽量保持原高亮项。"""
        current_model = self._current_model() if provider.key == self._current_provider() else None
        menu = self._menu()
        previous = menu.highlighted_option
        previous_id = previous.id if previous is not None else None

        options, highlight = [], 0
        for index, (name, note) in enumerate(items):
            is_current = name == current_model
            text = Text()
            text.append("● " if is_current else "○ ", style="green" if is_current else "dim")
            text.append(name, style="bold")
            if note:
                text.append(f"  （{note}）", style="dim")
            options.append(Option(text, id=name))
            if is_current:
                highlight = index
            # 用户已经手动挪到某一项时，刷新后别把光标弹回去。
            if previous_id is not None and name == previous_id:
                highlight = index
        menu.set_options(options)
        menu.highlighted = highlight
        menu.add_class("visible")

        if source == "online":
            self._notice(f"已从 {provider.label} 拉取到 {len(items)} 个模型", kind="info")
        elif source == "cached":
            self._notice(f"使用 {provider.label} 的模型缓存（{len(items)} 个）", kind="info")
        else:
            hint = "" if provider.list_models else "（该厂商不支持在线列模型）"
            self._notice(f"已选供应商 {provider.label}，再选一个模型{hint}（Esc 返回上一步）", kind="info")

    def _request_online_models(self, provider):
        """把「拉取在线模型清单」丢给 Agent 线程，避免网络请求卡住 UI 线程。"""
        if not provider.list_models:
            return
        self._to_agent.put(("__models__", provider.key))

    def _confirm_model_selection(self):
        """二级菜单确认：把选中的供应商 + 模型交给 Agent 线程切换。"""
        option = self._menu().highlighted_option
        provider = self._menu_provider
        if option is None or option.id is None or provider is None:
            return
        model = option.id
        self._close_flow()
        self._notice(f"切换到 {provider.label} · {model}…", kind="model")
        self._to_agent.put(("__model__", (provider.key, model)))

    def _apply_menu_selection(self):
        """当前高亮项的确认动作，按菜单所处层级分派。"""
        if self._menu_flow == "provider":
            option = self._menu().highlighted_option
            provider = providers.get(option.id) if option and option.id else None
            if provider is None:
                return
            self.open_model_choice(provider)
            self._request_online_models(provider)
        elif self._menu_flow == "model":
            self._confirm_model_selection()
        else:
            self._apply_completion()

    @staticmethod
    def _menu_label(cmd):
        """菜单一行：命令名（对齐）+ 说明。"""
        text = Text()
        text.append(cmd.display.ljust(9), style="bold cyan")
        text.append(" " + cmd.summary, style="dim")
        return text

    def _refresh_menu(self):
        """按输入框内容刷新补全菜单。

        只在「/前缀」状态下展开；一旦出现空格（开始输参数）或 //（转义），就收起。
        /model 的多级选择菜单是独立流程，输入变化时不参与刷新。
        """
        if self._menu_flow is not None:
            return
        value = self.query_one("#prompt", Input).value
        if not commands.is_command_line(value) or value.startswith("//") or " " in value:
            self._hide_menu()
            return
        items = commands.matching(value)
        menu = self._menu()
        if not items:
            self._hide_menu()
            return
        menu.set_options(Option(self._menu_label(c), id=c.name) for c in items)
        menu.highlighted = 0
        menu.add_class("visible")

    def _highlighted_command(self):
        option = self._menu().highlighted_option
        if option is None or option.id is None:
            return None
        return commands.get(option.id)

    def _apply_completion(self):
        """把当前高亮的命令填进输入框，并留在末尾方便补参数。"""
        cmd = self._highlighted_command()
        if cmd is None:
            return
        inp = self.query_one("#prompt", Input)
        inp.value = cmd.display + " "
        inp.cursor_position = len(inp.value)
        self._hide_menu()

    def action_complete(self):
        if self._menu_visible():
            self._apply_menu_selection()

    def action_menu_up(self):
        if self._menu_visible():
            self._menu().action_cursor_up()

    def action_menu_down(self):
        if self._menu_visible():
            self._menu().action_cursor_down()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option_list.id == "cmd-menu":
            self._apply_menu_selection()

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "prompt":
            self._refresh_menu()

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
        elif kind == "model_options":
            self._on_model_options(payload)
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
        raw = event.value.strip()

        # 多级菜单流程中，回车 = 确认当前高亮项（选供应商 → 选模型 → 切换）。
        if self._menu_flow is not None and self._menu_visible():
            if not raw or raw.startswith("/model") or raw.startswith("/"):
                self._apply_menu_selection()
            else:
                self._close_flow()
                self._notice("已取消模型选择", kind="info")
            event.input.value = ""
            return

        if not raw:
            return

        self._hide_menu()

        # 斜杠命令：// 开头转义为普通消息（去掉一个斜杠），其余查命令表。
        text = raw
        if commands.is_command_line(raw):
            parsed = commands.parse(raw)
            if parsed.kind == "command":
                if parsed.command is None:
                    self._notice(commands.unknown_text(raw[1:].split(" ")[0]), kind="warn")
                    event.input.value = ""
                    return
                self._run_command(parsed.command, parsed.arg, event.input)
                return
            text = parsed.text

        if text.lower() in {"exit", "quit", "退出"}:
            self.exit()
            return
        if self._busy:
            # 注意：这里不清空输入框，保留用户已经打好的文字，避免白打一遍。
            self._notice("上一轮仍在处理中，请等待结束或按 Esc 中断", kind="warn")
            return

        event.input.value = ""
        self._busy = True
        self._cancel.clear()
        self._add(Static(Text(f"你 ❯ {text}", style="bold cyan"), classes="user"))
        self._render_status()
        self._to_agent.put(("__ask__", text))

    def _run_command(self, cmd, arg, input_widget):
        """路由一条命令：会话命令交给 Agent 线程，界面命令当场执行。"""
        label = cmd.display + (f" {arg}" if arg else "")
        if cmd.where == "agent" and self._busy:
            self._notice("上一轮仍在处理中，请等待结束或按 Esc 中断", kind="warn")
            return

        input_widget.value = ""
        self._add(Static(Text(f"你 ❯ {label}", style="bold cyan"), classes="user"))

        # /model 不带参数：不是普通命令，而是打开多级选择菜单。
        if cmd.name == "model" and not arg:
            self.open_model_menu()
            return

        if cmd.where == "agent":
            # 会话状态只在 Agent 线程里安全，转过去执行；/compact 可能调模型较慢，
            # 因此同样置忙，结束后由 worker 发 turn_end 解除。
            self._busy = True
            self._cancel.clear()
            self._render_status()
            self._to_agent.put(("__cmd__", (cmd, arg)))
            return

        if cmd.name == "exit":
            self.exit()
        elif cmd.name == "clear":
            self.action_clear_log()
        elif cmd.name == "help":
            self._block(commands.detail_text(arg) if arg else commands.help_text())

    def action_cancel(self):
        # 菜单开着时 Esc 先处理菜单，不打断正在跑的回合。
        if self._menu_visible():
            # 二级菜单里 Esc 退回一级（选供应商），一级菜单里 Esc 直接取消整个流程。
            if self._menu_flow == "model" and self._menu_provider is not None:
                self.open_model_menu()
            else:
                self._close_flow()
            return
        if self._busy:
            self._cancel.set()
            self._notice("已请求中断，将在当前步骤结束后停止", kind="warn")
        else:
            self._notice("当前没有进行中的回合", kind="info")

    def _on_model_options(self, payload):
        """在线模型清单到达：只在用户仍停在该供应商的二级菜单时才刷新。"""
        provider = self._menu_provider
        if self._menu_flow != "model" or provider is None:
            return
        if provider.key != payload.get("provider_key"):
            return
        self._render_model_options(provider, payload.get("items") or [], payload.get("source", "offline"))

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
            elif command == "__cmd__":
                cmd, arg = payload
                try:
                    agent_module.run_session_command(session, self._ui, cmd, arg)
                except Exception as error:
                    self._ui.notice(f"/{cmd.name} 执行失败：{error}", kind="error")
                finally:
                    # 解除置忙（run_session_command 不一定发 turn_end）
                    self._ui.turn_end()
            elif command == "__model__":
                provider_key, model = payload
                try:
                    session.switch_model(provider=provider_key, model=model)
                except Exception as error:
                    self._ui.notice(f"/model 切换失败：{error}", kind="error")
            elif command == "__models__":
                provider = providers.get(payload)
                if provider is not None:
                    try:
                        items, source = session.model_list_online(provider)
                    except Exception as error:
                        self._ui.notice(f"拉取模型清单失败，沿用推荐清单：{error}", kind="warn")
                    else:
                        self._ui.model_options(provider.key, items, source)
            elif command == "__ask__":
                try:
                    session.handle(payload, should_stop=self._cancel.is_set)
                except Exception as error:
                    self._ui.notice(f"本轮处理异常：{error}", kind="error")
                    self._ui.turn_end()


def run_tui(root, model):
    """全屏 TUI 入口。"""
    AgentTUI(root, model).run()
