import asyncio
from types import SimpleNamespace

import pytest

from services.media import orchestration
from utils.download_manager import DownloadMetrics


class _FakeDb:
    def __init__(self) -> None:
        self.file_ids: dict[str, str] = {}

    async def get_file_id(self, url: str):
        return self.file_ids.get(url)

    async def add_file(self, url: str, file_id: str, _file_type: str):
        self.file_ids[url] = file_id


@pytest.mark.asyncio
async def test_run_single_media_flow_reuses_inflight_result_for_duplicate_requests():
    orchestration.reset_single_media_flow_tracking()
    db = _FakeDb()
    cache_key = "https://tiktok.com/@demo/video/123"
    download_started = asyncio.Event()
    allow_first_download_to_finish = asyncio.Event()
    call_counts = {
        "leader_download": 0,
        "follower_download": 0,
        "leader_send_downloaded": 0,
        "follower_send_downloaded": 0,
        "leader_send_cached": 0,
        "follower_send_cached": 0,
        "cleanup": 0,
    }

    async def _run_flow(label: str):
        async def _noop(*_args, **_kwargs):
            return None

        async def _download_media():
            call_counts[f"{label}_download"] += 1
            if label == "leader":
                download_started.set()
                await allow_first_download_to_finish.wait()
                return DownloadMetrics(
                    url=cache_key,
                    path="downloads/demo.mp4",
                    size=1024,
                    elapsed=1.0,
                    used_multipart=False,
                    resumed=False,
                )
            raise AssertionError("follower must not start a second download")

        async def _send_cached(file_id: str):
            call_counts[f"{label}_send_cached"] += 1
            return {"mode": "cached", "file_id": file_id, "label": label}

        async def _send_downloaded(path: str):
            call_counts[f"{label}_send_downloaded"] += 1
            return {
                "mode": "downloaded",
                "path": path,
                "label": label,
                "video": SimpleNamespace(file_id="telegram-file-id-1"),
            }

        async def _cleanup_path(_path: str):
            call_counts["cleanup"] += 1

        return await orchestration.run_single_media_flow(
            cache_key=cache_key,
            cache_file_type="video",
            db_service=db,
            upload_status_text="Uploading...",
            upload_action="upload_video",
            update_status=_noop,
            send_chat_action=_noop,
            send_cached=_send_cached,
            download_media=_download_media,
            send_downloaded=_send_downloaded,
            extract_file_id=lambda sent: getattr(sent.get("video"), "file_id", None),
            cleanup_path=_cleanup_path,
            delete_status_message=_noop,
            on_missing_media=_noop,
        )

    leader_task = asyncio.create_task(_run_flow("leader"))
    await download_started.wait()
    follower_task = asyncio.create_task(_run_flow("follower"))
    await asyncio.sleep(0)
    allow_first_download_to_finish.set()

    leader_result, follower_result = await asyncio.gather(leader_task, follower_task)

    assert leader_result["mode"] == "downloaded"
    assert follower_result == {
        "mode": "cached",
        "file_id": "telegram-file-id-1",
        "label": "follower",
    }
    assert db.file_ids[cache_key] == "telegram-file-id-1"
    assert call_counts == {
        "leader_download": 1,
        "follower_download": 0,
        "leader_send_downloaded": 1,
        "follower_send_downloaded": 0,
        "leader_send_cached": 0,
        "follower_send_cached": 1,
        "cleanup": 1,
    }
