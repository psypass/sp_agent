# DeepSeek 极简编程 Agent

一个约 700 行的中文编程 Agent，带全屏 TUI。核心思路是用尽量少的代码把
「对话循环 + 工具调用 + 上下文管理」讲清楚，适合当作自己写 Agent 的起点。

## 目录结构

前端（界面）与后端（核心）分成两个包：

```
simple_agent/
├── main.py              # 唯一启动入口：python3 main.py
├── backend/             # 后端：Agent 核心，不含任何终端呈现
│   ├── agent.py         #   AgentSession（对话状态与单轮处理）、流式调用、热重载
│   ├── tools.py         #   工具实现、OpenAI 工具 schema、System Prompt 生成
│   ├── context.py       #   上下文管理：体积估算、按轮次裁剪、历史摘要压缩
│   └── agent.py.bak     #   早期版本备份（仅 bash 工具、非流式），仅供对比参考
├── frontend/            # 前端：怎么把状态呈现出来
│   ├── ui.py            #   UI 事件接口 + PlainUI（纯文本兜底）
│   └── tui.py           #   全屏 TUI（Textual）：对话区、状态栏、工具卡片
├── tests/               # 冒烟测试、核心逻辑测试、真实终端启动测试
├── requirements.txt
└── README.md
```

依赖方向是单向的：`frontend` 依赖 `backend`（`tui.py` 调用 `AgentSession`），
而 `backend.agent` 只知道 `frontend.ui.UI` 这个抽象接口，不关心对方是纯文本还是
全屏 TUI（`frontend.tui` 仅在 TTY 下惰性导入）。新增一种界面 = 在 `frontend/`
里加一个 `UI` 子类，后端不用改。

> 注意：所有命令都要在项目根目录执行。`backend.agent.ROOT` 取自当前工作目录，
> 也是 agent 工具的默认工作目录。

## 快速开始

```bash
pip install -r requirements.txt
export DEEPSEEK_API_KEY='你的 API Key'
python3 main.py
```

也可以 `python3 -m backend.agent`（等价入口）。

有 TTY 时自动进入全屏 TUI；重定向到文件或管道时自动退回纯文本模式。

## 界面

```
┌─ DeepSeek 编程 Agent ─── deepseek-v4-flash · /path/to/project
  · 已就绪。Ctrl+Q 退出，Esc 中断，Ctrl+R 重载 tools.py。
  你 ❯ 把 tools.py 的 grep 改成支持多个 pattern
  ▸ ╭─ ✓ read_file  0.02s ──────────────────────────────
    │ 参数：path=tools.py offset=90 limit=20
    │ 输出（47 字符）：
    │ 90	def grep(pattern):
    ╰────────────────────────────────────────────────────
  ▸ ╭─ ✗ bash  0.31s ───────────────────────────────────
    │ 参数：command=python3 -c 'import tools'
    │ 输出（33 字符）：
    │ 退出码：1
    ╰────────────────────────────────────────────────────
  AGENT:
    改好了。现在支持用列表传入多个模式：

    ```python
    def grep(patterns, path="."):
        pattern = "|".join(patterns)
    ```
  · 已压缩 2 个较早轮次为摘要（摘要长度 0→186 字符）
└──────────────────────────────────────────────────────────
  ● 就绪  轮次 9 · 工具 10 · 上下文 13.9k/80k (17%) · 已压缩 2 轮 · deepseek-v4-flash
```

| 按键 | 作用 |
| --- | --- |
| `Enter` | 发送指令 |
| `Ctrl+Q` | 退出 |
| `Esc` | 中断当前回合 |
| `Ctrl+L` | 清屏（不影响对话历史） |
| `Ctrl+R` | 手动重新加载 `tools.py` |

输入 `exit`、`quit` 或 `退出` 也可结束程序。

界面细节：

- **工具调用收成折叠卡片**，标题带状态与耗时（`✓ read_file 0.02s`）。默认折叠，
  因为工具输出往往很长；**执行失败时自动展开**，省得手动点开找原因。
- **正文流式期间用纯文本更新**，本轮结束后整体换成 Markdown widget（代码块高亮）。
  这样既不会每来一个分片就重排 Markdown，最终又有完整排版。
- **状态栏实时显示**上下文占用（`13.9k/80k (17%)`）、轮次、工具调用数、已压缩轮次数。
- 只有当视图停在底部时才自动滚动，向上翻看历史时不会被拽回去。

## 内置工具

| 工具 | 用途 |
| --- | --- |
| `bash` | 在项目根目录执行 shell 命令 |
| `read_file` | 按行号范围读取文件（`offset` / `limit`） |
| `write_file` | 创建或覆盖文件，内容原样写入（避免 heredoc 转义问题） |
| `edit_file` | 精确替换 `old` → `new`，要求 `old` 唯一出现 |
| `list_dir` | 列出目录内容，含类型与大小 |
| `grep` | 按正则搜索文本，可限定文件名 glob |
| `run_python` | 执行 Python 片段，用于计算或验证 |

