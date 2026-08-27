"""本地文件系统技能 provider（``ctx.skills`` 注册表的一个实现）。

对齐 dsh 的 ``@deepseek-ai/dsh-skill-filesystem``：从项目 / 自定义 / 用户根发现
目录包（``<dir>/SKILL.md``）与扁平 Markdown 技能（``<root>/*.md``），解析 YAML
frontmatter（name / description / whenToUse / disable-model-invocation /
user-invocable / metadata），经注册表按 rank 合并。

适配（dsh_py 差异，均已注明）：
- 发现与读取用 stdlib ``pathlib``（dsh 走 node fs / ``ctx.fs``；dsh_py 不依赖
  fs 服务，差异仅传输层）。
- YAML frontmatter：优先懒加载 ``yaml``（PyYAML）；缺失或解析失败时回退手写
  极简映射解析器（扁平标量 + 内联 ``metadata: {k: v}``，零依赖妥协，与
  attachment 图像头解析同风格）。
- 监视（watch）：懒加载 ``watchdog``；缺依赖时降级为不监视（目录发现照常，
  目录变更不再自动失效——dsh_py 无 ``fs/observed`` 事件，宿主变更观测为 no-op）。
  默认 ``watch=False``（dsh 默认 True），避免未装 watchdog 时产生静默陈旧目录。
- ``SkillProviderControl.signal`` 是 :class:`CancelSignal`：provider 以轮询
  ``aborted`` 替代 dsh 的事件监听。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.services.skill import (
    BUNDLED_SKILL_RANK,
    SkillCandidate,
    SkillDefinition,
    SkillInvocationPolicy,
    SkillProvider,
    SkillProviderControl,
    SkillProviderObservation,
    SkillResourceBase,
    is_skill_name,
)
from dsh_py.util.home_paths import resolve_dsh_home

PROJECT_DSH_RANK = 100
PROJECT_AGENTS_RANK = 200
CUSTOM_RANK = 300
USER_DSH_RANK = 400
USER_AGENTS_RANK = 500


@dataclass(frozen=True)
class SkillRoot:
    """一个技能根目录及其来源 / 优先级。"""

    path: str
    source: str
    rank: int
    project_root: Optional[str] = None
    skip_system: bool = False


@dataclass(frozen=True)
class ParsedSkill:
    """解析出的技能文件元数据 + 正文。"""

    name: str
    description: str
    when_to_use: Optional[str]
    invocation: SkillInvocationPolicy
    metadata: Optional[dict]
    content: str


# ---------------------------------------------------------------------------
# frontmatter（懒 yaml + 极简回退）
# ---------------------------------------------------------------------------

def _minimal_yaml_map(text: str) -> Optional[dict]:
    """极简 YAML 子集解析：顶层 ``key: value`` 标量 + 内联嵌套 ``{k: v}``。

    失败返回 None（视为无 frontmatter）。零依赖妥协——覆盖技能 frontmatter 的
    实际形状（扁平标量 + 可选 metadata 映射）。
    """
    result: dict = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        if ":" not in line:
            return None
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            return None
        # 内联嵌套映射（metadata: {a: 1, b: "x"}）
        if value.startswith("{") and value.endswith("}"):
            inner: dict = {}
            for part in _split_inline_parts(value[1:-1]):
                if ":" not in part:
                    return None
                ik, _, iv = part.partition(":")
                inner[ik.strip()] = _coerce_scalar(iv.strip())
            result[key] = inner
            continue
        result[key] = _coerce_scalar(value)
    return result


def _split_inline_parts(text: str) -> list[str]:
    """按逗号切分内联映射条目，尊重双引号字符串内的逗号。"""
    parts: list[str] = []
    buf: list[str] = []
    in_quote = False
    for char in text:
        if char == '"':
            in_quote = not in_quote
        if char == "," and not in_quote:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    if buf:
        parts.append("".join(buf))
    return parts


def _coerce_scalar(value: str) -> Any:
    """标量尽力转换：引号字符串 / 布尔 / 数字 / 原样字符串。"""
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1]
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1]
    if value.lower() in ("true", "yes", "on"):
        return True
    if value.lower() in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _parse_frontmatter_data(yaml_text: str) -> Optional[dict]:
    """解析 frontmatter 为映射：优先懒加载 ``yaml``，失败回退极简解析器。"""
    try:
        import yaml  # 懒加载：可选依赖

        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:  # noqa: BLE001 -- 缺依赖或 YAML 不合法都走回退
        pass
    return _minimal_yaml_map(yaml_text)


def parse_frontmatter(raw: str) -> Optional[tuple[dict, str]]:
    """拆分 ``---`` 围栏 frontmatter：返回 ``(data, body)``；无围栏返回 None。"""
    first_line_end = raw.find("\n")
    if first_line_end < 0:
        return None
    first_line = raw[:first_line_end].rstrip("\r")
    if first_line != "---":
        return None
    start = first_line_end + 1
    closing = _find_closing_frontmatter(raw, start)
    if closing is None:
        return None
    yaml_text = raw[start:closing]
    data = _parse_frontmatter_data(yaml_text)
    if not isinstance(data, dict):
        return None
    # closing 指向闭合围栏行末的换行符；正文从其下一字符开始
    return data, raw[closing + 1:]


def _find_closing_frontmatter(raw: str, start: int) -> Optional[int]:
    line_start = start
    while line_start <= len(raw):
        next_newline = raw.find("\n", line_start)
        line_end = len(raw) if next_newline < 0 else next_newline
        line = raw[line_start:line_end].rstrip("\r")
        if line == "---":
            return line_end if next_newline >= 0 else len(raw)
        if next_newline < 0:
            return None
        line_start = next_newline + 1
    return None


# ---------------------------------------------------------------------------
# 发现 / 解析
# ---------------------------------------------------------------------------

def _warn(ctx: AppContext, message: str) -> None:
    """防御性告警：无 logger 服务（裸 ctx）时静默。"""
    try:
        ctx.logger.warn(message)
    except Exception:  # noqa: BLE001
        pass


def _string_field(data: dict, key: str) -> Optional[str]:
    value = data.get(key)
    return value if isinstance(value, str) and len(value) > 0 else None


def _optional_string(data: dict, key: str) -> Optional[str]:
    return _string_field(data, key)


def _frontmatter_boolean(data: dict, key: str) -> Optional[bool]:
    if key not in data:
        return None
    value = data[key]
    if isinstance(value, bool):
        return value
    if value in (1, "1"):
        return True
    if value in (0, "0"):
        return False
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "yes", "on"):
            return True
        if lowered in ("false", "no", "off"):
            return False
    raise TypeError(f'frontmatter field "{key}" must be a boolean')


def _parse_invocation_policy(data: dict) -> SkillInvocationPolicy:
    for legacy, canonical in (
        ("disableModelInvocation", "disable-model-invocation"),
        ("modelInvocable", "disable-model-invocation"),
        ("userInvocable", "user-invocable"),
    ):
        if legacy in data:
            raise RuntimeError(f'frontmatter field "{legacy}" is unsupported; use "{canonical}"')
    disable_model = _frontmatter_boolean(data, "disable-model-invocation")
    user_invocable = _frontmatter_boolean(data, "user-invocable")
    return SkillInvocationPolicy(
        model_invocable=disable_model is not True,
        user_invocable=user_invocable is not False,
    )


def _optional_metadata(data: dict) -> Optional[dict]:
    value = data.get("metadata")
    if isinstance(value, dict):
        return value
    return None


def parse_skill_file(path: str, ctx: AppContext) -> Optional[ParsedSkill]:
    """读取并解析一个技能文件；不可读 / 无 frontmatter / 字段非法返回 None。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warn(ctx, f"skill file {path} ignored: {exc}")
        return None
    parsed = parse_frontmatter(raw)
    if parsed is None:
        _warn(ctx, f"skill file {path} ignored: missing YAML frontmatter")
        return None
    data, body = parsed
    name = _string_field(data, "name")
    description = _string_field(data, "description")
    if name is None or description is None:
        _warn(ctx, f"skill file {path} ignored: frontmatter requires name and description")
        return None
    if not is_skill_name(name):
        _warn(ctx, f'skill file {path} ignored: invalid skill name "{name}"')
        return None
    try:
        invocation = _parse_invocation_policy(data)
    except Exception as exc:  # noqa: BLE001
        _warn(ctx, f"skill file {path} ignored: invalid invocation frontmatter: {exc}")
        return None
    return ParsedSkill(
        name=name,
        description=description,
        when_to_use=_optional_string(data, "whenToUse"),
        invocation=invocation,
        metadata=_optional_metadata(data),
        content=body.strip(),
    )


