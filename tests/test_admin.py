import os
from types import SimpleNamespace

import pytest

from handlers import admin


@pytest.mark.asyncio
async def test_clear_downloads_and_notify_removes_files(tmp_path, monkeypatch):
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "file1.txt").write_text("data")
    (folder / "file2.txt").write_text("data")

    sent_messages = []

    async def fake_send_message(chat_id, text):
        sent_messages.append((chat_id, text))

    monkeypatch.setattr(admin, "OUTPUT_DIR", str(folder))
    monkeypatch.setattr(admin, "ADMINS_UID", [1, 2])
    monkeypatch.setattr(admin, "bot", SimpleNamespace(send_message=fake_send_message))

    await admin.clear_downloads_and_notify()

    assert not any(path.is_file() for path in folder.iterdir())
    assert len(sent_messages) == 2
    assert "successfully cleared" in sent_messages[0][1]
