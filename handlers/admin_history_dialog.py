from __future__ import annotations

import csv
from datetime import datetime
import html
import io
import math
from typing import Any

from aiogram import Bot, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.kbd import (
    Button,
    Column,
    Group,
    Row,
    Select,
    SwitchTo,
)
from aiogram_dialog.widgets.link_preview import LinkPreview
from aiogram_dialog.widgets.text import Const, Format

import messages as bm
from services.logger import logger as logging

logger = logging.bind(service="admin_history_dialog")


class AdminHistorySG(StatesGroup):
    history = State()
    select_service = State()
    message_user = State()


AVAILABLE_SERVICES: list[tuple[str, str]] = [
    ("all", "🌐 All Services"),
    ("tiktok", "🎵 TikTok"),
    ("instagram", "📸 Instagram"),
    ("youtube", "▶️ YouTube"),
    ("youtube_audio", "🎶 YouTube Audio"),
    ("twitter", "🐦 Twitter"),
    ("pinterest", "📌 Pinterest"),
    ("threads", "🧵 Threads"),
    ("spotify", "🟢 Spotify"),
    ("soundcloud", "☁️ SoundCloud"),
]

SERVICE_ICONS: dict[str, str] = {
    "tiktok": "🎵",
    "instagram": "📸",
    "youtube": "▶️",
    "youtube_audio": "🎶",
    "youtube_music": "🎶",
    "twitter": "🐦",
    "pinterest": "📌",
    "threads": "🧵",
    "spotify": "🟢",
    "soundcloud": "☁️",
}


def format_history_entry(item: Any) -> str:
    service_raw = str(getattr(item, "service", "unknown")).lower()
    icon = SERVICE_ICONS.get(service_raw, "📥")
    service_title = service_raw.replace("_", " ").title()

    user_name = None
    if getattr(item, "user", None):
        user_obj = item.user
        if getattr(user_obj, "user_username", None):
            user_name = f"@{user_obj.user_username}"
        elif getattr(user_obj, "user_name", None):
            user_name = user_obj.user_name

    if not user_name:
        user_name = "User"
    user_display = f"👤 <b>{html.escape(user_name)}</b> (<code>{item.user_id}</code>)"

    status_str = getattr(item, "status", "success")
    if status_str == "success":
        status_badge = "✅ Success"
    elif status_str == "cached":
        status_badge = "⚡ Cached"
    else:
        status_badge = f"❌ {html.escape(str(status_str).capitalize())}"

    raw_title = getattr(item, "title", None) or "View Media"
    if len(raw_title) > 40:
        raw_title = raw_title[:37] + "..."
    safe_title = html.escape(raw_title)
    url = getattr(item, "url", "#")
    media_link = f'<a href="{url}">{safe_title}</a>'

    created_at = getattr(item, "created_at", None)
    created_str = (
        created_at.strftime("%Y-%m-%d %H:%M:%S")
        if created_at
        else "Unknown"
    )

    extras: list[str] = []
    file_size = getattr(item, "file_size_bytes", None)
    if file_size:
        mb = file_size / (1024 * 1024)
        extras.append(f"{mb:.1f}MB")
    duration = getattr(item, "duration_seconds", None)
    if duration is not None:
        extras.append(f"{duration:.1f}s")
    chat_type = getattr(item, "chat_type", None)
    if chat_type and chat_type != "private":
        extras.append(f"{chat_type}")

    extras_str = f" ({', '.join(extras)})" if extras else ""

    lines = [
        "<blockquote expandable>",
        f"{icon} <b>{service_title}</b> | {media_link}",
        f"{user_display} | {status_badge}",
        f"⏱ <i>{created_str}</i>{extras_str}",
    ]
    err_msg = getattr(item, "error_message", None)
    if status_str != "success" and err_msg:
        err_snippet = html.escape(str(err_msg)[:80])
        lines.append(f"⚠️ <code>{err_snippet}</code>")
    lines.append("</blockquote>")

    return "\n".join(lines)


