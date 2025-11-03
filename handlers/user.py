import datetime
from collections import defaultdict
from pathlib import Path
from typing import List, Tuple

import matplotlib.pyplot as plt
from aiogram import types, Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command
from aiogram.types import FSInputFile, ChatMemberUpdated
from matplotlib.ticker import MaxNLocator

import keyboards as kb
import messages as bm
from handlers.utils import remove_file
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


@router.message(Command("start"))
async def send_welcome(message: types.Message):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name='start')

    await message.reply(bm.welcome_message())
    await update_info(message)


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


@router.message(Command("settings"))
async def settings_menu(message: types.Message):
    await send_analytics(user_id=message.from_user.id,
                         chat_type=message.chat.type,
                         action_name='settings')
    if message.chat.type != "private":
        await message.reply(bm.settings_private_only())
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
    _, field = call.data.split(":")
    current_value = await db.get_user_setting(user_id=call.from_user.id, field=field)

    keyboard = kb.return_field_keyboard(field, current_value)

    await call.message.edit_text(
        text=bm.get_field_text(field),
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("setting:"))
async def change_setting(call: types.CallbackQuery):
    _, field, value = call.data.split(":")
    await db.set_user_setting(user_id=call.from_user.id, field=field, value=value)

    current_value = await db.get_user_setting(user_id=call.from_user.id, field=field)
    keyboard = kb.return_field_keyboard(field, current_value)

    await call.message.edit_reply_markup(reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data == "noop")
async def noop_callback(call: types.CallbackQuery):
    await call.answer()


def _prepare_series(data: dict[str, int]) -> List[Tuple[datetime.datetime, int]]:
    series = []
    for date_str, count in data.items():
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        series.append((dt, count))
    series.sort(key=lambda item: item[0])
    return series


def _decimate_series(
        dates: List[datetime.datetime],
        counts: List[int],
        max_points: int,
) -> tuple[list[datetime.datetime], list[int]]:
    if len(dates) <= max_points:
        return dates, counts

    step = max(1, len(dates) // max_points)
    sampled_dates = dates[::step]
    sampled_counts = counts[::step]

    if sampled_dates[-1] != dates[-1]:
        sampled_dates.append(dates[-1])
        sampled_counts.append(counts[-1])

    return sampled_dates, sampled_counts


def create_and_save_chart(data, period):
    filename = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S") + "_chart.png"
    charts_dir = Path("downloads")
    charts_dir.mkdir(parents=True, exist_ok=True)
    file_path = charts_dir / filename

    series = _prepare_series(data)
    if not series:
        now = datetime.datetime.now()
        series = [(now, 0)]

    dates = [dt for dt, _ in series]
    counts = [count for _, count in series]

    if period == 'Week':
        dates = dates[-7:]
        counts = counts[-7:]
    elif period == 'Month':
        dates = dates[-30:]
        counts = counts[-30:]
        dates, counts = _decimate_series(dates, counts, max_points=12)
    elif period == 'Year':
        monthly_data = defaultdict(int)
        for dt, count in series:
            month_key = dt.strftime("%Y-%m")
            monthly_data[month_key] += count
        all_months = sorted(set(monthly_data.keys()))
        if len(all_months) > 12:
            all_months = all_months[-12:]
        dates = [datetime.datetime.strptime(month, "%Y-%m") for month in all_months]
        counts = [monthly_data[month] for month in all_months]

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#0E1117')
    ax.set_facecolor('#0E1117')

    line_color = '#6C5DD3'
    fill_color = '#6C5DD3'
    highlight_color = '#FF9F43'

    ax.plot(dates, counts, color=line_color, linewidth=2.5, label='Downloads')
    ax.fill_between(dates, counts, color=fill_color, alpha=0.25)
    ax.scatter(dates, counts, color=line_color, s=45, zorder=3)

    if dates and counts:
        ax.scatter(dates[-1], counts[-1], color=highlight_color, s=80, zorder=4)
        ax.annotate(
            f"{counts[-1]}",
            (dates[-1], counts[-1]),
            textcoords="offset points",
            xytext=(0, 10),
            ha='center',
            color=highlight_color,
            fontsize=10,
            fontweight='bold'
        )

    ax.set_title('Downloads Overview', fontsize=18, color='#F9FAFC', pad=16)
    ax.set_xlabel('Date', fontsize=12, color='#A1A5B7')
    ax.set_ylabel('Downloads', fontsize=12, color='#A1A5B7')

    if period == 'Week':
        ax.xaxis.set_major_locator(MaxNLocator(7))
    elif period == 'Month':
        ax.xaxis.set_major_locator(MaxNLocator(8))
    elif period == 'Year':
        ax.xaxis.set_major_locator(MaxNLocator(12))

    if period == 'Year':
        import matplotlib.dates as mdates
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))

    ax.grid(color='#1E2233', linestyle='--', linewidth=0.8, alpha=0.6)
    for spine in ax.spines.values():
        spine.set_color('#2C3142')
    ax.tick_params(axis='x', colors='#D0D3F9')
    ax.tick_params(axis='y', colors='#D0D3F9')
    ax.set_ylim(bottom=0)

    fig.savefig(file_path, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)

    return str(file_path)


def build_stats_caption(period: str, data: dict[str, int]) -> str:
    header = f"<b>Statistics for {period}</b>"

    if not data:
        return f"{header}\n\n📊 No downloads recorded for this period."

    total = sum(data.values())
    days_tracked = len(data)
    top_day, top_value = max(data.items(), key=lambda item: item[1])
    average = total / days_tracked if days_tracked else 0
    today_key = datetime.datetime.now().strftime("%Y-%m-%d")
    today_downloads = data.get(today_key, 0)

    summary_lines = [
        f"📊 Total downloads: <b>{total}</b>",
        f"⭐ Peak day: <b>{top_day}</b> — <b>{top_value}</b>",
        f"⚖️ Average per day: <b>{average:.1f}</b>",
    ]

    return f"{header}\n\n" + "\n".join(summary_lines)


@router.message(Command("stats"))
async def stats_command(message: types.Message):
    period = "Week"
    data_today = await db.get_downloaded_files_count(period)
    filename = create_and_save_chart(data_today, period)
    caption = build_stats_caption(period, data_today)

    chart_input_file = FSInputFile(filename)
    await message.answer_photo(chart_input_file, caption=caption,
                               reply_markup=kb.stats_keyboard())

    await remove_file(filename)


@router.callback_query(F.data.startswith('date_'))
async def switch_period(call: types.CallbackQuery):
    await call.message.delete()

    period = call.data.split("_")[1]
    data = await db.get_downloaded_files_count(period)
    filename = create_and_save_chart(data, period)
    caption = build_stats_caption(period, data)

    chart_input_file = FSInputFile(filename)
    await call.message.answer_photo(chart_input_file, caption=caption,
                                    reply_markup=kb.stats_keyboard())

    await remove_file(filename)
