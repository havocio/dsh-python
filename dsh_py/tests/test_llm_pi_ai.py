"""llm-pi-ai 通用多供应商适配器测试（对标 dsh 的 llm-pi-ai）。

覆盖：配置解析（目录路由/手工路由/modelOverrides/各类校验）、模型目录材料化、
适配器元信息与推理能力、stream 端到端（openai/deepseek 两种 thinkingFormat）、
错误分类、图像/stop/推理拒绝、插件生命周期（dormant/settings/MISSING_CREDENTIAL）、
模型发现（目录短路 + LlmService 注册）。
"""

import asyncio
import json
import os
import unittest

from dsh_py.core.context import AppContext
from dsh_py.services import llm as L
from dsh_py.services import session as S
from dsh_py.services import settings as ST
from dsh_py.services.adapters import pi_ai as P
from dsh_py.services.llm import ChunkType, GenerateOptions, LlmError
from dsh_py.services.message import MessageSource, TextBlock, create_user_message


def _mock_profiles(cfg: dict):
    """把配置解析成固定的 profiles thunk（模拟 apply 的 memoized 快照）。"""
    resolved = P.resolve_profiles(cfg)
    return lambda: resolved


def _sse(*objs):
    """构造一个 mock transport：按给定对象产出 SSE data 行 + [DONE]。"""
    async def transport(url, body, headers):
        async def gen():
            for obj in objs:
                yield "data: " + json.dumps(obj)
            yield "data: [DONE]"
        return gen()
    return transport


def _hang_transport():
    """永不产出的 transport（用于 idle timeout 测试）。"""
    async def transport(url, body, headers):
        async def gen():
            while True:
                await asyncio.sleep(3600)
                yield "data: {}"  # 永不执行到；保证 gen 是 async generator
        return gen()
    return transport


async def _noop_resolve(provider, profile):
    return "sk-test-key"


def _cfg(provider="mock", **extra):
    """构造一个手工 mock 路由配置。"""
    cfg = {"api": "openai-completions", "baseURL": "https://m.example/v1",
           "models": [{"id": "m1"}]}
    cfg.update(extra)
    return {provider: cfg}