def _discover_root(root: SkillRoot, provider: str, ctx: AppContext) -> list[SkillCandidate]:
    """扫描一个技能根目录，产出候选列表。"""
    candidates: list[SkillCandidate] = []
    base = Path(root.path)
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except FileNotFoundError:
        return []
    except NotADirectoryError:
        return []
    except OSError as exc:
        _warn(ctx, f"skill root {root.path} skipped: {exc}")
        return []
    for entry in entries:
        if root.skip_system and entry.name == ".system":
            continue
        if entry.is_dir():
            locator_path = entry / "SKILL.md"
            directory = str(entry)
        elif entry.is_file() and entry.name.endswith(".md"):
            locator_path = entry
            directory = root.path
        else:
            continue
        parsed = parse_skill_file(str(locator_path), ctx)
        if parsed is None:
            continue
        candidates.append(SkillCandidate(
            name=parsed.name,
            description=parsed.description,
            when_to_use=parsed.when_to_use,
            invocation=parsed.invocation,
            provider=provider,
            source=root.source,
            rank=root.rank,
            locator={"path": str(locator_path), "directory": directory},
            resource_base=SkillResourceBase(kind="directory", path=directory),
            path=str(locator_path),
            metadata=parsed.metadata,
        ))
    return candidates


def _find_project_root(cwd: str) -> str:
    """向上找含 ``.git`` 的目录；找不到返回原 cwd。"""
    current = Path(cwd).resolve()
    while True:
        if (current / ".git").exists():
            return str(current)
        parent = current.parent
        if parent == current:
            return cwd
        current = parent


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------

