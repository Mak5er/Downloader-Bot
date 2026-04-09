import re
import secrets
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import types

from services.logger import logger as logging

T = TypeVar("T")
FAsync = TypeVar("FAsync", bound=Callable[..., Awaitable[Any]])


def build_request_id(prefix: str, *parts: Any) -> str:
    normalized_parts: list[str] = []
    for part in parts:
        if part in (None, ""):
            continue
        value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(part)).strip("-")
        if value:
            normalized_parts.append(value[:24])

    if not normalized_parts:
        normalized_parts.append(secrets.token_hex(4))
    return f"{prefix}-{'-'.join(normalized_parts[:4])}"


@contextmanager
def log_context_scope(**context: Any):
    with logging.context(**context):
        yield


def log_duration(event_name: str, started_at: float, **fields: Any) -> None:
    logging.perf(
        event_name,
        duration_ms=(time.perf_counter() - started_at) * 1000.0,
        **fields,
    )


def with_message_logging(service: str, flow: str) -> Callable[[FAsync], FAsync]:
    def decorator(func: FAsync) -> FAsync:
        @wraps(func)
        async def wrapper(message: types.Message, *args: Any, **kwargs: Any):
            request_id = build_request_id(
                f"{service}-{flow}",
                getattr(getattr(message, "from_user", None), "id", None),
                getattr(message, "chat", None).id if getattr(message, "chat", None) else None,
                getattr(message, "message_id", None),
            )
            started_at = time.perf_counter()
            with logging.context(
                service=service,
                flow=flow,
                request_id=request_id,
                user_id=getattr(getattr(message, "from_user", None), "id", None),
                chat_type=getattr(getattr(message, "chat", None), "type", None),
            ):
                logging.event("flow_started", entrypoint=func.__name__)
                try:
                    result = await func(message, *args, **kwargs)
                    logging.event("flow_completed", entrypoint=func.__name__)
                    return result
                except Exception:
                    logging.event("flow_failed", level=40, entrypoint=func.__name__)
                    raise
                finally:
                    log_duration("flow_total", started_at, entrypoint=func.__name__)

        return wrapper  # type: ignore[return-value]

    return decorator


def with_inline_query_logging(service: str, flow: str) -> Callable[[FAsync], FAsync]:
    def decorator(func: FAsync) -> FAsync:
        @wraps(func)
        async def wrapper(query: types.InlineQuery, *args: Any, **kwargs: Any):
            request_id = build_request_id(
                f"{service}-{flow}",
                getattr(getattr(query, "from_user", None), "id", None),
                getattr(query, "id", None),
            )
            started_at = time.perf_counter()
            with logging.context(
                service=service,
                flow=flow,
                request_id=request_id,
                user_id=getattr(getattr(query, "from_user", None), "id", None),
                chat_type=getattr(query, "chat_type", None),
            ):
                logging.event("flow_started", entrypoint=func.__name__)
                try:
                    result = await func(query, *args, **kwargs)
                    logging.event("flow_completed", entrypoint=func.__name__)
                    return result
                except Exception:
                    logging.event("flow_failed", level=40, entrypoint=func.__name__)
                    raise
                finally:
                    log_duration("flow_total", started_at, entrypoint=func.__name__)

        return wrapper  # type: ignore[return-value]

    return decorator


def with_callback_logging(service: str, flow: str) -> Callable[[FAsync], FAsync]:
    def decorator(func: FAsync) -> FAsync:
        @wraps(func)
        async def wrapper(call: types.CallbackQuery, *args: Any, **kwargs: Any):
            request_id = build_request_id(
                f"{service}-{flow}",
                getattr(getattr(call, "from_user", None), "id", None),
                getattr(call, "id", None),
            )
            started_at = time.perf_counter()
            with logging.context(
                service=service,
                flow=flow,
                request_id=request_id,
                user_id=getattr(getattr(call, "from_user", None), "id", None),
                chat_type=getattr(getattr(call, "message", None), "chat", None).type
                if getattr(getattr(call, "message", None), "chat", None)
                else None,
                inline_message_id=getattr(call, "inline_message_id", None),
            ):
                logging.event("flow_started", entrypoint=func.__name__)
                try:
                    result = await func(call, *args, **kwargs)
                    logging.event("flow_completed", entrypoint=func.__name__)
                    return result
                except Exception:
                    logging.event("flow_failed", level=40, entrypoint=func.__name__)
                    raise
                finally:
                    log_duration("flow_total", started_at, entrypoint=func.__name__)

        return wrapper  # type: ignore[return-value]

    return decorator


def with_chosen_inline_logging(service: str, flow: str) -> Callable[[FAsync], FAsync]:
    def decorator(func: FAsync) -> FAsync:
        @wraps(func)
        async def wrapper(result: types.ChosenInlineResult, *args: Any, **kwargs: Any):
            request_id = build_request_id(
                f"{service}-{flow}",
                getattr(getattr(result, "from_user", None), "id", None),
                getattr(result, "result_id", None),
            )
            started_at = time.perf_counter()
            with logging.context(
                service=service,
                flow=flow,
                request_id=request_id,
                user_id=getattr(getattr(result, "from_user", None), "id", None),
                inline_message_id=getattr(result, "inline_message_id", None),
            ):
                logging.event("flow_started", entrypoint=func.__name__)
                try:
                    outcome = await func(result, *args, **kwargs)
                    logging.event("flow_completed", entrypoint=func.__name__)
                    return outcome
                except Exception:
                    logging.event("flow_failed", level=40, entrypoint=func.__name__)
                    raise
                finally:
                    log_duration("flow_total", started_at, entrypoint=func.__name__)

        return wrapper  # type: ignore[return-value]

    return decorator


def with_inline_send_logging(service: str, flow: str) -> Callable[[FAsync], FAsync]:
    def decorator(func: FAsync) -> FAsync:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any):
            token = kwargs.get("token")
            inline_message_id = kwargs.get("inline_message_id")
            request_event_id = kwargs.get("request_event_id")
            request_id = build_request_id(f"{service}-{flow}", token, request_event_id)
            started_at = time.perf_counter()
            with logging.context(
                service=service,
                flow=flow,
                request_id=request_id,
                inline_message_id=inline_message_id,
            ):
                logging.event("flow_started", entrypoint=func.__name__)
                try:
                    outcome = await func(*args, **kwargs)
                    logging.event("flow_completed", entrypoint=func.__name__)
                    return outcome
                except Exception:
                    logging.event("flow_failed", level=40, entrypoint=func.__name__)
                    raise
                finally:
                    log_duration("flow_total", started_at, entrypoint=func.__name__)

        return wrapper  # type: ignore[return-value]

    return decorator
