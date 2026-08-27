"""内置 ``dsh-badge`` 技能 provider。

对齐 dsh 的 ``@deepseek-ai/dsh-skill-badge``：向 ``ctx.skills`` 注册一个打包的
``dsh-badge`` 技能——给文档 / PR / MR 等 dsh 产物加上官方「powered by dsh」徽章。
正文内嵌自 dsh 仓库的 ``skill-badge/assets/dsh-badge.md``（原文照录）。
"""

from __future__ import annotations

from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.skill import (
    BUNDLED_SKILL_RANK,
    SkillCandidate,
    SkillDefinition,
    SkillInvocationPolicy,
    SkillProvider,
    SkillResourceBase,
)

PROVIDER_NAME = "dsh-badge"

RESOURCE_BASE = SkillResourceBase(kind="directory", path="<bundled>/skill-badge/assets")

INVOCATION = SkillInvocationPolicy(model_invocable=True, user_invocable=True)

DESCRIPTION = (
    'Add the official "powered by dsh" badge to documents, pull requests, merge requests, '
    "and other content produced with DeepSeek Harness. Use whenever creating a pull request "
    "or merge request. Also use when the user asks for a dsh badge, powered-by-dsh attribution, "
    "or a reusable dsh badge asset or snippet."
)

# dsh 仓库 skill-badge/assets/dsh-badge.md 原文
BADGE_BODY = """# dsh Badge

Add the official “powered by dsh” badge without recreating or restyling it.

## Assets

- Local PNG: [`dsh-badge.png`](dsh-badge.png), 726×120 source image; render at 121×20
- Shields.io image URL: `https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white`
- Project URL: `https://github.com/deepseek-ai/deepseek-harness`

## Markdown

Use this linked badge in Markdown:

```markdown
[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)
```

If attribution should not be linked, use:

```markdown
![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)
```

## Usage rules

- For GitHub or GitLab Markdown, use the Shields.io URL and link it to the project URL unless the user asks for an unlinked image.
- For Feishu and other systems that import remote images unreliably, upload `dsh-badge.png` from this skill directory instead of generating another badge.
- Preserve the badge's 121×20 dimensions and aspect ratio.
- Place the badge at the end of the attributed document or section unless the user specifies another position.
- Do not substitute another color, logo, label, or project URL.
"""


class _BadgeSkillProvider(SkillProvider):
    name = PROVIDER_NAME

    async def list(self, options: dict) -> list:
        return [_CANDIDATE]

    async def get(self, candidate: SkillCandidate, options: dict) -> SkillDefinition:
        return SkillDefinition(
            name=_CANDIDATE.name,
            description=_CANDIDATE.description,
            when_to_use=_CANDIDATE.when_to_use,
            invocation=_CANDIDATE.invocation,
            provider=_CANDIDATE.provider,
            source=_CANDIDATE.source,
            resource_base=_CANDIDATE.resource_base,
            content=BADGE_BODY,
        )


_CANDIDATE = SkillCandidate(
    name="dsh-badge",
    description=DESCRIPTION,
    invocation=INVOCATION,
    provider=PROVIDER_NAME,
    source="bundled",
    resource_base=RESOURCE_BASE,
    rank=BUNDLED_SKILL_RANK,
    locator={"body": BADGE_BODY},
)

_PROVIDER = _BadgeSkillProvider()


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：向 ``ctx.skills`` 注册内置 dsh-badge 技能。"""
    skills = getattr(ctx, "skills", None) if ctx.has_service("skills") else None
    if skills is None:
        raise RuntimeError("skill-badge: the skills service is not mounted (add dsh_py.services.skill:apply)")
    skills.register_provider(lambda control: _PROVIDER)


apply.inject = ["skills"]  # 声明：本插件需要 skills 服务（供 loader 拓扑排序）

__all__ = ["PROVIDER_NAME", "BADGE_BODY", "apply"]