class TestConfig(unittest.TestCase):
    """配置解析与校验（对标 dsh 的 config.ts / catalog.ts）。"""

    def test_catalog_route_defaults(self):
        profiles = P.resolve_profiles({"openai": {"apiKeyEnv": "OPENAI_API_KEY"}})
        self.assertIn("openai", profiles)
        profile = profiles["openai"]
        self.assertEqual(profile["displayName"], "openai")
        self.assertEqual(profile["apiKeyEnv"], "OPENAI_API_KEY")
        self.assertEqual(profile["api"], None)  # 目录默认
        models = profile["models"]
        self.assertEqual(models[0]["api"], "openai-completions")
        self.assertEqual(models[0]["baseUrl"], "https://api.openai.com/v1")
        self.assertIn("gpt-4o", [m["id"] for m in models])
        # o3-mini 是目录推理模型
        o3 = next(m for m in models if m["id"] == "o3-mini")
        self.assertTrue(o3["reasoning"])
        self.assertIn("high", o3["reasoningEfforts"])

    def test_hand_declared_route(self):
        profiles = P.resolve_profiles(_cfg(displayName="Acme", apiKeyEnv="ACME_KEY"))
        profile = profiles["mock"]
        self.assertEqual(profile["displayName"], "Acme")
        model = profile["models"][0]
        self.assertEqual(model["baseUrl"], "https://m.example/v1")
        self.assertEqual(model["contextWindow"], P.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(model["maxTokens"], P.DEFAULT_MAX_TOKENS)
        self.assertEqual(profile["configuredMaxTokens"], {})

    def test_reasoning_efforts(self):
        profiles = P.resolve_profiles(_cfg(models=[
            {"id": "m1", "reasoningEfforts": {"off": None, "high": "high", "max": "ultra"}}]))
        model = profiles["mock"]["models"][0]
        self.assertTrue(model["reasoning"])
        self.assertEqual(model["reasoningEfforts"], {"off": None, "high": "high", "max": "ultra"})
        # reasoningEfforts: false → 非推理
        profiles = P.resolve_profiles(_cfg(models=[
            {"id": "m1", "reasoningEfforts": False}]))
        self.assertFalse(profiles["mock"]["models"][0]["reasoning"])

    def test_reasoning_efforts_invalid(self):
        # 空 dict
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(models=[{"id": "m1", "reasoningEfforts": {}}]))
        # 只有 off
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(models=[{"id": "m1", "reasoningEfforts": {"off": None}}]))
        # 非 off 档位空值
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(models=[{"id": "m1", "reasoningEfforts": {"high": None}}]))
        # 空字符串值
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(models=[{"id": "m1", "reasoningEfforts": {"high": ""}}]))
        # 未知档位
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(models=[{"id": "m1", "reasoningEfforts": {"turbo": "x"}}]))

    def test_model_overrides(self):
        # 目录路由用 modelOverrides 修正单个模型
        profiles = P.resolve_profiles({
            "openai": {"modelOverrides": {"gpt-4o": {"contextWindow": 100000}}},
        })
        gpt4o = next(m for m in profiles["openai"]["models"] if m["id"] == "gpt-4o")
        self.assertEqual(gpt4o["contextWindow"], 100000)
        gpt41 = next(m for m in profiles["openai"]["models"] if m["id"] == "gpt-4.1")
        self.assertEqual(gpt41["contextWindow"], 1_047_576)  # 其余保持目录
        # 未知模型 id 拒绝
        with self.assertRaises(ValueError):
            P.resolve_profiles({"openai": {"modelOverrides": {"no-such": {}}}})
        # models 与 modelOverrides 互斥
        with self.assertRaises(ValueError):
            P.resolve_profiles({"openai": {"models": [{"id": "gpt-4o"}],
                                           "modelOverrides": {"gpt-4o": {}}}})
        # 手工路由不能 modelOverrides
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(modelOverrides={"m1": {}}))

    def test_duplicate_model_rejected(self):
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(models=[{"id": "m1"}, {"id": "m1"}]))

    def test_malformed_profiles(self):
        # providers 是数组
        with self.assertRaises(ValueError):
            P.resolve_profiles([{"provider": "x"}])
        # 空 provider 名
        with self.assertRaises(ValueError):
            P.resolve_profiles({"": {"api": "openai-completions", "models": [{"id": "m"}]}})
        # 空 baseURL
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(baseURL=""))
        # 空 displayName
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(displayName=""))
        # 未知协议
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(api="anthropic-messages"))
        # 未知 thinkingFormat
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(compat={"thinkingFormat": "together"}))
        # 未知档位
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(reasoning="turbo"))
        # 手工路由缺 api
        with self.assertRaises(ValueError):
            P.resolve_profiles({"mock": {"baseURL": "https://m.example/v1",
                                         "models": [{"id": "m1"}]}})
        # 已移除字段
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(maxRetries=3))
        with self.assertRaises(ValueError):
            P.resolve_profiles({"mock": {"api": "openai-completions",
                                         "baseURL": "https://m.example/v1",
                                         "models": [{"id": "m1"}], "provider": "x"}})
        # streamIdleTimeoutMs 越界
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(streamIdleTimeoutMs=0))
        with self.assertRaises(ValueError):
            P.resolve_profiles(_cfg(streamIdleTimeoutMs=P.MAX_TIMER_DELAY_MS + 1))

    def test_configured_max_tokens(self):
        profiles = P.resolve_profiles(_cfg(models=[
            {"id": "m1", "maxTokens": 4096}, {"id": "m2"}]))
        self.assertEqual(profiles["mock"]["configuredMaxTokens"], {"m1": 4096})


