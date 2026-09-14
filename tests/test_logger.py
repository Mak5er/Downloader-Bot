from __future__ import annotations

import io
import logging

from services import logger as logger_module


def test_safe_add_handler_falls_back_to_console_on_permission_error(monkeypatch):
    stream = io.StringIO()
    test_logger = logging.getLogger("maxload-test-bootstrap")
    test_logger.handlers.clear()
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    handler = logging.StreamHandler(stream)
    handler.addFilter(logger_module.ContextFilter())
    handler.setFormatter(logging.Formatter("%(message)s %(path)s %(error_type)s"))
    test_logger.addHandler(handler)

    monkeypatch.setattr(logger_module, "_base_logger", test_logger)
    log_path = "/app/logs/bot_log.log"

    def _raise_permission_error():
        raise PermissionError(13, "Permission denied", log_path)

    added = logger_module._safe_add_handler(
        _raise_permission_error,
        description="info file logger",
        path=log_path,
    )

    assert added is False
    output = stream.getvalue()
    assert "Disabled info file logger because it is not writable." in output
    assert log_path in output
    assert "PermissionError" in output


def test_third_party_loggers_route_through_app_handlers():
    logger_module._configure_third_party_loggers()

    for name, level in logger_module._THIRD_PARTY_LOG_LEVELS.items():
        third_party = logging.getLogger(name)
        assert third_party.propagate is False
        assert third_party.level == level
        for handler in logger_module._base_logger.handlers:
            assert handler in third_party.handlers


def test_sinks_disabled_env_parsing(monkeypatch):
    monkeypatch.setenv(logger_module._DISABLE_SINKS_ENV, "1")
    assert logger_module._sinks_disabled() is True
    monkeypatch.setenv(logger_module._DISABLE_SINKS_ENV, "0")
    assert logger_module._sinks_disabled() is False
    monkeypatch.delenv(logger_module._DISABLE_SINKS_ENV)
    assert logger_module._sinks_disabled() is False


def test_disable_log_sinks_detaches_and_closes_all_sinks(monkeypatch):
    # Register the env var with monkeypatch first so its mutation by
    # disable_log_sinks() is rolled back after the test.
    monkeypatch.setenv(logger_module._DISABLE_SINKS_ENV, "")

    shared_handler = logging.StreamHandler(io.StringIO())
    base = logging.getLogger("maxload-test-disable-base")
    base.handlers.clear()
    base.addHandler(shared_handler)

    third_name = "maxload-test-disable-third"
    third = logging.getLogger(third_name)
    third.handlers.clear()
    third.addHandler(shared_handler)

    monkeypatch.setattr(logger_module, "_base_logger", base)
    monkeypatch.setattr(
        logger_module,
        "_THIRD_PARTY_LOG_LEVELS",
        {third_name: logging.ERROR},
    )

    logger_module.disable_log_sinks()

    assert third.handlers == []
    assert len(base.handlers) == 1
    assert isinstance(base.handlers[0], logging.NullHandler)
    assert logger_module._sinks_disabled() is True


def test_console_filter_suppresses_telemetry_events_by_default(monkeypatch):
    monkeypatch.delenv("LOG_CONSOLE_VERBOSE", raising=False)
    stream = io.StringIO()
    handler = logger_module._build_console_handler(logging.INFO)
    # Replace stream so we can capture output
    handler.setStream(stream)

    record_event = logging.LogRecord(
        name="maxload",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="flow_started",
        args=(),
        exc_info=None,
    )
    record_event.kind = "event"
    record_event.event_name = "flow_started"

    record_perf = logging.LogRecord(
        name="maxload",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="download_probe",
        args=(),
        exc_info=None,
    )
    record_perf.kind = "perf"
    record_perf.event_name = "download_probe"

    record_app = logging.LogRecord(
        name="maxload",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="[REQUEST] user=123 | twitter | https://x.com/status/1",
        args=(),
        exc_info=None,
    )
    record_app.kind = "app"

    handler.handle(record_event)
    handler.handle(record_perf)
    handler.handle(record_app)

    output = stream.getvalue()
    assert "flow_started" not in output
    assert "download_probe" not in output
    assert "[REQUEST] user=123 | twitter | https://x.com/status/1" in output


def test_console_filter_allows_telemetry_when_verbose_enabled(monkeypatch):
    monkeypatch.setenv("LOG_CONSOLE_VERBOSE", "1")
    stream = io.StringIO()
    handler = logger_module._build_console_handler(logging.INFO)
    handler.setStream(stream)

    record_event = logging.LogRecord(
        name="maxload",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="flow_started",
        args=(),
        exc_info=None,
    )
    record_event.kind = "event"
    record_event.event_name = "flow_started"

    handler.handle(record_event)
    output = stream.getvalue()
    assert "flow_started" in output


def test_logger_download_summary_helpers():
    stream = io.StringIO()
    test_logger = logging.getLogger("maxload-test-summary")
    test_logger.handlers.clear()
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    test_logger.addHandler(handler)

    adapter = logger_module.ContextLoggerAdapter(test_logger, {})
    adapter.download_request(user_id=123, username="john_doe", service="twitter", url="https://x.com/post/1")
    adapter.download_success(user_id=123, service="twitter", file_type="video", size_bytes=2514440, duration_s=0.55, url="https://x.com/post/1")
    adapter.download_cached(user_id=123, service="twitter", file_type="video", url="https://x.com/post/1")
    adapter.download_error(user_id=123, service="twitter", error="File too large", url="https://x.com/post/1")

    output = stream.getvalue()
    assert "[REQUEST] user=123 (@john_doe) | service=twitter | url=https://x.com/post/1" in output
    assert "[SUCCESS] user=123 | twitter | video (2.40 MB in 0.55s) | https://x.com/post/1" in output
    assert "[CACHED] user=123 | twitter | video (cache hit) | https://x.com/post/1" in output
    assert "[FAILED] user=123 | twitter | error=File too large | https://x.com/post/1" in output


