"""统一启动入口：python3 main.py

- 有 TTY 且装了 textual → 进入全屏 TUI（frontend/tui.py）
- 否则 → 退回纯文本模式（frontend/ui.py 的 PlainUI）

真正的逻辑都在 backend/（Agent 核心）与 frontend/（界面）里，本文件只负责转发。
必须在项目根目录运行（backend.agent 的 ROOT 取自当前工作目录）。
"""

from backend.agent import run

if __name__ == "__main__":
    run()
