"""调度域（schedule_domain）纯函数验证（第 3 层治理类）。

运行：python dsh_py/tests/test_schedule_domain.py

覆盖：解码 / 折叠重放 / 固定间隔锚点对齐 / 时区 / 渲染抗性 / 错误分类。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import datetime, timezone

from dsh_py.services.schedule_domain import (
    SCHEDULE_CHANGE_VERSION,
    MIN_EVERY_INTERVAL_SECONDS,
    AfterScheduleRecord,
    AtScheduleRecord,
    EveryScheduleRecord,
    FoldedSchedules,
    ScheduleInputError,
    ScheduleLogError,
    allocate_schedule_id,
    create_after_schedule_record,
    create_at_schedule_record,
    create_every_schedule_record,
    decode_schedule_change,
    fold_schedule_events,
    render_every_reminder_batch_framing,
    render_reminder_framing,
    resolve_every_occurrence,
    schedule_view,
)


# 轻量事件替身（fold 只读 .type / .data）
class Ev:
    def __init__(self, type, data):
        self.type = type
        self.data = data


def instant(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def change_create(record: dict) -> dict:
    return {"version": SCHEDULE_CHANGE_VERSION, "operation": "create", "schedule": record}


def change_delete(sid: str) -> dict:
    return {"version": SCHEDULE_CHANGE_VERSION, "operation": "delete", "id": sid}


def change_dispatch(sid: str, accepted_at: str = None) -> dict:
    base = {"version": SCHEDULE_CHANGE_VERSION, "operation": "dispatch", "id": sid}
    if accepted_at is not None:
        base["acceptedAt"] = accepted_at
    return base


def test_create_after_future_and_trim():
    now = 1_700_000_000_000
    rec = create_after_schedule_record("s1", "  喝水  ", 600, now)
    assert isinstance(rec, AfterScheduleRecord)
    assert rec.prompt == "喝水"                       # trim 生效
    assert rec.after_seconds == 600
    assert datetime.fromisoformat(rec.scheduled_at.replace("Z", "+00:00")).timestamp() * 1000 == now + 600_000
    # after 永远在未来，无法触发 not_future；用 at 的过去绝对时刻验证
    bad = None
    try:
        create_at_schedule_record("s1", "x", "2000-01-01T00:00:00Z", now)
    except ScheduleInputError as e:
        bad = e
    assert bad is not None and bad.code == "not_future"
    # 非正延迟
    try:
        create_after_schedule_record("s1", "x", 0, now)
    except ScheduleInputError:
        pass
    else:
        raise AssertionError("after_seconds<=0 应报错")
    # 空 prompt
    try:
        create_after_schedule_record("s1", "   ", 600, now)
    except ScheduleInputError as e:
        assert e.code == "invalid_prompt"
    else:
        raise AssertionError("空 prompt 应报错")


def test_create_at_offset_and_local_timezone():
    now = 1_700_000_000_000
    # 显式偏移（UTC 等价）
    rec = create_at_schedule_record("s2", "开会", "2099-01-01T00:00:00Z", now)
    assert isinstance(rec, AtScheduleRecord)
    assert rec.scheduled_at == "2099-01-01T00:00:00.000Z"
    # 本地 + IANA 时区（Asia/Shanghai = UTC+8）
    rec2 = create_at_schedule_record("s3", "晨会", {
        "date": "2099-01-01", "time": "09:00:00", "time_zone": "Asia/Shanghai"}, now)
    # 09:00 +08:00 → 01:00 UTC
    assert rec2.scheduled_at == "2099-01-01T01:00:00.000Z"
    # 非法时区
    try:
        create_at_schedule_record("s4", "x", {"date": "2099-01-01", "time": "09:00:00",
                                              "time_zone": "Mars/Phobos"}, now)
    except ScheduleInputError as e:
        assert e.code == "invalid_time_zone"
    else:
        raise AssertionError("非法时区应报错")


def test_create_every_min_interval():
    now = 1_700_000_000_000
    rec = create_every_schedule_record("e1", "心跳", MIN_EVERY_INTERVAL_SECONDS, now)
    assert isinstance(rec, EveryScheduleRecord)
    assert rec.every_seconds == MIN_EVERY_INTERVAL_SECONDS
    # 间隔过短
    try:
        create_every_schedule_record("e2", "x", MIN_EVERY_INTERVAL_SECONDS - 1, now)
    except ScheduleInputError as e:
        assert e.code == "frequency_too_high"
    else:
        raise AssertionError("过短间隔应报错")


def test_decode_schedule_change_validation():
    rec = {"id": "s1", "kind": "after", "prompt": "x", "afterSeconds": 600,
           "scheduledAt": "2099-01-01T00:00:00.000Z"}
    decoded = decode_schedule_change(change_create(rec))
    assert decoded["operation"] == "create"
    assert decoded["schedule"].id == "s1"
    # 版本错误
    try:
        decode_schedule_change({"version": 2, "operation": "create", "schedule": rec})
    except ScheduleLogError:
        pass
    else:
        raise AssertionError("version!=1 应报错")
    # 多余键
    try:
        decode_schedule_change({"version": 1, "operation": "create", "schedule": rec, "extra": 1})
    except ScheduleLogError:
        pass
    else:
        raise AssertionError("create 多余键应报错")
    # delete 形状
    assert decode_schedule_change(change_delete("s1"))["id"] == "s1"


def test_fold_create_delete_dispatch_one_shot():
    events = [
        Ev("schedule/change", change_create({
            "id": "s1", "kind": "after", "prompt": "p", "afterSeconds": 600,
            "scheduledAt": "2099-01-01T00:00:00.000Z"})),
        Ev("turn/start", {"turn": 1}),
        Ev("schedule/change", change_delete("s1")),
    ]
    folded = fold_schedule_events(events)
    assert isinstance(folded, FoldedSchedules)
    assert len(folded.active) == 0
    assert folded.seen_ids == ("s1",)


def test_fold_every_dispatch_advances():
    base = 1_700_000_000_000
    every = 3600  # 秒
    sched_at = instant(base)
    events = [
        Ev("schedule/change", change_create({
            "id": "e1", "kind": "every", "prompt": "p", "everySeconds": every,
            "scheduledAt": sched_at})),
        Ev("schedule/change", change_dispatch("e1", accepted_at=instant(base + every * 1000))),
    ]
    folded = fold_schedule_events(events)
    assert len(folded.active) == 1
    assert isinstance(folded.active[0], EveryScheduleRecord)
    # dispatch 后锚点推进一个间隔（accepted 在第 1 个间隔末 → 进入第 2 个间隔）
    assert folded.active[0].scheduled_at == instant(base + every * 1000 * 2)


def test_fold_seed_length_excludes_prefix():
    seed = Ev("schedule/change", change_create({
        "id": "old", "kind": "after", "prompt": "p", "afterSeconds": 600,
        "scheduledAt": "2099-01-01T00:00:00.000Z"}))
    own = Ev("schedule/change", change_create({
        "id": "s1", "kind": "after", "prompt": "p", "afterSeconds": 600,
        "scheduledAt": "2099-02-01T00:00:00.000Z"}))
    folded = fold_schedule_events([seed, own], seed_length=1)
    ids = [r.id for r in folded.active]
    assert ids == ["s1"]


def test_fold_id_reuse_is_corrupt():
    rec = {"id": "s1", "kind": "after", "prompt": "p", "afterSeconds": 600,
           "scheduledAt": "2099-01-01T00:00:00.000Z"}
    events = [Ev("schedule/change", change_create(rec)),
              Ev("schedule/change", change_create(rec))]
    try:
        fold_schedule_events(events)
    except ScheduleLogError:
        pass
    else:
        raise AssertionError("复用 id 应判为损坏日志")


def test_fold_dispatch_missing_id_is_corrupt():
    events = [Ev("schedule/change", change_dispatch("ghost"))]
    try:
        fold_schedule_events(events)
    except ScheduleLogError:
        pass
    else:
        raise AssertionError("dispatch 非活跃 id 应判为损坏日志")


def test_resolve_every_occurrence_skips_missed():
    base = 1_700_000_000_000
    every = 600  # 秒
    sched_at = instant(base)
    rec = EveryScheduleRecord(id="e1", kind="every", prompt="p",
                              every_seconds=every, scheduled_at=sched_at)
    # 接受时间在第 3 个间隔：应锚点到第 2 个发生（不枚举中间）
    accept = base + every * 1000 * 2 + 1000
    occ = resolve_every_occurrence(rec, accept)
    assert occ.occurrence_at == instant(base + every * 1000 * 2)
    assert occ.next_scheduled_at == instant(base + every * 1000 * 3)
    # 早于锚点 → 损坏
    try:
        resolve_every_occurrence(rec, base - 1000)
    except ScheduleLogError:
        pass
    else:
        raise AssertionError("早于锚点应报错")


def test_allocate_schedule_id_avoids_seen():
    folded = FoldedSchedules(active=(), seen_ids=("schedule-1", "schedule-2"))
    assert allocate_schedule_id(folded) == "schedule-3"


def test_schedule_view_state():
    now = 1_700_000_000_000
    future = create_after_schedule_record("s1", "p", 3600, now)
    # overdue 状态：用直接构造的过去记录 + 未来 now 验证（create_at 自身拒绝过去时刻）
    past = AtScheduleRecord(id="s2", kind="at", prompt="p", scheduled_at="2000-01-01T00:00:00.000Z")
    assert schedule_view(future, now)["state"] == "scheduled"
    assert schedule_view(past, now + 1)["state"] == "overdue"
    assert schedule_view(future, now)["deliveryMode"] == "session-local"


def test_render_framing_is_injection_resistant():
    rec = AtScheduleRecord(id="s1", kind="at", prompt="做点事", scheduled_at="2099-01-01T00:00:00.000Z")
    text = render_reminder_framing(rec)
    assert "[SCHEDULE REMINDER]" in text
    # 提醒内容以 JSON 形式携带，明确标注为不可信内容而非指令
    assert "reminder_prompt_json" in text and "untrusted" in text.lower()
    batch = render_every_reminder_batch_framing([
        {"record": rec, "occurrence_at": "2099-01-01T00:00:00.000Z"}])
    assert "[SCHEDULE REMINDER BATCH]" in batch
    assert "reminders_json" in batch


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nOK: schedule_domain 测试通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
