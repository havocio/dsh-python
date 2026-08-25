"""subprocess 能力 seam（对标 dsh 的 ``@deepseek-ai/dsh-subprocess``）。

完全指定的 spawn 请求、Node 形状的逐流 stdio 模式、有界收集输出（含 spill
恢复）、原始管道流与树级终止。命令默认、shell 语义、协议成帧与展示属于消费
方（如 bash 执行器 seam）。本地实现在 ``services/subprocess_local.py``。
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Literal, Optional, Protocol, Union

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service

#: DeepSeek Harness 托管的子进程环境事实保留的命名空间前缀。
DSH_ENV_PREFIX = "DSH_"

#: 凭据形状的环境名**不**转发给子进程（harness 自己的 key/secret 不得隐式泄
#: 露给 spawn 的进程）。显式提供的条目因在 scrub 之后合并而幸存。
SENSITIVE_ENV_PATTERN = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.IGNORECASE)


def scrubbed_parent_env() -> dict[str, str]:
    """父环境减去凭据形状名与全部 ``DSH_*`` 名——每个 harness 子进程的规范
    基底。``PATH``/``HOME``/locale/代理变量保留，子 CLI 正常运行；harness 身份
    永不隐式泄漏（显式转发的凭据或当前 ``DSH_*`` 事实走 spec 的显式
    ``env``，在 scrub 之后合并）。两种 scrub 均大小写不敏感（Windows 环境名
    大小写不敏感）。
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if value is None:
            continue
        if SENSITIVE_ENV_PATTERN.search(key):
            continue
        if key.upper().startswith(DSH_ENV_PREFIX):
            continue
        env[key] = value
    return env


# --------------------------------------------------------------------------- #
# 词汇表
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CollectedOutput:
    """一路被捕获的流：文本（截断时是**尾部**）+ 恢复信息。

    :ivar text: 收集到的文本——截断时是流的 TAIL。
    :ivar truncated: 是否从 ``text`` 丢弃过字节。
    :ivar spillPath: 截断且可用时，保存完整流的文件路径。
    """

    text: str
    truncated: bool = False
    spillPath: Optional[str] = None


#: stdin 处置：``'ignore'`` 把 fd 0 指向 /dev/null；``'pipe'`` 暴露
#: ``SubprocessHandle.stdin`` 供调用方持续写协议；``{"data": ...}`` 写入字节并
#: 关闭（批处理形状）。
SubprocessStdinMode = Union[str, dict]


@dataclass(frozen=True)
class SubprocessCollect:
    """一路输出流的有界内存收集，可带完整流 spill 文件。

    :ivar maxBytes: 内存上限（字节）；溢出保留 TAIL。
    :ivar spill: 完整流 spill 文件；缺席则完全禁用 spill。
    """

    maxBytes: int
    spill: Optional[dict] = None


#: stdout/stderr 处置：``'pipe'`` 暴露原始流；``'inherit'`` 直通父描述符；
#: :class:`SubprocessCollect` 有界缓冲并支持偏移读。
SubprocessOutputMode = Union[str, SubprocessCollect]


@dataclass(frozen=True)
class SubprocessStdio:
    """逐流 stdio 处置，全部显式——本 seam 不施加默认值。"""

    stdin: SubprocessStdinMode
    stdout: SubprocessOutputMode
    stderr: SubprocessOutputMode


@dataclass(frozen=True)
class SubprocessSpawnSpec:
    """完全指定的 spawn 请求。本 seam 不施加默认值：每个处置、上限与目录都
    显式，由调用方自己的配置决定。

    :ivar argv: 可执行文件与参数；``argv[0]`` 是程序。此处永不经 shell 解释。
    :ivar cwd: 子进程工作目录。
    :ivar stdio: 逐流 stdio 处置。
    :ivar graceMs: 终止升级与管道排空的正常有限宽限期（毫秒）。
    :ivar signal: 中止信号——触发进程树的终止升级（CancelSignal）。
    :ivar env: 显式环境条目，合并到实现的 scrub 父基底之上。
    """

    argv: tuple
    cwd: str
    stdio: SubprocessStdio
    graceMs: int
    signal: Any = None
    env: Optional[dict] = None


