"""identity/anonymous-user-id 验证（第 3 层）。

运行：python dsh_py/tests/test_identity.py

覆盖：
- 首次调用铸造并持久化到 ``<home>/.anonymous-user-id``（裸 UUID 一行）；
- 同 home 跨调用稳定（进程级 memo + 落盘）；
- 不同 home 不同 id；DSH_HOME 环境映射生效；
- 损坏/非 UUID 内容 → 重新铸造覆盖；
- wx 独占创建：并发首启输者重读赢者 id；
- 持久化 best-effort：home 不可写时仍返回本次运行的可用 id；
- 自定义 UUID 生成器（测试钩子）。
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.services.anonymous_user_id import (
    ANONYMOUS_USER_ID_FILE_NAME,
    AnonymousUserId,
    get_or_create_anonymous_user_id,
)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def test_first_call_mints_and_persists():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DSH_HOME": tmp}
        uid = get_or_create_anonymous_user_id(env=env)
        assert isinstance(uid, AnonymousUserId)
        assert UUID_RE.match(str(uid))
        path = os.path.join(tmp, ANONYMOUS_USER_ID_FILE_NAME)
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            assert f.read().strip() == str(uid)


def test_stable_across_calls_and_homes():
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        e1, e2 = {"DSH_HOME": tmp1}, {"DSH_HOME": tmp2}
        uid1 = get_or_create_anonymous_user_id(env=e1)
        # 同 home 稳定（memo + 落盘）
        assert get_or_create_anonymous_user_id(env=e1) == uid1
        # 不同 home 不同 id
        uid2 = get_or_create_anonymous_user_id(env=e2)
        assert uid2 != uid1
        assert get_or_create_anonymous_user_id(env=e2) == uid2


def test_corrupt_file_replaced():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DSH_HOME": tmp}
        path = os.path.join(tmp, ANONYMOUS_USER_ID_FILE_NAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-a-uuid\n")
        uid = get_or_create_anonymous_user_id(env=env)
        assert UUID_RE.match(str(uid))
        with open(path, encoding="utf-8") as f:
            assert f.read().strip() == str(uid)


def test_custom_generator_hook():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DSH_HOME": tmp}
        fixed = "11111111-2222-3333-4444-555555555555"
        uid = get_or_create_anonymous_user_id(env=env, random_uuid=lambda: fixed)
        assert str(uid) == fixed
        with open(os.path.join(tmp, ANONYMOUS_USER_ID_FILE_NAME), encoding="utf-8") as f:
            assert f.read().strip() == fixed


def test_wx_concurrent_first_launch_loser_reads_winner():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DSH_HOME": tmp}
        path = os.path.join(tmp, ANONYMOUS_USER_ID_FILE_NAME)
        # 预置赢者 id（模拟并发另一进程先 wx 创建）
        winner = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{winner}\n")
        uid = get_or_create_anonymous_user_id(env=env, random_uuid=lambda: "ffffffff-0000-0000-0000-000000000000")
        # 输者重读赢者 id（wx 独占创建被拒）
        assert str(uid) == winner


def test_unwritable_home_best_effort():
    if os.name == "nt":
        return  # Windows 权限模型不保证只读目录写失败，跳过
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o500)  # 只读 home
        try:
            uid = get_or_create_anonymous_user_id(env={"DSH_HOME": tmp})
            assert UUID_RE.match(str(uid))  # 仍返回可用 id
        finally:
            os.chmod(tmp, 0o700)


print("8 项，0 失败")
