# 统一供应商接入网关

一个约 700 行的中文编程 Agent，带全屏 TUI。核心思路是用尽量少的代码把
「对话循环 + 工具调用 + 上下文管理」讲清楚，适合当作自己写 Agent 的起点。

内置**统一大模型供应商网关**：一份代码接多家厂商（DashScope / DeepSeek /
Moonshot / 智谱 / 硅基流动 / OpenAI / Anthropic / OpenRouter……），
运行时用 `/model` 多级菜单（先选供应商、再选模型）随时切换。

## 目录结构

前端（界面）与后端（核心）分成两个包：

```
simple_agent/
├── main.py              # 唯一启动入口：python3 main.py
├── backend/             # 后端：Agent 核心，不含任何终端呈现
│   ├── agent.py         #   AgentSession（对话状态与单轮处理）、流式调用、热重载
│   ├── providers.py     #   统一供应商网关：供应商表、环境变量解析、切换选择
│   ├── commands.py      #   斜杠命令表：单点定义，前端只负责路由
│   ├── tools.py         #   工具实现、OpenAI 工具 schema、System Prompt 生成
│   ├── context.py       #   上下文管理：体积估算、按轮次裁剪、历史摘要压缩
│   └── agent.py.bak     #   早期版本备份（仅 bash 工具、非流式），仅供对比参考
├── frontend/            # 前端：怎么把状态呈现出来
│   ├── ui.py            #   UI 事件接口 + PlainUI（纯文本兜底）
│   └── tui.py           #   全屏 TUI（Textual）：对话区、状态栏、命令/模型多级菜单
├── tests/               # 冒烟测试、核心逻辑测试、真实终端启动测试
├── scripts/             # 工作树开发流程脚本
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
export DASHSCOPE_API_KEY='你的 API Key'   # 换成别家见「配置」一节
python3 main.py
```

也可以 `python3 -m backend.agent`（等价入口）。

有 TTY 时自动进入全屏 TUI；重定向到文件或管道时自动退回纯文本模式。

### 切换模型

启动后输入 `/model`，会弹出**两级菜单**：先选供应商，再选该供应商的模型，
回车确认、`Esc` 逐级返回。也可以带参数一步到位：

```
/model deepseek deepseek-chat     # 指定供应商 + 模型
/model kimi                       # 供应商别名，用它的默认模型
/model glm-4.6                    # 只给模型名，按清单反查供应商
```

切换是即时的：不用重启，下一句话就走新网关。状态栏会同步显示 `供应商/模型`。
若新供应商缺 API Key，会**照常切过去**并提示缺哪把钥匙，不会把选择回滚。

## 界面

```
┌─ 编程 Agent ─── qwen-plus · /path/to/project
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
  你 ❯ /model
  · 选择模型供应商（↑↓ 选择，回车进入下一步，Esc 取消）
  ╭─────────────────────────────────────────────────────
  │ ○ 阿里云百炼（通义千问） dashscope  DASHSCOPE_API_KEY · 默认 qwen-plus
  │ ● DeepSeek（深度求索）   deepseek   DEEPSEEK_API_KEY · 默认 deepseek-chat
  │ ○ Moonshot（月之暗面 Kimi） moonshot MOONSHOT_API_KEY · 默认 kimi-latest
  ╰─────────────────────────────────────────────────────
  · 已选供应商 DeepSeek（深度求索），再选一个模型（Esc 返回上一步）
  ─────────────────────────────────────────────────────
  │ ● deepseek-chat  （默认）
  │ ○ deepseek-reasoner
  ─────────────────────────────────────────────────────
  · 已切换模型 → DeepSeek（深度求索） · deepseek-chat（Key 来自 DEEPSEEK_API_KEY）
└──────────────────────────────────────────────────────────
  ● 就绪  轮次 9 · 工具 10 · 上下文 13.9k/256k (5%) · 已压缩 2 轮 · deepseek/deepseek-chat
```

| 按键 | 作用 |
| --- | --- |
| `Enter` | 发送指令 |
| `Ctrl+Q` | 退出 |
| `Esc` | 中断当前回合；菜单开着时逐级返回（菜单中 `Esc` 不打断回合） |
| `Ctrl+L` | 清屏（不影响对话历史） |
| `Ctrl+R` | 手动重新加载 `tools.py` |
| `/` | 弹出命令补全菜单 |
| `Tab` | 补全当前高亮的命令 / 确认当前菜单项 |
| `↑` `↓` | 在补全菜单或模型菜单里上下移动 |

输入 `exit`、`quit` 或 `退出` 也可结束程序。

## 斜杠命令

输入 `/` 会弹出补全菜单（带每条的说明），`Tab` 补全、`↑↓` 选择、`Esc` 收起。
命令永不写入对话历史，不会污染上下文。

| 命令 | 别名 | 作用 |
| --- | --- | --- |
| `/help [命令名]` | `/h` `/?'` | 列出全部命令；带参数时显示该命令详情 |
| `/model [供应商] [模型]` | | 切换模型供应商与模型；不带参数弹出多级菜单 |
| `/status` | | 查看供应商、模型、工作目录、轮次、上下文占用、压缩情况 |
| `/tools` | | 列出当前加载的工具 |
| `/compact` | | 手动压缩较早的历史为摘要（会调用模型，稍慢） |
| `/reload` | | 重新加载工具（等价 `Ctrl+R`） |
| `/clear` | `/cls` | 清屏，对话历史不受影响（等价 `Ctrl+L`） |
| `/exit` | `/quit` `/q` | 退出程序（等价 `Ctrl+Q`） |

想发送以斜杠开头的普通消息（比如绝对路径 `/Users/me/x.txt`），用 `//` 开头转义，
`//Users/me/x.txt` 会原样作为消息发出。

命令分成两类，由 `backend/commands.py` 的 `where` 字段决定在哪条线程执行：

- `where="ui"`：界面命令（`help`/`clear`/`exit`），UI 线程当场处理。
- `where="agent"`：会话命令（`model`/`status`/`tools`/`compact`/`reload`），必须交给
  Agent 线程——会话状态只在那条线程里安全访问。执行期间界面显示「处理中」。

加一条命令只需在 `backend/commands.py` 的 `COMMANDS` 里加一行，补全菜单、`/help`
和路由都会自动跟上；会话命令再去 `backend/agent.py` 的 `run_session_command()` 里
补一个分支即可。

`/model` 的多级菜单是前端（`frontend/tui.py`）的一个小状态机：`_menu_flow`
在 `"provider"` / `"model"` 之间切换，复用命令补全用的同一个 `OptionList`。
一级确认后不直接切换，而是带着供应商进入二级；二级确认才把
`("__model__", (供应商, 模型))` 投给 Agent 线程真正执行切换。

界面细节：

- **工具调用收成折叠卡片**，标题带状态与耗时（`✓ read_file 0.02s`）。默认折叠，
  因为工具输出往往很长；**执行失败时自动展开**，省得手动点开找原因。
- **正文流式期间用纯文本更新**，本轮结束后整体换成 Markdown widget（代码块高亮）。
  这样既不会每来一个分片就重排 Markdown，最终又有完整排版。
- **状态栏实时显示**上下文占用（`13.9k/80k (17%)`）、轮次、工具调用数、已压缩轮次数。
- 只有当视图停在底部时才自动滚动，向上翻看历史时不会被拽回去。

## 统一供应商接入网关

各家的「OpenAI 兼容」接口只差三样东西：`base_url`、API Key 变量名、可用模型名。
`backend/providers.py` 把这三样收进一张供应商表，让核心代码只面向一个统一的
`Selection`（供应商 + 模型 + 连接参数）工作。

```python
Provider(
    key="deepseek",
    label="DeepSeek（深度求索）",
    base_url="https://api.deepseek.com/v1",
    key_env="DEEPSEEK_API_KEY",
    models=("deepseek-chat", "deepseek-reasoner"),
    aliases=("ds",),
)
```

