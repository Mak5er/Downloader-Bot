import datetime
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import time

import matplotlib.pyplot as plt
from aiogram import types, Router, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command
from aiogram.types import FSInputFile, ChatMemberUpdated
from matplotlib.ticker import MaxNLocator

import keyboards as kb
import messages as bm
from handlers.utils import get_message_text, remove_file
from log.logger import logger as logging
from main import db, send_analytics, bot
from services.pending_requests import pop_pending

router = Router()

_UPDATE_INFO_TTL_SECONDS = 120.0
_update_info_cache: dict[int, tuple[float, str, Optional[str]]] = {}

SERVICE_ORDER = ["Instagram", "TikTok", "YouTube", "Twitter", "Other"]
SERVICE_COLORS = ["#6C5DD3", "#FF6B6B", "#28C76F", "#00CFE8", "#FFA500"]
SERVICE_EMOJI = {
    "Instagram": "📸",
    "TikTok": "🎵",
    "YouTube": "▶️",
    "Twitter": "🐦",
    "Other": "📦",
}


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


async def update_info(message: types.Message):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    user_username = message.from_user.username

    now = time.monotonic()
    cached = _update_info_cache.get(user_id)
    if cached and now - cached[0] <= _UPDATE_INFO_TTL_SECONDS:
        if cached[1] == user_name and cached[2] == user_username:
            return

    result = await db.user_exist(user_id)
    if result:
        await db.user_update_name(user_id, user_name, user_username)
    else:
        await db.add_user(user_id, user_name, user_username, "private", "uk", 'active')
    await db.set_active(user_id)
    _update_info_cache[user_id] = (now, user_name, user_username)


@router.message(Command("start"))
async def send_welcome(message: types.Message):
    await send_analytics(user_id=message.from_user.id, chat_type=message.chat.type, action_name='start')

    await message.reply(bm.welcome_message())
    await update_info(message)

    if message.chat.type == ChatType.PRIVATE:
        pending = pop_pending(message.from_user.id)
        if pending:
            try:
                await bot.delete_message(pending.notice_chat_id, pending.notice_message_id)
            except Exception:
                pass
            await _process_pending_message(pending.message)


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
    if not text:
        return

    if re.search(r"(https?://(www\.|vm\.|vt\.|vn\.)?tiktok\.com/\S+)", text, re.IGNORECASE):
        from handlers import tiktok
        await tiktok.process_tiktok(message)
        return

    if re.search(r"(https?://(www\.)?instagram\.com/\S+)", text, re.IGNORECASE):
        from handlers import instagram
        await instagram.process_instagram_url(message)
        return

    if re.search(r"(https?://(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+)", text, re.IGNORECASE):
        from handlers import youtube
        await youtube.download_video(message)
        return

    if re.search(r"(https?://(www\.)?(twitter|x)\.com/\S+|https?://t\.co/\S+)", text, re.IGNORECASE):
        from handlers import twitter
        await twitter.handle_tweet_links(message)


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
    _, field = call.data.split(":")
    if call.message and call.message.chat.type != "private":
        is_admin = await _is_group_admin(call.message.chat.id, call.from_user.id)
        if not is_admin:
            await call.answer(bm.settings_admin_only(), show_alert=True)
            return
        target_id = call.message.chat.id
    else:
        target_id = call.from_user.id

    current_value = await db.get_user_setting(user_id=target_id, field=field)

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
    if call.message and call.message.chat.type != "private":
        is_admin = await _is_group_admin(call.message.chat.id, call.from_user.id)
        if not is_admin:
            await call.answer(bm.settings_admin_only(), show_alert=True)
            return
        target_id = call.message.chat.id
    else:
        target_id = call.from_user.id

    await db.set_user_setting(user_id=target_id, field=field, value=value)

    current_value = await db.get_user_setting(user_id=target_id, field=field)
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


def _prepare_series_for_period(data: dict[str, int], period: str) -> tuple[list[datetime.datetime], list[int]]:
    series = _prepare_series(data)
    if not series:
        now = datetime.datetime.now()
        return [now], [0]

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

    return dates, counts


def _aggregate_monthly(data: dict[str, int]) -> dict[datetime.datetime, int]:
    monthly: dict[datetime.datetime, int] = defaultdict(int)
    for date_str, count in data.items():
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        month_key = datetime.datetime(dt.year, dt.month, 1)
        monthly[month_key] += count
    return monthly