class FileSystemSkillProvider(SkillProvider):
    """把本地项目 / 用户技能根映射进 ``ctx.skills``。"""

    def __init__(
        self,
        ctx: AppContext,
        control: SkillProviderControl,
        config: Optional[dict] = None,
    ) -> None:
        self.ctx = ctx
        self.control = control
        config = config or {}
        self.name = config.get("providerName") or "filesystem"
        self.include_default_roots = config.get("includeDefaultRoots", True)
        self.dsh_home = resolve_dsh_home(config.get("dshHome"))
        self.agents_home = config.get("agentsHome") or os.environ.get("DSH_AGENTS_HOME") or str(Path.home() / ".agents")
        self.custom_skill_dirs = [os.path.abspath(d) for d in (config.get("customSkillDirs") or [])]
        bundled = config.get("bundledSkillDir")
        if bundled is None and self.include_default_roots:
            bundled = os.environ.get("DSH_BUNDLED_SKILL_DIR")
        self.bundled_skill_dir = os.path.abspath(bundled) if bundled else None
        self.watch_enabled = bool(config.get("watch", False))
        self._watch_manager: Any = None
        if self.watch_enabled:
            try:
                from dsh_py.services.skill_watch import SkillWatchManager  # 懒加载

                self._watch_manager = SkillWatchManager(control.invalidate, config)
            except ImportError:
                _warn(self.ctx, 
                    "skill-filesystem: watchdog 未安装，监视降级为关闭（目录发现照常）；安装 watchdog 以启用"
                )
                self.watch_enabled = False

    async def list(self, options: dict) -> Union[list, SkillProviderObservation]:
        roots = self._roots(options.get("cwd"))
        complete = True
        if self._watch_manager is not None:
            try:
                await self._watch_manager.observe_roots([r.path for r in roots])
            except Exception as exc:  # noqa: BLE001
                if getattr(self.control.signal, "aborted", False):
                    raise
                _warn(self.ctx, f"skill-filesystem: watcher failed: {exc}")
                complete = False
        candidates: list = []
        for root in roots:
            candidates.extend(_discover_root(root, self.name, self.ctx))
        return candidates if complete else SkillProviderObservation(candidates=candidates, complete=False)

    async def get(self, candidate: SkillCandidate, options: dict) -> Optional[SkillDefinition]:
        locator = candidate.locator
        parsed = parse_skill_file(locator["path"], self.ctx)
        if parsed is None:
            return None
        return SkillDefinition(
            name=parsed.name,
            description=parsed.description,
            when_to_use=parsed.when_to_use,
            invocation=parsed.invocation,
            source=candidate.source,
            provider=self.name,
            resource_base=SkillResourceBase(kind="directory", path=locator["directory"]),
            path=locator["path"],
            metadata=parsed.metadata,
            content=parsed.content,
        )

    def observe_host_mutation(self, path: str) -> None:
        """宿主写 / 编辑后的同步失效入口（dsh_py 无 ``fs/observed`` 事件，no-op 占位）。"""
        if self._watch_manager is not None:
            self._watch_manager.observe_host_mutation(path)

    async def dispose(self) -> None:
        if self._watch_manager is not None:
            await self._watch_manager.dispose()

    def _roots(self, cwd: Optional[str]) -> list[SkillRoot]:
        roots: list[SkillRoot] = []
        if self.include_default_roots and cwd:
            project_root = _find_project_root(cwd)
            roots.append(SkillRoot(
                path=str(Path(project_root) / ".dsh" / "skills"),
                source="project-dsh",
                rank=PROJECT_DSH_RANK,
                project_root=project_root,
            ))
            roots.append(SkillRoot(
                path=str(Path(project_root) / ".agents" / "skills"),
                source="project-agents",
                rank=PROJECT_AGENTS_RANK,
                project_root=project_root,
            ))
        roots.extend(SkillRoot(path=d, source="custom", rank=CUSTOM_RANK) for d in self.custom_skill_dirs)
        if self.include_default_roots:
            roots.append(SkillRoot(path=str(Path(self.dsh_home) / "skills"), source="user-dsh", rank=USER_DSH_RANK, skip_system=True))
            roots.append(SkillRoot(path=str(Path(self.agents_home) / "skills"), source="user-agents", rank=USER_AGENTS_RANK))
        if self.bundled_skill_dir is not None:
            roots.append(SkillRoot(path=self.bundled_skill_dir, source="bundled", rank=BUNDLED_SKILL_RANK))
        return roots


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------

