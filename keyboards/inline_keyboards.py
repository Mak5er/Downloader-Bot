from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from aiogram.utils.keyboard import InlineKeyboardBuilder


def format_number(value: int) -> str | None:
    if value is None:
        return None
    elif value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    elif value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}K"
    else:
        return str(value)


def return_captions_keyboard(captions):
    captions_button_text = ('✅Enabled') if captions == 'on' else ('❌Disabled')
    captions_button_callback = 'captions_off' if captions == 'on' else 'captions_on'
    buttons = [[InlineKeyboardButton(text=captions_button_text, callback_data=captions_button_callback)],
               [InlineKeyboardButton(text="🔙Back", callback_data="back_to_settings")]]

    captions_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return captions_keyboard


def return_settings_keyboard():
    buttons = [[InlineKeyboardButton(text=('✏️Descriptions'), callback_data='settings_caption')]]

    settings_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return settings_keyboard


def admin_keyboard():
    buttons = [
        [InlineKeyboardButton(text=('💬Mailing'), callback_data='send_to_all')],
        [InlineKeyboardButton(text=("👤Control User"), callback_data='control_user')],
        [
            InlineKeyboardButton(text=("📄View log"), callback_data='download_log'),
            InlineKeyboardButton(text=("❌📄Delete log"), callback_data='delete_log')
        ],
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    return keyboard


def return_search_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="ID", callback_data="search_id"),
            InlineKeyboardButton(text="Username", callback_data="search_username")
        ],
        [InlineKeyboardButton(text="🔙Back", callback_data="back_to_admin")]
    ]
    search_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return search_keyboard


def return_control_user_keyboard(user_id, status):
    builder = InlineKeyboardBuilder()

    go_to_chat = InlineKeyboardButton(text=("Enter in Conversation"), url=f"tg://user?id={user_id}")
    write_user = InlineKeyboardButton(text=('Write as a bot'), callback_data=f"write_{user_id}")
    ban_button = InlineKeyboardButton(text=("❌Ban"), callback_data=f"ban_{user_id}")
    unban_button = InlineKeyboardButton(text=("✅Unban"), callback_data=f"unban_{user_id}")
    back_button = InlineKeyboardButton(text=("🔙Back"), callback_data="back_to_admin")
    builder.row(go_to_chat, write_user)

    if status == 'active':
        builder.row(ban_button)

    elif status == 'ban':
        builder.row(unban_button)

    builder.row(back_button)

    return builder.as_markup()


def return_back_to_admin_keyboard():
    back_button = [
        [(InlineKeyboardButton(text=("🔙Back"), callback_data="back_to_admin"))]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=back_button)
    return keyboard


def return_audio_download_keyboard(platform, url):
    audio_button = [
        [(InlineKeyboardButton(text=("🎵Download MP3"), callback_data=f"{platform}_audio_{url}"))]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=audio_button)
    return keyboard


def return_user_info_keyboard(nickname, followers, videos, likes, url):
    builder = InlineKeyboardBuilder()

    builder.row(InlineKeyboardButton(text=nickname, url=url))

    row1 = []
    if followers is not None:
        row1.append(InlineKeyboardButton(text=f"👥 {format_number(followers)}",
                                         callback_data=f"followers_{format_number(followers)}"))
    if videos is not None:
        row1.append(InlineKeyboardButton(text=f"🎥 {format_number(videos)}",
                                         callback_data=f"videos_{format_number(videos)}"))
    if likes is not None:
        row1.append(InlineKeyboardButton(text=f"❤️ {format_number(likes)}",
                                         callback_data=f"likes_{format_number(likes)}"))

    if row1:
        builder.row(*row1)

    return builder.as_markup()


def return_video_info_keyboard(views, likes, comments, shares, music_play_url, video_url):
    builder = InlineKeyboardBuilder()

    row1 = []
    if views is not None:
        row1.append(
            InlineKeyboardButton(text=f"👁️ {format_number(views)}", callback_data=f"views_{format_number(views)}"))
    if likes is not None:
        row1.append(
            InlineKeyboardButton(text=f"❤️ {format_number(likes)}", callback_data=f"likes_{format_number(likes)}"))
    if comments is not None:
        row1.append(InlineKeyboardButton(text=f"💬 {format_number(comments)}",
                                         callback_data=f"comments_{format_number(comments)}"))
    if shares is not None:
        row1.append(
            InlineKeyboardButton(text=f"🔄 {format_number(shares)}", callback_data=f"shares_{format_number(shares)}"))

    if row1:
        builder.row(*row1)

    if music_play_url:
        builder.row(InlineKeyboardButton(text="🎵 Download MP3", url=music_play_url))

    if video_url:
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

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard
