"""统一大模型供应商接入网关（provider gateway）。

不同厂商的「OpenAI 兼容」接口其实只差三样东西：base_url、API Key 环境变量名、
以及可用的模型名。本模块把这三样收进一张供应商表（PROVIDERS），让
backend/agent.py 只面向一个统一的 Selection（供应商 + 模型 + base_url + key）工作。

设计要点：
- 本模块是纯逻辑：不导入 openai、不导入界面，可以单独测试。
- 新增一家供应商 = 在 PROVIDERS 里加一行，/model 菜单、/status、/help 自动跟上。
- 配置优先级：显式参数 > 专用环境变量 > 供应商默认值。
- 允许「不在清单里的模型名」：公司内网自建网关、新发布的模型不必等改代码，
  只是会标记 custom=True 并在界面上给出提示。

环境变量：
- SP_AGENT_PROVIDER      指定供应商（key 或别名），默认 dashscope
- SP_AGENT_MODEL         指定模型，可写 "供应商/模型" 或裸模型名（按模型名反查供应商）
- SP_AGENT_BASE_URL      覆盖供应商 base_url（自建网关、代理用）
- SP_AGENT_API_KEY_ENV   覆盖读 Key 的环境变量名
- SP_AGENT_API_KEY       通用兜底 Key（任意供应商都能用）
- 各供应商自己的 Key 变量见 Provider.key_env，如 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY
"""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_PROVIDER_KEY = "dashscope"

# 在线模型清单的缓存有效期（秒）。拉的频率不高，但别每次都打网络。
MODELS_TTL = 600.0

# 通用兜底 Key 的变量名：任意供应商都可以用它，方便只有一把钥匙的场景。
GENERIC_KEY_ENV = "SP_AGENT_API_KEY"


class UnknownProvider(ValueError):
    """请求了一个不在供应商表里的供应商。"""


@dataclass(frozen=True)
class Provider:
    """一家供应商的接入参数。"""

    key: str                       # 稳定标识，用于 /model <provider> <model>
    label: str                     # 中文名，界面展示用
    base_url: str                  # OpenAI 兼容接口地址
    key_env: str                   # 默认读取 API Key 的环境变量名
    models: tuple = ()             # 推荐模型清单（离线兜底 / 菜单顺序）
    default_model: str = ""        # 不指定模型时用哪个
    aliases: tuple = ()            # 别名，如 kimi → moonshot
    note: str = ""                 # 一句话说明
    list_models: bool = True       # 是否支持 GET /models 在线拉取模型清单

    def __post_init__(self):
        if not self.default_model and self.models:
            object.__setattr__(self, "default_model", self.models[0])

    @property
    def display(self) -> str:
        return self.label

    def __str__(self) -> str:
        return self.key


