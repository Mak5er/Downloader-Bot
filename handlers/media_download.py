import asyncio
from typing import Optional

from aiogram import types

import keyboards as kb
import messages as bm
from config import (
    BATCH_LINKS_MAX_CONCURRENCY,
    BATCH_LINKS_MAX_ITEMS,
    BATCH_LINKS_MIN_CONCURRENCY,
    BATCH_LINKS_PARALLEL_ACTIVE_JOBS_THRESHOLD,
    BATCH_LINKS_PARALLEL_QUEUE_DEPTH_THRESHOLD,
)
from handlers.utils import get_bot_username, get_message_text, safe_delete_message
from services.logger import logger as logging, summarize_url_for_log
from services.stats.constants import SERVICE_DISPLAY_NAMES
from services.inline.album_links import get_inline_album_request
from services.links.detection import extract_supported_link, extract_supported_links
import handlers.user as user_mod

logging = logging.bind(service="media_download")


async def _process_inline_album_deeplink(message: types.Message, payload: str) -> bool:
    if not payload.startswith("album_"):
        return False
    token = payload.removeprefix("album_").strip()
    if not token:
        return False

    request = get_inline_album_request(token)
    if not request:
        await message.reply(bm.inline_album_link_invalid())
        return True

    try:
        if request.service == "instagram":
            from handlers import instagram
            await instagram.process_instagram(message, direct_url=request.url)
            return True
        if request.service == "threads":
            from handlers import threads
            await threads.process_threads(message, direct_url=request.url)
            return True
        if request.service == "tiktok":
            from handlers import tiktok
            await tiktok.process_tiktok(message, direct_url=request.url)
            return True
        if request.service == "pinterest":
            from handlers import pinterest
            await pinterest.process_pinterest(message, direct_url=request.url)
            return True
        if request.service == "twitter":
            from handlers import twitter
            await twitter.handle_tweet_links(message, direct_url=request.url)
            return True
    except Exception:
        logging.exception(
            "Failed to process inline album deeplink: user_id=%s service=%s url=%s",
            message.from_user.id,
            request.service,
            summarize_url_for_log(request.url),
        )
        await message.reply(bm.something_went_wrong())
        return True

    await message.reply(bm.inline_album_link_invalid())
    return True


async def _process_pending_message(message: types.Message) -> None:
    text = get_message_text(message)
    detected = extract_supported_link(text)
    if not detected:
        return
    service, url = detected
    await user_mod._process_supported_link(message, service, url)


async def _process_supported_link(message: types.Message, service: str, url: str) -> None:
    if service == "tiktok":
        from handlers import tiktok
        await tiktok.process_tiktok(message, direct_url=url)
        return

    if service == "instagram":
        from handlers import instagram
        await instagram.process_instagram_url(message, url=url)
        return

    if service == "threads":
        from handlers import threads
        await threads.process_threads_url(message, url=url)
        return

    if service == "soundcloud":
        from handlers import soundcloud
        await soundcloud.process_soundcloud_url(message, url=url)
        return

    if service == "spotify":
        from handlers import spotify
        await spotify.process_spotify_url(message, url=url)
        return

    if service == "pinterest":
        from handlers import pinterest
        await pinterest.process_pinterest_url(message, url=url)
        return

    if service == "youtube":
        from handlers import youtube
        if "music.youtube." in url.lower():
            await youtube.download_music(message, direct_url=url)
        else:
            await youtube.download_video(message, direct_url=url)
        return

    if service == "twitter":
        from handlers import twitter
        await twitter.handle_tweet_links(message, direct_url=url)


def _has_multiple_supported_links(message: types.Message) -> bool:
    return len(extract_supported_links(get_message_text(message))) > 1


def _resolve_batch_concurrency() -> int:
    min_concurrency = max(1, int(BATCH_LINKS_MIN_CONCURRENCY))
    max_concurrency = max(min_concurrency, int(BATCH_LINKS_MAX_CONCURRENCY))
    load = user_mod.get_download_queue().load_snapshot()
    if (
        load.queued_jobs > int(BATCH_LINKS_PARALLEL_QUEUE_DEPTH_THRESHOLD)
        or load.active_jobs >= int(BATCH_LINKS_PARALLEL_ACTIVE_JOBS_THRESHOLD)
    ):
        return min_concurrency
    return max_concurrency


async def process_batch_links(message: types.Message):
    links = extract_supported_links(get_message_text(message))
    if len(links) <= 1:
        return

    selected_links = links[:user_mod._MAX_BATCH_LINKS]
    await user_mod.send_analytics(
        user_id=message.from_user.id,
        chat_type=message.chat.type,
        action_name="batch_links",
    )
    await user_mod.update_info(message)

    concurrency = min(len(selected_links), user_mod._resolve_batch_concurrency())
    status_message = await message.answer(
        bm.batch_links_started(len(selected_links), len(links)),
        parse_mode="HTML",
    )
    try:
        if concurrency > 1:
            await safe_delete_message(status_message)
            await user_mod._process_batch_links_parallel(message, selected_links, concurrency)
            status_message = await message.answer(bm.batch_links_finished(len(selected_links)))
            return

        for index, (service, url) in enumerate(selected_links, start=1):
            service_name = SERVICE_DISPLAY_NAMES.get(service, service.title())
            await safe_delete_message(status_message)
            status_message = await message.answer(
                bm.batch_link_progress(index, len(selected_links), service_name),
            )
            try:
                await user_mod._process_supported_link(message, service, url)
            except Exception as exc:
                logging.exception(
                    "Batch link failed: user_id=%s service=%s url=%s error=%s",
                    message.from_user.id,
                    service,
                    summarize_url_for_log(url),
                    exc,
                )
                await message.reply(bm.something_went_wrong())

        await safe_delete_message(status_message)
        status_message = await message.answer(bm.batch_links_finished(len(selected_links)))
    finally:
        await asyncio.sleep(2)
        await safe_delete_message(status_message)


async def _process_batch_links_parallel(
    message: types.Message,
    links: list[tuple[str, str]],
    concurrency: int,
) -> None:
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    total = len(links)

    async def _run_one(index: int, service: str, url: str) -> None:
        async with semaphore:
            service_name = SERVICE_DISPLAY_NAMES.get(service, service.title())
            status_message = await message.answer(
                bm.batch_link_progress(index, total, service_name),
            )
            try:
                await user_mod._process_supported_link(message, service, url)
            except Exception as exc:
                logging.exception(
                    "Parallel batch link failed: user_id=%s service=%s url=%s error=%s",
                    message.from_user.id,
                    service,
                    summarize_url_for_log(url),
                    exc,
                )
                await message.reply(bm.something_went_wrong())
            finally:
                await safe_delete_message(status_message)

    await asyncio.gather(
        *(_run_one(index, service, url) for index, (service, url) in enumerate(links, start=1))
    )


async def show_supported_sites(call: types.CallbackQuery):
    bot_username = await get_bot_username(user_mod.bot)
    await call.message.edit_text(
        bm.help_message(bot_username),
        reply_markup=kb.start_keyboard(bot_username, ref_user_id=call.from_user.id),
        parse_mode="HTML",
    )
    await call.answer()
