import asyncio
import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Iterator

import colorlog

LOG_DIR = "log"
INFO_LOG = os.path.join(LOG_DIR, "bot_log.log")
ERROR_LOG = os.path.join(LOG_DIR, "error_log.log")
EVENT_LOG = os.path.join(LOG_DIR, "events_log.jsonl")
PERF_LOG = os.path.join(LOG_DIR, "perf_log.jsonl")

os.makedirs(LOG_DIR, exist_ok=True)

_log_context: ContextVar[dict[str, Any]] = ContextVar("maxload_log_context", default={})

_DEFAULT_TEXT_FIELDS = {
    "service": "-",
    "flow": "-",
    "request_id": "-",
    "event_name": "-",
    "kind": "app",
    "user_id": "-",
    "chat_type": "-",
    "task_name": "-",
    "duration_ms": "-",
}

_STANDARD_RECORD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def _sanitize_field_key(key: str) -> str:
    if key in _STANDARD_RECORD_ATTRS:
        return f"extra_{key}"
    return key


def _sanitize_mapping(values: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in values.items():
        if value in (None, ""):
            continue
        sanitized[_sanitize_field_key(str(key))] = value
    return sanitized


class LocalTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return super().formatTime(record, datefmt=datefmt or "%Y-%m-%d %H:%M:%S")


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": getattr(record, "service", None),
            "flow": getattr(record, "flow", None),
            "request_id": getattr(record, "request_id", None),
            "event_name": getattr(record, "event_name", None),
            "kind": getattr(record, "kind", "app"),
            "user_id": getattr(record, "user_id", None),
            "chat_type": getattr(record, "chat_type", None),
            "task_name": getattr(record, "task_name", None),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        duration_ms = getattr(record, "duration_ms", None)
        if duration_ms not in (None, "-", ""):
            payload["duration_ms"] = duration_ms

        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS
            and key not in payload
            and value not in (None, "", "-")
        }
        if extra:
            payload["extra"] = extra

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = _log_context.get()
        for key, value in context.items():
            if not hasattr(record, key):
                setattr(record, key, value)

        for key, default in _DEFAULT_TEXT_FIELDS.items():
            if not hasattr(record, key):
                setattr(record, key, default)

        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if getattr(record, "task_name", "-") in (None, "-", "") and task is not None:
            record.task_name = task.get_name()

        duration_ms = getattr(record, "duration_ms", None)
        if isinstance(duration_ms, (int, float)):
            record.duration_ms = f"{duration_ms:.2f}"
        elif duration_ms in (None, ""):
            record.duration_ms = "-"

        return True


class KindFilter(logging.Filter):
    def __init__(self, allowed_kind: str) -> None:
        super().__init__()
        self.allowed_kind = allowed_kind

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "kind", "app") == self.allowed_kind


class LogContextManager:
    def __init__(self, **context: Any) -> None:
        self._context = _sanitize_mapping(context)
        self._token: Token | None = None

    def __enter__(self) -> dict[str, Any]:
        current = dict(_log_context.get())
        current.update(self._context)
        self._token = _log_context.set(current)
        return current

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _log_context.reset(self._token)


class ContextLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        merged_extra = _sanitize_mapping(dict(self.extra))
        extra = kwargs.get("extra")
        if extra:
            merged_extra.update(_sanitize_mapping(dict(extra)))
        kwargs["extra"] = merged_extra
        return msg, kwargs

    def bind(self, **extra: Any) -> "ContextLoggerAdapter":
        merged = _sanitize_mapping(dict(self.extra))
        merged.update(_sanitize_mapping(extra))
        return ContextLoggerAdapter(self.logger, merged)

    @contextmanager
    def context(self, **extra: Any) -> Iterator[dict[str, Any]]:
        with LogContextManager(**extra) as context:
            yield context

    def event(self, event_name: str, message: str | None = None, *, level: int = logging.INFO, **fields: Any) -> None:
        self.log(
            level,
            message or event_name,
            extra={
                "kind": "event",
                "event_name": event_name,
                **fields,
            },
        )

    def perf(
        self,
        event_name: str,
        *,
        duration_ms: float,
        message: str | None = None,
        level: int = logging.INFO,
        **fields: Any,
    ) -> None:
        self.log(
            level,
            message or event_name,
            extra={
                "kind": "perf",
                "event_name": event_name,
                "duration_ms": duration_ms,
                **fields,
            },
        )


CONSOLE_FORMAT = (
    "%(log_color)s%(asctime)s | %(levelname)s | %(name)s | "
    "%(service)s | %(flow)s | %(request_id)s | %(message)s%(reset)s"
)
FILE_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | "
    "service=%(service)s flow=%(flow)s request_id=%(request_id)s "
    "event=%(event_name)s kind=%(kind)s user_id=%(user_id)s chat_type=%(chat_type)s "
    "task=%(task_name)s duration_ms=%(duration_ms)s | %(message)s"
)


def _build_console_handler(level: int) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = colorlog.ColoredFormatter(
        CONSOLE_FORMAT,
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())
    return handler


def _build_file_handler(path: str, level: int) -> logging.Handler:
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setLevel(level)
    formatter = LocalTimeFormatter(FILE_FORMAT)
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())
    return handler


def _build_json_handler(path: str, kind: str) -> logging.Handler:
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(JsonLinesFormatter())
    handler.addFilter(ContextFilter())
    handler.addFilter(KindFilter(kind))
    return handler


_base_logger = logging.getLogger("maxload")
_base_logger.setLevel(logging.DEBUG)
_base_logger.propagate = False
_base_logger.handlers.clear()

_base_logger.addHandler(_build_console_handler(logging.INFO))
_base_logger.addHandler(_build_file_handler(INFO_LOG, logging.INFO))
_base_logger.addHandler(_build_file_handler(ERROR_LOG, logging.ERROR))
_base_logger.addHandler(_build_json_handler(EVENT_LOG, "event"))
_base_logger.addHandler(_build_json_handler(PERF_LOG, "perf"))

logger = ContextLoggerAdapter(_base_logger, {})


def set_log_context(**context: Any) -> Token:
    current = dict(_log_context.get())
    current.update(_sanitize_mapping(context))
    return _log_context.set(current)


def reset_log_context(token: Token) -> None:
    _log_context.reset(token)


def get_log_context() -> dict[str, Any]:
    return dict(_log_context.get())


def _configure_third_party_loggers() -> None:
    noisy_loggers = {
        "aiogram": logging.ERROR,
        "aiogram.event": logging.CRITICAL,
        "aiosqlite": logging.ERROR,
        "httpcore": logging.WARNING,
        "httpx": logging.WARNING,
        "asyncio": logging.WARNING,
    }

    for name, level in noisy_loggers.items():
        third_party_logger = logging.getLogger(name)
        third_party_logger.setLevel(level)
        third_party_logger.propagate = False


_configure_third_party_loggers()