# 供应商表。顺序即 /model 一级菜单的展示顺序。
PROVIDERS = (
    Provider(
        key="dashscope",
        label="阿里云百炼（通义千问）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        key_env="DASHSCOPE_API_KEY",
        models=("qwen-plus", "deepseek-v4.1-flash", "qwen-max", "qwen-turbo", "qwen3-coder-plus",
                "qwen-long", "qwen-vl-max"),
        default_model="qwen-plus",
        aliases=("qwen", "aliyun", "bailian", "tongyi"),
        note="通义千问，兼容模式端点",
    ),
    Provider(
        key="deepseek",
        label="DeepSeek（深度求索）",
        base_url="https://api.deepseek.com/v1",
        key_env="DEEPSEEK_API_KEY",
        models=("deepseek-chat", "deepseek-reasoner"),
        default_model="deepseek-chat",
        aliases=("ds",),
        note="官方直连",
    ),
    Provider(
        key="moonshot",
        label="Moonshot（月之暗面 Kimi）",
        base_url="https://api.moonshot.cn/v1",
        key_env="MOONSHOT_API_KEY",
        models=("kimi-latest", "kimi-k2-0905-preview",
                "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
        default_model="kimi-latest",
        aliases=("kimi", "yuezhianmian"),
        note="长上下文见长",
    ),
    Provider(
        key="zhipu",
        label="智谱 AI（GLM）",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        key_env="ZHIPUAI_API_KEY",
        models=("glm-4.6", "glm-4-plus", "glm-4-air", "glm-4-flash"),
        default_model="glm-4.6",
        aliases=("glm", "bigmodel", "chatglm"),
        note="GLM 系列",
    ),
    Provider(
        key="siliconflow",
        label="硅基流动（SiliconFlow）",
        base_url="https://api.siliconflow.cn/v1",
        key_env="SILICONFLOW_API_KEY",
        models=("deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-32B",
                "Qwen/Qwen2.5-72B-Instruct", "THUDM/glm-4-9b-chat"),
        default_model="deepseek-ai/DeepSeek-V3",
        aliases=("sf", "guijiliudong"),
        note="多开源模型聚合",
    ),
    Provider(
        key="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        models=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"),
        default_model="gpt-4o",
        note="官方直连",
    ),
    Provider(
        key="anthropic",
        label="Anthropic（Claude）",
        base_url="https://api.anthropic.com/v1",
        key_env="ANTHROPIC_API_KEY",
        models=("claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"),
        default_model="claude-sonnet-4-5",
        aliases=("claude",),
        note="走 OpenAI 兼容层",
        list_models=False,  # 原生端点用 x-api-key，标准 /models 拉不到
    ),
    Provider(
        key="openrouter",
        label="OpenRouter（模型聚合）",
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        models=("openai/gpt-4o", "anthropic/claude-sonnet-4.5",
                "google/gemini-2.5-pro", "deepseek/deepseek-chat"),
        default_model="openai/gpt-4o",
        aliases=("or", "router"),
        note="一把钥匙调多家",
    ),
)

# key / 别名（小写）→ Provider
_INDEX = {}
for _p in PROVIDERS:
    for _k in (_p.key,) + _p.aliases:
        _INDEX[_k.lower()] = _p


@dataclass(frozen=True)
class Selection:
    """一次「选定的接入方式」：供应商 + 模型 + 实际生效的连接参数。"""

    provider: Provider
    model: str
    base_url: str
    key_env: str                       # 实际去哪个环境变量读 Key
    api_key: str | None = None         # 读到的 Key，可能为空
    custom: bool = False               # 模型不在该供应商的推荐清单里
    key_from: str = ""                 # Key 读自哪个环境变量（可读性用）

    @property
    def ready(self) -> bool:
        """Key 已就位才算可用。"""
        return bool(self.api_key)

    @property
    def spec(self) -> str:
        """规范的 "供应商/模型" 字符串，可回填给 /model 使用。"""
        return f"{self.provider.key}/{self.model}"

    def describe(self) -> str:
        lines = [
            f"供应商：{self.provider.label}（{self.provider.key}）",
            f"模型：{self.model}" + ("（自定义，不在推荐清单）" if self.custom else ""),
            f"接口地址：{self.base_url}",
            f"API Key：{'已就绪（来自 ' + self.key_from + '）' if self.ready else '未设置，请 export ' + self.key_env}",
        ]
        return "\n".join(lines)


def all_providers() -> tuple:
    """全部供应商，顺序即菜单顺序。"""
    return PROVIDERS


def get(name) -> Provider | None:
    """按 key 或别名取供应商，大小写不敏感。"""
    if name is None:
        return None
    return _INDEX.get(str(name).strip().lower())


def resolve_provider(name) -> Provider:
    """同上，但找不到就抛 UnknownProvider。"""
    provider = get(name)
    if provider is None:
        raise UnknownProvider(f"未知供应商：{name}。可用：" + "、".join(p.key for p in PROVIDERS))
    return provider


def default_provider() -> Provider:
    """默认供应商（SP_AGENT_PROVIDER 未指定时）。"""
    return get(DEFAULT_PROVIDER_KEY) or PROVIDERS[0]


def find_by_model(model: str) -> Provider | None:
    """按模型名反查供应商：只有恰好命中一家的清单时才返回。"""
    name = (model or "").strip()
    if not name:
        return None
    hits = [p for p in PROVIDERS if name in p.models]
    return hits[0] if len(hits) == 1 else None


def key_env_names(provider: Provider, key_env: str | None = None) -> tuple:
    """读 Key 时依次尝试的环境变量名。"""
    names = []
    if key_env:
        names.append(key_env)
    names.append(f"SP_AGENT_{provider.key.upper()}_API_KEY")
    names.append(provider.key_env)
    names.append(GENERIC_KEY_ENV)
    # 去重且保序
    seen, ordered = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return tuple(ordered)