async def get_history_data(dialog_manager: DialogManager, **kwargs) -> dict[str, Any]:
    db = dialog_manager.middleware_data.get("db")
    if db is None:
        from app_context import db as app_db
        db = app_db

    dialog_data = dialog_manager.dialog_data
    page = int(dialog_data.get("page", 1))
    service_filter = dialog_data.get("service")
    status_filter = dialog_data.get("status")
    per_page = 10

    total_count = await db.get_download_history_count(
        service=service_filter,
        status=status_filter,
    )
    total_pages = max(1, math.ceil(total_count / per_page))
    if page > total_pages:
        page = total_pages
        dialog_data["page"] = page

    dialog_data["total_pages"] = total_pages

    items = await db.get_download_history(
        page=page,
        per_page=per_page,
        service=service_filter,
        status=status_filter,
    )

    if items:
        formatted_items = [format_history_entry(it) for it in items]
        history_text = "\n\n".join(formatted_items)
    else:
        history_text = "<i>No download records found matching criteria.</i>"

    # Extract unique users from the current page
    unique_users: dict[int, str] = {}
    for item in items:
        uid = getattr(item, "user_id", None)
        if uid and uid not in unique_users:
            uname = None
            if getattr(item, "user", None):
                u = item.user
                if getattr(u, "user_username", None):
                    uname = f"@{u.user_username}"
                elif getattr(u, "user_name", None):
                    uname = u.user_name
            if not uname:
                uname = f"User {uid}"
            unique_users[uid] = uname

    page_users = [(uid, uname) for uid, uname in unique_users.items()][:8]

    svc_label = f"🎬 {service_filter.replace('_', ' ').title()}" if service_filter else "🎬 All Services"
    status_toggle = "🟢 Show All" if status_filter == "error" else "❌ Errors Only"

    return {
        "history_text": history_text,
        "total_count": total_count,
        "page": page,
        "total_pages": total_pages,
        "service_filter": service_filter.replace("_", " ").title() if service_filter else "All",
        "status_filter": status_filter.capitalize() if status_filter else "All",
        "service_btn_label": svc_label,
        "status_toggle_label": status_toggle,
        "page_users": page_users,
    }


async def get_services_data(dialog_manager: DialogManager, **kwargs) -> dict[str, Any]:
    current_svc = dialog_manager.dialog_data.get("service")
    current_label = current_svc.replace("_", " ").title() if current_svc else "All"
    return {
        "services": AVAILABLE_SERVICES,
        "current_service": current_label,
    }


async def get_message_user_data(dialog_manager: DialogManager, **kwargs) -> dict[str, Any]:
    # Reuse page_users computed in get_history_data
    history_data = await get_history_data(dialog_manager, **kwargs)
    return {
        "page_users": history_data["page_users"],
    }


# Click handlers
async def on_prev_page(c: types.CallbackQuery, button: Button, manager: DialogManager) -> None:
    page = int(manager.dialog_data.get("page", 1))
    if page > 1:
        manager.dialog_data["page"] = page - 1
    else:
        await c.answer("Already on first page")


async def on_next_page(c: types.CallbackQuery, button: Button, manager: DialogManager) -> None:
    page = int(manager.dialog_data.get("page", 1))
    total_pages = int(manager.dialog_data.get("total_pages", 1))
    if page < total_pages:
        manager.dialog_data["page"] = page + 1
    else:
        await c.answer("Already on last page")


async def on_page_info(c: types.CallbackQuery, button: Button, manager: DialogManager) -> None:
    page = manager.dialog_data.get("page", 1)
    total_pages = manager.dialog_data.get("total_pages", 1)
    await c.answer(f"Page {page} of {total_pages}")