@dataclass(frozen=True)
class SubprocessOutcome:
    """一个已关闭进程的退出事实（Node ``close`` 事件词汇）。

    :ivar exitCode: 退出码；进程死于信号时为 None。
    :ivar signal: 终止信号（如 ``SIGTERM``）；正常退出为 None。
    """

    exitCode: Optional[int]
    signal: Optional[str] = None


@dataclass(frozen=True)
class SubprocessOutputRead:
    """一次增量 :meth:`SubprocessOutputReader.read_from` 读取。"""

    text: str
    nextOffset: int
    lossy: bool
    spillPath: Optional[str] = None


class SubprocessOutputReader(Protocol):
    """一路已收集输出流的无游标增量访问。偏移是调用方拥有的整流字节坐标，
    独立读者互不消费对方输出；``read_from(0)`` 在终止后即批处理结果。

    :param from_byte: 整流偏移（上次读的 ``nextOffset``；首读为 0）。
    :returns: 增量文本、下一偏移、``lossy`` 标志与 spill 路径（若有）。
    """

    def read_from(self, from_byte: int) -> SubprocessOutputRead:
        ...


@dataclass(frozen=True)
class SubprocessCollectedOutputs:
    """collect 模式流的偏移读器。"""

    stdout: Optional[SubprocessOutputReader] = None
    stderr: Optional[SubprocessOutputReader] = None


class SubprocessHandle(Protocol):
    """一个扎根于自身进程树的活子进程。收集的输出在退出后仍可读；管道流归
    调用方。终止处处树级（POSIX 发信号给分离的进程组，Windows 经
    ``taskkill /T``）。

    :ivar pid: 进程 id（树根）；spawn 本身失败时为 -1。
    :ivar done: 进程关闭时以退出事实解析；仅 spawn 级失败拒绝。
    """

    pid: int
    stdin: Any
    stdout: Any
    stderr: Any
    collected: SubprocessCollectedOutputs
    done: Awaitable[SubprocessOutcome]

    def terminate(self) -> None:
        """在进程树上开始 SIGTERM → grace → SIGKILL 升级（Windows 立即强制
        终止）——本 seam 唯一的终止动词。幂等；树已消失时为 no-op；spec 的
        中止信号也会触发它。"""

    async def wait_for_exit(self, signal: Any = None) -> bool:
        """等待进程树退出（树，不只是直接子）。返回 True 表示树已退出，
        False 表示信号先中止。"""


#: 终端进程原语支持的信号（与 dsh-terminal 的 TerminalSignal 成员一致）。
SubprocessTerminalSignal = Literal["SIGINT", "SIGTERM", "SIGKILL", "SIGTSTP", "SIGHUP"]


@dataclass(frozen=True)
class SubprocessTerminalSpawnSpec:
    """完全指定的终端进程 spawn。

    :ivar argv: 可执行文件与参数；``argv[0]`` 是程序。
    :ivar cwd: 该 provider 执行世界中的工作目录。
    :ivar env: 在 provider 的 ambient scrub 之后层叠的显式环境。
    :ivar rows: 初始终端行数。
    :ivar cols: 初始终端列数。
    :ivar graceMs: 完整终端会话的 TERM→KILL 清理宽限。
    :ivar signal: 终端分配的取消；已发布的句柄拥有其后续生命周期。
    """

    argv: tuple
    cwd: str
    rows: int
    cols: int
    graceMs: int
    env: Optional[dict] = None
    signal: Any = None


@dataclass(frozen=True)
class SubprocessTerminalForeground:
    """一个终端当前前台进程组的事实。"""

    processGroupId: int
    inputWaiting: bool