def read_api_key(provider: Provider, key_env: str | None = None):
    """返回 (key, 读自哪个变量)。都没有则 (None, 建议的变量名)。"""
    for name in key_env_names(provider, key_env):
        value = os.environ.get(name)
        if value:
            return value, name
    return None, (key_env or provider.key_env)


def split_spec(text: str):
    """把 "供应商/模型" 或 "供应商 模型" 拆成 (provider, model)。空串返回 (None, None)。"""
    text = (text or "").strip()
    if not text:
        return None, None
    for sep in ("/", " ", ":"):
        if sep in text:
            head, _, tail = text.partition(sep)
            return head.strip() or None, tail.strip() or None
    return text, None


def resolve_config(provider=None, model=None, base_url=None, key_env=None) -> Selection:
    """把「供应商 + 模型 + 连接参数」解析成一个 Selection。

    优先级（从高到低）：
      显式参数 > 专用环境变量 > 供应商默认值。
    供应商的确定顺序：
      显式 provider > SP_AGENT_PROVIDER > 按模型名反查 > 默认供应商。
    """
    provider_hint = provider if provider is not None else os.environ.get("SP_AGENT_PROVIDER")
    model_hint = model if model is not None else os.environ.get("SP_AGENT_MODEL")
    base_hint = base_url if base_url is not None else os.environ.get("SP_AGENT_BASE_URL")
    env_key_hint = key_env if key_env is not None else os.environ.get("SP_AGENT_API_KEY_ENV")

    # 模型也可以写成 "供应商/模型"，此时供应商从模型串里取。
    spec_provider, spec_model = split_spec(model_hint or "")
    if spec_provider and get(spec_provider) is not None:
        provider_hint, model_hint = spec_provider, spec_model

    chosen = resolve_provider(provider_hint) if provider_hint else None
    if chosen is None and model_hint:
        chosen = find_by_model(model_hint)
    if chosen is None:
        chosen = default_provider()

    final_model = (model_hint or "").strip() or chosen.default_model
    key, key_from = read_api_key(chosen, env_key_hint)

    return Selection(
        provider=chosen,
        model=final_model,
        base_url=(base_hint or "").strip() or chosen.base_url,
        key_env=env_key_hint or key_from or chosen.key_env,
        api_key=key,
        custom=final_model not in chosen.models,
        key_from=key_from,
    )


def resolve_arg(arg: str) -> Selection:
    """解析 /model 命令的参数。

    - "供应商"                       → 该供应商的默认模型
    - "供应商 模型" / "供应商/模型"    → 指定模型
    - "模型"                         → 按模型名反查供应商
    """
    provider, model = split_spec(arg or "")
    if provider and get(provider) is None:
        # 前缀不是供应商，那就整个当作模型名（如 "deepseek-chat"）。
        provider, model = None, (arg or "").strip() or None
    return make_selection(provider, model)


def make_selection(provider=None, model=None, base_url=None, key_env=None) -> Selection:
    """按**显式选择**构造 Selection，不读 SP_AGENT_MODEL / SP_AGENT_BASE_URL。

    这是 /model 切换用的入口：上游「上一次选了什么」会写回环境变量，
    若这里再读一遍环境变量，就会出现「切了供应商却还在用旧 base_url」的串味问题。
    只有 API Key 仍然实时从环境变量读（用户 export 后立即生效）。
    """
    chosen = resolve_provider(provider) if provider else None
    if chosen is None and model:
        chosen = find_by_model(model)
    if chosen is None:
        chosen = default_provider()

    final_model = (model or "").strip() or chosen.default_model
    key, key_from = read_api_key(chosen, key_env)

    return Selection(
        provider=chosen,
        model=final_model,
        base_url=(base_url or "").strip() or chosen.base_url,
        key_env=key_env or key_from or chosen.key_env,
        api_key=key,
        custom=final_model not in chosen.models,
        key_from=key_from,
    )


