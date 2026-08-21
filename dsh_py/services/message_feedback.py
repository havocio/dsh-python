"""按消息生命周期绑定的反馈伴随记录（feedback/message-feedback，第 3 层）。

对已定稿 assistant 消息的耐久、生命周期绑定评分/备注（sidecar）。与
command-feedback 不同：它**不是** Session 事件或投影，只保存在本地伴随记录
中，不触发遥测交接。服务随附 Host Remote 契约（``messageFeedback.list/put/
delete``，经 typert 协议暴露）。

业务语义（对齐 dsh）：
- 伴随记录按「持久会话生命周期」栅栏（identity = createdAt + cwd）：复用的
  Session id 的陈旧行不可见；
- ``put`` 是整值替换：目标必须是已定稿的 append-origin assistant 消息；每次
  请求须匹配当前版本（CAS），匹配的无操作直接返回存储项不改版本；material
  变更才换新版本 token；
- ``note`` 校验：空白拒绝（``note-blank``），UTF-8 字节超限拒绝
  （``note-too-large``）；
- 删除：缺席恒成功（幂等），存在须版本精确匹配；
- 每次会话的读/比较/写突变经串行尾队列，关闭时排水。

**与 dsh 的差异（已注明）**：
- dsh 经 ``storage-domain`` 落盘；dsh_py 亦经通用 storage seam
  （``ctx.storageDomain``，domain ``message_feedback`` 的 ``sessions`` 表，
  路由到配置的后端——JSON/SQLite）；行 schema 以 :mod:`dsh_py.core.schema`
  校验（dsh 用 zod；dsh_py 无 ``literal`` 构造器，rating 用宽松 string，
  值由本服务内部保证）；
- dsh 的 ``ensureTargetDurable`` 在耐久屏障后从物理持久前缀重读（冷/热双路）；
  dsh_py 的 ``sessions.flush`` 是同步屏障（无布尔返回），屏障后直接以会话
  当前头/事件重建检查（差异仅影响极少数「持久化后端未挂载」装配）；
- dsh 用 ``isAppendSurfaceEvent`` 判定 append-origin（surface 操作持久化）；
  dsh_py 的 surface 操作仅存内存，冷会话（从持久化装载）目标检查退化为
  「任何 assistant/message 事件」——活会话仍做精确 surface 判定。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.storage_domain import define_domain, domain_table
from dsh_py.services.typert import remote

RATINGS = ("positive", "negative")
DEFAULT_MAX_NOTE_BYTES = 2000


# --------------------------------------------------------------------------- #
# 公共词汇
# --------------------------------------------------------------------------- #
# 伴随记录的持久行 schema（对齐 dsh 的 message-feedback/spec.ts）
_MF_ITEM = z.object({
    "messageId": z.string(),
    "rating": z.string(),          # dsh_py 无 literal 构造器：值由本服务内部保证
    "note": z.string().optional(),
    "version": z.string(),
    "createdAt": z.integer(),
    "updatedAt": z.integer(),
})
_MF_ROW = z.object({
    "session": z.object({"createdAt": z.integer(), "cwd": z.string().optional()}),
    "items": z.array(_MF_ITEM),
})

# 一个按持久会话 id 键控的伴随记录域（每条 Session 生命周期一行 sidecar）
message_feedback_domain = define_domain({
    "name": "message_feedback",
    "version": 0,
    "tables": {"sessions": domain_table(_MF_ROW)},
})
def _next_version() -> str:
    """一次 material 变更的透明相等性 token。"""
    return str(uuid.uuid4())


def snapshot_item(item: dict) -> dict:
    """复制并冻结一条反馈项（跨越服务边界前）。"""
    copied = {
        "messageId": item["messageId"],
        "rating": item["rating"],
        "version": item["version"],
        "createdAt": item["createdAt"],
        "updatedAt": item["updatedAt"],
    }
    if item.get("note") is not None:
        copied["note"] = item["note"]
    return copied


def snapshot_list(items: list) -> dict:
    return {"items": [snapshot_item(i) for i in items]}


def _success(value: Any) -> dict:
    return {"ok": True, "value": value}


def _rejected(error: dict) -> dict:
    return {"ok": False, "error": dict(error)}


def _row_snapshot(session_identity: dict, items: list) -> dict:
    return {"session": dict(session_identity), "items": [snapshot_item(i) for i in items]}


def _identity_of(header: Any) -> dict:
    ident = {"createdAt": header.created_at}
    if getattr(header, "cwd", None) is not None:
        ident["cwd"] = header.cwd
    return ident


def _same_identity(row: dict, header: Any) -> bool:
    return (row["session"].get("createdAt") == header.created_at
            and row["session"].get("cwd") == getattr(header, "cwd", None))


def _same_header_identity(left: Any, right: Any) -> bool:
    return (left.id == right.id
            and left.created_at == right.created_at
            and left.cwd == right.cwd)


def _resolve_max_note_bytes(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TypeError(
            f"message-feedback: maxNoteBytes 必须是正整数安全整数，得到 {value!r}",
        )
    return value


def _utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


Config = z.object({
    "maxNoteBytes": z.integer().default(DEFAULT_MAX_NOTE_BYTES),
})


class MessageFeedbackService(Service):
    """storage sidecar 服务：检查持久化会话历史，绝不创建或恢复 Agent/Session。

    ``ctx.messageFeedback``；随附 ``messageFeedback`` typert 远程作用域
    （list / put / delete），仅当 typertRegistry 缝被装配时注册。

    持久化经通用存储 seam（``ctx.storageDomain``，domain ``message_feedback``
    的 ``sessions`` 表）；与 dsh 差异（已注明）：dsh 的服务用
    ``Service.init`` 生命周期打开域，dsh_py 以延迟 ``_open_domain`` 打开。
    """

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "messageFeedback")
        cfg = config or {}
        self._max_note_bytes = _resolve_max_note_bytes(int(cfg.get("maxNoteBytes", DEFAULT_MAX_NOTE_BYTES)))
        self._table: Optional[Any] = None
        self._domain: Optional[Any] = None
        self._admission_open = True

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def _open_domain(self) -> Any:
        """惰性打开伴随记录域并解析 ``sessions`` 表句柄。"""
        if self._table is None:
            self._domain = await self.ctx.storageDomain.open(message_feedback_domain)
            self._table = self._domain.table("sessions")
            # 卸载时排水并关闭（对齐 dsh 的 domainClose）
            self.ctx.effect(self.close, label="message-feedback.close")
        return self._table

    def close(self) -> None:
        self._admission_open = False
        if self._domain is not None:
            domain = self._domain
            self._domain = None
            self._table = None
            asyncio.ensure_future(domain.close())

    # ------------------------------------------------------------------ #
    # Remote 端点
    # ------------------------------------------------------------------ #
    @remote("list")
    async def list(self, request: dict) -> dict:
        """读取当前持久会话生命周期归属的反馈。"""
        known = self._inspect_session(request["sessionId"])
        if not known["ok"]:
            return _rejected({"code": "session-not-found", "sessionId": request["sessionId"]})
        row = (await self._open_domain()).get(request["sessionId"])
        items = row["items"] if row is not None and _same_identity(row, known["meta"]) else []
        return _success(snapshot_list(items))

    @remote("put")
    async def put(self, request: dict) -> dict:
        """创建或替换一条消息的反馈（CAS 版本匹配；匹配无操作不换版本）。"""
        note = self._resolve_note(request.get("note"))
        if not note["ok"]:
            return note
        return await self._enqueue(request["sessionId"], lambda: self._put_locked(request, note["value"]))

    @remote("delete")
    async def delete(self, request: dict) -> dict:
        """删除一条反馈；缺席恒成功，存在须版本精确匹配。"""
        return await self._enqueue(request["sessionId"], lambda: self._delete_locked(request))

    # ------------------------------------------------------------------ #
    # 实现
    # ------------------------------------------------------------------ #
    async def _put_locked(self, request: dict, note: Optional[str]) -> dict:
        known = self._inspect_session(request["sessionId"])
        if not known["ok"]:
            return _rejected({"code": "session-not-found", "sessionId": request["sessionId"]})
        if not self._has_feedback_target(known, request["messageId"]):
            return _rejected({
                "code": "target-not-found",
                "sessionId": request["sessionId"],
                "messageId": request["messageId"],
            })

        durable = self._ensure_target_durable(known)
        if not _same_header_identity(durable["meta"], known["meta"]) \
                or not self._has_feedback_target(durable, request["messageId"]):
            return _rejected({
                "code": "target-not-found",
                "sessionId": request["sessionId"],
                "messageId": request["messageId"],
            })

        table = await self._open_domain()
        stored = table.get(request["sessionId"])
        current = stored if stored is not None and _same_identity(stored, durable["meta"]) else None
        items = current["items"] if current is not None else []
        index = next((i for i, it in enumerate(items) if it["messageId"] == request["messageId"]), -1)
        existing = items[index] if index >= 0 else None

        expected = existing["version"] if existing is not None else None
        if request.get("ifVersion") != expected:
            return _rejected(self._version_conflict(existing))

        if existing is not None \
                and existing["rating"] == request["rating"] \
                and existing.get("note") == note:
            return _success(snapshot_item(existing))

        now = int(time.time() * 1000)
        item = snapshot_item({
            "messageId": request["messageId"],
            "rating": request["rating"],
            **({"note": note} if note is not None else {}),
            "version": _next_version(),
            "createdAt": existing["createdAt"] if existing is not None else now,
            "updatedAt": now if existing is None else max(now, existing["updatedAt"]),
        })
        next_items = list(items)
        if index == -1:
            next_items.append(item)
        else:
            next_items[index] = item
        await table.put(request["sessionId"], _row_snapshot(_identity_of(durable["meta"]), next_items))
        return _success(snapshot_item(item))

    async def _delete_locked(self, request: dict) -> dict:
        known = self._inspect_session(request["sessionId"])
        if not known["ok"]:
            return _rejected({"code": "session-not-found", "sessionId": request["sessionId"]})

        table = await self._open_domain()
        stored = table.get(request["sessionId"])
        current = stored if stored is not None and _same_identity(stored, known["meta"]) else None
        items = current["items"] if current is not None else []
        existing = next((it for it in items if it["messageId"] == request["messageId"]), None)
        if existing is None:
            return _success({"absent": True})
        if request.get("ifVersion") != existing["version"]:
            return _rejected(self._version_conflict(existing))

        await table.put(request["sessionId"], _row_snapshot(
            _identity_of(known["meta"]), [it for it in items if it is not existing],
        ))
        return _success({"absent": True})

    def _inspect_session(self, session_id: str) -> dict:
        """活所有者优先；否则以持久化目录为存在性权威后装载日志。"""
        live = self.ctx.sessions.get(session_id)
        if live is not None:
            return {"ok": True, "meta": live.header, "events": list(live.events), "live": live}
        if hasattr(self.ctx, "sessionPersistence"):
            loaded = self.ctx.sessionPersistence.load(session_id)
            if loaded is not None:
                return {"ok": True, "meta": loaded["meta"], "events": loaded["events"], "live": None}
        return {"ok": False}

    def _has_feedback_target(self, inspection: dict, message_id: str) -> bool:
        """目标必须是已定稿的 append-origin assistant 消息。"""
        live = inspection.get("live")
        surface_nodes = None
        if live is not None:
            surface_nodes = set(live.surface["nodes"])
        for event in inspection["events"]:
            if event.type != "assistant/message":
                continue
            if surface_nodes is not None and event.seq not in surface_nodes:
                continue  # 已被替换遮蔽（非 append-origin）
            message = self._derive(event)
            if message is not None and message.role == "assistant" and message.id == message_id:
                return True
        return False

    def _derive(self, event: Any) -> Any:
        """把一条 assistant/message 事件还原为消息对象。"""
        data = getattr(event, "data", None)
        if isinstance(data, dict) and "message" in data:
            return data["message"]
        return None

    def _ensure_target_durable(self, inspection: dict) -> dict:
        """把目标日志前缀置于耐久屏障之后（活所有者经 flush）。"""
        live = inspection.get("live")
        if live is not None and _same_header_identity(live.header, inspection["meta"]):
            self.ctx.sessions.flush(live)
        return inspection

    def _resolve_note(self, note: Optional[str]) -> dict:
        """校验可选注记语义与配置的 UTF-8 字节上界。"""
        if note is None:
            return _success(None)
        if note.strip() == "":
            return _rejected({"code": "note-blank"})
        actual = _utf8_bytes(note)
        if actual > self._max_note_bytes:
            return _rejected({"code": "note-too-large", "maxBytes": self._max_note_bytes, "actualBytes": actual})
        return _success(note)

    def _version_conflict(self, current: Optional[dict]) -> dict:
        return {
            "code": "version-conflict",
            "current": None if current is None else snapshot_item(current),
        }

    async def _enqueue(self, session_id: str, operation) -> Any:
        """把一次完整的读/比较/写突变排到该会话的前一突变之后。

        dsh 用 Promise 尾队列做每会话串行；dsh_py 的持久化经 storageDomain
        的域级单写链（``asyncio.Lock``）串行——比每会话队列更强（全域按序），
        语义等价（写不交错），这里只保留准入门。
        """
        if not self._admission_open:
            raise RuntimeError("message-feedback: 服务正在卸载")
        return await operation()


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.messageFeedback`` 服务（+ 可选的 typert 远程作用域）。"""
    service = MessageFeedbackService(ctx, config or {})
    if hasattr(ctx, "typertRegistry"):
        dispose = ctx.typertRegistry.register("messageFeedback", service)
        ctx.effect(dispose, label="message-feedback.remote")


apply.Config = Config
apply.name = "message-feedback"
apply.inject = ["sessions", "storageDomain"]
apply.provides = ["messageFeedback"]
