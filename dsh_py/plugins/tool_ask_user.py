"""模型侧消费 ``ctx.userQuestions`` 能力 seam（对齐 dsh-tool-ask-user）。

工具会暂停，直到 UI provider 返回人类回答，再把回答作为普通工具结果喂回
agent 循环。
"""

from __future__ import annotations

from typing import Any

from dsh_py.core.context import AppContext

#: 工具描述：需要确认、选择或缺失信息时，用简洁问题问用户。
DESCRIPTION = (
    "Ask the user a concise question when you need confirmation, a choice, or missing "
    "information before proceeding. Send one or more questions, each with a stable id "
    "that will be echoed in the answer."
)

#: ask_user_question 的参数 schema（对齐 dsh tool-ask-user，完整 JSON Schema）。
ASK_USER_QUESTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "description": "Questions to ask the user before continuing.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Stable id for this question; echoed in the answer."},
                    "question": {"type": "string", "description": "The specific question to ask the user."},
                    "header": {
                        "type": "string",
                        "description": 'Optional short heading for the question, such as "Confirm" or "Choose Mode".',
                    },
                    "options": {
                        "type": "array",
                        "description": 'Optional choices to show the user. If you recommend one, put it first and append "(Recommended)" to that label.',
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Short user-facing option label."},
                                "description": {"type": "string", "description": "One sentence explaining the tradeoff or impact."},
                            },
                            "required": ["label"],
                        },
                    },
                    "multi_select": {
                        "type": "boolean",
                        "description": "Whether the user may select more than one option. Defaults to false.",
                    },
                },
                "required": ["id", "question"],
            },
        },
    },
    "required": ["questions"],
}

apply_meta: dict = {
    "name": "tool-ask-user",
    "inject": ["tools", "userQuestions"],
}


def apply(ctx: AppContext, config: Any = None) -> None:
    """注册 ``ask_user_question`` 工具。要求 ``tools`` / ``userQuestions`` 已挂载。"""
    if not ctx.has_service("tools"):
        raise RuntimeError("tool-ask-user: the tools service is not mounted (add dsh_py.services.tools:apply)")
    if not ctx.has_service("userQuestions"):
        raise RuntimeError(
            "tool-ask-user: the userQuestions service is not mounted (add dsh_py.services.user_questions:apply)"
        )

    async def handler(args: dict, exec: dict) -> tuple[str, bool]:  # noqa: ANN001
        from dsh_py.services.user_questions import (
            AskUserQuestionAnswerItem,
            AskUserQuestionItem,
            AskUserQuestionOption,
            AskUserQuestionRequest,
        )

        questions: list[AskUserQuestionItem] = []
        for raw in args.get("questions", []):
            options = None
            raw_options = raw.get("options")
            if raw_options:
                options = [
                    AskUserQuestionOption(
                        label=option["label"],
                        description=option.get("description"),
                    )
                    for option in raw_options
                ]
            questions.append(AskUserQuestionItem(
                id=raw.get("id", ""),
                question=raw.get("question", ""),
                header=raw.get("header"),
                options=options,
                multiSelect=bool(raw.get("multi_select", False)),
            ))
        request = AskUserQuestionRequest(
            questions=questions,
            agent=exec.get("agent"),
            signal=exec.get("signal"),
        )
        answer = await ctx.userQuestions.ask(request)
        payload = {
            "answers": [
                {
                    "id": item.id,
                    "selected": list(item.selected),
                    **({"custom": item.custom} if item.custom is not None else {}),
                }
                for item in answer.answers
            ]
        }
        import json

        return json.dumps(payload, ensure_ascii=False), False

    ctx.tools.register(
        "ask_user_question",
        DESCRIPTION,
        ASK_USER_QUESTION_SCHEMA,
        handler,
    )


__all__ = ["DESCRIPTION", "ASK_USER_QUESTION_SCHEMA", "apply"]