def _decimate_dates(dates: list[datetime.datetime], max_points: int) -> list[datetime.datetime]:
    if len(dates) <= max_points:
        return dates
    step = max(1, len(dates) // max_points)
    sampled = dates[::step]
    if sampled[-1] != dates[-1]:
        sampled.append(dates[-1])
    return sampled


def create_and_save_chart(data: dict[str, int], period: str, per_service: Optional[Dict[str, Dict[str, int]]] = None):
    filename = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S") + "_chart.png"
    charts_dir = Path("downloads")
    charts_dir.mkdir(parents=True, exist_ok=True)
    file_path = charts_dir / filename

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#0E1117')
    ax.set_facecolor('#0E1117')

    def _plot_series(
            dates: list[datetime.datetime],
            counts: list[int],
            color: str,
            label: str,
            *,
            annotate_last: bool,
            marker_size: int = 36,
            highlight_size: int = 60,
    ):
        ax.plot(dates, counts, color=color, linewidth=2.3, label=label)
        ax.fill_between(dates, counts, color=color, alpha=0.18)
        ax.scatter(dates, counts, color=color, s=marker_size, zorder=3)
        if annotate_last and dates and counts:
            ax.scatter(dates[-1], counts[-1], color='#FF9F43', s=highlight_size, zorder=4)
            ax.annotate(
                f"{counts[-1]}",
                (dates[-1], counts[-1]),
                textcoords="offset points",
                xytext=(0, 10),
                ha='center',
                color='#FF9F43',
                fontsize=10,
                fontweight='bold'
            )

    if per_service:
        # Build a shared axis so services drop to zero instead of stopping early.
        axis_dates: list[datetime.datetime] = []
        if period == "Year":
            total_map = _aggregate_monthly(data)
            axis_dates = list(total_map.keys())
            for service_data in per_service.values():
                axis_dates.extend(_aggregate_monthly(service_data).keys())
            axis_dates = sorted(set(axis_dates))
            if len(axis_dates) > 12:
                axis_dates = axis_dates[-12:]
            axis_dates = _decimate_dates(axis_dates, 12)
        else:
            axis_dates = [datetime.datetime.strptime(k, "%Y-%m-%d") for k in data.keys()]
            for service_data in per_service.values():
                axis_dates.extend(datetime.datetime.strptime(k, "%Y-%m-%d") for k in service_data.keys())
            axis_dates = sorted(set(axis_dates))
            window = 7 if period == "Week" else 30
            axis_dates = axis_dates[-window:] if axis_dates else [datetime.datetime.now()]
            if period == "Month":
                axis_dates = _decimate_dates(axis_dates, 12)

        # Plot each service aligned to the shared axis with zeros for missing points.
        for idx, service in enumerate(SERVICE_ORDER):
            raw_service = per_service.get(service)
            if not raw_service:
                continue
            if period == "Year":
                service_map = _aggregate_monthly(raw_service)
            else:
                service_map = {
                    datetime.datetime.strptime(k, "%Y-%m-%d"): v for k, v in raw_service.items()
                }
            counts = [service_map.get(dt, 0) for dt in axis_dates]
            color = SERVICE_COLORS[idx % len(SERVICE_COLORS)]
            _plot_series(
                axis_dates,
                counts,
                color,
                service,
                annotate_last=False,
                marker_size=30,
                highlight_size=40,
            )

        if not ax.lines:
            dates, counts = _prepare_series_for_period(data, period)
            _plot_series(dates, counts, "#6C5DD3", "Downloads", annotate_last=False, marker_size=30, highlight_size=40)
        ax.legend(facecolor='#0E1117', edgecolor='#2C3142', labelcolor='#F9FAFC')
    else:
        dates, counts = _prepare_series_for_period(data, period)
        _plot_series(dates, counts, "#6C5DD3", "Downloads", annotate_last=True)

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


def build_stats_caption(
        period: str,
        data: dict[str, int],
        per_service: Optional[dict[str, dict[str, int]]] = None,
) -> str:
    header = f"<b>Statistics for {period}</b>"

    if not data:
        return f"{header}\n\nNo downloads recorded for this period."

    total = sum(data.values())
    days_tracked = len(data)
    top_day, top_value = max(data.items(), key=lambda item: item[1])
    average = total / days_tracked if days_tracked else 0

    summary_lines = [
        f"Total downloads: <b>{total}</b>",
        f"Peak day: <b>{top_day}</b> - <b>{top_value}</b>",
        f"Average per day: <b>{average:.1f}</b>",
    ]

    if per_service:
        summary_lines.append("")
        summary_lines.append("<b>By platform:</b>")
        for service in SERVICE_ORDER:
            service_data = per_service.get(service)
            if not service_data:
                continue
            service_total = sum(service_data.values())
            emoji = SERVICE_EMOJI.get(service, "*")
            summary_lines.append(f"{emoji} {service}: <b>{service_total}</b>")

    return f"{header}\n\n" + "\n".join(summary_lines)


@router.message(Command("stats"))
async def stats_command(message: types.Message):
    try:
        period = "Week"
        mode = "total"
        filename, caption = await _render_stats(period, mode)

        chart_input_file = FSInputFile(filename)
        await message.answer_photo(
            chart_input_file,
            caption=caption,
            reply_markup=kb.stats_keyboard(period, mode),
        )

        await remove_file(filename)
    except Exception:
        await message.answer("Could not generate stats right now. Please try again later.")
        logging.exception("Error handling /stats")


async def _render_stats(payload_period: str, mode: str) -> tuple[str, str]:
    data = await db.get_downloaded_files_count(payload_period)
    per_service = await db.get_downloaded_files_by_service(payload_period) if mode == "split" else None
    filename = create_and_save_chart(data, payload_period, per_service)
    caption = build_stats_caption(payload_period, data, per_service)
    return filename, caption


@router.callback_query(F.data.startswith('stats:'))
async def switch_stats(call: types.CallbackQuery):
    await call.message.delete()

    parts = call.data.split(":")
    if len(parts) != 3:
        return
    _, period, mode = parts

    filename, caption = await _render_stats(period, mode)

    chart_input_file = FSInputFile(filename)
    await call.message.answer_photo(
        chart_input_file,
        caption=caption,
        reply_markup=kb.stats_keyboard(period, mode),
    )

    await remove_file(filename)


@router.callback_query(F.data.startswith('date_'))
async def switch_period(call: types.CallbackQuery):
    # Backward compatibility for older stats keyboards without mode toggle
    await call.message.delete()

    period = call.data.split("_")[1]
    filename, caption = await _render_stats(period, "total")

    chart_input_file = FSInputFile(filename)
    await call.message.answer_photo(
        chart_input_file,
        caption=caption,
        reply_markup=kb.stats_keyboard(period, "total"),
    )

    await remove_file(filename)
