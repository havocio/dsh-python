"""统一配置文件（dsh_config.py）与 key 解析优先级测试。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, AsyncIterator

import dsh_py.config as cfg_mod
from dsh_py.config import AppConfig, load_app_config
from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.adapters import deepseek as ds
from dsh_py.services.adapters import openai_compatible as oc
from dsh_py.services.llm import GenerateOptions


# --------------------------------------------------------------------------- #
# 加载与分层
# --------------------------------------------------------------------------- #
def test_load_explicit_path():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "c.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("CONFIG = {'llm': {'api_key': 'sk-x', 'temperature': 0.3}}\n")
        cfg = load_app_config(explicit=path)
        assert cfg.get("llm.api_key") == "sk-x"
        assert cfg.get("llm.temperature") == 0.3
    print("  ✓ 显式路径加载")


def test_explicit_missing_raises():
    try:
        load_app_config(explicit=os.path.join(tempfile.gettempdir(), "no-such-dsh-config.py"))
        raise AssertionError("应抛 FileNotFoundError")
    except FileNotFoundError:
        pass
    print("  ✓ 显式路径不存在 → fail loud")


def test_empty_when_no_config_file():
    orig_default, orig_home = cfg_mod.DEFAULT_CONFIG_PATH, cfg_mod.HOME_CONFIG_PATH
    cfg_mod.DEFAULT_CONFIG_PATH = os.path.join(tempfile.gettempdir(), "no-a.py")
    cfg_mod.HOME_CONFIG_PATH = os.path.join(tempfile.gettempdir(), "no-b.py")
    try:
        cfg = load_app_config()
        assert cfg.get("llm.api_key") is None
        assert cfg.get("anything", 42) == 42
    finally:
        cfg_mod.DEFAULT_CONFIG_PATH, cfg_mod.HOME_CONFIG_PATH = orig_default, orig_home
    print("  ✓ 无配置文件 → 空配置（向后兼容，不报错）")


def test_layered_merge_home_overrides_project():
    with tempfile.TemporaryDirectory() as td:
        project = os.path.join(td, "project.py")
        home = os.path.join(td, "home.py")
        with open(project, "w", encoding="utf-8") as f:
            f.write("CONFIG = {'llm': {'provider': 'deepseek', 'api_key': '',"
                    " 'api_keys': {}}, 'workdir': '~/a'}\n")
        with open(home, "w", encoding="utf-8") as f:
            f.write("CONFIG = {'llm': {'api_key': 'sk-home'},"
                    " 'database': {'url': 'sqlite:///x'}}\n")
        orig_default, orig_home = cfg_mod.DEFAULT_CONFIG_PATH, cfg_mod.HOME_CONFIG_PATH
        cfg_mod.DEFAULT_CONFIG_PATH, cfg_mod.HOME_CONFIG_PATH = project, home
        try:
            cfg = load_app_config()
            # 项目层保留
            assert cfg.get("llm.provider") == "deepseek"
            assert cfg.get("workdir") == "~/a"
            # home 层覆盖 / 新增（深合并）
            assert cfg.get("llm.api_key") == "sk-home"
            assert cfg.get("database.url") == "sqlite:///x"
            assert cfg.get("llm.api_keys") == {}
        finally:
            cfg_mod.DEFAULT_CONFIG_PATH, cfg_mod.HOME_CONFIG_PATH = orig_default, orig_home
    print("  ✓ 项目层 + home 层深合并（home 覆盖，嵌套保留）")


def test_env_interpolation():
    cfg = load_app_config(explicit=_write_tmp({"llm": {"api_key": "${MY_KEY}", "x": "$OTHER"}}))
    merged = cfg.data
    env = {"MY_KEY": "sk-abc"}
    cfg2 = AppConfig(_interp(merged, env))
    assert cfg2.get("llm.api_key") == "sk-abc"
    # 未定义变量保留原文
    assert cfg2.get("llm.x") == "$OTHER"
    print("  ✓ ${VAR} 插值（未定义保留原文）")


def test_app_config_dot_path():
    cfg = AppConfig({"llm": {"api_key": "k", "nested": {"a": 1}}, "top": True})
    assert cfg.get("llm.api_key") == "k"
    assert cfg.get("llm.nested.a") == 1
    assert cfg.get("llm.missing", "d") == "d"
    assert cfg.get("llm.missing") is None
    assert "llm.api_key" in cfg
    assert "llm.nope" not in cfg
    assert cfg["llm.api_key"] == "k"
    try:
        cfg["llm.nope"]
        raise AssertionError("应抛 KeyError")
    except KeyError:
        pass
    assert cfg.data["top"] is True
    print("  ✓ 点路径读取 / 包含 / 缺省")


# --------------------------------------------------------------------------- #
# key 解析优先级：配置文件 > credentials > 环境变量
# --------------------------------------------------------------------------- #
def _patch_transport(module, init: Any, transport: Any):
    """给适配器类的 __init__ 注入 fake transport（捕获 authorization）。"""
    orig = module.__init__
    def patched(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        orig(self, *args, **kwargs)
    module.__init__ = patched
    return orig


def _capturing_transport(captured: dict):
    async def transport(url: str, body: dict, headers: dict) -> AsyncIterator[str]:
        captured["authorization"] = headers.get("authorization")

        async def gen() -> AsyncIterator[str]:
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield "data: [DONE]"
        return gen()
    return transport


async def test_deepseek_key_prefers_config_over_env():
    captured: dict = {}
    orig_init = _patch_transport(ds.DeepSeekAdapter, ds.DeepSeekAdapter.__init__,
                                 _capturing_transport(captured))
    os.environ["DEEPSEEK_API_KEY"] = "sk-env-key"
    ctx = AppContext()
    ctx.provide("appConfig", AppConfig({"llm": {"api_key": "sk-config-key"}}))
    try:
        load_profile(ctx, [*CORE_PROFILE, ds.apply])
        chunks = [c async for c in ctx.llm.stream(
            GenerateOptions(provider=ds.PROVIDER, model="deepseek-v4-flash", messages=[]))]
        assert chunks, "应有输出分块"
        assert captured["authorization"] == "Bearer sk-config-key"
    finally:
        ds.DeepSeekAdapter.__init__ = orig_init
        os.environ.pop("DEEPSEEK_API_KEY", None)
        ctx.dispose()
    print("  ✓ deepseek-official：配置文件 llm.api_key 优先于环境变量")


async def test_openai_key_from_config_by_provider():
    captured: dict = {}
    orig_init = _patch_transport(oc.OpenAICompatibleAdapter, oc.OpenAICompatibleAdapter.__init__,
                                 _capturing_transport(captured))
    os.environ["OPENAI_API_KEY"] = "sk-env-key"
    ctx = AppContext()
    ctx.provide("appConfig", AppConfig({"llm": {"api_keys": {"openai": "sk-config-key"}}}))
    try:
        load_profile(ctx, [*CORE_PROFILE, oc.apply])
        chunks = [c async for c in ctx.llm.stream(
            GenerateOptions(provider="openai", model="gpt-4o", messages=[]))]
        assert chunks, "应有输出分块"
        assert captured["authorization"] == "Bearer sk-config-key"
    finally:
        oc.OpenAICompatibleAdapter.__init__ = orig_init
        os.environ.pop("OPENAI_API_KEY", None)
        ctx.dispose()
    print("  ✓ openai：配置文件 llm.api_keys.<provider> 优先于环境变量")


async def test_openai_key_falls_back_to_env():
    captured: dict = {}
    orig_init = _patch_transport(oc.OpenAICompatibleAdapter, oc.OpenAICompatibleAdapter.__init__,
                                 _capturing_transport(captured))
    os.environ["OPENAI_API_KEY"] = "sk-env-key"
    ctx = AppContext()
    # 未注入 app-config（或配置为空）→ 回落环境变量
    ctx.provide("appConfig", AppConfig({}))
    try:
        load_profile(ctx, [*CORE_PROFILE, oc.apply])
        chunks = [c async for c in ctx.llm.stream(
            GenerateOptions(provider="openai", model="gpt-4o", messages=[]))]
        assert chunks, "应有输出分块"
        assert captured["authorization"] == "Bearer sk-env-key"
    finally:
        oc.OpenAICompatibleAdapter.__init__ = orig_init
        os.environ.pop("OPENAI_API_KEY", None)
        ctx.dispose()
    print("  ✓ 配置为空 → 回落环境变量（向后兼容）")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _write_tmp(config: dict) -> str:
    td = tempfile.mkdtemp()
    path = os.path.join(td, "c.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"CONFIG = {config!r}\n")
    return path


def _interp(data: dict, env: dict) -> dict:
    from dsh_py.env import interpolate_env
    return interpolate_env(data, env)


async def main():
    print("== test_config ==")
    test_load_explicit_path()
    test_explicit_missing_raises()
    test_empty_when_no_config_file()
    test_layered_merge_home_overrides_project()
    test_env_interpolation()
    test_app_config_dot_path()
    await test_deepseek_key_prefers_config_over_env()
    await test_openai_key_from_config_by_provider()
    await test_openai_key_falls_back_to_env()
    print("OK: 统一配置文件测试通过")


if __name__ == "__main__":
    asyncio.run(main())