def _assert_positive_integer(name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"skill-filesystem: {name} must be a positive integer")


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：向 ``ctx.skills`` 注册本地文件系统技能 provider。"""
    config = config or {}
    for key in ("watchStabilityThresholdMs", "watchPollIntervalMs", "watchMaxProjects"):
        if config.get(key) is not None:
            _assert_positive_integer(key, config[key])
    provider: Optional[FileSystemSkillProvider] = None

    def create(control: SkillProviderControl) -> SkillProvider:
        nonlocal provider
        provider = FileSystemSkillProvider(ctx, control, config)
        return provider

    skills = getattr(ctx, "skills", None) if ctx.has_service("skills") else None
    if skills is None:
        raise RuntimeError("skill-filesystem: the skills service is not mounted (add dsh_py.services.skill:apply)")
    skills.register_provider(create)

    # 监视器的 fiber 级回收：provider 注销时随 fiber 拆除（best-effort 异步）
    def _dispose_watcher() -> None:
        if provider is None or provider._watch_manager is None:
            return
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            loop.create_task(provider.dispose())
        except RuntimeError:  # noqa: PERF203 -- 无运行循环时跳过（进程退出期）
            pass

    ctx.effect(_dispose_watcher, "skill-filesystem watcher")


apply.inject = ["skills"]  # 声明：本插件需要 skills 服务（供 loader 拓扑排序）

__all__ = [
    "FileSystemSkillProvider",
    "parse_frontmatter",
    "parse_skill_file",
    "SkillRoot",
    "apply",
]
