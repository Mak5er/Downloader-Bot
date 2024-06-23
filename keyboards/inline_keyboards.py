from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from aiogram.utils.keyboard import InlineKeyboardBuilder


def return_captions_keyboard(captions):
    captions_button_text = '✅Enabled' if captions == 'on' else '❌Disabled'
    captions_button_callback = 'captions_off' if captions == 'on' else 'captions_on'
    buttons = [[InlineKeyboardButton(text=captions_button_text, callback_data=captions_button_callback)],
               [InlineKeyboardButton(text="🔙Back", callback_data="back_to_settings")]]

    captions_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return captions_keyboard


def return_settings_keyboard():
    buttons = [[InlineKeyboardButton(text='✏️Descriptions', callback_data='settings_caption')]]

    settings_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return settings_keyboard
