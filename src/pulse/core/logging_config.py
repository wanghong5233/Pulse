"""Pulse unified logging configuration — ADR-005 Observability.

两条硬合同 (see ADR-005):

  1. **trace_id 是主键, 不是字段**
     每条 log record 都通过 ``_TraceIdFilter`` 携带 ``trace_id`` 字段
     (默认 ``"-"``), 业务进入时调 :func:`set_trace_id` 绑定当前 async/thread
     context 之后, 所有下游 logger 自动带该 trace_id 直至 context 切换.

  2. **per-trace 自动分桶到独立目录**
     任何 ``trace_id != "-"`` 的 log 同时复制一份到
     ``logs/traces/<trace_id>/<service>.log`` (service 由
     :func:`setup_logging` 的 ``service_name`` 决定, 默认 ``pulse``).
     一条用户消息 -> 一个目录 -> ``ls logs/traces/<id>/`` 一次看完整链路.

保留两类全局文件:
  * ``logs/<service>.log``        — 主时序, 跨 trace 全局排列
  * ``logs/traces/<tid>/*.log``  — per-request 隔离桶

显式废弃: 历史上的 ``brain.log`` / ``boss.log`` / ``memory.log`` /
``wechat.log`` 按 domain 过滤的复制品不再创建 — 它们和 ``pulse.log`` 是
超集/子集关系, 只会干扰定位. 迁移备注见 ADR-005 §3.
"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.getenv("PULSE_LOG_DIR", "logs"))
LOG_LEVEL_FILE = os.getenv("PULSE_LOG_LEVEL", "DEBUG").upper()
TRACES_SUBDIR = "traces"

_CONSOLE_FMT = "%(asctime)s [%(levelname)-5s] trace=%(trace_id)s %(name)s: %(message)s"
_FILE_FMT = (
    "%(asctime)s [%(levelname)-5s] trace=%(trace_id)s "
    "%(name)s [%(filename)s:%(lineno)d] %(message)s"
)
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# trace_id context — propagated across async tasks and (most) threads
# ---------------------------------------------------------------------------

_trace_id_var: ContextVar[str] = ContextVar("pulse_trace_id", default="-")


def set_trace_id(trace_id: str | None) -> None:
    """Bind ``trace_id`` to the current async/thread context.

    Call once at the entry of a user turn / patrol tick / subagent spawn.
    Passing an empty value resets to ``"-"``.
    """
    value = str(trace_id or "").strip() or "-"
    _trace_id_var.set(value)


def get_trace_id() -> str:
    return _trace_id_var.get()


class _TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Never override an explicitly supplied trace_id (e.g. via ``extra=``).
        if not hasattr(record, "trace_id"):
            record.trace_id = _trace_id_var.get()
        return True


# ---------------------------------------------------------------------------
# Per-trace bucket handler — ADR-005 §2
# ---------------------------------------------------------------------------


class _TraceBucketHandler(logging.Handler):
    """Write each log record (with ``trace_id != "-"``) to
    ``logs/traces/<trace_id>/<service>.log``.

    Non-rotating by design: a trace is a short-lived entity (one user turn
    or one patrol tick), kept around for post-mortem grep. Stale directories
    are cleaned by the ``cleanup_old_traces`` cron / future ADR-005 §4 job;
    rotation inside a single trace bucket has no meaning.
    """

    def __init__(self, base_dir: Path, service_name: str) -> None:
        super().__init__(level=logging.DEBUG)
        self._base_dir = Path(base_dir)
        self._service_name = str(service_name or "pulse").strip() or "pulse"
        self._file_handlers: dict[str, logging.FileHandler] = {}
        self.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))

    def emit(self, record: logging.LogRecord) -> None:
        trace_id = getattr(record, "trace_id", "-")
        if not trace_id or trace_id == "-":
            return
        try:
            handler = self._get_or_create(trace_id)
            handler.emit(record)
        except Exception:  # pragma: no cover — handler must never break app
            self.handleError(record)

    def _get_or_create(self, trace_id: str) -> logging.FileHandler:
        handler = self._file_handlers.get(trace_id)
        if handler is not None:
            return handler
        directory = self._base_dir / trace_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._service_name}.log"
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(self.formatter)
        handler.addFilter(_TraceIdFilter())
        self._file_handlers[trace_id] = handler
        return handler

    def close(self) -> None:
        for handler in list(self._file_handlers.values()):
            try:
                handler.close()
            except Exception:  # pragma: no cover
                pass
        self._file_handlers.clear()
        super().close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_main_file_handler(path: Path, level: int) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        filename=str(path),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
    handler.addFilter(_TraceIdFilter())
    return handler


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def setup_logging(service_name: str = "pulse") -> None:
    """Configure logging for one Pulse process.

    ``service_name`` determines the main log filename and the per-trace
    bucket filename. Call once at process startup. Safe to call multiple
    times — existing handlers are replaced (needed under uvicorn --reload).

    Current supported services:
      * ``pulse``     — main backend (FastAPI / brain / modules)
      * ``boss_mcp`` — BOSS platform MCP gateway (child process)

    ``PULSE_LOG_DIR`` and ``PULSE_LOG_LEVEL`` are re-read on every call so
    tests can ``monkeypatch.setenv`` before invoking.
    """
    safe_service = str(service_name or "pulse").strip() or "pulse"
    # Re-read env here (not at import time) so test fixtures can redirect
    # the log tree with ``monkeypatch.setenv("PULSE_LOG_DIR", ...)``.
    log_dir = Path(os.getenv("PULSE_LOG_DIR", "logs"))
    log_level_name = os.getenv("PULSE_LOG_LEVEL", "DEBUG").upper()
    log_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = log_dir / TRACES_SUBDIR
    traces_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level_name, logging.DEBUG)
    trace_filter = _TraceIdFilter()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Replace existing handlers — uvicorn --reload triggers setup twice.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
        try:
            existing.close()
        except Exception:  # pragma: no cover
            pass

    # ── Console: WARNING+ only — terminal stays quiet ──
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATE_FMT))
    console.addFilter(trace_filter)
    root.addHandler(console)

    # ── Main time-series file: all loggers, DEBUG+, daily rotation ──
    root.addHandler(_make_main_file_handler(log_dir / f"{safe_service}.log", level))

    # ── Per-trace bucket (additive): logs/traces/<tid>/<service>.log ──
    bucket = _TraceBucketHandler(base_dir=traces_dir, service_name=safe_service)
    root.addHandler(bucket)

    # ── Suppress noisy third-party loggers ──
    for name in ("websockets", "httpcore", "httpx", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

    logging.getLogger("pulse").info(
        "logging.init service=%s dir=%s level=%s per_trace=%s",
        safe_service,
        log_dir,
        log_level_name,
        traces_dir,
    )


__all__ = [
    "setup_logging",
    "set_trace_id",
    "get_trace_id",
    "LOG_DIR",
    "TRACES_SUBDIR",
]
