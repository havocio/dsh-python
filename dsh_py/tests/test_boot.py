"""Loader/Boot 管线（多 layer、patch 指令、env 插值、热重载）的验证（第 0 层第四批）。

运行：python dsh_py/tests/test_boot.py
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.env import interpolate_env, load_layered_env, parse_env
from dsh_py.loader import CORE_PROFILE, boot, compose_entries, load_profile
from dsh_py.watcher import ProfileWatcher


# --------------------------------------------------------------------------- #
# compose_entries：patch 语义
# --------------------------------------------------------------------------- #
def test_compose_basic_rows_and_id_override():
    layers = [
        [{"id": "a", "apply": lambda c, cfg: None, "config": {"x": 1, "keep": "v"}}],
        # 用户层 patch：覆盖 a 的配置（深合并）
        [{"id": "a", "config": {"x": 2, "extra": True}}],
    ]
    rows = compose_entries(*layers)
    assert len(rows) == 1
    assert rows[0]["id"] == "a"
    assert rows[0]["config"] == {"x": 2, "keep": "v", "extra": True}


def test_compose_disable():
    layers = [
        [{"id": "a", "apply": lambda c, cfg: None}],
        [{"id": "a", "disabled": True}],
    ]
    rows = compose_entries(*layers)
    assert rows[0]["disabled"] is True


def test_compose_insert_tail_before_after():
    def p1(c, cfg): pass
    base = [
        {"id": "x", "apply": p1},
        {"id": "z", "apply": p1},
    ]
    # 尾部插入
    rows = compose_entries(base, [{"insert": [{"id": "t", "apply": p1}]}])
    assert [r["id"] for r in rows] == ["x", "z", "t"]
    # before 插入
    rows = compose_entries(base, [{"insert": {"before": "z", "entries": [{"id": "b", "apply": p1}]}}])
    assert [r["id"] for r in rows] == ["x", "b", "z"]
    # after 插入
    rows = compose_entries(base, [{"insert": {"after": "x", "entries": [{"id": "m", "apply": p1}]}}])
    assert [r["id"] for r in rows] == ["x", "m", "z"]
    # patch 引用不存在的 id → 报错
    try:
        compose_entries(base, [{"id": "nope", "config": {}}])
    except RuntimeError as e:
        assert "nope" in str(e)
    else:  # pragma: no cover
        raise AssertionError("patch 引用不存在的 id 应报错")


# --------------------------------------------------------------------------- #
# boot：多 layer 装配 + disabled 跳过
# --------------------------------------------------------------------------- #
def test_boot_merges_layers_and_skips_disabled():
    ctx = AppContext()
    seen = []

    def row_a(c, cfg):
        seen.append(("a", cfg))

    def row_b(c, cfg):
        seen.append(("b", cfg))

    bundle = [
        {"id": "a", "apply": row_a, "config": {"n": 1}},
        {"id": "b", "apply": row_b},
    ]
    user = [{"id": "a", "config": {"n": 2}}, {"id": "b", "disabled": True}]
    handles = boot(ctx, bundle, user)
    assert seen == [("a", {"n": 2})]  # a 配置被覆盖、b 被禁用
    assert len(handles) == 1


def test_boot_with_core_profile_and_custom_row():
    ctx = AppContext()
    marker = {}

    def my_plugin(c, cfg):
        marker["ran"] = True

    handles = boot(ctx, CORE_PROFILE, [my_plugin])
    assert ctx.has_service("llm") and ctx.has_service("agentLoop")
    assert marker.get("ran") is True
    assert len(handles) == 6  # 5 核心 + 1 业务


def test_load_profile_still_accepts_id_rows():
    ctx = AppContext()
    handles = load_profile(ctx, CORE_PROFILE)
    assert len(handles) == 5
    assert ctx.has_service("llm") and ctx.has_service("agents")


# --------------------------------------------------------------------------- #
# env 分层 + 插值
# --------------------------------------------------------------------------- #
def test_parse_env_and_interpolate():
    env = parse_env("A=1\n# 注释\nB=\"quoted\"\n\nC='single'")
    assert env == {"A": "1", "B": "quoted", "C": "single"}

    interpolated = interpolate_env(
        {"base": "http://${HOST}:8080", "nested": {"k": "$PORT"}, "arr": ["${A}", 1]},
        {"HOST": "localhost", "PORT": "9000", "A": "x"},
    )
    assert interpolated == {
        "base": "http://localhost:8080",
        "nested": {"k": "9000"},
        "arr": ["x", 1],
    }


def test_load_layered_env_with_temp_env():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
            f.write("MY_LAYERED=from-env\n")
        os.environ["MY_LAYERED"] = "inherited"
        merged = load_layered_env(cwd=tmp, home=tmp)
        # 继承变量优先，.env 不覆盖
        assert merged["MY_LAYERED"] == "inherited"
        # bootstrap-only 变量由 .env 设置 → 拒绝
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
            f.write("DSH_SECRET=bad\n")
        try:
            load_layered_env(cwd=tmp, home=tmp)
        except ValueError as e:
            assert "DSH_SECRET" in str(e)
        else:  # pragma: no cover
            raise AssertionError(".env 设置 DSH_ 前缀变量应被拒绝")


# --------------------------------------------------------------------------- #
# 热重载 watcher
# --------------------------------------------------------------------------- #
async def test_watcher_reloads_on_file_change():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "profile.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("PROFILE = []\n")

        reloaded = []
        watcher = ProfileWatcher([path], lambda: reloaded.append(True), interval=0.05)
        await watcher.start()
        try:
            # 修改文件（写入后让 mtime 稳定，再等待一轮轮询触发）
            await asyncio.sleep(0.05)
            with open(path, "w", encoding="utf-8") as f:
                f.write("PROFILE = [1]\n")
            for _ in range(40):
                if reloaded:
                    break
                await asyncio.sleep(0.05)
            assert reloaded, "文件变化应触发 reload"
        finally:
            await watcher.stop()


async def main():
    test_compose_basic_rows_and_id_override()
    test_compose_disable()
    test_compose_insert_tail_before_after()
    test_boot_merges_layers_and_skips_disabled()
    test_boot_with_core_profile_and_custom_row()
    test_load_profile_still_accepts_id_rows()
    test_parse_env_and_interpolate()
    test_load_layered_env_with_temp_env()
    await test_watcher_reloads_on_file_change()
    print("OK: Loader/Boot 管线测试通过（多 layer、patch 指令、env 插值、热重载）")


if __name__ == "__main__":
    asyncio.run(main())
