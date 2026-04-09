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

