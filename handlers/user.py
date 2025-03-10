import datetime
from collections import defaultdict

from aiogram import types, Router, F
from aiogram.filters import Command

import os
import matplotlib.pyplot as plt
from aiogram.types import FSInputFile
from matplotlib.ticker import MaxNLocator

import keyboards as kb
import messages as bm
from main import db, send_analytics, bot

router = Router()


async def update_info(message: types.Message):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    user_username = message.from_user.username
    result = await db.user_exist(user_id)
    if result:
        await db.user_update_name(user_id, user_name, user_username)
    else:
        await db.add_user(user_id, user_name, user_username, "private", "uk", 'active')
    await db.set_active(user_id)


@router.message(F.new_chat_member)
async def send_welcome(message: types.Message):
    for user in message.new_chat_members:
        if user.is_bot and user.id == bot.id:
            chat_info = await bot.get_chat(message.chat.id)
            chat_type = "public"
            user_id = message.chat.id
            user_name = chat_info.title
            user_username = None
            language = 'uk'
            status = 'active'

            await db.add_user(user_id, user_name, user_username, chat_type, language, status)

            chat_title = chat_info.title
            await bot.send_message(
                chat_id=message.chat.id,
                text=bm.join_group(chat_title),
                parse_mode="HTML")


@router.message(Command("start"))
async def send_welcome(message: types.Message):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name='start')

    await message.reply(bm.welcome_message())
    await update_info(message)


@router.message(Command("remove_keyboard"))
async def remove_reply_keyboard(message: types.Message):
    await message.reply(text=bm.keyboard_removed(), reply_markup=types.ReplyKeyboardRemove())


@router.message(Command("settings"))
async def settings(message: types.Message):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name='settings')

    await message.reply(
        text=bm.settings(),
        reply_markup=kb.return_settings_keyboard(),
        parse_mode="HTML")


@router.callback_query(F.data == 'back_to_settings')
async def back_to_settings(call: types.CallbackQuery):
    await call.message.edit_text(
        text=bm.settings(),
        reply_markup=kb.return_settings_keyboard())
    await call.answer()


@router.callback_query(F.data == "settings_caption")
async def captions_setting(call: types.CallbackQuery):
    user_captions = await db.get_user_captions(call.from_user.id)
    await call.message.edit_text(
        text=bm.captions_settings(),
        reply_markup=kb.return_captions_keyboard(captions=user_captions), parse_mode='HTML')
    await call.answer()


@router.callback_query(F.data.startswith('captions_'))
async def change_captions(call: types.CallbackQuery):
    captions = call.data.split('_')[1]
    await db.update_captions(captions=captions, user_id=call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=kb.return_captions_keyboard(captions))
    await call.answer()


def create_and_save_chart(data, period):
    filename = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S") + "_chart.png"

    if period == 'Week':
        # Використовуємо лише перші 7 днів
        dates = list(data.keys())[:7]
        counts = list(data.values())[:7]
    elif period == 'Month':
        dates = list(data.keys())[::3]  # Для місяця беремо кожну 3-ю точку
        counts = list(data.values())[::3]
    elif period == 'Year':
        # Агрегуємо дані за місяцями
        monthly_data = defaultdict(int)
        for date_str, count in data.items():
            date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            month_key = date.strftime("%Y-%m")  # Формат: "2025-02"
            monthly_data[month_key] += count
        # Беремо останні 12 місяців
        all_months = sorted(set(monthly_data.keys()))
        if len(all_months) > 12:
            all_months = all_months[-12:]
        # Перетворюємо ключі місяців у datetime-об’єкти (наприклад, 1 число місяця)
        dates = [datetime.datetime.strptime(month, "%Y-%m") for month in all_months]
        counts = [monthly_data[month] for month in all_months]
    else:
        # За замовчуванням (наприклад, для Week) використовуємо дані без змін
        dates = list(data.keys())
        counts = list(data.values())

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#2E2E2E')

    ax.plot(dates, counts, marker='o', color='#4CAF50', markersize=8, linewidth=2, label='Downloads')
    ax.fill_between(dates, counts, color='#4CAF50', alpha=0.3)

    ax.set_title('Statistics of Downloaded Videos', fontsize=16, color='#FFFFFF')
    ax.set_xlabel('Date', fontsize=12, color='#B0B0B0')
    ax.set_ylabel('Number of Downloads', fontsize=12, color='#B0B0B0')

    if period == 'Week':
        ax.xaxis.set_major_locator(MaxNLocator(7))  # Для тижня 7 міток
    elif period == 'Month':
        ax.xaxis.set_major_locator(MaxNLocator(8))  # Для місяця 8 міток
    elif period == 'Year':
        ax.xaxis.set_major_locator(MaxNLocator(12))  # Для року 12 міток

    if period == 'Year':
        import matplotlib.dates as mdates
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))

    ax.grid(True, color='#444444', linestyle='--', linewidth=0.5)
    ax.spines['bottom'].set_color('#FFFFFF')
    ax.spines['left'].set_color('#FFFFFF')
    ax.tick_params(axis='x', colors='#B0B0B0')
    ax.tick_params(axis='y', colors='#B0B0B0')

    fig.savefig(filename, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)

    return filename


@router.message(Command("stats"))
async def stats_command(message: types.Message):
    data_today = await db.get_downloaded_files_count('Week')
    period = "Week"
    filename = create_and_save_chart(data_today, period)

    chart_input_file = FSInputFile(filename)
    await message.answer_photo(chart_input_file, caption='Statistics for Week',
                               reply_markup=kb.stats_keyboard())

    if os.path.exists(filename):
        os.remove(filename)


@router.callback_query(F.data.startswith('date_'))
async def switch_period(call: types.CallbackQuery):
    await call.message.delete()

    period = call.data.split("_")[1]
    data = await db.get_downloaded_files_count(period)
    filename = create_and_save_chart(data, period)

    chart_input_file = FSInputFile(filename)
    await call.message.answer_photo(chart_input_file, caption=f'Statistics for {period}',
                                    reply_markup=kb.stats_keyboard())

    if os.path.exists(filename):
        os.remove(filename)
