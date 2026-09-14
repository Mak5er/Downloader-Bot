from __future__ import annotations

import asyncio
import re
import uuid
from typing import Optional

from aiogram import Router, types
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent

import keyboards as kb
import messages as bm
from app_context import (
    bot as ctx_bot,
    db as ctx_db,
    send_analytics as ctx_send_analytics,
)
from handlers.deps import HandlerDependencies, build_handler_dependencies
from handlers.utils import get_bot_username
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import create_inline_video_request
from services.links.detection import extract_supported_link
from services.logger import (
    logger as logging,
    summarize_text_for_log,
    summarize_url_for_log,
)

logging = logging.bind(service="guest_mode")

_guest_tasks: set[asyncio.Task[None]] = set()

router = Router(name=__name__)


def _get_service_sender(service: str):
    if service == "tiktok":
        from handlers.tiktok import _send_inline_tiktok_video

        return _send_inline_tiktok_video
    if service == "instagram":
        from handlers.instagram import _send_inline_instagram_video

        return _send_inline_instagram_video
    if service == "twitter":
        from handlers.twitter import _send_inline_twitter_media

        return _send_inline_twitter_media
    if service == "youtube":
        from handlers.youtube import _send_inline_youtube_video

        return _send_inline_youtube_video
    if service == "pinterest":
        from handlers.pinterest import _send_inline_pinterest_video

        return _send_inline_pinterest_video
    if service == "soundcloud":
        from handlers.soundcloud import _send_inline_soundcloud_audio

        return _send_inline_soundcloud_audio
    if service == "threads":
        from handlers.threads import _send_inline_threads_media

        return _send_inline_threads_media
    return None


def extract_link_from_guest_message(message: types.Message) -> tuple[str, str] | None:
    """Extract a supported service link from a guest message or its reply/quote context.

    Search priority:
    1. Direct message text or caption (user explicitly typed link in summon).
    2. Direct message text_link entities.
    3. Direct message quote text (user quoted specific text with a link).
    4. Replied-to message text or caption.
    5. Replied-to message text_link entities.
    6. Replied-to message quote text.
    """
    # 1. Direct message text / caption
    direct_text = message.text or message.caption or ""
    detected = extract_supported_link(direct_text)
    if detected:
        return detected

    # 2. Direct message text_link entities
    entities = getattr(message, "entities", None) or ()
    caption_entities = getattr(message, "caption_entities", None) or ()
    for ent in list(entities) + list(caption_entities):
        url = getattr(ent, "url", None)
        if getattr(ent, "type", None) == "text_link" and url:
            detected = extract_supported_link(url)
            if detected:
                return detected

    # 3. Direct message quote (Telegram quote replies)
    quote = getattr(message, "quote", None)
    if quote:
        quote_text = getattr(quote, "text", "") or ""
        detected = extract_supported_link(quote_text)
        if detected:
            return detected
        for ent in getattr(quote, "entities", None) or []:
            url = getattr(ent, "url", None)
            if getattr(ent, "type", None) == "text_link" and url:
                detected = extract_supported_link(url)
                if detected:
                    return detected

    # 4. Replied-to message
    reply = getattr(message, "reply_to_message", None)
    if reply:
        reply_text = (
            getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
        )
        detected = extract_supported_link(reply_text)
        if detected:
            return detected

        for ent in list(getattr(reply, "entities", None) or []) + list(
            getattr(reply, "caption_entities", None) or []
        ):
            url = getattr(ent, "url", None)
            if getattr(ent, "type", None) == "text_link" and url:
                detected = extract_supported_link(url)
                if detected:
                    return detected

        reply_quote = getattr(reply, "quote", None)
        if reply_quote:
            reply_quote_text = getattr(reply_quote, "text", "") or ""
            detected = extract_supported_link(reply_quote_text)
            if detected:
                return detected
            for ent in getattr(reply_quote, "entities", None) or []:
                url = getattr(ent, "url", None)
                if getattr(ent, "type", None) == "text_link" and url:
                    detected = extract_supported_link(url)
                    if detected:
                        return detected

    return None