def export_env(selection: Selection) -> None:
    """把选定的非敏感配置写回环境变量。

    这样热重载、重启、以及 agent 派生的子进程都能拿到同一套配置。
    注意：不写回 API Key 明文，Key 始终从它自己的环境变量读。
    """
    os.environ["SP_AGENT_PROVIDER"] = selection.provider.key
    os.environ["SP_AGENT_MODEL"] = selection.model
    os.environ["SP_AGENT_BASE_URL"] = selection.base_url
    os.environ["SP_AGENT_API_KEY_ENV"] = selection.key_env


def provider_menu_items() -> list:
    """一级菜单数据：[(provider, 副标题)]。"""
    return [(p, f"{p.key_env} · 默认 {p.default_model}") for p in PROVIDERS]


def fetch_models(provider: Provider, base_url: str | None = None, api_key: str | None = None,
                 key_env: str | None = None, timeout: float = 8.0) -> list | None:
    """在线拉取某供应商的模型清单（OpenAI 兼容的 GET /models）。

    返回**真实拉到的**模型名列表（保持接口返回顺序）；任何失败（网络不通、
    没 Key、接口不支持、返回体不是预期结构）都返回 None，由调用方回退到
    provider.models 这份离线推荐清单。这里不抛异常，是因为它服务于落后的
    UI 流程，不该把一次网络抖动放大成界面报错。

    base_url / api_key 省略时按 provider 的默认值解析；/model 试连时可用
    显式 base_url 探一个尚未保存的网关。
    """
    if not provider.list_models:
        return None

    url = (base_url or provider.base_url).rstrip("/")
    if not url.endswith("/models"):
        url = url + "/models"

    if api_key is None:
        api_key, _ = read_api_key(provider, key_env)

    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None

    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None

    names = []
    for item in items:
        name = item.get("id") if isinstance(item, dict) else None
        if isinstance(name, str) and name.strip() and name not in names:
            names.append(name.strip())
    return names or None


# 在线清单缓存：key -> (时间戳, 模型名列表)
_MODELS_CACHE: dict = {}


def cached_models(provider: Provider, base_url: str | None = None, api_key: str | None = None,
                  key_env: str | None = None, ttl: float = MODELS_TTL) -> list | None:
    """带缓存的 fetch_models：TTL 内命中直接回，避免反复打网络。"""
    cache_key = (provider.key, (base_url or provider.base_url).rstrip("/"))
    hit = _MODELS_CACHE.get(cache_key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]

    names = fetch_models(provider, base_url=base_url, api_key=api_key, key_env=key_env)
    if names:
        _MODELS_CACHE[cache_key] = (now, names)
    return names


def model_choices(provider: Provider, base_url: str | None = None, key_env: str | None = None,
                  online: bool = True) -> tuple:
    """二级菜单用的模型清单：优先在线拉取，失败回退离线推荐。

    返回 (模型名列表, 来源)，来源取值 "online" / "offline" / "cached"，
    便于界面如实告诉用户「这份清单是实时拉的还是兜底的」。
    """
    if not online:
        return list(provider.models), "offline"

    cache_key = (provider.key, (base_url or provider.base_url).rstrip("/"))
    hit = _MODELS_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < MODELS_TTL:
        return list(hit[1]), "cached"

    names = fetch_models(provider, base_url=base_url, key_env=key_env)
    if names:
        _MODELS_CACHE[cache_key] = (time.time(), names)
        # 默认模型不在实时清单里时，把它插到最前，保证回车确认仍落在可用项上。
        if provider.default_model and provider.default_model not in names:
            names = [provider.default_model] + names
        return names, "online"
    return list(provider.models), "offline"


def model_menu_items(provider: Provider) -> list:
    """二级菜单数据：[(模型名, 说明)]。离线版，不联网。

    联网版见 model_menu_items_online()：它会把「默认」标注加回来。
    """
    items = []
    for m in provider.models:
        note = "默认" if m == provider.default_model else ""
        items.append((m, note))
    return items


def model_menu_items_online(provider: Provider, base_url: str | None = None,
                            key_env: str | None = None, online: bool = True) -> tuple:
    """二级菜单数据（联网版）：返回 ([(模型名, 说明)], 来源)。"""
    names, source = model_choices(provider, base_url=base_url, key_env=key_env, online=online)
    items = [(m, "默认" if m == provider.default_model else "") for m in names]
    return items, source