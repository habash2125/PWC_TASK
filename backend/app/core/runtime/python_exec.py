"""Sandboxed execution of model-generated Python.

Each ``run_python`` call executes in a **forked, disposable child** of the API
process, so ``resource.setrlimit`` caps apply to the child only and the server
is never capped or killed by generated code:

* memory cap (``RLIMIT_DATA``), CPU budget (``RLIMIT_CPU``), wall-clock timeout (parent kills);
* no new processes (``RLIMIT_NPROC``), no file growth beyond a few hundred kB (``RLIMIT_FSIZE``);
* ``cwd`` is a per-request scratch directory that is deleted afterwards;
* all socket creation raises — there is no network egress;
* an import allow-list enforced by a restricted ``__import__`` (pandas, numpy, plotly, math, datetime…);
* ``open``, ``eval``, ``exec``, ``compile``, ``input``, ``breakpoint``, ``globals``… are absent from builtins.

The child never holds a database handle: data arrives as DataFrames produced
by the guarded ``run_sql`` tool in the parent.  Figures are captured by a
patched ``Figure.show()`` and returned with stdout and the (picklable) namespace.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import logging
import multiprocessing
import os
import pickle
import resource
import shutil
import socket
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.runtime.chart_capture import CapturedChart, figure_to_capture
from app.observability import metrics

log = logging.getLogger("lens.chart")

ALLOWED_MODULES = frozenset(
    {
        "pandas",
        "numpy",
        "plotly",
        "math",
        "datetime",
        "json",
        "re",
        "statistics",
        "decimal",
        "collections",
        "itertools",
        "functools",
        "textwrap",
        "string",
        "calendar",
        "typing",
        "dataclasses",
        "operator",
        "fractions",
        "numbers",
        "enum",
        "copy",
        "time",
        "zoneinfo",
        "dateutil",
        "pytz",
    }
)
BLOCKED_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "importlib",
        "shutil",
        "pathlib",
        "ctypes",
        "signal",
        "threading",
        "multiprocessing",
        "builtins",
        "io",
        "pickle",
        "marshal",
        "code",
        "codeop",
        "inspect",
        "gc",
        "resource",
        "http",
        "urllib",
        "requests",
        "ssl",
        "ftplib",
        "smtplib",
        "telnetlib",
        "asyncio",
        "concurrent",
        "sqlite3",
        "psycopg",
        "asyncpg",
        "sqlalchemy",
        "openai",
        "app",
    }
)
REMOVED_BUILTINS = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "help",
        "globals",
        "locals",
        "vars",
        "memoryview",
        "__import__",
    }
)


@dataclass(slots=True)
class ExecResult:
    ok: bool
    stdout: str = ""
    error: str | None = None
    error_code: str | None = None  # timeout | memory | forbidden | exception | crashed
    figures: list[CapturedChart] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0


def _restricted_import(name: str, globals=None, locals=None, fromlist=(), level=0):
    top = name.split(".")[0]
    if level != 0 or top in BLOCKED_MODULES or top not in ALLOWED_MODULES:
        raise ImportError(
            f"import of '{name}' is not permitted in the analysis sandbox; allowed: {', '.join(sorted(ALLOWED_MODULES))}"
        )
    return _real_import(name, globals, locals, fromlist, level)


_real_import = builtins.__import__


def _safe_builtins() -> dict[str, Any]:
    safe = {k: v for k, v in vars(builtins).items() if k not in REMOVED_BUILTINS and not k.startswith("_")}
    safe["__import__"] = _restricted_import
    safe["__build_class__"] = builtins.__build_class__
    safe["__name__"] = "lens_sandbox"
    return safe


def _block_network() -> None:
    def _blocked(*_a, **_k):
        raise OSError("network access is disabled in the analysis sandbox")

    socket.socket = _blocked  # type: ignore[misc,assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]
    socket.socketpair = _blocked  # type: ignore[assignment]


def _current_vm_data_bytes() -> int:
    """RLIMIT_DATA counts private anonymous mappings, so the headroom must be measured on the same scale."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:  # noqa: PTH123 - the child still has the real open()
            for line in fh:
                if line.startswith("VmData:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _apply_limits(memory_mb: int, cpu_seconds: int) -> None:
    data_limit = memory_mb * 1024 * 1024 + _current_vm_data_bytes()
    for res, limit in (
        (resource.RLIMIT_DATA, (data_limit, data_limit)),
        (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2)),
        (resource.RLIMIT_FSIZE, (512 * 1024, 512 * 1024)),
        (resource.RLIMIT_NPROC, (1, 1)),
        (resource.RLIMIT_CORE, (0, 0)),
    ):
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(res, limit)


