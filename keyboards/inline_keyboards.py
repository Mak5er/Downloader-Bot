from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


from urllib.parse import quote


def start_keyboard(bot_username: str | None = None, ref_user_id: int | None = None) -> InlineKeyboardMarkup:
    username = bot_username or "MaxLoadBot"
    base_link = f"https://t.me/{username}"

    share_text = "Fast downloader bot for Instagram, TikTok, YouTube & more!"
    share_url = f"https://t.me/share/url?url={quote(base_link)}&text={quote(share_text)}"
    add_to_group_url = f"https://t.me/{username}?startgroup=true"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ Try inline", switch_inline_query_current_chat=""),
                InlineKeyboardButton(text="⚙️ Settings", callback_data="back_to_settings"),
            ],
            [
                InlineKeyboardButton(text="🚀 Share bot", url=share_url),
                InlineKeyboardButton(text="➕ Add to group", url=add_to_group_url),
            ],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Cancel", callback_data="cancel_action")
    return builder.as_markup()


def format_number(value: int) -> str | None:
    if value is None:
        return None
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


FIELD_CATEGORY_MAP = {
    "video_quality": "media",
    "as_document": "media",
    "audio_format": "media",
    "captions": "appearance",
    "info_buttons": "appearance",
    "audio_button": "appearance",
    "file_button": "appearance",
    "url_button": "appearance",
    "delete_message": "chat",
}


def return_settings_categories_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🎬 Media & Quality", callback_data="settings_cat:media")],
        [InlineKeyboardButton(text="🎨 Appearance & Buttons", callback_data="settings_cat:appearance")],
        [InlineKeyboardButton(text="💬 Chat & Clean-up", callback_data="settings_cat:chat")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def return_category_settings_keyboard(category: str) -> InlineKeyboardMarkup:
    if category == "media":
        fields = [
            ("🎬 Video Quality", "video_quality"),
            ("📄 Send as File", "as_document"),
            ("🎵 Audio Format", "audio_format"),
        ]
    elif category == "appearance":
        fields = [
            ("📝 Descriptions", "captions"),
            ("ℹ️ Info Buttons", "info_buttons"),
            ("🎧 MP3 Button", "audio_button"),
            ("📄 File Button", "file_button"),
            ("🔗 URL Button", "url_button"),
        ]
    else:
        fields = [
            ("🗑️ Delete Messages", "delete_message"),
        ]

    buttons = [
        [InlineKeyboardButton(text=text, callback_data=f"settings:{field}")]
        for text, field in fields
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Back to Categories", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def return_field_keyboard(field: str, value: str | None):
    val = (value or "").strip().lower()
    cat = FIELD_CATEGORY_MAP.get(field, "media")
    back_cb = f"settings_cat:{cat}"

    if field == "video_quality":
        current = val or "best"
        opt_best = "✅ 🏆 Best (1080p+)" if current == "best" else "🏆 Best (1080p+)"
        opt_bal = "✅ ⚖️ Balanced (720p)" if current == "balanced" else "⚖️ Balanced (720p)"
        opt_saver = "✅ ⚡ Data Saver (480p)" if current == "saver" else "⚡ Data Saver (480p)"

        buttons = [
            [InlineKeyboardButton(text=opt_best, callback_data="setting:video_quality:best")],
            [InlineKeyboardButton(text=opt_bal, callback_data="setting:video_quality:balanced")],
            [InlineKeyboardButton(text=opt_saver, callback_data="setting:video_quality:saver")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data=back_cb)],
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    if field == "audio_format":
        current = val or "mp3"
        opt_mp3 = "✅ 🎧 MP3 Audio" if current == "mp3" else "🎧 MP3 Audio"
        opt_m4a = "✅ 📱 M4A (AAC)" if current == "m4a" else "📱 M4A (AAC)"
        opt_best = "✅ 🎼 FLAC / Original" if current == "best" else "🎼 FLAC / Original"

        buttons = [
            [InlineKeyboardButton(text=opt_mp3, callback_data="setting:audio_format:mp3")],
            [InlineKeyboardButton(text=opt_m4a, callback_data="setting:audio_format:m4a")],
            [InlineKeyboardButton(text=opt_best, callback_data="setting:audio_format:best")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data=back_cb)],
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    is_enabled = val == "on"
    status_text = "🟢 Currently ON" if is_enabled else "🔴 Currently OFF"
    next_value = "off" if is_enabled else "on"
    action_text = "🔴 Turn OFF" if is_enabled else "🟢 Turn ON"

    buttons = [
        [InlineKeyboardButton(text=status_text, callback_data="noop")],
        [InlineKeyboardButton(text=action_text, callback_data=f"setting:{field}:{next_value}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data=back_cb)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def return_settings_keyboard():
    return return_settings_categories_keyboard()


def stats_keyboard(current_period: str = "Week", mode: str = "total"):
    periods = ["Week", "Month", "Year"]
    period_buttons = [
        InlineKeyboardButton(
            text=f"[{period}]" if period == current_period else period,
            callback_data=f"stats:{period}:{mode}",
        )
        for period in periods
    ]

    toggle_target = "split" if mode == "total" else "total"
    toggle_label = "View: By platform" if mode == "total" else "View: Overall"

    buttons = [
        period_buttons,
        [InlineKeyboardButton(text=toggle_label, callback_data=f"stats:{current_period}:{toggle_target}")],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="🩺 Health", callback_data="admin_ops"),
            InlineKeyboardButton(text="📦 Runtime", callback_data="admin_runtime_storage"),
        ],
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_refresh")],
        [InlineKeyboardButton(text="👥 Check Active Users", callback_data="check_active_users")],
        [InlineKeyboardButton(text="📬 Mailing", callback_data="send_to_all")],
        [InlineKeyboardButton(text="✉️ Message by Chat ID", callback_data="message_chat_id")],
        [
            InlineKeyboardButton(text="📄 View Log", callback_data="download_log"),
            InlineKeyboardButton(text="🗑️ Delete Log", callback_data="delete_log"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_detail_keyboard(refresh_callback: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data=refresh_callback)],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_admin")],
        ]
    )


def downloads_admin_keyboard(can_cleanup: bool = True, refresh_callback: str = "admin_downloads"):
    buttons = [[InlineKeyboardButton(text="🔄 Refresh", callback_data=refresh_callback)]]
    if can_cleanup:
        buttons.append([InlineKeyboardButton(text="🧹 Clean downloads", callback_data="admin_cleanup_downloads")])
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def return_search_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="ID", callback_data="search_id"),
            InlineKeyboardButton(text="Username", callback_data="search_username"),
        ],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_admin")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def return_control_user_keyboard(user_id, status):
    builder = InlineKeyboardBuilder()

    go_to_chat = InlineKeyboardButton(text="Open Chat", url=f"tg://user?id={user_id}")
    write_user = InlineKeyboardButton(text="Write as Bot", callback_data=f"write_{user_id}")
    ban_button = InlineKeyboardButton(text="Ban", callback_data=f"ban_{user_id}")
    unban_button = InlineKeyboardButton(text="Unban", callback_data=f"unban_{user_id}")
    back_button = InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_admin")

    builder.row(go_to_chat, write_user)

    if status == "active":
        builder.row(ban_button)
    elif status == "ban":
        builder.row(unban_button)

    builder.row(back_button)

    return builder.as_markup()


def return_back_to_admin_keyboard():
    back_button = [
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_admin")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=back_button)


def start_private_chat_keyboard(bot_username: str):
    url = f"https://t.me/{bot_username}?start=from_group"
    button = [[InlineKeyboardButton(text="💬 Open bot chat", url=url)]]
    return InlineKeyboardMarkup(inline_keyboard=button)


def return_audio_download_keyboard(platform, url):
    audio_button = [
        [InlineKeyboardButton(text="🎧 Download MP3", callback_data=f"{platform}_audio_{url}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=audio_button)


def inline_send_video_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Send video inline", callback_data=f"inline:tiktok:{token}")]
        ]
    )


def inline_send_media_keyboard(text: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=callback_data)]
        ]
    )


