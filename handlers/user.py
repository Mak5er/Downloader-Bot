import asyncio
import time
from copy import copy
from typing import Optional

from aiogram import types, Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated

import keyboards as kb
import messages as bm
from config import (
    BATCH_LINKS_MAX_CONCURRENCY,
    BATCH_LINKS_MAX_ITEMS,
    BATCH_LINKS_MIN_CONCURRENCY,
    BATCH_LINKS_PARALLEL_ACTIVE_JOBS_THRESHOLD,
    BATCH_LINKS_PARALLEL_QUEUE_DEPTH_THRESHOLD,
)
from handlers.utils import get_message_text, safe_delete_message
from services.logger import logger as logging, summarize_url_for_log
from app_context import db, send_analytics, bot
from services.download.queue import get_download_queue
from services.settings import parse_setting_toggle_callback, parse_settings_view_callback
from services.stats.constants import (
    SERVICE_DISPLAY_NAMES,
)
from services.stats.chart import (
    _render_stats,
    _send_stats_photo,
    _handle_stats_update,
)
from services.inline.album_links import get_inline_album_request
from services.links.detection import extract_supported_link, extract_supported_links
from services.runtime.pending_requests import pop_pending

logging = logging.bind(service="user")

router = Router()

_UPDATE_INFO_TTL_SECONDS = 120.0
_update_info_cache: dict[int, tuple[float, str, Optional[str]]] = {}
_MAX_BATCH_LINKS = max(1, int(BATCH_LINKS_MAX_ITEMS))
_MESSAGE_NOT_MODIFIED_MARKERS = ("message is not modified", "specified new message content and reply markup are exactly the same")


def _admin_statuses() -> set[ChatMemberStatus]:
    statuses = {ChatMemberStatus.ADMINISTRATOR}
    owner = getattr(ChatMemberStatus, "OWNER", None)
    creator = getattr(ChatMemberStatus, "CREATOR", None)
    if owner:
        statuses.add(owner)
    if creator:
        statuses.add(creator)
    return statuses


async def _is_group_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in _admin_statuses()


def _is_message_not_modified_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _MESSAGE_NOT_MODIFIED_MARKERS)


def _settings_chat_name(chat: types.Chat) -> str:
    title = getattr(chat, "title", None) or getattr(chat, "full_name", None)
    if title:
        return title
    first_name = getattr(chat, "first_name", None)
    last_name = getattr(chat, "last_name", None)
    name_parts = [part for part in (first_name, last_name) if part]
    if name_parts:
        return " ".join(name_parts)
    return f"Chat {chat.id}"


async def _ensure_settings_entities(
    message: types.Message | None,
    actor: types.User | None,
) -> None:
    if actor and not getattr(actor, "is_bot", False):
        await db.upsert_chat(
            user_id=actor.id,
            user_name=getattr(actor, "full_name", None) or getattr(actor, "username", None) or str(actor.id),
            user_username=getattr(actor, "username", None),
            chat_type="private",
            language=getattr(actor, "language_code", None),
            status="active",
        )

    if message and message.chat and message.chat.type != ChatType.PRIVATE:
        chat = message.chat
        await db.upsert_chat(
            user_id=chat.id,
            user_name=_settings_chat_name(chat),
            user_username=getattr(chat, "username", None),
            chat_type="public",
            language=getattr(chat, "language_code", None),
            status="active",
        )


async def update_info(message: types.Message):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    user_username = message.from_user.username
    language = getattr(message.from_user, "language_code", None)

    now = time.monotonic()
    cached = _update_info_cache.get(user_id)
    if cached and now - cached[0] <= _UPDATE_INFO_TTL_SECONDS:
        if cached[1] == user_name and cached[2] == user_username:
            return

    await db.upsert_chat(
        user_id=user_id,
        user_name=user_name,
        user_username=user_username,
        chat_type="private",
        language=language,
        status="active",
    )
    _update_info_cache[user_id] = (now, user_name, user_username)


@router.message(Command("start"))
async def send_welcome(message: types.Message):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name='start')
    await update_info(message)

    if message.chat.type == ChatType.PRIVATE:
        payload = _extract_start_payload(get_message_text(message))
        if payload and await _process_inline_album_deeplink(message, payload):
            return

    await message.reply(
        bm.welcome_message(),
        reply_markup=kb.start_keyboard(),
        parse_mode="HTML",
    )

    if message.chat.type == ChatType.PRIVATE:
        pending = pop_pending(message.from_user.id)
        if pending:
            try:
                await bot.delete_message(pending.notice_chat_id, pending.notice_message_id)
            except Exception:
                pass
            await _process_pending_message(_build_pending_private_message(message, pending.url))


@router.message(Command("help"))
async def send_help(message: types.Message):
    await message.reply(
        bm.supported_sites_message(),
        reply_markup=kb.start_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "start_supported_sites")