@router.guest_message()
async def handle_guest_message(
    message: types.Message,
    deps: Optional[HandlerDependencies] = None,
) -> None:
    guest_query_id = getattr(message, "guest_query_id", None)
    if not guest_query_id:
        return

    if deps is None:
        deps = build_handler_dependencies(
            bot=ctx_bot,
            db=ctx_db,
            send_analytics=ctx_send_analytics,
        )

    user = getattr(message, "guest_bot_caller_user", None) or message.from_user
    chat = getattr(message, "guest_bot_caller_chat", None) or message.chat
    user_id = getattr(user, "id", 0) or 0
    chat_type = getattr(chat, "type", "guest")

    text = message.text or message.caption or ""
    has_reply = getattr(message, "reply_to_message", None) is not None
    logging.info(
        "Guest message received: user_id=%s guest_query_id=%s text=%s has_reply=%s",
        user_id,
        guest_query_id,
        summarize_text_for_log(text),
        has_reply,
    )

    await deps.send_analytics(
        user_id=user_id,
        chat_type=chat_type,
        action_name="guest_query",
    )

    bot_username = await get_bot_username(deps.bot)

    # Check for commands in guest message (e.g. "@bot /settings", "@bot /stats", "/settings@bot")
    stripped = text.strip()
    command_match = re.search(r"^@\w+\s+(/\w+)", stripped) or re.search(
        r"^(/[\w@]+)", stripped
    )
    command = command_match.group(1).split("@")[0].lower() if command_match else None

    if command == "/settings":
        article = InlineQueryResultArticle(
            id=f"guest_settings_{uuid.uuid4().hex[:8]}",
            title="Settings (Private Only)",
            description="Open settings in private chat with the bot.",
            input_message_content=InputTextMessageContent(
                message_text=bm.settings_private_only(),
                parse_mode="HTML",
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="⚙️ Open Settings in DM",
                            url=f"https://t.me/{bot_username}?start=settings",
                        )
                    ]
                ]
            ),
        )
        await message.answer_guest_query(result=article)
        return

    if command == "/stats":
        article = InlineQueryResultArticle(
            id=f"guest_stats_{uuid.uuid4().hex[:8]}",
            title="Statistics",
            description="View bot statistics in DM.",
            input_message_content=InputTextMessageContent(
                message_text="📊 <b>MaxLoad Statistics</b>\n\nInteractive charts and stats are available in private chat.",
                parse_mode="HTML",
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="📊 View Stats in DM",
                            url=f"https://t.me/{bot_username}?start=stats",
                        )
                    ]
                ]
            ),
        )
        await message.answer_guest_query(result=article)
        return

    detected = extract_link_from_guest_message(message)
    if not detected:
        # Summoned without a supported link (e.g. @bot, @bot /help, @bot hello)
        article = InlineQueryResultArticle(
            id=f"guest_help_{uuid.uuid4().hex[:8]}",
            title="MaxLoad — Guest Mode",
            description="Summon me with a media link to download videos/audio directly here!",
            thumbnail_url=get_inline_service_icon("tiktok"),
            input_message_content=InputTextMessageContent(
                message_text=bm.guest_help_message(bot_username),
                parse_mode="HTML",
            ),
            reply_markup=kb.guest_help_keyboard(bot_username),
        )
        await message.answer_guest_query(result=article)
        return

    service, url = detected
    logging.info(
        "Supported link detected in guest query: user_id=%s service=%s url=%s",
        user_id,
        service,
        summarize_url_for_log(url),
    )

    await deps.send_analytics(
        user_id=user_id,
        chat_type=chat_type,
        action_name=f"guest_{service}_download",
    )

    sender_func = _get_service_sender(service)
    if sender_func is None:
        # Platform does not have an inline/guest sender (e.g. spotify)
        from services.inline.album_links import create_inline_album_request

        token = create_inline_album_request(user_id, service, url)
        article = InlineQueryResultArticle(
            id=f"guest_{service}_{token}",
            title=f"{service.capitalize()} Link",
            description=f"Open in bot to download {service.capitalize()} media.",
            thumbnail_url=get_inline_service_icon(service),
            input_message_content=InputTextMessageContent(
                message_text=f"🎵 <b>{service.capitalize()} Media</b>\n\nTap below to open in private chat with the bot and download.",
                parse_mode="HTML",
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="⬇️ Open in Bot",
                            url=f"https://t.me/{bot_username}?start=dl_{token}",
                        )
                    ]
                ]
            ),
        )
        await message.answer_guest_query(result=article)
        return

    user_settings = await deps.db.user_settings(user_id) if user_id else {}
    token = create_inline_video_request(service, url, user_id, user_settings)

    button_text = (
        "Send audio inline"
        if service in {"soundcloud", "spotify"}
        else bm.inline_send_video_button()
    )
    initial_result = InlineQueryResultArticle(
        id=f"guest_{service}_{token}",
        title=f"{service.capitalize()} Media",
        description=f"Preparing {service.capitalize()} download...",
        thumbnail_url=get_inline_service_icon(service),
        input_message_content=InputTextMessageContent(
            message_text=bm.guest_downloading_status(service.capitalize()),
            parse_mode="HTML",
        ),
        reply_markup=kb.inline_send_media_keyboard(
            button_text,
            f"inline:{service}:{token}",
        ),
    )

    try:
        sent = await message.answer_guest_query(result=initial_result)
    except Exception as exc:
        logging.exception(
            "Failed to answer guest query: query_id=%s error=%s", guest_query_id, exc
        )
        return

    inline_message_id = getattr(sent, "inline_message_id", None)
    if not inline_message_id:
        logging.warning(
            "SentGuestMessage missing inline_message_id for token=%s", token
        )
        return

    actor_name = user.full_name if user else "Guest User"

    async def _safe_run_sender() -> None:
        try:
            await sender_func(
                token=token,
                inline_message_id=inline_message_id,
                actor_name=actor_name,
                actor_user_id=user_id,
                request_event_id=f"guest_{token}",
                duplicate_handler="guest",
            )
        except Exception as exc:
            logging.exception(
                "Failed to execute guest sender_func: token=%s service=%s error=%s",
                token,
                service,
                exc,
            )

    task = asyncio.create_task(_safe_run_sender())
    _guest_tasks.add(task)
    task.add_done_callback(_guest_tasks.discard)