class SubprocessTerminalHandle(Protocol):
    """一个活终端进程及其拥有的 OS 会话。终端分配、前台组检查/发信号与会话树
    清理是一个深层子进程原语——没有基板特定的进程控制，普通管道 stdio 无法
    重建它们。

    :ivar pid: 顶层终端进程 id。
    :ivar output: 按投递顺序的 UTF-8 终端输出字节；终端退出时排空排队输出后结束。
    :ivar done: 顶层进程退出时解析；仅活传输失败拒绝。
    """

    pid: int
    output: Any
    done: Awaitable[SubprocessOutcome]

    async def write(self, data: str) -> None:
        """向终端输入写文本（不做隐式换行转换）。"""

    async def inspect_foreground(self) -> Optional[SubprocessTerminalForeground]:
        """检查当前前台进程组；无法解析时返回 None。"""

    async def signal_foreground(self, signal: str) -> int:
        """向当前前台进程组投递信号；返回实际接收的组 id。"""

    async def terminate(self) -> None:
        """幂等地终止 provider 仍能观察到的每个终端会话成员并等待静默。"""


# --------------------------------------------------------------------------- #
# Service 定义
# --------------------------------------------------------------------------- #


class SubprocessRuntime(Service, ABC):
    """抽象 subprocess 服务。子类化、实现 :meth:`spawn` 并作为插件加载——它以
    ``ctx.subprocess`` 注册（每上下文一个实现；再加载一个会抛，即 cordis 的
    重复服务行为）。

    实现必须遵守这些语义：

    - 可执行文件路径属于与挂载文件系统 provider 共享的一个执行世界。
    - :meth:`spawn` 立即返回活句柄；``done`` 在进程关闭时以退出事实解析，
      仅 spawn 级失败拒绝。
    - collect 模式读器基于偏移、不消费，独立读者互不消费对方输出；lossy 读
      报告截断与保存完整流的 spill 文件（若有）。管道流原样交给调用方，绝不
      在此缓冲。
    - :meth:`SubprocessHandle.terminate`（以及 spec 的中止信号）在所有平台树级
      升级 SIGTERM→grace→SIGKILL——唯一终止动词。
    - 服务释放时终止所有仍在运行的托管进程并等待其退出。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "subprocess")

    @abstractmethod
    async def resolve_executable(
        self, command: str, env: Optional[dict] = None, signal: Any = None
    ) -> str:
        """在该 provider 的执行世界中解析一个配置的可执行文件。绝对路径被校验；
        裸名用 provider 的 scrub PATH 加显式环境覆盖。含分隔符的相对路径被拒：
        解析基未定义，provider 响亮失败而非猜测。

        :param command: 绝对可执行路径或裸 PATH 名。
        :param env: 用于查找的显式环境条目。
        :param signal: 中止远程或本地查找。
        :returns: 规范可执行路径。
        """

    @abstractmethod
    def spawn(self, spec: SubprocessSpawnSpec) -> SubprocessHandle:
        """从一个完全指定的 spec 启动一个托管子进程；本 seam 不施加默认值。

        :param spec: argv、目录、stdio 处置、宽限、取消与环境。
        :returns: 活进程句柄（流/读器、发信号、结果 promise）。
        """

    @abstractmethod
    async def spawn_terminal(self, spec: SubprocessTerminalSpawnSpec) -> SubprocessTerminalHandle:
        """分配一个真实终端并启动一个所属进程会话。这是唯一非管道进程原语：
        实现拥有终端字节 I/O、前台组、信号与完整会话树清理。

        :param spec: 完全指定的 argv、cwd、环境、尺寸、宽限与分配取消。
        :returns: 分配成功后的活终端句柄。
        """


def apply_seam_invariant(ctx: AppContext) -> None:
    """注册 subprocess seam 不变式伴生（对标 dsh 的 ``subprocess/invariant``）。

    无运行时不变式：这个无状态 Service 定义拥有 spawn-spec/handle 类型，观察
    属于 Service Provider；此处仅保留包名额。
    """
    if ctx.has_service("invariants"):
        ctx.invariants.register("dsh-subprocess", lambda _ctx, _fail: None)