async def show_supported_sites(call: types.CallbackQuery):
    await call.message.edit_text(
        bm.supported_sites_message(),
        reply_markup=kb.start_keyboard(),
        parse_mode="HTML",
    )
    await call.answer()


def _build_pending_private_message(message: types.Message, pending_text: str) -> types.Message:
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"text": pending_text, "caption": None})

    replayed_message = copy(message)
    replayed_message.text = pending_text
    replayed_message.caption = None
    return replayed_message


def _extract_start_payload(text: str) -> Optional[str]:
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    if not parts[0].startswith("/start"):
        return None
    if len(parts) < 2:
        return None
    return parts[1].strip() or None


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


@router.my_chat_member()
async def handle_bot_membership(update: ChatMemberUpdated):
    chat = update.chat
    new_status = update.new_chat_member.status
    old_status = getattr(update.old_chat_member, "status", None)

    if new_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        chat_id = chat.id
        chat_type_value = "private" if chat.type == ChatType.PRIVATE else "public"
        chat_name = chat.title or getattr(chat, "full_name", None) or f"Chat {chat_id}"
        chat_username = getattr(chat, "username", None)
        language = getattr(chat, "language_code", None)

        await db.upsert_chat(
            user_id=chat_id,
            user_name=chat_name,
            user_username=chat_username,
            chat_type=chat_type_value,
            language=language,
            status="active",
        )
        chat_title = chat.title or chat_name

        became_member = old_status not in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
        became_admin = new_status == ChatMemberStatus.ADMINISTRATOR and old_status != ChatMemberStatus.ADMINISTRATOR

        if chat.type != ChatType.PRIVATE:
            if became_member:
                await bot.send_message(
                    chat_id=chat_id,
                    text=bm.join_group(chat_title),
                    parse_mode="HTML",
                )

            if became_admin:
                await bot.send_message(
                    chat_id=chat_id,
                    text=bm.admin_rights_granted(chat_title),
                    parse_mode="HTML",
                )

    elif new_status in {ChatMemberStatus.KICKED, ChatMemberStatus.LEFT, ChatMemberStatus.RESTRICTED}:
        await db.set_inactive(update.chat.id)


@router.message(Command("remove_keyboard"))
async def remove_reply_keyboard(message: types.Message):
    await message.reply(text=bm.keyboard_removed(), reply_markup=types.ReplyKeyboardRemove())


async def _process_pending_message(message: types.Message) -> None:
    text = get_message_text(message)
    detected = extract_supported_link(text)
    if not detected:
        return
    service, url = detected
    await _process_supported_link(message, service, url)


async def _process_supported_link(message: types.Message, service: str, url: str) -> None:
    if service == "tiktok":
        from handlers import tiktok
        await tiktok.process_tiktok(message, direct_url=url)
        return

    if service == "instagram":
        from handlers import instagram
        await instagram.process_instagram_url(message, url=url)
        return

    if service == "soundcloud":
        from handlers import soundcloud
        await soundcloud.process_soundcloud_url(message, url=url)
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
    load = get_download_queue().load_snapshot()
    if (
        load.queued_jobs > int(BATCH_LINKS_PARALLEL_QUEUE_DEPTH_THRESHOLD)
        or load.active_jobs >= int(BATCH_LINKS_PARALLEL_ACTIVE_JOBS_THRESHOLD)
    ):
        return min_concurrency
    return max_concurrency


@router.message(_has_multiple_supported_links)
async def process_batch_links(message: types.Message):
    links = extract_supported_links(get_message_text(message))
    if len(links) <= 1:
        return

    selected_links = links[:_MAX_BATCH_LINKS]
    await send_analytics(
        user_id=message.from_user.id,
        chat_type=message.chat.type,
        action_name="batch_links",
    )
    await update_info(message)

    concurrency = min(len(selected_links), _resolve_batch_concurrency())
    status_message = await message.answer(
        bm.batch_links_started(len(selected_links), len(links)),
        parse_mode="HTML",
    )
    try:
        if concurrency > 1:
            await safe_delete_message(status_message)
            await _process_batch_links_parallel(message, selected_links, concurrency)
            status_message = await message.answer(bm.batch_links_finished(len(selected_links)))
            return

        for index, (service, url) in enumerate(selected_links, start=1):
            service_name = SERVICE_DISPLAY_NAMES.get(service, service.title())
            await safe_delete_message(status_message)
            status_message = await message.answer(
                bm.batch_link_progress(index, len(selected_links), service_name),
            )
            try:
                await _process_supported_link(message, service, url)
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
                await _process_supported_link(message, service, url)
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


@router.message(Command("settings"))
async def settings_menu(message: types.Message):
    await send_analytics(user_id=message.from_user.id,
                         chat_type=message.chat.type,
                         action_name='settings')
    if message.chat.type != "private":
        is_admin = await _is_group_admin(message.chat.id, message.from_user.id)
        if not is_admin:
            await message.reply(bm.settings_admin_only())
            return
    await message.reply(
        text=bm.settings(),
        reply_markup=kb.return_settings_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == 'back_to_settings')
