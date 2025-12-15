from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


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


def return_field_keyboard(field: str, value: str | None):
    is_enabled = value == "on"
    status_text = "✅ Enabled" if is_enabled else "🚫 Disabled"
    next_value = "off" if is_enabled else "on"
    action_text = "🔴 Turn OFF" if is_enabled else "🟢 Turn ON"

    buttons = [
        [InlineKeyboardButton(text=status_text, callback_data="noop")],
        [InlineKeyboardButton(text=action_text, callback_data=f"setting:{field}:{next_value}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_settings")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def return_settings_keyboard():
    settings_fields = [
        ("📝 Descriptions", "captions"),
        ("🗑️ Delete Messages", "delete_message"),
        ("ℹ️ Info Buttons", "info_buttons"),
        ("🔗 URL Button", "url_button"),
    ]

    buttons = [
        [InlineKeyboardButton(text=text, callback_data=f"settings:{field}")]
        for text, field in settings_fields
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_keyboard():
    buttons = [
        [InlineKeyboardButton(text="👥 Check Active Users", callback_data="check_active_users")],
        [InlineKeyboardButton(text="✉️ Message by Chat ID", callback_data="message_chat_id")],
        [InlineKeyboardButton(text="📬 Mailing", callback_data="send_to_all")],
        [
            InlineKeyboardButton(text="📄 View Log", callback_data="download_log"),
            InlineKeyboardButton(text="🗑️ Delete Log", callback_data="delete_log"),
        ],
    ]
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
    ban_button = InlineKeyboardButton(text="🚫 Ban", callback_data=f"ban_{user_id}")
    unban_button = InlineKeyboardButton(text="✅ Unban", callback_data=f"unban_{user_id}")
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


def return_audio_download_keyboard(platform, url):
    audio_button = [
        [InlineKeyboardButton(text="🎧 Download MP3", callback_data=f"{platform}_audio_{url}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=audio_button)


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


def return_video_info_keyboard(views, likes, comments, shares, music_play_url, video_url, user_settings):
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

        if music_play_url:
            builder.row(InlineKeyboardButton(text="🎧 Download MP3", url=music_play_url))

    if user_settings["url_button"] == "on" and video_url:
        builder.row(InlineKeyboardButton(text="🔗 URL", url=video_url))

    return builder.as_markup()


def stats_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="Week", callback_data="date_Week"),
            InlineKeyboardButton(text="Month", callback_data="date_Month"),
            InlineKeyboardButton(text="Year", callback_data="date_Year"),
        ]
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)
