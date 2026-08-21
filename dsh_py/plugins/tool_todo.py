"""面向模型的 todo 任务列表能力（todo/tool-todo，第 3 层）。

单一**产品**包：一个 agent 会话拥有该列表，不存在可替换的提供方约定。

- 注册 ``todo_write`` 工具到 ``ctx.tools``：模型每次调用携带**完整**列表，整体
  替换上一份（无部分更新、无单条编辑）——最后写入获胜（last-write-wins）；
- 每次调用向所属 agent 会话追加一条 ``todo/write`` 事件（UIs 从会话事件渲染）；
- 非 agent 调用者（无所属会话）无处写入，直接拒绝（而非静默 no-op）；
- 当 ``sessionProjections`` 缝被装配时，注册 ``todos`` 投影单元（仅整机装配时
  激活，headless 无该缝的装配不受影响）。

配置 ``allowParallelInProgress`` 决定「同一时刻可有几个 in_progress」：并发型
agent（子智能体、后台命令、workflow 扇出）允许多个；否则强制单活跃纪律。

值约束（ParameterSchema 表达不了的）由 :func:`to_todo_list` 收紧：content 修剪后
非空且全局唯一；除非部署允许并行，否则 in_progress 至多一个。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.projection import ProjectionDefinition


# 合法 todo 状态（运行时集合，用于输入收窄）
STATUSES = ("pending", "in_progress", "completed")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
Config = z.object({
    "allowParallelInProgress": z.boolean().default(False),
})


DESCRIPTION_HEAD = (
    "Record and update a structured task list for the current work. Send the ENTIRE "
    "list every call — it REPLACES the previous list (there are no partial updates, "
    "no per-item edits). Use it to plan multi-step work and show progress: add one "
    "todo per concrete step before you start. "
)

DESCRIPTION_PARALLEL = (
    "Mark every todo being actively worked "
    "on `in_progress` — several at once when work genuinely runs in parallel (e.g. "
    "concurrent subagents or background commands), one for sequential work; while "
    "work remains, at least one task should be `in_progress`. "
)

DESCRIPTION_SINGLE = (
    "Keep AT MOST ONE todo `in_progress` at a "
    "time; while work remains, exactly one active task should be `in_progress`. "
)

DESCRIPTION_TAIL = (
    "Mark a todo "
    "`completed` the moment it is done (do not batch completions), and allow no "
    "`in_progress` item only once all work is complete. Skip the list for trivial "
    "single-step tasks. Statuses: `pending` (not started), `in_progress` (being "
    "worked on now), `completed` (finished)."
)


def describe(allow_parallel: bool) -> str:
    """合成一次激活所用的工具描述（仅「活跃状态」条款随并行策略变化）。"""
    return DESCRIPTION_HEAD + (DESCRIPTION_PARALLEL if allow_parallel else DESCRIPTION_SINGLE) + DESCRIPTION_TAIL


def to_todo_list(raw: list[dict], allow_parallel: bool) -> list[dict]:
    """把模型提供的列表收敛成规范 todo 列表（按值收紧，schema 表达不了的约束在此兜底）。

    - content 修剪后非空；全局唯一（重复 content 报错）；
    - status 必须是合法三态之一（dsh_py 的 schema 转换器不强制 enum，在此兜底）；
    - 除非部署允许并行，否则 ``in_progress`` 至多一个。
    """
    todos: list[dict] = []
    seen: set[str] = set()
    active = 0
    for item in raw:
        content = (item.get("content") or "").strip()
        if content == "":
            raise ValueError("invalid todo: `content` 必须是非空字符串")
        if content in seen:
            raise ValueError(f"invalid todos: 重复的 content {content!r}")
        seen.add(content)
        status = item.get("status")
        if status not in STATUSES:
            raise ValueError(f"invalid todos: 非法 status {status!r}（应为 {STATUSES}）")
        if status == "in_progress":
            active += 1
        todos.append({"content": content, "status": status})
    if not allow_parallel and active > 1:
        raise ValueError(f"invalid todos: 至多一个任务可为 in_progress（实际 {active}）")
    return todos


def _register_projection(ctx: AppContext) -> None:
    """仅当 sessionProjections 缝被装配时注册 ``todos`` 单元（全值事件规则）。

    schema 取 ``None``（透传）：dsh_py 的 schema 模块无 ``null`` 字面量构造器，
    而 ``apply`` 产出的数据已是规范列表或 None，无需再校验；投影模块对
    ``schema=None`` 明确支持（测试/简单单位可用）。
    """
    if not hasattr(ctx, "sessionProjections"):
        return

    def apply(state: Any, event: Any) -> Any:
        if event.type == "todo/write":
            return event.data.get("todos")
        if event.type == "turn/start":
            return None
        return state  # 单元不关心的事件：返回同一引用（零下游变更通知）

    ctx.sessionProjections.register(ProjectionDefinition(
        key="todos",
        schema=None,
        init=lambda: None,
        apply=apply,
        view=lambda state: state,
        state_version=2,
    ))


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #
def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """在 ``ctx.tools`` 注册 ``todo_write`` 工具；缝齐备时注册 ``todos`` 投影单元。"""
    cfg = config or {}
    allow_parallel = bool(cfg.get("allowParallelInProgress", False))

    _register_projection(ctx)

    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "required": True,
                "description": "The COMPLETE task list, replacing any previous list.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "content": {
                            "type": "string", "required": True,
                            "description": "What the task is — a short imperative line.",
                        },
                        "status": {
                            "type": "string", "required": True, "enum": list(STATUSES),
                            "description": "pending (not started) | in_progress (now) | completed (done).",
                        },
                    },
                },
            },
        },
    }

    async def todo_write(arguments: dict, exec: dict) -> tuple:
        todos = to_todo_list(arguments.get("todos", []), allow_parallel)
        agent = exec.get("agent")
        if agent is None:
            # 列表是「每 agent 会话」状态；无所属会话的调用者无处写入，拒绝而非静默 no-op
            return "todo_write requires an owning agent session", True
        agent.session.append("todo/write", {"todos": todos})

        def count(status: str) -> int:
            return sum(1 for t in todos if t["status"] == status)

        pending = count("pending")
        in_progress = count("in_progress")
        completed = count("completed")
        text = (
            f"Updated todo list: {pending} pending, {in_progress} in progress, "
            f"{completed} completed."
        )
        return text, False

    ctx.tools.register(
        "todo_write",
        describe(allow_parallel),
        parameters,
        todo_write,
    )


apply.Config = Config
apply.name = "tool-todo"
apply.inject = ["tools"]
