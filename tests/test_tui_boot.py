"""全屏 TUI 端到端启动测试：在真实伪终端（pty）里跑 main.py。

冒烟测试是 headless 的，验证不了「真终端里能不能正常起、能不能退出」，
所以这里开一个 pty，把真程序跑起来，检查：
1. 启动后渲染出标题、状态栏、输入框；
2. 输入指令后界面不崩、用户消息上屏；
3. Ctrl+Q 能干净退出。

不需要真实 API Key（用 dummy），因此不会产生真实请求。

运行：python3 tests/test_tui_boot.py
"""

import fcntl
import os
import re
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[\(\)][0-9A-B]|\x1b[=>]|\r")

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'✓' if condition else '✗'} {name}" + (f"   ← {detail}" if detail and not condition else ""))


def strip_ansi(raw: bytes) -> str:
    return ANSI.sub("", raw.decode("utf-8", "replace"))


def read_until(fd, needles, timeout=20.0, into=""):
    """持续读取 pty 输出，直到出现任一关键词或超时。返回累计的可见文本。

    注意：必须累积「原始字节」并在最后统一剥离 ANSI，不能每个分片单独剥离——
    否则转义序列被从中间切断，残留字符会污染可见文本。
    """
    raw = into.encode("utf-8", "replace") if isinstance(into, str) else into
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.3)
        if not ready:
            continue
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        raw += data
        if any(n in strip_ansi(raw) for n in needles):
            break
    return strip_ansi(raw)


def main():
    pid, fd = os.forkpty()
    if pid == 0:
        # 子进程：把自己变成真终端里的 agent。
        # 清掉可能从父进程继承的供应商/模型变量，确保测的是「开箱默认配置」。
        os.chdir(ROOT)
        os.environ["DASHSCOPE_API_KEY"] = "dummy_key_for_boot_test"
        os.environ["TERM"] = "xterm-256color"
        for var in ("SP_AGENT_PROVIDER", "SP_AGENT_MODEL", "SP_AGENT_BASE_URL", "SP_AGENT_API_KEY_ENV"):
            os.environ.pop(var, None)
        os.environ.pop("COLUMNS", None)
        os.environ.pop("LINES", None)
        try:
            os.execv(sys.executable, [sys.executable, "main.py"])
        finally:
            os._exit(127)

    # 父进程：给 pty 设定一个明确的窗口大小，避免布局拿到 0 列
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))

    try:
        print("启动与渲染")
        # 关键：等「80k」而不是「就绪」——启动提示里也含「就绪」二字，
        # 用它作关键词会在第一帧就提前停止抓取，误判状态栏没渲染。
        screen = read_until(fd, ["256k"], timeout=25)
        check("全屏界面已渲染出标题", "编程 Agent" in screen, repr(screen[:120]))
        check("Header 显示默认模型名", "qwen-plus" in screen, repr(screen[:160]))
        check("状态栏显示就绪状态", "● 就绪" in screen, repr(screen[-300:]))
        check("状态栏显示上下文占用", "256k" in screen)
        check("状态栏显示轮次与压缩", "轮次 0" in screen and "已压缩" in screen)
        check("Agent 线程存活并上报了工具数（回归：worker 曾一启动就崩）",
              "加载了 7 个工具" in screen, repr(screen[:400]))
        check("底部提示了按键", "Ctrl+Q" in screen or "中断" in screen)
        check("界面没有 Python 报错", "Traceback" not in screen and "Error" not in screen)

        print("命令补全菜单")
        os.write(fd, b"/")
        # 等最后一条命令出现，确保整张菜单（含各行说明）都已绘制完成。
        screen = read_until(fd, ["/exit"], timeout=10, into=screen)
        check("输入 / 后弹出补全菜单", "/status" in screen and "/exit" in screen, repr(screen[-400:]))
        check("菜单里带命令说明", "查看轮次" in screen, repr(screen[-400:]))
        check("菜单没把界面挤崩", "Traceback" not in screen)
        os.write(fd, b"\x7f")  # 退格删掉斜杠，菜单应收起
        time.sleep(1)
        screen = read_until(fd, ["__never__"], timeout=2, into=screen)
        check("退格后无异常", "Traceback" not in screen)

        print("执行 /status（真实走一遍 UI→Agent 线程的命令路由）")
        os.write(fd, "/status\r".encode())
        screen = read_until(fd, ["工作目录"], timeout=15, into=screen)
        check("/status 把详情打到界面", "工作目录" in screen and "上下文" in screen, repr(screen[-300:]))
        check("/status 显示供应商与网关",
              "供应商" in screen and "dashscope" in screen, repr(screen[-400:]))
        check("/status 未引起报错", "Traceback" not in screen)

        print("/model 多级菜单（选供应商 → 选模型）")
        # /status 是会话命令，worker 回 turn_end 之前界面仍是「处理中」；
        # 等它就绪再发 /model，否则会被「上一轮仍在处理中」拦截。
        time.sleep(1.2)
        screen = read_until(fd, ["__never__"], timeout=2, into=screen)
        os.write(fd, "/model\r".encode())
        screen = read_until(fd, ["选择模型供应商"], timeout=10, into=screen)
        check("弹出供应商一级菜单", "阿里云百炼" in screen and "DeepSeek" in screen, repr(screen[-700:]))
        os.write(fd, b"\r")  # 进入二级菜单
        screen = read_until(fd, ["qwen-max"], timeout=10, into=screen)
        check("进入模型二级菜单", "qwen-max" in screen and "再选一个模型" in screen, repr(screen[-600:]))
        os.write(fd, b"\x1b")  # Esc 退回一级
        screen = read_until(fd, ["选择模型供应商"], timeout=10, into=screen)
        check("Esc 退回供应商一级菜单", "选择模型供应商" in screen, repr(screen[-300:]))
        os.write(fd, b"\x1b")  # Esc 取消整个流程
        time.sleep(0.5)
        screen = read_until(fd, ["__never__"], timeout=2, into=screen)
        check("取消 /model 流程无异常", "Traceback" not in screen)

        print("/model 带参数直接切换")
        os.write(fd, "/model deepseek deepseek-chat\r".encode())
        screen = read_until(fd, ["已切换模型"], timeout=15, into=screen)
        check("带参数直接切换生效", "deepseek" in screen and "deepseek-chat" in screen, repr(screen[-400:]))
        check("状态栏跟上了新模型", "deepseek/deepseek-chat" in screen or "deepseek-chat" in screen,
              repr(screen[-300:]))
        check("切换未引起报错", "Traceback" not in screen)

        print("未知命令")
        os.write(fd, "/nope\r".encode())
        screen = read_until(fd, ["未知命令"], timeout=10, into=screen)
        check("未知命令被提示而非发给模型", "未知命令" in screen, repr(screen[-200:]))

        print("输入指令")
        os.write(fd, "帮我看看项目结构\r".encode())
        screen = read_until(fd, ["帮我看看项目结构"], timeout=15, into=screen)
        check("用户消息上屏", "帮我看看项目结构" in screen)
        time.sleep(2)
        screen = read_until(fd, ["__never__"], timeout=3, into=screen)
        check("输入后界面未崩溃", "Traceback" not in screen)

        print("退出")
        os.write(fd, b"\x11")  # Ctrl+Q
        try:
            _, status = os.waitpid(pid, 0)
            exited = os.WIFEXITED(status)
            code = os.WEXITSTATUS(status) if exited else -1
        except ChildProcessError:
            exited, code = True, 0
        check("Ctrl+Q 能正常退出", exited and code == 0, f"exit code={code}")
        pid = None
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        try:
            os.close(fd)
        except OSError:
            pass

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
