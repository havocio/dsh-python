"""inject DI（依赖拓扑 + 延迟就绪）的验证（第 0 层收尾）。

运行：python -m dsh_py.tests.test_inject
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext


# --------------------------------------------------------------------------- #
# 延迟就绪：lazy=True 时依赖缺失挂起，provide 后自动唤醒
# --------------------------------------------------------------------------- #
def test_lazy_readiness_resolves_on_provide():
    ctx = AppContext()
    ran = {}

    def needs_x(c, cfg):
        ran["ok"] = c.x is not None

    # 依赖 x 未就绪，lazy 挂起
    handle = ctx.plugin(needs_x, {}, name="needs-x", inject=["x"], lazy=True)
    assert "ok" not in ran
    assert len(ctx._pending_plugins) == 1
    # provide x → 唤醒执行
    ctx.provide("x", object())
    assert ran.get("ok") is True
    assert ctx._pending_plugins == []
    handle.dispose()


def test_lazy_dispose_cancels_pending():
    """挂起态 dispose 应从延迟队列移除，finalize 不再报错。"""
    ctx = AppContext()

    def needs_x(c, cfg):
        pass

    handle = ctx.plugin(needs_x, {}, name="needs-x", inject=["x"], lazy=True)
    assert len(ctx._pending_plugins) == 1
    handle.dispose()  # 取消挂起
    assert ctx._pending_plugins == []
    ctx.finalize_pending()  # 无残留，不报错


def test_pending_chain_resolves_in_order():
    """链式依赖：a←b←c，provide 触发逆序唤醒。"""
    ctx = AppContext()
    order = []

    def a(c, cfg):
        order.append("a")
        c.provide("A", object())

    a.inject = ["B"]

    def b(c, cfg):
        order.append("b")
        c.provide("B", object())

    b.inject = ["C"]

    def c_plug(c, cfg):
        order.append("c")
        c.provide("C", object())

    # 全部 lazy 加载（a/b 挂起，c 无依赖立即执行）
    ctx.plugin(a, lazy=True)
    ctx.plugin(b, lazy=True)
    ctx.plugin(c_plug, lazy=True)
    # 唤醒顺序：c 执行(提供C) → b 执行(提供B) → a 执行
    assert order == ["c", "b", "a"]
    assert ctx._pending_plugins == []


# --------------------------------------------------------------------------- #
# 向后兼容：默认 lazy=False 仍「缺失即报错」
# --------------------------------------------------------------------------- #
def test_default_inject_missing_raises():
    ctx = AppContext()

    def needs_llm(c, cfg):
        c.llm.stream(cfg)

    try:
        ctx.plugin(needs_llm, {}, name="needs-llm", inject=["llm"])
    except RuntimeError as e:
        assert "llm" in str(e)
    else:  # pragma: no cover
        raise AssertionError("默认（lazy=False）缺失依赖应抛 RuntimeError")


# --------------------------------------------------------------------------- #
# finalize_pending：加载期结束仍有挂起 = 缺失 / 循环，报错
# --------------------------------------------------------------------------- #
def test_finalize_raises_on_orphan():
    ctx = AppContext()

    def orphan(c, cfg):
        pass

    orphan.inject = ["不存在的东西"]
    ctx.plugin(orphan, lazy=True)  # 挂起
    try:
        ctx.finalize_pending()
    except RuntimeError as e:
        assert "不存在的东西" in str(e)
    else:  # pragma: no cover
        raise AssertionError("finalize 应报缺失依赖错误")
    assert ctx._pending_plugins == []


def test_boot_lazy_loading_keeps_topo_order():
    """loader 走 lazy 加载，但拓扑排序保证 provider 先执行。"""
    from dsh_py.loader import load_profile

    ctx = AppContext()
    order = []

    def provider(c, cfg):
        order.append("provider")
        c.provide("cache", object())

    provider.provides = ["cache"]

    def consumer(c, cfg):
        order.append("consumer")
        assert c.cache is not None

    consumer.inject = ["cache"]
    # consumer 排在 provider 前，依赖 lazy + 拓扑排序仍能正确
    load_profile(ctx, [consumer, provider])
    assert order == ["provider", "consumer"]


def main():
    test_lazy_readiness_resolves_on_provide()
    test_lazy_dispose_cancels_pending()
    test_pending_chain_resolves_in_order()
    test_default_inject_missing_raises()
    test_finalize_raises_on_orphan()
    test_boot_lazy_loading_keeps_topo_order()
    print("OK: inject DI（延迟就绪 + 拓扑）测试通过")


if __name__ == "__main__":
    main()
