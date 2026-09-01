"""Shell 环境注册表（shell-env seam，对标 dsh 的 ``dsh-shell-env``）。

提供 ``ctx.shellEnv`` 注册表，管理受信任的、每次执行收集的 ``DSH_*`` 环境变量，
供模型可见的 shell 工具（``tool-bash``、``tool-bash-persistent``、未来的
``tool-pwsh``）收集进每一次 shell 调用的环境。内置 shell 事实
（``DSH_HOME``、``DSH_SHELL=1``、``DSH_SESSION_ID``）归注册表自身所有；其他插件
可注册额外的可枚举事实，注册随插件纤维释放（显式 disposer），重复所有权或未声明
的运行时键会响亮失败（fail loud）。

- :meth:`ShellEnvRegistry.register` —— 注册贡献者（命名/键唯一、保留键、命名空间校验）；
- :meth:`ShellEnvRegistry.collect` —— 为一次 shell 执行构建受信任 ``DSH_*`` 快照；
- :meth:`ShellEnvRegistry.list` —— 枚举插件贡献的变量（内置不在内，不执行 resolver）。
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

#: 受管理环境变量的命名空间前缀（对标 dsh 的 ``DSH_ENV_PREFIX``）。
DSH_ENV_PREFIX = "DSH_"
#: DeepSeek Harness home 目录（``DSH_HOME``）。
DSH_HOME_ENV = f"{DSH_ENV_PREFIX}HOME"
#: shell 指示器（``DSH_SHELL=1``）。
DSH_SHELL_KEY = f"{DSH_ENV_PREFIX}SHELL"
#: 当前会话 id（``DSH_SESSION_ID``）。
DSH_SESSION_ID_KEY = f"{DSH_ENV_PREFIX}SESSION_ID"
#: 当前会话 JSONL 路径（``DSH_SESSION_JSONL``）。
DSH_SESSION_JSONL_KEY = f"{DSH_ENV_PREFIX}SESSION_JSONL"

#: 注册表自身拥有的保留键——插件不可认领。
RESERVED_KEYS = frozenset({DSH_HOME_ENV, DSH_SHELL_KEY, DSH_SESSION_ID_KEY})
#: 变量名后缀（前缀之后部分）必须是 ``[A-Z][A-Z0-9_]*``。
_KEY_SUFFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class BashEnvVariable:
    """单个受管理 ``DSH_*`` 环境变量的模型可见元数据。"""

    def __init__(self, description: str) -> None:
        self.description = description


class BashEnvVariableInfo:
    """``ShellEnvRegistry.list`` 返回的可枚举声明。"""

    def __init__(self, contributor: str, key: str, description: str) -> None:
        self.contributor = contributor
        self.key = key
        self.description = description

    def as_dict(self) -> dict:
        """转为可比较的字典（供测试与序列化）。"""
        return {"contributor": self.contributor, "key": self.key, "description": self.description}


class BashEnvContributor:
    """插件对每次模型 shell 调用受管理环境的贡献。

    声明的键使所有权冲突可在首次命令前被检出；``resolve`` 只为当前执行计算可用的
    值（只返回在 ``variables`` 中声明的键）。
    """

    def __init__(
        self,
        name: str,
        variables: dict[str, BashEnvVariable],
        resolve: Callable[[Any], dict[str, str]],
    ) -> None:
        self.name = name
        self.variables = variables
        self.resolve = resolve


def _agent_of(execution: Any) -> Any:
    """从 ToolExecution（dict 或对象）中取出调用方 agent（可能为 ``None``）。"""
    if execution is None:
        return None
    if isinstance(execution, dict):
        return execution.get("agent")
    return getattr(execution, "agent", None)


def resolve_dsh_home(configured: Optional[str] = None, env: Optional[dict] = None) -> str:
    """解析单一根的 DeepSeek Harness home。

    优先级（从高到低）：显式配置路径、``$DSH_HOME``、``~/.dsh``。空或仅空白的
    ``$DSH_HOME`` 视为未设置，绝不解析到当前工作目录。
    """
    env = env if env is not None else os.environ
    from_env = env.get(DSH_HOME_ENV)
    if configured and configured.strip():
        selected = configured
    elif from_env and from_env.strip():
        selected = from_env
    else:
        selected = os.path.join(os.path.expanduser("~"), ".dsh")
    return os.path.normpath(os.path.abspath(os.path.expanduser(selected)))


def merge_env(base: dict, snapshot: Optional[dict]) -> dict:
    """把受信任 ``DSH_*`` 快照合并进基础环境，并先剥离 ``base`` 中继承的 ``DSH_*``。

    本地执行器在合并快照前移除所有继承的 ``DSH_*``，使嵌套 harness 与并发的父子
    agent 无法泄漏陈旧身份。``process.env``（``os.environ``）永不被原地修改。
    """
    if snapshot is None:
        return dict(base)
    merged = {k: v for k, v in base.items() if not k.startswith(DSH_ENV_PREFIX)}
    merged.update(snapshot)
    return merged


def collect_for(ctx: AppContext, execution: Any) -> Optional[dict]:
    """为一次工具执行收集受信任 ``DSH_*`` 快照；``shellEnv`` 未挂载时返回 ``None``。

    shell 工具（``tool-bash`` / ``tool-bash-persistent`` / 后台 bash job）统一经此
    入口注入，避免每个调用方重复写 ``has_service`` 守卫。
    """
    if not ctx.has_service("shellEnv"):
        return None
    return ctx.shellEnv.collect(execution)


class ShellEnvRegistry(Service):
    """``ctx.shellEnv`` 注册表：受信任的、每次执行的 ``DSH_*`` 变量。

    命名空间在每次模型 shell 调用时重建：环境里继承的 ``DSH_*`` 由执行器丢弃，再注入
    注册表当前的快照。内置 shell 事实归注册表自身所有；其他插件可注册额外的、可枚举的
    事实，注册随插件纤维释放。
    """

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "shellEnv")
        config = config or {}
        self._dsh_home = resolve_dsh_home(config.get("dshHome"))
        self._contributors: dict[str, BashEnvContributor] = {}
        self._key_owners: dict[str, str] = {}

    def register(self, contributor: BashEnvContributor) -> Callable[[], None]:
        """注册一个环境贡献者。

        名称与键唯一；内置键被保留；声明即所有权。注册随插件纤维释放（返回 disposer）。
        """
        name = contributor.name
        if not name or not name.strip():
            raise ValueError("bash env contributor name must be non-empty")
        if name in self._contributors:
            raise ValueError(f'bash env contributor "{name}" is already registered')

        variables = contributor.variables
        if not isinstance(variables, dict) or len(variables) == 0:
            raise ValueError(f'bash env contributor "{name}" must declare at least one variable')
        for key, variable in variables.items():
            if (not key.startswith(DSH_ENV_PREFIX)
                    or not _KEY_SUFFIX_RE.match(key[len(DSH_ENV_PREFIX):])):
                raise ValueError(f'bash env contributor "{name}" declared invalid key "{key}"')
            if key in RESERVED_KEYS:
                raise ValueError(f'bash env contributor "{name}" cannot own reserved key "{key}"')
            description = getattr(variable, "description", None) or ""
            if not description.strip():
                raise ValueError(f'bash env contributor "{name}" must describe "{key}"')
            if key in self._key_owners:
                owner = self._key_owners[key]
                raise ValueError(
                    f'bash env key "{key}" is already owned by contributor "{owner}"; '
                    f'contributor "{name}" cannot also own it'
                )

        # 全部校验通过后再登记（避免半注册状态）。
        self._contributors[name] = contributor
        for key in variables:
            self._key_owners[key] = name

        def dispose() -> None:
            self._contributors.pop(name, None)
            for key in variables:
                if self._key_owners.get(key) == name:
                    self._key_owners.pop(key, None)

        return dispose

    def collect(self, execution: Any) -> dict:
        """为一次 shell 工具执行构建受信任 ``DSH_*`` 快照。

        :param execution: 当前工具执行（含可选 ``agent``）。
        :returns: 内置事实 + 当前贡献者解析结果（按 key 排序的不可变式字典）。
        """
        values: dict[str, str] = {
            DSH_HOME_ENV: self._dsh_home,
            DSH_SHELL_KEY: "1",
        }
        agent = _agent_of(execution)
        if agent is not None:
            values[DSH_SESSION_ID_KEY] = agent.session.header.id

        for contributor in sorted(self._contributors.values(), key=lambda c: c.name):
            resolved = contributor.resolve(execution) or {}
            for raw_key, value in resolved.items():
                if raw_key not in contributor.variables:
                    raise RuntimeError(
                        f'bash env contributor "{contributor.name}" returned undeclared key "{raw_key}"'
                    )
                if not isinstance(value, str):
                    raise RuntimeError(
                        f'bash env contributor "{contributor.name}" returned a non-string value for "{raw_key}"'
                    )
                values[raw_key] = value

        return dict(sorted(values.items()))

    def list(self) -> list[BashEnvVariableInfo]:
        """枚举插件贡献的变量（不执行 resolver，不含注册表内置事实）。"""
        infos: list[BashEnvVariableInfo] = []
        for contributor in self._contributors.values():
            for key, variable in contributor.variables.items():
                infos.append(BashEnvVariableInfo(contributor.name, key, variable.description))
        infos.sort(key=lambda i: i.key)
        return infos


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.shellEnv`` 服务与内置 session-persistence 贡献者。

    内置贡献者 ``session-persistence`` 贡献 ``DSH_SESSION_JSONL``：仅当活动持久化后端
    提供 JSONL 路径时解析，否则省略（与 dsh 对齐）。
    """
    config = config or {}
    registry = ShellEnvRegistry(ctx, config)

    def _resolve_session_jsonl(execution: Any) -> dict[str, str]:
        agent = _agent_of(execution)
        if agent is None or not ctx.has_service("sessionPersistence"):
            return {}
        from dsh_py.services.session_persistence import JsonlSessionPersistence

        sp = ctx.sessionPersistence
        if not isinstance(sp, JsonlSessionPersistence):
            return {}
        path = sp.locate(agent.session.header)
        return {DSH_SESSION_JSONL_KEY: path} if path else {}

    registry.register(BashEnvContributor(
        name="session-persistence",
        variables={
            DSH_SESSION_JSONL_KEY: BashEnvVariable(
                "Absolute target path of the current session JSONL when the active "
                "persistence backend provides one."
            )
        },
        resolve=_resolve_session_jsonl,
    ))


apply.provides = ["shellEnv"]  # 声明：本插件提供 shellEnv 服务
apply.inject: list[str] = []     # 仅依赖 ctx；内置贡献者按需 has_service 守卫 sessionPersistence