内置供应商：`dashscope`、`deepseek`、`moonshot`(别名 `kimi`)、`zhipu`(别名 `glm`)、
`siliconflow`、`openai`、`anthropic`(别名 `claude`)、`openrouter`。

几个刻意的设计取舍：

- **允许清单外的模型名**（标记 `custom=True` 并提示）。新模型发布、公司内网自建
  网关，不必等改代码就能用。
- **缺 Key 不回滚切换**。选中项照样生效、状态栏如实显示，只是提示缺哪把钥匙，
  真正发请求时再拦一次——否则用户会以为「菜单点了没反应」。
- **客户端按签名缓存**：签名是 `(供应商, base_url, api_key)`，切模型后自动重建，
  不会出现「换了模型还打旧网关」。
- **`make_selection()` 不读 `SP_AGENT_MODEL`/`SP_AGENT_BASE_URL`**。上一步的选择
  会写回环境变量，若这里再读一遍就会「切了供应商却还带着旧 base_url」。
  只有 API Key 实时读环境变量，`export` 后立即生效。

新增一家供应商 = 在 `PROVIDERS` 里加一行，`/model` 菜单、`/status`、`/help` 自动跟上。

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
python3 tests/test_session.py      # 核心逻辑：工具循环、协议合法性、中断、压缩、模型切换
python3 tests/test_providers.py    # 供应商网关：供应商表、环境变量解析、/model 参数
python3 tests/test_commands.py     # 斜杠命令表：解析、前缀匹配、帮助文本
python3 tests/test_commands_ui.py  # 斜杠命令界面：补全菜单、按键、命令路由、/model 多级菜单
python3 tests/test_smoke.py        # TUI 界面：渲染、状态流转、输入交互（无头）
python3 tests/test_tui_boot.py     # 真实伪终端里启动全屏 TUI、走一遍 /model 菜单并退出
```

`test_session.py` 用假客户端驱动，不联网；`test_tui_boot.py` 会真的跑起程序，
但用 dummy API Key，不会产生真实请求。

一条命令跑全部：

```bash
scripts/wt-check.sh
```

## 开发流程

改动一律先在 git 工作树里做，测试全过才合并回 `main` 并推送，保证主分支
任何时候都能跑。详见 [WORKFLOW.md](WORKFLOW.md)。

```bash
scripts/wt-new.sh feature/xxx     # 开隔离工作树（.worktrees/feature-xxx）
cd .worktrees/feature-xxx         # 在这里改
scripts/wt-check.sh               # 自测
cd ../.. && scripts/wt-ship.sh feature/xxx "改了什么"   # 测试通过才合并推送
```

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
| 供应商 | 环境变量 `SP_AGENT_PROVIDER`（key 或别名） | `dashscope` |
| 模型 | 环境变量 `SP_AGENT_MODEL`，可用 `供应商/模型` 写法 | 供应商的 `default_model` |
| API Key | 各供应商的 `key_env`，见 `backend/providers.py` | 无，发请求时强制校验 |
| Key 变量改名 | 环境变量 `SP_AGENT_API_KEY_ENV` | 无（用供应商默认的） |
| 通用兜底 Key | 环境变量 `SP_AGENT_API_KEY`（任意供应商都能用） | 无 |
| 接口地址 | 环境变量 `SP_AGENT_BASE_URL`（自建网关/代理用） | 供应商的 `base_url` |
| 上下文阈值 | `backend/context.py` | 256000 字符 / 保留 6 轮 |
| 事件泵间隔 | `frontend/tui.py` 的 `TICK` | 0.05 秒 |
| 工具输出展示上限 | `frontend/tui.py` 的 `MAX_TOOL_CHARS` | 3000 字符 |

> 运行中用 `/model` 切换的选择会写回 `SP_AGENT_PROVIDER` / `SP_AGENT_MODEL` /
> `SP_AGENT_BASE_URL`，让热重载与子进程保持一致。它会覆盖启动时的同名环境变量，
> 但**不会**写回 API Key 明文。
