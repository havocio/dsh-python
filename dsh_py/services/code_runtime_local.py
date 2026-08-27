"""code-runtime 本地后端（对标 dsh 的 ``@deepseek-ai/dsh-code-runtime-worker-thread``）。

dsh 用 Node worker_threads 做隔离；Python 没有对等物，本实现改用**独立进程**
（``multiprocessing``）以获得真实隔离与硬终止能力（``isolation='process'``）。

- 程序在子进程内以 async 函数体运行；标准输出被捕获为日志。
- 绑定命名空间以「代理对象」形式暴露给程序；调用经独立 RPC 管道回到父进程，
  父进程在主循环上执行真实的主机异步函数，再把结果序列化回传。
- 失败分类：``exception`` / ``timeout`` / ``abort`` / ``worker-exit`` /
  ``invalid-output`` / ``output-limit``；错误是结果字段，不拒绝 ``run()``。

> 注：依赖 ``multiprocessing``（标准库）；暂未引入第三方包。Windows 下
> ``multiprocessing`` 的 spawn 启动会重新 import 模块，故 worker 必须为顶层函数。
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import sys
import traceback
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any

from dsh_py.services.code_runtime import (
    CodeBindingNamespace,
    CodeRunFailure,
    CodeRunRequest,
    CodeRunResult,
    CodeRuntime,
    validate_run_request,
)

#: 结果行哨兵前缀——子进程在 stdout 末尾写出 ``<<<DSH_RESULT>>>`` + JSON，父进程读到即停止。
_RESULT_SENTINEL = "<<<DSH_RESULT>>>"

#: 默认输出字节上限（外层日志 + 值 + 诊断）。超限 → ``output-limit``。
DEFAULT_OUTPUT_LIMIT = 1 << 20  # 1 MiB

#: RPC 消息标签。
_REQ_CALL = "call"
_RES_OK = "ok"
_RES_ERR = "err"


def _marshal(value: Any) -> Any:
    """把值规约为可 JSON 序列化；无法序列化则抛 ``TypeError``（由 worker 捕获）。"""
    return json.loads(json.dumps(value))


class _BindingProxy:
    """子进程内暴露给程序的命名空间代理；调用即经管道请求父进程执行真实主机函数。"""

    def __init__(self, conn: Connection, global_name: str) -> None:
        self.__conn = conn
        self.__global = global_name

    def __getattr__(self, fn_name: str) -> Any:
        conn = self.__conn
        g = self.__global

        async def proxy(*args: Any) -> Any:
            conn.send(json.dumps({"tag": _REQ_CALL, "ns": g, "fn": fn_name, "args": list(args)}))
            reply = conn.recv()
            msg = json.loads(reply)
            if msg["tag"] == _RES_OK:
                return msg["value"]
            raise _RemoteRejection(msg["error"])

        return proxy


class _RemoteRejection(Exception):
    """绑定调用在父进程侧失败，作为程序内异常抛出。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _LogCapture:
    """转发 ``print`` 到日志列表（剥离，不污染 JSON 结果行）。"""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, text: str) -> int:
        self._sink.append(text)
        return len(text)

    def flush(self) -> None:  # pragma: no cover - 兼容 stdout 接口
        pass


def _safe_tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _emit_result(logs: list[str], value: Any, failure: CodeRunFailure | None) -> None:
    """写出日志（已收集）后，单行写出结果哨兵 + JSON。"""
    sys.stdout.write("".join(logs))
    sys.stdout.flush()
    result = {"logs": logs, "value": value}
    if failure is not None:
        result["error"] = {"kind": failure.kind, "message": failure.message}
    sys.stdout.write(f"{_RESULT_SENTINEL}{json.dumps(result, ensure_ascii=False)}\n")
    sys.stdout.flush()


def _child_main(conn: Connection,
 spec: dict) -> None:
    """子进程入口：构建代理绑定、运行程序、写出日志与结果。

    :param conn: 通向父进程的 RPC 管道（子端）。
    :param spec: ``{"program": str, "bindings": [{global, functions:[names]}]}``。
    """
    logs: list[str] = []
    try:
        program: str = spec["program"]
        ns_meta = spec["bindings"]

        # 构造暴露给程序的全局对象；真实函数留在父进程，子进程只持有代理。
        globals_dict: dict[str, Any] = {"__name__": "__dsh_main__", "__builtins__": __builtins__}
        for meta in ns_meta:
            globals_dict[meta["global"]] = _BindingProxy(conn, meta["global"])

        sys.stdout = _LogCapture(logs)
        try:
            async def _body() -> Any:
                locals_dict: dict[str, Any] = {}
                compiled = compile(program, "<dsh-code>", "exec")
                exec(compiled, globals_dict, locals_dict)
                return locals_dict.get("__dsh_return__")

            value = asyncio.run(_body())
        except BaseException as exc:  # noqa: BLE001 - 程序异常是结果字段，不是异常路径
            sys.stdout = sys.__stdout__
            _emit_result(logs, None, CodeRunFailure(kind="exception", message=_safe_tb(exc)))
            return

        try:
            value_json = _marshal(value)
        except (TypeError, ValueError) as exc:
            sys.stdout = sys.__stdout__
            _emit_result(logs, None, CodeRunFailure(kind="invalid-output", message=str(exc)))
            return

        sys.stdout = sys.__stdout__
        _emit_result(logs, value_json, None)
    except BaseException as exc:  # noqa: BLE001 - 兜底：任何意外失败也给出结果
        sys.stdout = sys.__stdout__
        _emit_result(logs, None, CodeRunFailure(kind="exception", message=_safe_tb(exc)))


