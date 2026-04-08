from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main as main_module


@pytest.mark.asyncio
async def test_main_closes_resources_when_init_fails(monkeypatch):
    monkeypatch.setattr(main_module.bot, "get_me", AsyncMock(return_value=SimpleNamespace(username="TestBot")))
    monkeypatch.setattr(main_module.db, "init_db", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(main_module, "start_analytics_workers", AsyncMock())
    monkeypatch.setattr(main_module, "stop_analytics_workers", AsyncMock())
    monkeypatch.setattr(main_module, "shutdown_download_queue", AsyncMock())
    monkeypatch.setattr(main_module, "close_http_session", AsyncMock())
    monkeypatch.setattr(main_module.session, "close", AsyncMock())
    monkeypatch.setattr(main_module, "set_app_context", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="boom"):
        await main_module.main()

    main_module.start_analytics_workers.assert_not_awaited()
    main_module.stop_analytics_workers.assert_not_awaited()
    main_module.shutdown_download_queue.assert_awaited_once()
    main_module.close_http_session.assert_awaited_once()
    main_module.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_applies_polling_backpressure_settings(monkeypatch):
    monkeypatch.setattr(main_module, "BOT_POLLING_TASKS_CONCURRENCY_LIMIT", 123)
    monkeypatch.setattr(main_module.bot, "get_me", AsyncMock(return_value=SimpleNamespace(username="TestBot")))
    monkeypatch.setattr(main_module.bot, "set_my_commands", AsyncMock())
    monkeypatch.setattr(main_module.bot, "delete_webhook", AsyncMock())
    monkeypatch.setattr(main_module.db, "init_db", AsyncMock())
    monkeypatch.setattr(main_module, "start_analytics_workers", AsyncMock())
    monkeypatch.setattr(main_module, "stop_analytics_workers", AsyncMock())
    monkeypatch.setattr(main_module, "shutdown_download_queue", AsyncMock())
    monkeypatch.setattr(main_module, "close_http_session", AsyncMock())
    monkeypatch.setattr(main_module.session, "close", AsyncMock())
    monkeypatch.setattr(main_module, "set_app_context", lambda **_kwargs: None)
    monkeypatch.setattr(main_module, "crontab", Mock())
    monkeypatch.setattr(main_module.dp, "include_router", Mock())
    monkeypatch.setattr(main_module.dp.message, "outer_middleware", Mock())
    monkeypatch.setattr(main_module.dp.callback_query, "outer_middleware", Mock())
    monkeypatch.setattr(main_module.dp.inline_query, "outer_middleware", Mock())
    monkeypatch.setattr(main_module.dp, "resolve_used_update_types", Mock(return_value=["message", "callback_query"]))
    monkeypatch.setattr(main_module.dp, "start_polling", AsyncMock(side_effect=RuntimeError("stop-polling")))

    with pytest.raises(RuntimeError, match="stop-polling"):
        await main_module.main()

    main_module.dp.start_polling.assert_awaited_once_with(
        main_module.bot,
        allowed_updates=["message", "callback_query"],
        tasks_concurrency_limit=123,
    )
