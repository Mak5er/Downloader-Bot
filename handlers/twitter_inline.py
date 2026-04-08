import os
from typing import Optional
from urllib.parse import urlsplit

from aiogram import types
from aiogram.types import FSInputFile

import keyboards as kb
import messages as bm
from handlers.deps import HandlerDependencies
from handlers.utils import (
    build_inline_album_result,
    build_inline_status_editor,
    build_start_deeplink_url,
    get_bot_url,
    make_status_text_progress_updater,
    remove_file,
    safe_answer_inline_query,
    safe_edit_inline_media,
    safe_edit_inline_text,
)
from log.logger import logger as logging, summarize_text_for_log, summarize_url_for_log
from services.inline.album_links import create_inline_album_request
from services.inline.service_icons import get_inline_service_icon
from services.inline.video_requests import (
    claim_inline_video_request_for_send,
    complete_inline_video_request,
    create_inline_video_request,
    reset_inline_video_request,
)
from utils.download_manager import log_download_metrics

logging = logging.bind(service="twitter_inline")


async def handle_twitter_inline_query(
    query: types.InlineQuery,
    *,
    deps: HandlerDependencies,
    twitter_link_regex: str,
    channel_id: Optional[int],
    get_tweet_context_fn,
    extract_twitter_media_items_fn,
    normalize_twitter_media_kind_fn,
    build_twitter_media_cache_key_fn,
    get_twitter_media_preview_url_fn,
    build_twitter_open_in_bot_result_fn,
    get_bot_url_fn=get_bot_url,
    safe_answer_inline_query_fn=safe_answer_inline_query,
) -> None:
    try:
        await deps.send_analytics(
            user_id=query.from_user.id,
            chat_type=query.chat_type,
            action_name="inline_twitter_media",
        )
        import re

        match = re.search(twitter_link_regex, query.query or "")
        if not match:
            await query.answer([], cache_time=1, is_personal=True)
            return

        source_url = match.group(0)
        user_settings = await deps.db.user_settings(query.from_user.id)
        bot_url = await get_bot_url_fn(deps.bot)
        album_token = create_inline_album_request(query.from_user.id, "twitter", source_url)
        album_deep_link = build_start_deeplink_url(bot_url, f"album_{album_token}")
        context = await get_tweet_context_fn(source_url)
        if not context:
            await safe_answer_inline_query_fn(
                query,
                [
                    build_twitter_open_in_bot_result_fn(
                        result_id="twitter_open_in_bot",
                        deep_link=album_deep_link,
                        description="Open this post in the bot.",
                    )
                ],
                cache_time=10,
                is_personal=True,
            )
            return

        tweet_id, tweet_media = context
        media_items = extract_twitter_media_items_fn(tweet_media)
        if len(media_items) > 1:
            preview_file_id = None
            first_media = media_items[0]
            first_media_kind = normalize_twitter_media_kind_fn(first_media.get("type"))
            if first_media_kind == "photo" and channel_id:
                preview_cache_key = build_twitter_media_cache_key_fn(source_url, 0, "photo", len(media_items))
                preview_file_id = await deps.db.get_file_id(preview_cache_key)
                if not preview_file_id:
                    try:
                        sent = await deps.bot.send_photo(
                            chat_id=channel_id,
                            photo=first_media["url"],
                            caption="X / Twitter Album Preview",
                        )
                        if sent.photo:
                            preview_file_id = sent.photo[-1].file_id
                            await deps.db.add_file(preview_cache_key, preview_file_id, "photo")
                    except Exception as exc:
                        logging.warning(
                            "Failed to cache Twitter album preview photo: url=%s error=%s",
                            summarize_url_for_log(source_url),
                            exc,
                        )
            preview_url = get_twitter_media_preview_url_fn(media_items[0], tweet_media) or next(
                (
                    get_twitter_media_preview_url_fn(item, tweet_media)
                    for item in media_items
                    if get_twitter_media_preview_url_fn(item, tweet_media)
                ),
                None,
            )
            results = [
                build_inline_album_result(
                    result_id=f"twitter_album_{tweet_id}",
                    service_name="Twitter",
                    deep_link=album_deep_link,
                    message_text=bm.captions(
                        user_settings["captions"],
                        tweet_media.get("text"),
                        bot_url,
                    ),
                    preview_file_id=preview_file_id,
                    preview_url=preview_url,
                    thumbnail_url=preview_url or get_inline_service_icon("twitter"),
                )
            ]
            await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
            return

        if len(media_items) != 1:
            await safe_answer_inline_query_fn(
                query,
                [
                    build_twitter_open_in_bot_result_fn(
                        result_id=f"twitter_open_{tweet_id}",
                        deep_link=album_deep_link,
                        description="Inline preview is limited for this post. Open it in the bot.",
                    )
                ],
                cache_time=10,
                is_personal=True,
            )
            return

        media = media_items[0]
        media_kind = normalize_twitter_media_kind_fn(media.get("type"))
        if not media_kind:
            await safe_answer_inline_query_fn(
                query,
                [
                    build_twitter_open_in_bot_result_fn(
                        result_id=f"twitter_open_unknown_{tweet_id}",
                        deep_link=album_deep_link,
                        description="Open this post in the bot.",
                    )
                ],
                cache_time=10,
                is_personal=True,
            )
            return
        token = create_inline_video_request("twitter", source_url, query.from_user.id, user_settings)
        action_text = "Send photo inline" if media_kind == "photo" else "Send video inline"
        prompt_text = (
            bm.inline_send_video_prompt("Twitter")
            if media_kind == "video"
            else "Twitter photo is being prepared...\nIf it does not start automatically, tap the button below."
        )
        preview_url = get_twitter_media_preview_url_fn(media, tweet_media) or get_inline_service_icon("twitter")
        results = [
            types.InlineQueryResultArticle(
                id=f"twitter_inline:{token}",
                title="X / Twitter Post",
                description=tweet_media.get("text") or f"Press the button to send this {media_kind} inline.",
                thumbnail_url=preview_url,
                input_message_content=types.InputTextMessageContent(message_text=prompt_text),
                reply_markup=kb.inline_send_media_keyboard(action_text, f"inline:twitter:{token}"),
            )
        ]
        await safe_answer_inline_query_fn(query, results, cache_time=10, is_personal=True)
    except Exception as exc:
        logging.exception(
            "Error processing Twitter inline query: user_id=%s query=%s error=%s",
            query.from_user.id,
            summarize_text_for_log(query.query),
            exc,
        )
        await query.answer([], cache_time=1, is_personal=True)


