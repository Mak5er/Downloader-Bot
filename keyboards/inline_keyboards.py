from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from aiogram.utils.keyboard import InlineKeyboardBuilder


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
        [
            InlineKeyboardButton(text=('💬Mailing'), callback_data='send_to_all'),
        ],
        [InlineKeyboardButton(text=("👤Control User"), callback_data='control_user')]
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


def stats_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="Week", callback_data="date_Week"),
            InlineKeyboardButton(text="Month", callback_data="date_Month"),
            InlineKeyboardButton(text="All Time", callback_data="date_All-Time"),
        ]
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard
