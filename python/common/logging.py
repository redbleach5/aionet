"""Логирование с пробросом trace_id в каждое сообщение."""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from logging import Formatter, LogRecord

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_span_id: ContextVar[str] = ContextVar("span_id", default="-")


class _TraceFormatter(Formatter):
    DEFAULT_FMT = (
        "%(asctime)s | %(levelname)-7s | "
        "%(name)s | tid=%(trace_id)s sid=%(span_id)s | "
        "%(message)s"
    )

    def format(self, record: LogRecord) -> str:  # noqa: D401
        record.trace_id = _trace_id.get()
        record.span_id = _span_id.get()
        return super().format(record)


_CONFIGURED = False


def _ensure_handler() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_TraceFormatter(_TraceFormatter.DEFAULT_FMT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str, level: str | None = None) -> logging.Logger:
    _ensure_handler()
    logger = logging.getLogger(name)
    if level:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


class trace_context:
    """Контекстный менеджер: устанавливает trace_id/span_id для логов внутри блока."""

    def __init__(self, trace_id: str | None = None, span_id: str | None = None):
        self.trace_id = trace_id or uuid.uuid4().hex[:16]
        self.span_id = span_id or uuid.uuid4().hex[:8]
        self._tokens: list = []

    def __enter__(self):
        self._tokens.append(_trace_id.set(self.trace_id))
        self._tokens.append(_span_id.set(self.span_id))
        return self

    def __exit__(self, exc_type, exc, tb):
        for tok in reversed(self._tokens):
            tok.var.reset(tok)
        self._tokens.clear()


def new_trace() -> tuple[str, str]:
    return uuid.uuid4().hex[:16], uuid.uuid4().hex[:8]