async def send_inline_twitter_media(
    *,
    token: str,
    inline_message_id: str,
    actor_name: str,
    actor_user_id: int,
    request_event_id: str,
    duplicate_handler: str,
    deps: HandlerDependencies,
    channel_id: Optional[int],
    max_file_size: int,
    twitter_downloader,
    get_tweet_context_fn,
    extract_twitter_media_items_fn,
    normalize_twitter_media_kind_fn,
    build_twitter_media_cache_key_fn,
    get_bot_url_fn=get_bot_url,
    safe_edit_inline_media_fn=safe_edit_inline_media,
    safe_edit_inline_text_fn=safe_edit_inline_text,
) -> None:
    request = claim_inline_video_request_for_send(
        token,
        duplicate_handler=duplicate_handler,
        actor_user_id=actor_user_id,
    )
    if request is None:
        return

    download_path: Optional[str] = None

    _edit_inline_status = build_inline_status_editor(
        bot=deps.bot,
        inline_message_id=inline_message_id,
        callback_data_factory=lambda _media_kind: f"inline:twitter:{token}",
        safe_edit_inline_text_fn=safe_edit_inline_text_fn,
    )

    try:
        context = await get_tweet_context_fn(request.source_url)
        if not context:
            complete_inline_video_request(token)
            await _edit_inline_status("Only single photo or single video posts are supported inline.")
            return

        _, tweet_media = context
        media_items = extract_twitter_media_items_fn(tweet_media)
        if len(media_items) != 1:
            complete_inline_video_request(token)
            await _edit_inline_status("Only single photo or single video posts are supported inline.")
            return

        media = media_items[0]
        media_kind = normalize_twitter_media_kind_fn(media.get("type"))
        if not media_kind:
            complete_inline_video_request(token)
            await _edit_inline_status("Only single photo or single video posts are supported inline.")
            return
        post_url = tweet_media["tweetURL"]
        post_caption = tweet_media.get("text")
        likes = tweet_media.get("likes")
        comments = tweet_media.get("replies")
        retweets = tweet_media.get("retweets")

        if media_kind == "photo":
            cache_key = build_twitter_media_cache_key_fn(post_url, 0, "photo", 1)
            db_file_id = await deps.db.get_file_id(cache_key)
            if not db_file_id:
                if not channel_id:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return

                await _edit_inline_status(bm.uploading_status(), media_kind="photo")
                sent = await deps.bot.send_photo(
                    chat_id=channel_id,
                    photo=media["url"],
                    caption=f"X / Twitter Photo from {actor_name}",
                )
                if not sent.photo:
                    reset_inline_video_request(token)
                    await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
                    return
                db_file_id = sent.photo[-1].file_id
                await deps.db.add_file(cache_key, db_file_id, "photo")
            else:
                await _edit_inline_status(bm.uploading_status(), media_kind="photo")

            edited = await safe_edit_inline_media_fn(
                deps.bot,
                inline_message_id,
                types.InputMediaPhoto(
                    media=db_file_id,
                    caption=bm.captions(request.user_settings["captions"], post_caption, await get_bot_url_fn(deps.bot)),
                    parse_mode="HTML",
                ),
                reply_markup=kb.return_video_info_keyboard(
                    None,
                    likes,
                    comments,
                    retweets,
                    None,
                    post_url,
                    request.user_settings,
                ),
            )
            if edited:
                complete_inline_video_request(token)
                return
            reset_inline_video_request(token)
            await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True, media_kind="photo")
            return

        cache_key = post_url
        db_file_id = await deps.db.get_file_id(cache_key)
        if not db_file_id:
            if not channel_id:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            file_name = os.path.join(str(tweet_media["conversationID"]), os.path.basename(urlsplit(media["url"]).path))
            await _edit_inline_status(bm.downloading_video_status())

            on_progress = make_status_text_progress_updater("X / Twitter video", _edit_inline_status)

            metrics = await twitter_downloader.download(
                media["url"],
                file_name,
                skip_if_exists=True,
                user_id=request.owner_user_id,
                request_id=f"twitter_inline:{request.owner_user_id}:{request_event_id}:{tweet_media['conversationID']}",
                max_size_bytes=max_file_size,
                on_progress=on_progress,
            )
            if not metrics:
                reset_inline_video_request(token)
                await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
                return

            log_download_metrics("twitter_inline", metrics)
            download_path = metrics.path
            if metrics.size >= max_file_size:
                complete_inline_video_request(token)
                await _edit_inline_status(bm.video_too_large())
                return

            await _edit_inline_status(bm.uploading_status())
            sent = await deps.bot.send_video(
                chat_id=channel_id,
                video=FSInputFile(download_path),
                caption=f"X / Twitter Video from {actor_name}",
            )
            db_file_id = sent.video.file_id
            await deps.db.add_file(cache_key, db_file_id, "video")
        else:
            await _edit_inline_status(bm.uploading_status())

        edited = await safe_edit_inline_media_fn(
            deps.bot,
            inline_message_id,
            types.InputMediaVideo(
                media=db_file_id,
                caption=bm.captions(request.user_settings["captions"], post_caption, await get_bot_url_fn(deps.bot)),
                parse_mode="HTML",
            ),
            reply_markup=kb.return_video_info_keyboard(
                None,
                likes,
                comments,
                retweets,
                None,
                post_url,
                request.user_settings,
            ),
        )
        if edited:
            complete_inline_video_request(token)
            return

        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    except Exception as exc:
        logging.exception(
            "Error sending Twitter inline media: inline_message_id=%s token=%s error=%s",
            inline_message_id,
            token,
            exc,
        )
        reset_inline_video_request(token)
        await _edit_inline_status(bm.something_went_wrong(), with_retry_button=True)
    finally:
        if download_path:
            await remove_file(download_path)