加新工具只需两步：在 `backend/tools.py` 里写函数，然后在 `TOOLS` 加 schema、在 `FUNCTIONS`
注册。保存后自动热重载，无需重启。

## 设计要点

**上下文按「轮次」裁剪，而不是按消息条数。** OpenAI 协议要求每条 `assistant(tool_calls)`
消息后面必须紧跟对应的 `tool` 结果消息，按条数随意删除会让请求直接报错。因此
`context.trim()` 以「一条 user 消息到下一条 user 消息之前」为一个轮次整块丢弃。
中断时也遵守这条：未执行的工具会补上「（已中断，未执行）」占位，绝不留悬空调用。

**旧历史压缩成摘要而非直接丢弃。** 被裁掉的轮次会交给模型压成中文简报，以 system
消息插在原始 System Prompt 之后，并随裁剪不断合并更新。摘要调用失败时退化为截断
拼接，不会中断主流程。

**默认阈值**：`DEFAULT_MAX_CHARS = 80000`（序列化字符数上限）、
`KEEP_RECENT_ROUNDS = 6`（无论如何都保留的最近轮次数）。

**TUI 的线程模型**（`frontend/tui.py` 里最关键的部分）：

```
UI 线程   ── 只负责渲染与输入，绝不阻塞
Agent 线程 ── 执行模型调用与工具，绝不直接碰 widget
Agent → UI：事件推进 queue，UI 侧每 50ms 批量取出后一次性更新
UI → Agent：用户输入同样走 queue，Agent 线程阻塞等待（等价于原来的 input()）
```

队列 + 定时器泵的好处有两个：天然线程安全；上百个流式分片会被合并成每帧一次重绘，
不会卡。Agent 线程整体包在 try 里，一旦崩溃会把异常直接显示在界面上——线程静默死亡
是最难排查的故障。

**中断语义**：`Esc` 只是置一个标志，Agent 线程在「下一个分片」或「下一个工具之前」
检查它。所以正在跑的 `bash` 不会被立刻杀掉（子进程有自己的超时兜底），会等当前
步骤结束后停止。生成中断时半截的 `tool_calls` 会被丢弃，避免写入非法历史。

## 热重载

启动时会用 `watchdog` 监视 `backend/` 目录下的两个源文件：

- 改动 `backend/tools.py` → 自动重新加载，新工具立即生效，System Prompt 同步刷新。
- 改动 `backend/agent.py` → 只提示「需重启生效」，不自动重启主逻辑。

> 注意：`importlib.reload` 会把 `tools` 模块的 `ROOT` 重置回 `Path.cwd()`，
> 所以 `reload_tools()` 在 reload 之后必须重新 `set_root(ROOT)`，否则从项目目录
> 之外启动时，工具会静默操作到错误的目录。

## 测试

```bash
python3 tests/test_session.py    # 核心逻辑：工具循环、协议合法性、中断、压缩
python3 tests/test_smoke.py      # TUI 界面：渲染、状态流转、输入交互（无头）
python3 tests/test_tui_boot.py   # 真实伪终端里启动全屏 TUI 并退出
```

`test_session.py` 用假客户端驱动，不联网；`test_tui_boot.py` 会真的跑起程序，
但用 dummy API Key，不会产生真实请求。

## 安全约定

- 所有文件路径都经 `tools._safe_path()` 校验：解析为绝对路径后必须落在工作目录内，
  否则直接拒绝，可拦截 `../` 逃逸与软链接逃逸。
- `bash` 有 120 秒超时，`run_python` 有 60 秒超时，输出超长会截断后再喂给模型。
- System Prompt 中明确要求不访问工作目录之外的文件、不执行破坏性命令。
- 已知边界：`bash` 以 `shell=True` 执行，上述约定属于「提示词级」约束而非硬隔离。
  若要运行不可信内容，请放进容器或虚拟机。

## 配置

| 项 | 位置 | 默认值 |
| --- | --- | --- |
| API Key | 环境变量 `DEEPSEEK_API_KEY` | 无，启动时强制校验 |
| 模型 | `backend/agent.py` 的 `MODEL` | `deepseek-v4-flash` |
| 接口地址 | `backend/agent.py` 的 `BASE_URL` | `https://api.deepseek.com` |
| 上下文阈值 | `backend/context.py` | 80000 字符 / 保留 6 轮 |
| 事件泵间隔 | `frontend/tui.py` 的 `TICK` | 0.05 秒 |
| 工具输出展示上限 | `frontend/tui.py` 的 `MAX_TOOL_CHARS` | 3000 字符 |
