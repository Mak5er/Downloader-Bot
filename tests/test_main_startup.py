from types import SimpleNamespace
from unittest.mock import AsyncMock

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
