"""UI 事件接口：把 Agent 核心与「怎么呈现」彻底解耦。

Agent 核心只调用这里的抽象方法，不关心对方是纯文本还是全屏 TUI。
新增一种界面 = 新增一个 UI 子类，backend/agent.py 不用改。

约定：所有方法都可能被 Agent 后台线程调用，因此实现者必须自行保证线程安全
（TuiUI 的做法是只往 queue 里推事件，绝不直接碰 widget）。
"""


def truncate(text, limit: int = 4000, tail: str = "\n…（已截断，共 {n} 字符）") -> str:
    """把过长的工具输出截断到 limit 字符以内。"""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + tail.format(n=len(text))


class UI:
    """Agent 核心向界面单向播报的事件接口。默认实现全部为空操作。"""

    def banner(self, root, model) -> None:
        """启动横幅。"""

    def notice(self, text, kind: str = "info") -> None:
        """系统提示：热重载、上下文压缩、错误等。kind 见 notice_kind 取值。"""

    def delta(self, text) -> None:
        """流式正文的一个分片。"""

    def assistant_end(self, text) -> None:
        """本轮正文输出完毕，text 是完整内容。"""

    def tool_start(self, name, args) -> None:
        """开始执行工具，args 是已格式化（且可能截断）的参数串。"""

    def tool_end(self, name, result, ok: bool = True, seconds: float = 0.0) -> None:
        """工具执行结束。"""

    def status(self, **fields) -> None:
        """状态栏数据：turns / chars / limit / compressed / tools / model。"""

    def model_options(self, provider_key, items, source) -> None:
        """某供应商的在线模型清单到达，供 /model 二级菜单刷新。

        items 是 [(模型名, 说明)]；source 为 "online"/"cached"/"offline"。
        默认实现是空操作：纯文本模式不需要动态刷新菜单。
        """

    def turn_end(self) -> None:
        """一轮完整对话（含后续所有工具调用）结束。"""


class PlainUI(UI):
    """纯文本实现：非 TTY 或未装 textual 时的兜底，行为贴近改造前。"""

    def __init__(self, stream=None):
        import sys

        self.stream = stream or sys.stdout
        self._header_shown = False

    def _write(self, text="") -> None:
        print(text, file=self.stream, flush=True)

    def banner(self, root, model) -> None:
        self._write(f"编程 Agent 已启动，目录：{root}。模型：{model}。输入 /model 切换模型，exit 退出。")

    def notice(self, text, kind: str = "info") -> None:
        tag = {
            "reload": "[热重载]", "context": "[上下文]", "warn": "[提示]",
            "error": "[错误]", "info": "[信息]", "model": "[模型]",
        }.get(kind, "[信息]")
        self._write(f"\n{tag} {text}")

    def delta(self, text) -> None:
        if not self._header_shown:
            self._write()
            print("Agent：", end="", flush=True, file=self.stream)
            self._header_shown = True
        print(text, end="", flush=True, file=self.stream)

    def assistant_end(self, text) -> None:
        if self._header_shown:
            self._write()
            self._header_shown = False

    def tool_start(self, name, args) -> None:
        self._write(f"  🔧 {name}  {truncate(args, 200)}")

    def tool_end(self, name, result, ok: bool = True, seconds: float = 0.0) -> None:
        mark = "✓" if ok else "✗"
        body = truncate(result, 800).replace("\n", "\n     ")
        self._write(f"  {mark} {name} ({seconds:.2f}s)\n     {body}")

    def status(self, **fields) -> None:
        pass

    def turn_end(self) -> None:
        self._header_shown = False