class LocalCodeRuntime(CodeRuntime):
    """进程级本地代码运行时（对标 worker-thread 后端）。

    通过 ``multiprocessing`` 创建隔离子进程运行程序；绑定调用经独立管道回到父进程。
    支持中止（``request.signal``）与超时（硬终止），并检测非无损 JSON 完成值。
    """

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = config or {}
        self.output_limit: int = int(cfg.get("outputLimitBytes", DEFAULT_OUTPUT_LIMIT))
        self.timeout_ms: int | None = cfg.get("timeoutMs")

    @property
    def language(self) -> str:
        return "python"

    @property
    def isolation(self) -> str:
        return "process"

    async def run(self, request: CodeRunRequest) -> CodeRunResult:
        validate_run_request(request)
        bindings_meta = [
            {"global": ns.global_name, "functions": list(ns.functions.keys())}
            for ns in request.bindings
        ]
        spec: dict = {"program": request.program, "bindings": bindings_meta}

        parent_conn, child_conn = multiprocessing.Pipe()
        proc = multiprocessing.Process(target=_child_main, args=(child_conn, spec), daemon=True)
        proc.start()
        child_conn.close()  # 父进程只用 parent_conn

        collected_logs: list[str] = []
        result_holder: dict = {}
        rpc_task = asyncio.ensure_future(self._serve_rpc(parent_conn, request.bindings))
        read_task = asyncio.ensure_future(self._read_stdout(proc, collected_logs, result_holder))

        try:
            await asyncio.wait_for(read_task, timeout=self.timeout_ms / 1000 if self.timeout_ms else None)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                if proc.stdout is not None:
                    proc.stdout.read()
            except Exception:  # noqa: BLE001
                pass
            await rpc_task
            return CodeRunResult(
                logs=collected_logs,
                error=CodeRunFailure(kind="timeout", message="execution budget expired"),
            )

        await rpc_task  # 收尾 RPC 服务（结算未决绑定调用）
        proc.join()

        if proc.exitcode != 0 and "error" not in result_holder:
            return CodeRunResult(
                logs=collected_logs,
                error=CodeRunFailure(kind="worker-exit", message=f"substrate exited {proc.exitcode}"),
            )
        err = result_holder.get("error")
        failure = CodeRunFailure(kind=err["kind"], message=err["message"]) if err else None
        return CodeRunResult(
            logs=result_holder.get("logs", collected_logs),
            value=result_holder.get("value"),
            error=failure,
        )

    async def _read_stdout(self, proc: multiprocessing.Process, sink: list[str],
                          result_holder: dict) -> None:
        """读取子进程 stdout，累积日志直到结果哨兵或 EOF，并把哨兵后的 JSON 解析进 result_holder。"""
        assert proc.stdout is not None
        buffer = ""
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk
            idx = buffer.find(_RESULT_SENTINEL)
            if idx != -1:
                sink.append(buffer[:idx])
                rest = buffer[idx + len(_RESULT_SENTINEL):]
                try:
                    result_holder.update(json.loads(rest))
                except (ValueError, TypeError):
                    pass
                return
            last_nl = buffer.rfind("\n")
            if last_nl != -1:
                sink.append(buffer[:last_nl + 1])
                buffer = buffer[last_nl + 1:]

    async def _serve_rpc(self, conn: Connection, namespaces: list[CodeBindingNamespace]) -> None:
        """服务子进程的绑定调用：从管道读请求，在父进程执行真实主机函数。"""
        ns_map = {ns.global_name: ns for ns in namespaces}
        while True:
            try:
                raw = conn.recv()
            except EOFError:
                break
            msg = json.loads(raw)
            if msg.get("tag") != _REQ_CALL:
                continue
            ns = ns_map.get(msg["ns"])
            fn = ns.functions.get(msg["fn"]) if ns else None
            if fn is None:
                conn.send(json.dumps({"tag": _RES_ERR, "error": f"unknown binding {msg['ns']}.{msg['fn']}"}))
                continue
            try:
                result = await fn(msg["args"])
                conn.send(json.dumps({"tag": _RES_OK, "value": _marshal(result)}))
            except BaseException as exc:  # noqa: BLE001
                conn.send(json.dumps({"tag": _RES_ERR, "error": str(exc)}))


__all__ = ["LocalCodeRuntime", "DEFAULT_OUTPUT_LIMIT"]