def return_user_info_keyboard(nickname, followers, videos, likes, url):
    builder = InlineKeyboardBuilder()

    builder.row(InlineKeyboardButton(text=nickname, url=url))

    row1 = []
    if followers is not None:
        row1.append(
            InlineKeyboardButton(
                text=f"👥 {format_number(followers)}",
                callback_data=f"followers_{format_number(followers)}",
            )
        )
    if videos is not None:
        row1.append(
            InlineKeyboardButton(
                text=f"🎬 {format_number(videos)}",
                callback_data=f"videos_{format_number(videos)}",
            )
        )
    if likes is not None:
        row1.append(
            InlineKeyboardButton(
                text=f"❤️ {format_number(likes)}",
                callback_data=f"likes_{format_number(likes)}",
            )
        )

    if row1:
        builder.row(*row1)

    return builder.as_markup()


def return_video_info_keyboard(
    views,
    likes,
    comments,
    shares,
    music_play_url,
    video_url,
    user_settings,
    audio_callback_data: str | None = None,
    file_callback_data: str | None = None,
):
    builder = InlineKeyboardBuilder()

    if user_settings["info_buttons"] == "on":
        row1 = []
        if views is not None:
            formatted_views = format_number(views)
            row1.append(
                InlineKeyboardButton(
                    text=f"👁 {formatted_views}",
                    callback_data=f"views_{formatted_views}",
                )
            )
        if likes is not None:
            formatted_likes = format_number(likes)
            row1.append(
                InlineKeyboardButton(
                    text=f"❤️ {formatted_likes}",
                    callback_data=f"likes_{formatted_likes}",
                )
            )
        if comments is not None:
            formatted_comments = format_number(comments)
            row1.append(
                InlineKeyboardButton(
                    text=f"💬 {formatted_comments}",
                    callback_data=f"comments_{formatted_comments}",
                )
            )
        if shares is not None:
            formatted_shares = format_number(shares)
            row1.append(
                InlineKeyboardButton(
                    text=f"🔁 {formatted_shares}",
                    callback_data=f"shares_{formatted_shares}",
                )
            )

        if row1:
            builder.row(*row1)

    if user_settings.get("audio_button") == "on" and audio_callback_data:
        builder.row(InlineKeyboardButton(text="🎧 Download MP3", callback_data=audio_callback_data))

    if (
        user_settings.get("file_button") == "on"
        and user_settings.get("as_document") != "on"
        and file_callback_data
    ):
        builder.row(InlineKeyboardButton(text="📄 Download File", callback_data=file_callback_data))

    if user_settings["url_button"] == "on" and video_url:
        builder.row(InlineKeyboardButton(text="🔗 URL", url=video_url))

    return builder.as_markup()


def _stats_keyboard_legacy_bottom(current_period: str = "Week", mode: str = "total"):
    periods = ["Week", "Month", "Year"]
    period_buttons = [
        InlineKeyboardButton(
            text=f"{'· ' if period == current_period else ''}{period}",
            callback_data=f"stats:{period}:{mode}",
        )
        for period in periods
    ]

    toggle_target = "split" if mode == "total" else "total"
    toggle_label = f"Split view: {'On' if mode == 'split' else 'Off'}"

    buttons = [
        period_buttons,
        [InlineKeyboardButton(text=toggle_label, callback_data=f"stats:{current_period}:{toggle_target}")],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)
