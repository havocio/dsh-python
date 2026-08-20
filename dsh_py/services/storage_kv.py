"""极简 KV 表（对齐 dsh ``storage-domain`` 的 ``KvTable`` 子集，零依赖）。

投影缓存需要一条按会话 id 键控的持久记录（日志身份 + 检查点行）。dsh 用
storage-domain（工作区 JSON 旁的 domain 表）；本复刻提供一个零依赖的
JSON 文件 KV 表：内存态为权威，每次 ``put`` 同步原子落盘
（临时文件 + ``os.replace``，崩溃不产生半写行）。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Optional


class KvTable:
    """键 → JSON 值的持久表；``path=None`` 时纯内存（测试友好）。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        if path is not None and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = loaded
            except (json.JSONDecodeError, OSError):
                # 损坏的表文件视为空（fail-soft：缓存陈旧而非崩溃）
                self._data = {}

    def get(self, key: str) -> Optional[Any]:
        """取一条记录的**分离**深拷贝（防调用方腐蚀权威状态）。"""
        value = self._data.get(key)
        return copy.deepcopy(value) if value is not None else None

    def put(self, key: str, value: Any) -> None:
        """整体替换一条记录并原子落盘（分离存储，绝不持有调用方引用）。"""
        self._data[key] = copy.deepcopy(value)
        if self._path is not None:
            self._save()

    def _save(self) -> None:
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)
        os.replace(tmp, self._path)

    def close(self) -> None:
        """落盘并关闭（不销毁数据）。"""
        if self._path is not None:
            self._save()