class TestClassifyError(unittest.TestCase):
    """错误文本分类（对标 classifyPiAiError）。"""

    def test_categories(self):
        cases = [
            ("401 unauthorized", "AUTH"),
            ("403 forbidden", "AUTH"),
            ("insufficient_quota", "QUOTA"),
            ("rate limit exceeded (429)", "RATE_LIMIT"),
            ("invalid_request_error 400", "INVALID_REQUEST"),
            ("503 service unavailable", "SERVER"),
            ("stream idle timeout after 300000ms", "TIMEOUT"),
            ("stream ended without a terminal event", "TRANSPORT"),
            ("other side closed", "TRANSPORT"),
            ("ECONNRESET", "TRANSPORT"),
            ("something odd", "PI_AI_ERROR"),
        ]
        for text, expected in cases:
            self.assertEqual(P.classify_error(text), expected, text)


class TestAdapter(unittest.TestCase):
    """适配器元信息与流式调用。"""

    async def _stream(self, adapter, options):
        return [c async for c in adapter.stream(options)]

    def test_provider_info(self):
        adapter = P.PiAiAdapter(_mock_profiles(_cfg(displayName="Acme")), _noop_resolve)
        info = adapter.provider_info("mock")
        self.assertEqual(info.name, "Acme")
        info = adapter.provider_info("nope")
        self.assertEqual(info.name, "nope")  # 非本适配器路由回退键名

    def test_list_models_and_resolve(self):
        adapter = P.PiAiAdapter(_mock_profiles(_cfg(
            displayName="Acme",
            models=[{"id": "m1", "maxTokens": 4096,
                     "reasoningEfforts": {"off": None, "high": "high"}}])), _noop_resolve)
        asyncio.run(self._check(adapter))

    async def _check(self, adapter):
        models = await adapter.list_models("mock")
        self.assertEqual(models[0]["id"], "m1")
        info = await adapter.resolve_model("mock", "m1")
        self.assertEqual(info["context"]["contextWindow"], P.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(info["defaultMaxTokens"], 4096)  # 显式配置才成为请求默认
        self.assertNotIn("defaultEffort", info["reasoning"])  # 无 reasoning 配置 → 无默认档位
        self.assertEqual([e["id"] for e in info["reasoning"]["efforts"]], ["off", "high"])
        # 未知模型 / 未知供应商
        with self.assertRaises(LlmError) as cm:
            await adapter.resolve_model("mock", "nope")
        self.assertEqual(cm.exception.code, "UNKNOWN_MODEL")
        with self.assertRaises(LlmError) as cm:
            await adapter.resolve_model("nope", "m1")
        self.assertEqual(cm.exception.code, "NO_ADAPTER")

    def test_reasoning_default_effort(self):
        adapter = P.PiAiAdapter(_mock_profiles(_cfg(
            reasoning="high",
            models=[{"id": "m1", "reasoningEfforts": {"off": None, "high": "high"}}])), _noop_resolve)

        async def go():
            info = await adapter.resolve_model("mock", "m1")
            self.assertEqual(info["reasoning"]["defaultEffort"], "high")
            # 配置了模型不支持的档位 → 描述时省略（不毁掉目录）
            bad = P.PiAiAdapter(_mock_profiles(_cfg(
                reasoning="max",
                models=[{"id": "m1", "reasoningEfforts": {"off": None, "high": "high"}}])), _noop_resolve)
            info2 = await bad.resolve_model("mock", "m1")
            self.assertNotIn("defaultEffort", info2["reasoning"])
        asyncio.run(go())

    def test_stream_openai_format(self):
        adapter = P.PiAiAdapter(
            _mock_profiles(_cfg()),
            _noop_resolve,
            transport=_sse({"choices": [{"delta": {"content": "你好"}}]},
                           {"choices": [{"delta": {"content": "世界"}}]},
                           {"choices": [{"delta": {}}],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}))
        chunks = asyncio.run(self._stream(adapter, GenerateOptions(
            provider="mock", model="m1",
            messages=[create_user_message([TextBlock("hi")], MessageSource("user"))])))
        text = "".join(c.text or "" for c in chunks)
        self.assertEqual(text, "你好世界")
        kinds = [c.type for c in chunks]
        self.assertIn(ChunkType.USAGE, kinds)
        self.assertIn(ChunkType.FINISH, kinds)
        usage = next(c.usage for c in chunks if c.type == ChunkType.USAGE)
        self.assertEqual(usage["inputTokens"], 10)

    def test_stream_deepseek_thinking(self):
        adapter = P.PiAiAdapter(
            _mock_profiles(_cfg(compat={"thinkingFormat": "deepseek"},
                                models=[{"id": "m1",
                                         "reasoningEfforts": {"off": None, "high": "high"}}])),
            _noop_resolve,
            transport=_sse({"choices": [{"delta": {"reasoning_content": "想想"}}]},
                           {"choices": [{"delta": {"content": "答案"}}]},
                           {"choices": [{"delta": {}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
        chunks = asyncio.run(self._stream(adapter, GenerateOptions(
            provider="mock", model="m1", messages=[], reasoning_effort="high")))
        self.assertTrue(any(c.type == ChunkType.REASONING_DELTA for c in chunks))
        text = "".join(c.text or "" for c in chunks)
        self.assertEqual(text, "答案")

    def test_stream_rejects(self):
        adapter = P.PiAiAdapter(_mock_profiles(_cfg()), _noop_resolve, transport=_sse({}))
        # 非推理模型要求档位
        with self.assertRaises(LlmError) as cm:
            asyncio.run(self._stream(adapter, GenerateOptions(
                provider="mock", model="m1", messages=[], reasoning_effort="high")))
        self.assertEqual(cm.exception.code, "UNSUPPORTED_REASONING_EFFORT")
        # 图像块（dict 消息）
        with self.assertRaises(LlmError) as cm:
            asyncio.run(self._stream(adapter, GenerateOptions(
                provider="mock", model="m1",
                messages=[{"role": "user", "content": [{"type": "image"}]}])))
        self.assertEqual(cm.exception.code, "UNSUPPORTED_CONTENT")
        # stop 不支持
        with self.assertRaises(LlmError) as cm:
            asyncio.run(self._stream(adapter, GenerateOptions(
                provider="mock", model="m1", messages=[], stop=["x"])))
        self.assertEqual(cm.exception.code, "UNSUPPORTED_OPTION")

    def test_stream_idle_timeout(self):
        adapter = P.PiAiAdapter(
            _mock_profiles(_cfg(streamIdleTimeoutMs=100)),
            _noop_resolve,
            transport=_hang_transport())

        async def go():
            with self.assertRaises(LlmError) as cm:
                async for _ in adapter.stream(GenerateOptions(provider="mock", model="m1", messages=[])):
                    pass
            self.assertEqual(cm.exception.code, "TIMEOUT")
        asyncio.run(go())


class TestPlugin(unittest.TestCase):
    """插件生命周期与模型发现。"""

    def test_dormant_mount_and_settings(self):
        async def go():
            ctx = AppContext()
            L.apply(ctx)
            ST.apply(ctx)
            P.apply(ctx, {})
            # dormant：空路由集不注册
            self.assertEqual(ctx.llm.list_providers(), [])
            # 可配置目录含全部内置路由
            cfg = {e["provider"] for e in ctx.llm.list_configurable_providers()}
            self.assertIn("openai", cfg)
            self.assertIn("ollama", cfg)
            # settings 提供 profile 后注册
            ctx.settings.set(P.NS, {"providers": {"openai": {"apiKeyEnv": "OPENAI_API_KEY"}}})
            self.assertEqual([p.id for p in ctx.llm.list_providers()], ["openai"])
            # 清空后撤下
            ctx.settings.set(P.NS, {"providers": {}})
            self.assertEqual(ctx.llm.list_providers(), [])
            # 非法 profile 被拒绝（assertServiceable）
            with self.assertRaises(ValueError):
                ctx.settings.set(P.NS, {"providers": {"openai": {"api": "anthropic-messages"}}})
        asyncio.run(go())

    def test_missing_credential(self):
        async def go():
            ctx = AppContext()
            L.apply(ctx)
            ST.apply(ctx)
            P.apply(ctx, {})
            ctx.settings.set(P.NS, {"providers": {"acme": {
                "apiKeyEnv": "ACME_KEY", "api": "openai-completions",
                "baseURL": "https://acme.example/v1", "models": [{"id": "m1"}]}}})
            os.environ.pop("ACME_KEY", None)
            with self.assertRaises(LlmError) as cm:
                async for _ in ctx.llm.stream(GenerateOptions(provider="acme", model="m1", messages=[])):
                    pass
            self.assertEqual(cm.exception.code, "MISSING_CREDENTIAL")
            with self.assertRaises(LlmError) as cm:
                await ctx.llm.discover_models(P.NS, {"provider": "acme", "baseURL": "https://acme.example/v1"})
            self.assertEqual(cm.exception.code, "MISSING_CREDENTIAL")
        asyncio.run(go())

    def test_model_discovery_catalog_shortcircuit(self):
        async def go():
            ctx = AppContext()
            L.apply(ctx)
            P.apply(ctx, {})
            found = await ctx.llm.discover_models(P.NS, {"provider": "openai"})
            self.assertTrue(any(m["id"] == "gpt-4o" for m in found))
            # 未注册的命名空间
            from dsh_py.services.settings import settings_namespace
            with self.assertRaises(RuntimeError):
                await ctx.llm.discover_models(settings_namespace("nope"), {})
        asyncio.run(go())

    def test_register_model_discovery_handle(self):
        async def go():
            ctx = AppContext()
            L.apply(ctx)
            from dsh_py.services.settings import settings_namespace
            ns = settings_namespace("demo")
            handle = ctx.llm.register_model_discovery(ns, lambda req: [{"id": "a"}])
            # 同一命名空间重复注册拒绝
            with self.assertRaises(RuntimeError):
                ctx.llm.register_model_discovery(ns, lambda req: [])
            found = await ctx.llm.discover_models(ns, {})
            self.assertEqual(found, [{"id": "a"}])
            handle.replace(lambda req: [{"id": "b"}])
            self.assertEqual(await ctx.llm.discover_models(ns, {}), [{"id": "b"}])
            handle()
            with self.assertRaises(RuntimeError):
                await ctx.llm.discover_models(ns, {})
            # dispose 后允许重新注册
            ctx.llm.register_model_discovery(ns, lambda req: [{"id": "c"}])
            self.assertEqual(await ctx.llm.discover_models(ns, {}), [{"id": "c"}])
        asyncio.run(go())

    def test_agent_end_to_end(self):
        async def go():
            ctx = AppContext()
            L.apply(ctx)
            S.apply(ctx)
            from dsh_py.services.agent import AgentOptions, apply_loop, apply_registry
            apply_registry(ctx)
            apply_loop(ctx)
            adapter = P.PiAiAdapter(
                _mock_profiles(_cfg()),
                _noop_resolve,
                transport=_sse({"choices": [{"delta": {"content": "好"}}]},
                               {"choices": [{"delta": {}}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
            ctx.llm.register_adapter(["mock"], adapter)
            agent = ctx.agents.create_agent(ctx.sessions.create(),
                                            AgentOptions(provider="mock", model="m1"))
            await agent.run("你好")
            self.assertGreater(len(agent.session.events), 0)
            info = await ctx.llm.resolve_model_info("mock", "m1")
            self.assertEqual(info["context"]["contextWindow"], P.DEFAULT_CONTEXT_WINDOW)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
