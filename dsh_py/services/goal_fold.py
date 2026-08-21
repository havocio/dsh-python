"""
goal 域的纯函数部分（对齐 dsh ``packages/goal/goal/src/fold.ts`` 与 ``runtime.ts``）。

只含纯重放折叠与严格解码器：不触碰 cordis/agent/session 等宿主。服务层
（:mod:`dsh_py.services.goal`）持有本模块的状态并负责写入。

**与 dsh 的差异（已注明）**：
- 类型用普通 dict（dsh 用 TS interface + Branded）；``GoalId`` 为 str 子类品牌。
- 校验错误抛 ``GoalError``（code 对齐 dsh 的 ``GoalErrorCode``）而非 ``Error``。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Optional

from dsh_py.core.schema import SchemaError  # noqa: F401  保持 schema 错误命名空间一致（未直接使用）


class GoalError(Exception):
    """goal 域边界拒绝（code 对齐 dsh 的 ``GoalErrorCode``）。"""

    def __init__(self, message: str, code: str = "GOAL_INVALID_CHANGE") -> None:
        super().__init__(message)
        self.code = code
        self.message = message

GOAL_CHANGE_VERSION = 1

SNAPSHOT_OPERATIONS = frozenset({"create", "edit", "pause", "resume", "complete", "block"})
GOAL_OPERATIONS = SNAPSHOT_OPERATIONS | {"clear"}
GOAL_PHASES = frozenset({"active", "paused", "blocked", "complete"})

_KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

# 每种 phase 的快照精确字段集合（仅 blocked 相位携带 blockedReason）
_SNAPSHOT_KEYS = {
    "active": ("id", "maxGoalRounds", "objective", "phase", "revision"),
    "paused": ("id", "maxGoalRounds", "objective", "phase", "revision"),
    "complete": ("id", "maxGoalRounds", "objective", "phase", "revision"),
    "blocked": ("blockedReason", "id", "maxGoalRounds", "objective", "phase", "revision"),
}
_SNAPSHOT_CHANGE_KEYS = ("createdAt", "goal", "kind", "operation", "roundsStarted", "updatedAt", "version")
_CLEAR_CHANGE_KEYS = ("cleared", "clearedAt", "kind", "operation", "version")
_REF_KEYS = ("id", "revision")
_BLOCK_REASON_KEYS = ("code", "message")


class GoalId(str):
    """品牌化目标 id。"""


def GoalId_brand(value: str) -> GoalId:
    return GoalId(value)


class GoalError(Exception):
    """goal 域边界拒绝（code 对齐 dsh 的 ``GoalErrorCode``）。"""

    def __init__(self, message: str, code: str = "GOAL_INVALID_CHANGE") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# 校验辅助（严格：字段精确集合 / 整数 / 规范化）
# --------------------------------------------------------------------------- #
def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise GoalError(f"goal change {field} must be a positive safe integer", "GOAL_INVALID_CHANGE")
    return value


def _non_negative_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GoalError(f"goal change {field} must be a non-negative safe integer", "GOAL_INVALID_CHANGE")
    return value


def _sorted_keys(value: dict) -> tuple:
    return tuple(sorted(value.keys()))


def _exact_keys(value: dict, expected: tuple, what: str) -> None:
    if _sorted_keys(value) != tuple(sorted(expected)):
        raise GoalError(f"goal change {what} must have exactly {','.join(sorted(expected))} fields", "GOAL_INVALID_CHANGE")


def _decode_block_reason(value: Any) -> dict:
    if not _is_record(value):
        raise GoalError("goal change goal.blockedReason must be a record", "GOAL_INVALID_CHANGE")
    _exact_keys(value, _BLOCK_REASON_KEYS, "goal.blockedReason")
    code = value["code"]
    message = value["message"]
    if not isinstance(code, str) or not _KEBAB_RE.match(code):
        raise GoalError("goal change goal.blockedReason.code must be lower-kebab-case", "GOAL_INVALID_CHANGE")
    if not isinstance(message, str) or not message.strip() or message != message.strip():
        raise GoalError("goal change goal.blockedReason.message must be non-empty and normalized", "GOAL_INVALID_CHANGE")
    return {"code": code, "message": message}


def _decode_snapshot(value: Any) -> dict:
    if not _is_record(value):
        raise GoalError("goal change goal must be a record", "GOAL_INVALID_CHANGE")
    gid = value.get("id")
    if not isinstance(gid, str) or not gid:
        raise GoalError("goal change goal.id must be a non-empty string", "GOAL_INVALID_CHANGE")
    objective = value.get("objective")
    if not isinstance(objective, str) or not objective.strip() or objective != objective.strip():
        raise GoalError("goal change goal.objective must be non-empty and normalized", "GOAL_INVALID_CHANGE")
    phase = value.get("phase")
    if phase not in GOAL_PHASES:
        raise GoalError("goal change goal.phase is invalid", "GOAL_INVALID_CHANGE")
    _exact_keys(value, _SNAPSHOT_KEYS[phase], f"goal for phase {phase}")
    snapshot = {
        "id": GoalId(gid),
        "revision": _positive_integer(value["revision"], "goal.revision"),
        "objective": objective,
        "phase": phase,
        "maxGoalRounds": _positive_integer(value["maxGoalRounds"], "goal.maxGoalRounds"),
    }
    if phase == "blocked":
        snapshot["blockedReason"] = _decode_block_reason(value["blockedReason"])
    return snapshot


def _decode_ref(value: Any, what: str) -> dict:
    if not _is_record(value):
        raise GoalError(f"goal change {what} must be a record", "GOAL_INVALID_CHANGE")
    _exact_keys(value, _REF_KEYS, what)
    gid = value["id"]
    if not isinstance(gid, str) or not gid:
        raise GoalError(f"goal change {what} id must be a non-empty string", "GOAL_INVALID_CHANGE")
    return {"id": GoalId(gid), "revision": _positive_integer(value["revision"], f"{what}.revision")}


def decode_goal_change(value: Any) -> Optional[dict]:
    """解码一条声明自己是 goal 变更的值。

    :returns: 校验后的 change dict；与 goal 无关的值返回 ``None``；畸形变更
      fail-loud 抛 ``GoalError``。
    """
    if not _is_record(value) or value.get("kind") != "goal/change":
        return None
    if value.get("version") != GOAL_CHANGE_VERSION:
        raise GoalError(f"unsupported goal change version {value.get('version')!r}", "GOAL_INVALID_CHANGE")
    operation = value.get("operation")
    if operation == "clear":
        _exact_keys(value, _CLEAR_CHANGE_KEYS, "clear change")
        return {
            "kind": "goal/change",
            "version": GOAL_CHANGE_VERSION,
            "operation": "clear",
            "cleared": _decode_ref(value["cleared"], "cleared"),
            "clearedAt": _non_negative_integer(value["clearedAt"], "clearedAt"),
        }
    if operation not in SNAPSHOT_OPERATIONS:
        raise GoalError("goal change operation is invalid", "GOAL_INVALID_CHANGE")
    _exact_keys(value, _SNAPSHOT_CHANGE_KEYS, "snapshot change")
    created_at = _non_negative_integer(value["createdAt"], "createdAt")
    updated_at = _non_negative_integer(value["updatedAt"], "updatedAt")
    if updated_at < created_at:
        raise GoalError("goal change updatedAt cannot precede createdAt", "GOAL_INVALID_CHANGE")
    return {
        "kind": "goal/change",
        "version": GOAL_CHANGE_VERSION,
        "operation": operation,
        "goal": _decode_snapshot(value["goal"]),
        "roundsStarted": _non_negative_integer(value["roundsStarted"], "roundsStarted"),
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def goal_change_ref(change: dict) -> dict:
    """变更携带的修订身份（快照或墓碑）。"""
    if change["operation"] == "clear":
        return {"id": change["cleared"]["id"], "revision": change["cleared"]["revision"]}
    return {"id": change["goal"]["id"], "revision": change["goal"]["revision"]}


# --------------------------------------------------------------------------- #
# 折叠（纯重放）
# --------------------------------------------------------------------------- #
def empty_goal_fold_state() -> dict:
    """空重放累加器。"""
    return {
        "goal": None,
        "roundsStarted": 0,
        "createdAt": None,
        "updatedAt": None,
        "lastRef": None,
        "seenGoalIds": set(),
    }


def _require_same_definition(current: dict, next_snapshot: dict, operation: str) -> None:
    if next_snapshot["objective"] != current["objective"] or next_snapshot["maxGoalRounds"] != current["maxGoalRounds"]:
        raise GoalError(f"goal {operation} cannot change objective or maxGoalRounds", "GOAL_INVALID_CHANGE")


def _require_next_revision(current: dict, ref: dict, operation: str) -> None:
    if ref["id"] != current["id"] or ref["revision"] != current["revision"] + 1:
        raise GoalError(f"goal {operation} must advance the current goal by one revision", "GOAL_INVALID_CHANGE")


def _validate_snapshot_transition(state: dict, change: dict, current: dict) -> None:
    """校验一次非 create 快照操作相对前一投影的合法性。"""
    next_snapshot = change["goal"]
    _require_next_revision(current, next_snapshot, change["operation"])
    if state["updatedAt"] is None:
        raise GoalError("current goal fold lacks updatedAt", "GOAL_INVALID_CHANGE")
    if (change["createdAt"] != state["createdAt"]
            or change["updatedAt"] < state["updatedAt"]
            or change["roundsStarted"] != state["roundsStarted"]):
        raise GoalError(f"goal {change['operation']} does not preserve the current counters and timestamps", "GOAL_INVALID_CHANGE")
    operation = change["operation"]
    if operation == "edit":
        if next_snapshot["phase"] != current["phase"] or next_snapshot.get("blockedReason") != current.get("blockedReason"):
            raise GoalError("goal edit cannot change phase or blocked reason", "GOAL_INVALID_CHANGE")
    elif operation == "pause":
        _require_same_definition(current, next_snapshot, operation)
        if current["phase"] != "active" or next_snapshot["phase"] != "paused":
            raise GoalError("goal pause has an invalid phase transition", "GOAL_INVALID_CHANGE")
    elif operation == "resume":
        _require_same_definition(current, next_snapshot, operation)
        resumable = {"active", "paused", "blocked"}
        if current["phase"] not in resumable or next_snapshot["phase"] != "active" or state["roundsStarted"] >= next_snapshot["maxGoalRounds"]:
            raise GoalError("goal resume has an invalid phase transition or exhausted round budget", "GOAL_INVALID_CHANGE")
    elif operation == "complete":
        _require_same_definition(current, next_snapshot, operation)
        if current["phase"] == "complete" or next_snapshot["phase"] != "complete":
            raise GoalError("goal complete has an invalid phase transition", "GOAL_INVALID_CHANGE")
    elif operation == "block":
        _require_same_definition(current, next_snapshot, operation)
        if current["phase"] != "active" or next_snapshot["phase"] != "blocked":
            raise GoalError("goal block has an invalid phase transition", "GOAL_INVALID_CHANGE")
    elif operation == "create":
        raise GoalError("goal create cannot be validated as a current-goal transition", "GOAL_INVALID_CHANGE")
    else:  # pragma: no cover
        raise GoalError(f"unknown goal snapshot operation {operation!r}", "GOAL_INVALID_CHANGE")


def _goal_source(source: Any) -> Optional[dict]:
    """把模型消息来源收窄为合法 goal 来源。"""
    if source is None or source.kind != "goal":
        return None
    goal_id = getattr(source, "goalId", None)
    revision = getattr(source, "revision", None)
    round_ = getattr(source, "round", None)
    if not isinstance(goal_id, str) or not goal_id or not isinstance(revision, int) or revision < 1 \
            or not isinstance(round_, int) or round_ < 1:
        raise GoalError("goal message source is invalid", "GOAL_INVALID_CHANGE")
    return {"goalId": goal_id, "revision": revision, "round": round_}


def apply_goal_change(state: dict, change: dict) -> None:
    """把一条解码变更应用到可变累加器（严格校验）。"""
    ref = goal_change_ref(change)
    if change["operation"] == "clear":
        current = state["goal"]
        if current is None:
            raise GoalError("goal clear requires a current goal", "GOAL_INVALID_CHANGE")
        _require_next_revision(current, change["cleared"], "clear")
        if state["updatedAt"] is None:
            raise GoalError("current goal fold lacks updatedAt", "GOAL_INVALID_CHANGE")
        if change["clearedAt"] < state["updatedAt"]:
            raise GoalError("goal clear timestamp cannot precede the current goal update", "GOAL_INVALID_CHANGE")
        state["goal"] = None
        state["roundsStarted"] = 0
        state["createdAt"] = None
        state["updatedAt"] = None
        state["lastRef"] = ref
        return
    if change["operation"] == "create":
        if change["goal"]["revision"] != 1 or change["goal"]["phase"] != "active" or change["roundsStarted"] != 0 \
                or (state["goal"] is not None and state["goal"]["phase"] != "complete") \
                or change["goal"]["id"] in state["seenGoalIds"]:
            raise GoalError("goal create requires a fresh active revision-one goal with zero rounds", "GOAL_INVALID_CHANGE")
        state["seenGoalIds"].add(change["goal"]["id"])
    else:
        current = state["goal"]
        if current is None:
            raise GoalError(f"goal {change['operation']} requires a current goal", "GOAL_INVALID_CHANGE")
        _validate_snapshot_transition(state, change, current)
    state["goal"] = change["goal"]
    state["roundsStarted"] = change["roundsStarted"]
    state["createdAt"] = change["createdAt"]
    state["updatedAt"] = change["updatedAt"]
    state["lastRef"] = ref


def apply_goal_event(state: dict, event: Any) -> None:
    """把一个会话事件应用到严格重放折叠。"""
    if event.type == "goal/change":
        change = decode_goal_change(event.data)
        if change is None:
            raise GoalError(f"goal change at session event {event.seq} has an invalid kind", "GOAL_INVALID_CHANGE")
        apply_goal_change(state, change)
        return
    if event.type == "user/message":
        source = _goal_source(getattr(event.data, "source", None))
        if source is None:
            return
        current = state["goal"]
        if (current is None or current["phase"] != "active" or source["goalId"] != current["id"]
                or source["revision"] != current["revision"]
                or source["round"] != state["roundsStarted"] + 1
                or source["round"] > current["maxGoalRounds"]):
            raise GoalError(f"goal round at session event {event.seq} is not the next admitted round of the active goal", "GOAL_INVALID_CHANGE")
        state["roundsStarted"] = source["round"]


def fold_goal(events: list) -> dict:
    """从连续会话事件日志折叠当前 goal 状态（激活刻意缺席）。"""
    state = empty_goal_fold_state()
    for event in events:
        apply_goal_event(state, event)
    folded: dict = {"roundsStarted": state["roundsStarted"]}
    if state["goal"] is not None:
        folded["goal"] = dict(state["goal"])
    if state["createdAt"] is not None:
        folded["createdAt"] = state["createdAt"]
    if state["updatedAt"] is not None:
        folded["updatedAt"] = state["updatedAt"]
    if state["lastRef"] is not None:
        folded["lastRef"] = dict(state["lastRef"])
    return folded


# --------------------------------------------------------------------------- #
# 投影单元（last-wins 全值折叠；投影级宽松语义）
# --------------------------------------------------------------------------- #
def apply_goal_projection(state, event: Any):
    """``goal`` 投影单元：goal/change 全值 last-wins，无关/畸形事件返回同一引用。"""
    if event.type != "goal/change":
        return state
    try:
        change = decode_goal_change(event.data)
    except GoalError:
        return state
    if change is None:
        return state
    if change["operation"] == "clear":
        return None
    return {
        "goal": change["goal"],
        "roundsStarted": change["roundsStarted"],
        "createdAt": change["createdAt"],
        "updatedAt": change["updatedAt"],
    }


def new_goal_id() -> GoalId:
    """新建目标 id：``goal-<uuid>``。"""
    return GoalId(f"goal-{uuid.uuid4()}")