async def back_to_settings(call: types.CallbackQuery):
    await call.message.edit_text(
        text=bm.settings(),
        reply_markup=kb.return_settings_keyboard(),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("settings:"))
async def open_setting(call: types.CallbackQuery):
    field = parse_settings_view_callback(call.data)
    if field is None:
        await call.answer(bm.invalid_settings_option(), show_alert=True)
        return
    if call.message and call.message.chat.type != "private":
        is_admin = await _is_group_admin(call.message.chat.id, call.from_user.id)
        if not is_admin:
            await call.answer(bm.settings_admin_only(), show_alert=True)
            return
        target_id = call.message.chat.id
    else:
        target_id = call.from_user.id

    try:
        await _ensure_settings_entities(call.message, call.from_user)
        current_value = await db.get_user_setting(user_id=target_id, field=field)
        keyboard = kb.return_field_keyboard(field, current_value)

        await call.message.edit_text(
            text=bm.get_field_text(field),
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await call.answer()
    except Exception as exc:
        logging.exception(
            "Failed to open settings field: field=%s user_id=%s chat_id=%s error=%s",
            field,
            getattr(call.from_user, "id", None),
            getattr(getattr(call.message, "chat", None), "id", None),
            exc,
        )
        await call.answer(bm.something_went_wrong(), show_alert=True)


@router.callback_query(F.data.startswith("setting:"))
async def change_setting(call: types.CallbackQuery):
    setting_payload = parse_setting_toggle_callback(call.data)
    if setting_payload is None:
        await call.answer(bm.invalid_settings_option(), show_alert=True)
        return
    field, value = setting_payload
    if call.message and call.message.chat.type != "private":
        is_admin = await _is_group_admin(call.message.chat.id, call.from_user.id)
        if not is_admin:
            await call.answer(bm.settings_admin_only(), show_alert=True)
            return
        target_id = call.message.chat.id
    else:
        target_id = call.from_user.id

    try:
        await _ensure_settings_entities(call.message, call.from_user)
        await db.set_user_setting(user_id=target_id, field=field, value=value)
    except ValueError:
        await call.answer(bm.invalid_settings_option(), show_alert=True)
        return
    except Exception as exc:
        logging.exception(
            "Failed to change setting: field=%s value=%s user_id=%s chat_id=%s error=%s",
            field,
            value,
            getattr(call.from_user, "id", None),
            getattr(getattr(call.message, "chat", None), "id", None),
            exc,
        )
        await call.answer(bm.something_went_wrong(), show_alert=True)
        return

    try:
        current_value = await db.get_user_setting(user_id=target_id, field=field)
        keyboard = kb.return_field_keyboard(field, current_value)

        await call.message.edit_reply_markup(reply_markup=keyboard)
        await call.answer()
    except TelegramBadRequest as exc:
        if _is_message_not_modified_error(exc):
            logging.info(
                "Settings keyboard already up to date: field=%s user_id=%s chat_id=%s",
                field,
                getattr(call.from_user, "id", None),
                getattr(getattr(call.message, "chat", None), "id", None),
            )
            await call.answer()
            return
        logging.exception(
            "Failed to refresh settings keyboard: field=%s user_id=%s chat_id=%s error=%s",
            field,
            getattr(call.from_user, "id", None),
            getattr(getattr(call.message, "chat", None), "id", None),
            exc,
        )
        await call.answer("Couldn't update settings right now. Please try again later.", show_alert=True)
    except Exception as exc:
        logging.exception(
            "Failed to refresh settings keyboard: field=%s user_id=%s chat_id=%s error=%s",
            field,
            getattr(call.from_user, "id", None),
            getattr(getattr(call.message, "chat", None), "id", None),
            exc,
        )
        await call.answer("Couldn't update settings right now. Please try again later.", show_alert=True)


@router.callback_query(F.data == "noop")
async def noop_callback(call: types.CallbackQuery):
    await call.answer()


@router.message(Command("stats"))
async def stats_command(message: types.Message):
    period = "Week"
    mode = "total"
    try:
        chart_bytes, caption = await _render_stats(period, mode)
        await _send_stats_photo(message, period, mode, chart_bytes, caption)
    except Exception:
        await message.answer(bm.stats_temporarily_unavailable())
        logging.exception("Error handling /stats")


@router.callback_query(F.data.startswith("stats:"))
async def switch_stats(call: types.CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer()
        return

    _, period, mode = parts
    await _handle_stats_update(call, period, mode)


@router.callback_query(F.data.startswith("date_"))
async def switch_period(call: types.CallbackQuery):
    period = call.data.split("_")[1]
    await _handle_stats_update(call, period, "total")