async def on_toggle_status(c: types.CallbackQuery, button: Button, manager: DialogManager) -> None:
    current = manager.dialog_data.get("status")
    if current == "error":
        manager.dialog_data["status"] = None
    else:
        manager.dialog_data["status"] = "error"
    manager.dialog_data["page"] = 1


async def on_reset_filters(c: types.CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data["service"] = None
    manager.dialog_data["status"] = None
    manager.dialog_data["page"] = 1
    await c.answer("Filters reset")


async def on_refresh(c: types.CallbackQuery, button: Button, manager: DialogManager) -> None:
    await c.answer("Refreshed")


async def on_service_selected(
    c: types.CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
) -> None:
    if item_id == "all":
        manager.dialog_data["service"] = None
    else:
        manager.dialog_data["service"] = item_id
    manager.dialog_data["page"] = 1
    await manager.switch_to(AdminHistorySG.history)


async def on_user_selected(
    c: types.CallbackQuery,
    widget: Any,
    manager: DialogManager,
    item_id: str,
) -> None:
    user_id = int(item_id)
    await manager.done()
    state: FSMContext = manager.middleware_data["state"]
    bot: Bot = manager.middleware_data["bot"]
    db = manager.middleware_data.get("db")
    if db is None:
        from app_context import db as app_db
        db = app_db

    await state.update_data(target_chat_id=user_id)
    known_info = await db.get_user_info(user_id)
    preview_lines: list[str] = []
    if known_info:
        preview_lines.append(
            bm.known_chat_target(
                user_id,
                known_info[0],
                known_info[1],
                known_info[2],
            )
        )
    else:
        preview_lines.append(bm.unknown_chat_target(user_id))
    preview_lines.append("")
    preview_lines.append(bm.enter_chat_message())

    from handlers.admin import Admin
    import keyboards as kb

    if c.message:
        await bot.send_message(
            c.message.chat.id,
            "\n".join(preview_lines),
            reply_markup=kb.cancel_keyboard(),
            parse_mode="HTML",
        )
    await state.set_state(Admin.write_chat_text)


async def on_enter_custom_chat_id(
    c: types.CallbackQuery,
    button: Button,
    manager: DialogManager,
) -> None:
    await manager.done()
    state: FSMContext = manager.middleware_data["state"]
    bot: Bot = manager.middleware_data["bot"]
    from handlers.admin import Admin
    import keyboards as kb

    if c.message:
        await bot.send_message(
            c.message.chat.id,
            bm.enter_chat_id(),
            reply_markup=kb.cancel_keyboard(),
        )
    await state.set_state(Admin.write_chat_id)


async def on_export_csv(c: types.CallbackQuery, button: Button, manager: DialogManager) -> None:
    db = manager.middleware_data.get("db")
    if db is None:
        from app_context import db as app_db
        db = app_db

    dialog_data = manager.dialog_data
    service_filter = dialog_data.get("service")
    status_filter = dialog_data.get("status")

    await c.answer("Generating CSV export...")

    records = await db.get_download_history(
        page=1,
        per_page=1000,
        service=service_filter,
        status=status_filter,
    )
    if not records:
        await c.answer("No records found to export.", show_alert=True)
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID",
        "Created At (UTC)",
        "User ID",
        "Username",
        "Name",
        "Chat ID",
        "Chat Type",
        "Service",
        "Status",
        "URL",
        "Title",
        "Duration (s)",
        "Size (bytes)",
        "Error Message",
    ])
    for r in records:
        username = r.user.user_username if getattr(r, "user", None) and r.user.user_username else ""
        name = r.user.user_name if getattr(r, "user", None) and r.user.user_name else ""
        writer.writerow([
            r.id,
            r.created_at.isoformat() if r.created_at else "",
            r.user_id,
            username,
            name,
            r.chat_id or "",
            r.chat_type or "",
            r.service,
            r.status,
            r.url,
            r.title or "",
            r.duration_seconds or "",
            r.file_size_bytes or "",
            r.error_message or "",
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"download_history_{now_str}.csv"
    bot: Bot = manager.middleware_data["bot"]
    if c.message:
        await bot.send_document(
            chat_id=c.message.chat.id,
            document=BufferedInputFile(csv_bytes, filename=filename),
            caption=f"📊 <b>Download History Export</b>\nTotal exported: <b>{len(records)}</b> records",
            parse_mode="HTML",
        )


async def on_back_to_admin(c: types.CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.done()
    from handlers.admin import _render_admin_panel
    if c.message:
        try:
            await _render_admin_panel(c.message, edit=True)
        except Exception:
            await _render_admin_panel(c.message, edit=False)


# Main History Window
history_window = Window(
    Format(
        "📥 <b>Download History</b>\n"
        "Filter: <b>{service_filter}</b> | Status: <b>{status_filter}</b>\n"
        "Total: <b>{total_count}</b> downloads (Page <b>{page}</b>/<b>{total_pages}</b>)\n\n"
        "{history_text}"
    ),
    LinkPreview(is_disabled=True),
    Row(
        SwitchTo(Format("{service_btn_label}"), id="btn_service", state=AdminHistorySG.select_service),
        Button(Format("{status_toggle_label}"), id="btn_toggle_status", on_click=on_toggle_status),
        Button(Const("🔄 Reset"), id="btn_reset_filters", on_click=on_reset_filters),
    ),
    Row(
        Button(Const("◀️ Prev"), id="btn_prev", on_click=on_prev_page),
        Button(Format("📄 {page}/{total_pages}"), id="btn_page_info", on_click=on_page_info),
        Button(Const("Next ▶️"), id="btn_next", on_click=on_next_page),
    ),
    Row(
        SwitchTo(Const("✉️ Message by Chat ID"), id="btn_msg_user", state=AdminHistorySG.message_user),
        Button(Const("📊 Export CSV"), id="btn_export_csv", on_click=on_export_csv),
    ),
    Row(
        Button(Const("🔄 Refresh"), id="btn_refresh", on_click=on_refresh),
        Button(Const("⬅️ Back to Admin"), id="btn_back_admin", on_click=on_back_to_admin),
    ),
    state=AdminHistorySG.history,
    getter=get_history_data,
    parse_mode="HTML",
)

# Select Service Window
select_service_window = Window(
    Format(
        "🎬 <b>Filter by Service</b>\n\n"
        "Current filter: <b>{current_service}</b>\n"
        "Select a platform to filter the download history:"
    ),
    Group(
        Select(
            Format("{item[1]}"),
            id="sel_service",
            item_id_getter=lambda x: x[0],
            items="services",
            on_click=on_service_selected,
        ),
        width=2,
    ),
    SwitchTo(Const("⬅️ Back to History"), id="btn_back_from_svc", state=AdminHistorySG.history),
    state=AdminHistorySG.select_service,
    getter=get_services_data,
    parse_mode="HTML",
)

# Message User Window
message_user_window = Window(
    Const(
        "✉️ <b>Message User by Chat ID</b>\n\n"
        "Select a user from the recent downloads to start composing a message,\n"
        "or enter any custom Chat ID:"
    ),
    Column(
        Select(
            Format("💬 {item[1]} ({item[0]})"),
            id="sel_page_user",
            item_id_getter=lambda x: str(x[0]),
            items="page_users",
            on_click=on_user_selected,
        ),
    ),
    Button(Const("✏️ Enter Custom Chat ID"), id="btn_enter_custom", on_click=on_enter_custom_chat_id),
    SwitchTo(Const("⬅️ Back to History"), id="btn_back_from_msg", state=AdminHistorySG.history),
    state=AdminHistorySG.message_user,
    getter=get_message_user_data,
    parse_mode="HTML",
)

admin_history_dialog = Dialog(
    history_window,
    select_service_window,
    message_user_window,
)