def _picklable_namespace(ns: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in ns.items():
        if k.startswith("__") or callable(v) and not hasattr(v, "to_plotly_json"):
            continue
        if type(v).__name__ == "module":
            continue
        try:
            pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            continue
        out[k] = v
    return out


def _child(
    code: str, namespace_bytes: bytes, conn, memory_mb: int, cpu_seconds: int, scratch: str
) -> None:  # pragma: no cover - runs in fork
    result: dict[str, Any] = {
        "ok": False,
        "stdout": "",
        "error": None,
        "error_code": None,
        "figures": [],
        "variables": b"",
    }
    stdout = io.StringIO()
    try:
        sys.dont_write_bytecode = True  # never let a size-limited child leave a truncated .pyc for the parent
        os.chdir(scratch)
        _block_network()
        _apply_limits(memory_mb, cpu_seconds)
        import plotly.basedatatypes as _pbd

        captured: list[dict[str, Any]] = []

        def _show(self, *args, **kwargs):
            captured.append({"fig": self})

        _pbd.BaseFigure.show = _show  # type: ignore[assignment]
        namespace: dict[str, Any] = pickle.loads(namespace_bytes)
        namespace["__builtins__"] = _safe_builtins()
        import numpy as np
        import pandas as pd
        import plotly.express as px
        import plotly.graph_objects as go

        namespace.setdefault("pd", pd)
        namespace.setdefault("np", np)
        namespace.setdefault("px", px)
        namespace.setdefault("go", go)
        compiled = compile(code, "<analysis>", "exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            exec(compiled, namespace)  # noqa: S102 - this is the sandboxed executor
        figures: list[dict[str, Any]] = []
        for idx, item in enumerate(captured, start=1):
            try:
                figures.append(figure_to_capture(item["fig"], idx).model_dump())
            except ValueError as exc:
                result["error"] = (result["error"] or "") + f"\n{exc}"
        result.update(ok=result["error"] is None, figures=figures)
        namespace.pop("__builtins__", None)
        result["variables"] = pickle.dumps(_picklable_namespace(namespace), protocol=pickle.HIGHEST_PROTOCOL)
    except MemoryError:
        result.update(
            ok=False,
            error="MemoryError: the analysis exceeded its memory budget; work with aggregated data",
            error_code="memory",
        )
    except ImportError as exc:
        result.update(ok=False, error=f"ImportError: {exc}", error_code="forbidden")
    except BaseException as exc:  # noqa: BLE001 - everything must be reported back to the loop
        tb = traceback.format_exception_only(type(exc), exc)
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == "<analysis>"]
        where = f" (line {frames[-1].lineno})" if frames else ""
        result.update(ok=False, error="".join(tb).strip()[:2000] + where, error_code="exception")
    finally:
        result["stdout"] = stdout.getvalue()[-20_000:]
        try:
            conn.send(pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL))
        except Exception:
            pass
        conn.close()
        os._exit(0)


def warm_up() -> None:
    """Imports the plotting stack in the parent so forked children start warm and import nothing new."""
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import plotly.express  # noqa: F401
    import plotly.graph_objects  # noqa: F401
    import plotly.io  # noqa: F401
    import plotly.subplots  # noqa: F401


async def run_python(
    code: str,
    namespace: dict[str, Any],
    *,
    timeout_seconds: int,
    memory_mb: int,
    cpu_seconds: int,
    scratch_root: str,
) -> ExecResult:
    """Executes ``code`` in a forked child and returns stdout, figures and the updated namespace."""
    started = time.perf_counter()
    scratch = tempfile.mkdtemp(prefix=f"turn-{uuid.uuid4().hex[:8]}-", dir=scratch_root)
    ctx = multiprocessing.get_context("fork")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    ns_bytes = pickle.dumps(namespace, protocol=pickle.HIGHEST_PROTOCOL)
    proc = ctx.Process(target=_child, args=(code, ns_bytes, child_conn, memory_mb, cpu_seconds, scratch), daemon=True)
    proc.start()
    child_conn.close()
    payload: bytes | None = None

    def _wait() -> bytes | None:
        if parent_conn.poll(timeout_seconds):
            try:
                return parent_conn.recv()
            except EOFError:
                return None
        return None

    try:
        payload = await asyncio.to_thread(_wait)
    finally:
        if proc.is_alive():
            proc.kill()
        await asyncio.to_thread(proc.join, 5)
        parent_conn.close()
        shutil.rmtree(scratch, ignore_errors=True)
    elapsed = int((time.perf_counter() - started) * 1000)
    if payload is None:
        code_ = "timeout" if elapsed >= timeout_seconds * 1000 - 50 else "crashed"
        metrics.python_exec.labels(code_).inc()
        msg = (
            f"the analysis exceeded its {timeout_seconds}s time budget"
            if code_ == "timeout"
            else "the analysis process exited unexpectedly (out of memory?)"
        )
        return ExecResult(ok=False, error=msg, error_code=code_, duration_ms=elapsed)
    result = pickle.loads(payload)
    variables = pickle.loads(result["variables"]) if result.get("variables") else {}
    figures = [CapturedChart.model_validate(f) for f in result.get("figures", [])]
    metrics.python_exec.labels("ok" if result["ok"] else (result.get("error_code") or "exception")).inc()
    metrics.stage_latency.labels("python_exec").observe(elapsed / 1000)
    return ExecResult(
        ok=bool(result["ok"]),
        stdout=result.get("stdout", ""),
        error=result.get("error"),
        error_code=result.get("error_code"),
        figures=figures,
        variables=variables,
        duration_ms=elapsed,
    )
