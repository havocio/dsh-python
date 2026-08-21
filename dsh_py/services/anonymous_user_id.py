"""匿名用户 id（identity/anonymous-user-id，第 3 层）。

遥测、反馈与 DeepSeek 请求共享的、限定于 Harness home 的匿名关联 id。

- id 是随机 UUID，以**裸一行**持久化在 ``resolve_dsh_home`` 解析出的 home
  下的 ``.anonymous-user-id``（``$DSH_HOME`` > ``~/.dsh``），绝不从主机名、
  网络地址、git remote 或其他识别源派生；
- 作用域是 home 而非机器：共享同一 ``$DSH_HOME`` 的每个进程报告同一 id；
  删除文件则下次启动铸造新身份；
- 读/写**同步**（boot 期与命令消费方共用同一 API）；结果按解析出的文件
  路径 memo 化（每进程只碰一次盘；运行中删除文件，本进程保持该 id 到下次
  启动）；
- 并发首启由独占创建（``wx``）settle：输者重读赢者 id（竞态窗口内可能
  出现两个每进程 id，下次启动收敛）；持久化 best-effort——只读 home 的写
  失败仍返回本次运行的可用 id（反馈与遥测永不被阻塞）。

与 dsh 差异（已注明）：dsh 用 ``Branded<'AnonymousUserId'>`` 类型；dsh_py
用 ``str`` 子类品牌（见 :class:`AnonymousUserId`），行为等价。
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Optional

from dsh_py.util.home_paths import resolve_dsh_home

# Harness home 内存储 id 的文件：裸 UUID 行，无包装格式
ANONYMOUS_USER_ID_FILE_NAME = ".anonymous-user-id"

# 校验 UUID v4 字面格式（读取时 trim + 正则；损坏 → 重新铸造）
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# 进程生命周期 memo：按解析出的文件路径键控（不同测试 home 绝不共享 id）
_memo: dict[str, str] = {}


class AnonymousUserId(str):
    """一个 home 作用域匿名用户 id（UUID v4 品牌字符串）。"""

    def __new__(cls, value: str) -> "AnonymousUserId":
        return str.__new__(cls, value)


def _read_persisted_id(file: str) -> Optional[str]:
    """读取文件中的有效持久 id；缺失/不可读/损坏返回 None。"""
    try:
        with open(file, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    value = text.strip()
    return value if UUID_PATTERN.match(value) else None


def get_or_create_anonymous_user_id(
    env: Optional[dict] = None,
    random_uuid: Optional[callable] = None,
) -> AnonymousUserId:
    """返回 home 的匿名用户 id，首次使用即铸造并持久化。

    :param env: 供 ``DSH_HOME`` 解析的环境映射（缺省 ``os.environ``）。
    :param random_uuid: UUID 生成器（缺省 ``uuid.uuid4``；测试钩子）。
    :returns: 稳定的每 home 匿名用户 id。
    """
    file = os.path.join(resolve_dsh_home(env=env), ANONYMOUS_USER_ID_FILE_NAME)
    cached = _memo.get(file)
    if cached is not None:
        return AnonymousUserId(cached)

    id_value = _read_persisted_id(file)
    if id_value is None:
        generate = random_uuid or uuid.uuid4
        created = str(generate())
        os.makedirs(os.path.dirname(file), exist_ok=True)
        try:
            # wx 独占创建：并发首启中输者重读赢者 id
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            fd = os.open(file, flags, 0o600)
            try:
                os.write(fd, f"{created}\n".encode("utf-8"))
            finally:
                os.close(fd)
            id_value = created
        except FileExistsError:
            # wx 拒绝同时覆盖「并发赢者」与「既有损坏文件」：重读采纳合法赢者；
            # 非法重读落到覆盖路径
            id_value = _read_persisted_id(file)
            if id_value is None:
                try:
                    with open(file, "w", encoding="utf-8") as f:
                        f.write(f"{created}\n")
                except OSError:
                    # best-effort：home 不可写也保留内存 id，本次运行仍一致
                    pass
                id_value = created
        except OSError:
            # 非 EEXIST 失败（只读 home）：best-effort，内存保留本次运行 id
            id_value = created

    _memo[file] = id_value
    return AnonymousUserId(id_value)
