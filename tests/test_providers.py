"""统一供应商网关测试：供应商表、环境变量解析、切换语义。纯逻辑，不联网。

运行：python3 tests/test_providers.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import providers  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'✓' if condition else '✗'} {name}" + (f"   ← {detail}" if detail and not condition else ""))


class Env:
    """临时设置/清理环境变量，避免污染测试环境与相互干扰。"""

    KEYS = ("SP_AGENT_PROVIDER", "SP_AGENT_MODEL", "SP_AGENT_BASE_URL",
            "SP_AGENT_API_KEY_ENV", providers.GENERIC_KEY_ENV,
            "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.saved = {k: os.environ.pop(k, None) for k in self.KEYS}
        os.environ.update(self.values)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    print("【供应商表自洽性】")
    keys = [p.key for p in providers.PROVIDERS]
    check("供应商 key 不重复", len(keys) == len(set(keys)), str(keys))
    aliases = [a for p in providers.PROVIDERS for a in p.aliases]
    check("别名不与 key 冲突", not (set(aliases) & set(keys)), str(aliases))
    check("每家都有 base_url / key_env / 模型清单",
          all(p.base_url.startswith("http") and p.key_env and p.models for p in providers.PROVIDERS))
    check("default_model 一定在模型清单里",
          all(p.default_model in p.models for p in providers.PROVIDERS))
    check("按别名能找到供应商", providers.get("kimi").key == "moonshot" and providers.get("qwen").key == "dashscope")
    check("未知供应商返回 None", providers.get("nope") is None)

    print("\n【默认配置】")
    with Env(DASHSCOPE_API_KEY="sk-demo"):
        s = providers.resolve_config()
        check("默认供应商是 dashscope", s.provider.key == "dashscope", s.provider.key)
        check("默认模型取供应商默认值", s.model == "qwen-plus", s.model)
        check("读到 Key 并记录来源", s.ready and s.key_from == "DASHSCOPE_API_KEY", s.key_from)
        check("spec 形如 供应商/模型", s.spec == "dashscope/qwen-plus", s.spec)

    print("\n【环境变量驱动】")
    with Env(SP_AGENT_PROVIDER="deepseek", DEEPSEEK_API_KEY="sk-ds"):
        s = providers.resolve_config()
        check("SP_AGENT_PROVIDER 生效", s.provider.key == "deepseek", s.provider.key)
        check("换成对应供应商的默认模型", s.model == "deepseek-chat", s.model)
        check("换用对应供应商的 Key 变量", s.key_from == "DEEPSEEK_API_KEY", s.key_from)

    with Env(SP_AGENT_MODEL="deepseek-reasoner", DEEPSEEK_API_KEY="sk-ds"):
        s = providers.resolve_config()
        check("裸模型名能反查供应商", s.provider.key == "deepseek", s.provider.key)
        check("模型被采纳", s.model == "deepseek-reasoner", s.model)

    with Env(SP_AGENT_MODEL="moonshot/kimi-latest", MOONSHOT_API_KEY="sk-m"):
        s = providers.resolve_config()
        check("供应商/模型 写法被识别", s.provider.key == "moonshot" and s.model == "kimi-latest", s.spec)

    with Env(SP_AGENT_PROVIDER="openai", SP_AGENT_BASE_URL="https://proxy.internal/v1",
             OPENAI_API_KEY="sk-o"):
        s = providers.resolve_config()
        check("SP_AGENT_BASE_URL 覆盖网关地址", s.base_url == "https://proxy.internal/v1", s.base_url)

    with Env(SP_AGENT_PROVIDER="openai"):
        s = providers.resolve_config()
        check("缺 Key 时 ready=False 且给出变量名",
              not s.ready and s.key_env == "OPENAI_API_KEY", f"{s.ready} {s.key_env}")

    with Env(SP_AGENT_PROVIDER="openai", SP_AGENT_API_KEY="sk-generic"):
        s = providers.resolve_config()
        check("通用兜底 Key 可用", s.ready and s.key_from == providers.GENERIC_KEY_ENV, s.key_from)

    with Env(SP_AGENT_PROVIDER="openai", SP_AGENT_API_KEY_ENV="MY_KEY", MY_KEY="sk-custom"):
        s = providers.resolve_config()
        check("SP_AGENT_API_KEY_ENV 改名生效", s.ready and s.key_from == "MY_KEY", s.key_from)

    print("\n【显式参数优先于环境变量】")
    with Env(SP_AGENT_PROVIDER="openai", SP_AGENT_MODEL="gpt-4o-mini", OPENAI_API_KEY="sk-o"):
        s = providers.resolve_config(provider="zhipu", model="glm-4-flash")
        check("显式参数覆盖环境变量", s.provider.key == "zhipu" and s.model == "glm-4-flash", s.spec)

    print("\n【make_selection 不读环境变量（避免切换串味）】")
    with Env(SP_AGENT_PROVIDER="openai", SP_AGENT_MODEL="gpt-4o-mini",
             SP_AGENT_BASE_URL="https://stale.example/v1", DEEPSEEK_API_KEY="sk-ds"):
        s = providers.make_selection(provider="deepseek")
        check("base_url 不被旧环境变量污染", s.base_url == providers.get("deepseek").base_url, s.base_url)
        check("模型用新供应商的默认值", s.model == "deepseek-chat", s.model)
        check("Key 实时读环境变量", s.ready and s.key_from == "DEEPSEEK_API_KEY", s.key_from)

    print("\n【/model 参数解析】")
    with Env(DASHSCOPE_API_KEY="sk-demo"):
        check("供应商名 → 该家默认模型",
              providers.resolve_arg("deepseek").spec == "deepseek/deepseek-chat",
              providers.resolve_arg("deepseek").spec)
        check("别名可用", providers.resolve_arg("kimi").spec == "moonshot/kimi-latest",
              providers.resolve_arg("kimi").spec)
        check("供应商+模型", providers.resolve_arg("zhipu glm-4-flash").spec == "zhipu/glm-4-flash",
              providers.resolve_arg("zhipu glm-4-flash").spec)
        check("斜杠写法", providers.resolve_arg("deepseek/deepseek-reasoner").spec == "deepseek/deepseek-reasoner",
              providers.resolve_arg("deepseek/deepseek-reasoner").spec)
        check("裸模型名反查", providers.resolve_arg("glm-4.6").spec == "zhipu/glm-4.6",
              providers.resolve_arg("glm-4.6").spec)
        custom = providers.resolve_arg("dashscope qwen3-internal-preview")
        check("清单外模型标记 custom", custom.custom and custom.model == "qwen3-internal-preview", custom.model)

    check("未知供应商抛 UnknownProvider",
          _raises(providers.UnknownProvider, lambda: providers.resolve_provider("nope")))

    print("\n【菜单数据】")
    items = providers.provider_menu_items()
    check("一级菜单列出全部供应商", len(items) == len(providers.PROVIDERS))
    check("一级菜单副标题含 Key 变量",
          all(p.key_env in sub for p, sub in items), str(items[0]))
    models = providers.model_menu_items(providers.get("deepseek"))
    check("二级菜单列出该家模型", [m for m, _ in models] == list(providers.get("deepseek").models))
    check("默认模型被标注", dict(models)["deepseek-chat"] == "默认", str(models))

    print("\n【export_env 写回】")
    with Env(DEEPSEEK_API_KEY="sk-ds"):
        s = providers.resolve_config(provider="deepseek")
        providers.export_env(s)
        check("provider 写回环境变量", os.environ["SP_AGENT_PROVIDER"] == "deepseek")
        check("model 写回环境变量", os.environ["SP_AGENT_MODEL"] == "deepseek-chat")
        check("base_url 写回环境变量", os.environ["SP_AGENT_BASE_URL"] == s.base_url)

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())