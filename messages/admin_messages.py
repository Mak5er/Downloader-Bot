def admin_panel(
    *args,
    total_count=None,
    private_count=None,
    group_count=None,
    active_user_count=None,
    inactive_user_count=None,
    dm_total=None,
    dm_active=None,
    dm_inactive=None,
    dm_banned=None,
    groups_total=None,
    groups_active=None,
    groups_inactive=None,
    total_reach=None,
    tracked_group_members=None,
    **kwargs,
):
    if len(args) == 1 and isinstance(args[0], dict):
        d = args[0]
        dm_total = d.get("dm_total", dm_total)
        dm_active = d.get("dm_active", dm_active)
        dm_inactive = d.get("dm_inactive", dm_inactive)
        dm_banned = d.get("dm_banned", dm_banned)
        groups_total = d.get("groups_total", groups_total)
        groups_active = d.get("groups_active", groups_active)
        groups_inactive = d.get("groups_inactive", groups_inactive)
        total_reach = d.get("total_reach", total_reach)
        tracked_group_members = d.get("tracked_group_members", tracked_group_members)
        total_count = d.get("user_count", total_count)
        private_count = d.get("private_chat_count", private_count)
        group_count = d.get("group_chat_count", group_count)
        active_user_count = d.get("active_user_count", active_user_count)
        inactive_user_count = d.get("inactive_user_count", inactive_user_count)
    elif len(args) == 9:
        (
            dm_total,
            dm_active,
            dm_inactive,
            dm_banned,
            groups_total,
            groups_active,
            groups_inactive,
            total_reach,
            tracked_group_members,
        ) = args
    elif len(args) == 5:
        (
            total_count,
            private_count,
            group_count,
            active_user_count,
            inactive_user_count,
        ) = args

    if dm_total is not None:
        reach_val = total_reach or 0
        return (
            "<b>Hello, this is the admin panel.</b>\n\n"
            "👤 <b>Private Chats (DM):</b>\n"
            f"  ├ Total with DM: <b>{dm_total}</b>\n"
            f"  ├ ✅ Active (reachable): <b>{dm_active or 0}</b>\n"
            f"  ├ 🚫 Inactive (blocked bot): <b>{dm_inactive or 0}</b>\n"
            f"  └ ⛔ Banned: <b>{dm_banned or 0}</b>\n\n"
            "🏘 <b>Groups:</b>\n"
            f"  ├ Total groups: <b>{groups_total or 0}</b>\n"
            f"  ├ ✅ Active (bot member): <b>{groups_active or 0}</b>\n"
            f"  ├ 🚫 Inactive (kicked/left): <b>{groups_inactive or 0}</b>\n"
            f"  ├ 👥 <b>Estimated Reach:</b> <b>~{reach_val:,} members</b>\n"
            f"  └ 💬 Tracked chatters: <b>{tracked_group_members or 0}</b>"
        )

    return ("""<b>Hello, this is the admin panel.</b>

👥 Total chats: <b>{total_count}</b>
👤 Private users: <b>{private_count}</b>
🏘 Groups: <b>{group_count}</b>

✅ Active: <b>{active_user_count}</b>
🚫 Inactive: <b>{inactive_user_count}</b>""").format(
        total_count=total_count,
        private_count=private_count,
        group_count=group_count,
        active_user_count=active_user_count,
        inactive_user_count=inactive_user_count,
    )


def not_groups():
    return "This command cannot be used in a group!"


def finish_mailing():
    return "Mailing is complete!"


def start_mailing():
    return "Starting mailing..."


def mailing_audience_preview(total_users, active_users, inactive_users, banned_users, private_users, group_users):
    return ("""<b>Mailing audience preview</b>
Users to process: <b>{total_users}</b>
Active: <b>{active_users}</b>
Inactive: <b>{inactive_users}</b>
Banned: <b>{banned_users}</b>
Private chats: <b>{private_users}</b>
Groups: <b>{group_users}</b>

Enter the message to send:""").format(
        total_users=total_users,
        active_users=active_users,
        inactive_users=inactive_users,
        banned_users=banned_users,
        private_users=private_users,
        group_users=group_users,
    )


def mailing_select_audience() -> str:
    return (
        "<b>Broadcast Mailing</b>\n\n"
        "Select the target audience for your broadcast:"
    )


def mailing_audience_preview_segmented(
    audience_type: str | None = None,
    total_recipients: int = 0,
    active_count: int = 0,
    inactive_count: int = 0,
    estimated_reach: int = 0,
    *,
    audience_name: str | None = None,
    **kwargs,
) -> str:
    chosen_audience = audience_name if audience_name is not None else (audience_type or "")
    lines = [
        "<b>Mailing audience preview</b>",
        f"Target audience: <b>{chosen_audience}</b>",
        f"Recipients to process: <b>{total_recipients}</b>",
    ]
    if active_count > 0 or inactive_count > 0:
        lines.append(f"Active: <b>{active_count}</b>")
        if inactive_count > 0:
            lines.append(f"Inactive: <b>{inactive_count}</b>")
    if estimated_reach > 0:
        lines.append(f"Estimated reach: <b>~{estimated_reach:,} members</b>")
    lines.extend([
        "",
        "Enter the message to send:",
    ])
    return "\n".join(lines)


def canceled():
    return "Action canceled!"


def your_message_sent():
    return "Your message sent!"


def something_went_wrong():
    return "Something went wrong, see log for more information!"


def please_type_message():
    return "Please type message:"


def log_deleted():
    return "Log deleted, starting to write a new one."


def active_users_check_started(total_users):
    return f"Starting availability check for {total_users} users..."


def active_users_check_completed(total_users, reachable_users, unreachable_users):
    return ("""<b>Availability check finished.</b>
Total users processed: <b>{total_users}</b>
Reachable: <b>{reachable_users}</b>
Unreachable: <b>{unreachable_users}</b>""").format(
        total_users=total_users,
        reachable_users=reachable_users,
        unreachable_users=unreachable_users,
    )


def active_users_check_no_targets():
    return "There are no users available for checking."


def check_groups_started(total_groups: int) -> str:
    return f"Starting availability and reach check for {total_groups} groups..."


def check_groups_completed(
    total_groups: int,
    reachable_groups: int = 0,
    unreachable_groups: int = 0,
    total_reach: int = 0,
    *,
    active_groups: int | None = None,
    kicked_groups: int | None = None,
) -> str:
    reachable = active_groups if active_groups is not None else reachable_groups
    unreachable = kicked_groups if kicked_groups is not None else unreachable_groups
    reach_val = total_reach or 0
    return (
        "<b>Group check finished.</b>\n\n"
        f"Total groups processed: <b>{total_groups}</b>\n"
        f"Reachable (bot member): <b>{reachable}</b>\n"
        f"Unreachable (kicked/left): <b>{unreachable}</b>\n"
        f"Total estimated reach: <b>~{reach_val:,} members</b>"
    )


def check_groups_no_targets() -> str:
    return "There are no active groups available for checking."


active_groups_check_started = check_groups_started
active_groups_check_completed = check_groups_completed
active_groups_check_no_targets = check_groups_no_targets


def enter_chat_id():
    return "Enter the chat ID (for example, -1001234567890):"


def invalid_chat_id():
    return "Chat ID must be a number like -1001234567890. Try again or tap Cancel."


def enter_chat_message():
    return "Enter the message you want to send to this chat:"


def known_chat_target(chat_id, chat_name, chat_username, status):
    return ("""<b>Known chat target</b>
ID: <b>{chat_id}</b>
Name: <b>{chat_name}</b>
Username: <b>{chat_username}</b>
Status: <b>{status}</b>""").format(
        chat_id=chat_id,
        chat_name=chat_name,
        chat_username=chat_username or "—",
        status=status or "unknown",
    )


def unknown_chat_target(chat_id):
    return (
        "Chat {chat_id} is not in the local database yet. "
        "I'll still try to send the message if the bot has access."
    ).format(chat_id=chat_id)


def chat_message_sent(chat_id):
    return f"Message delivered to chat {chat_id}."


def chat_message_failed(chat_id):
    return f"Failed to send message to chat {chat_id}. Make sure the bot is a member and can write there."


def chat_message_sending():
    return "Sending message..."


def downloads_cleanup_blocked(active_jobs, queued_jobs):
    return (
        "Cleanup skipped because downloads are still running. "
        "active_jobs={active_jobs}, queued_jobs={queued_jobs}."
    ).format(active_jobs=active_jobs, queued_jobs=queued_jobs)


def downloads_cleanup_finished(removed_files, removed_dirs, skipped_recent_files):
    return (
        "Downloads cleanup finished. Removed {removed_files} files and {removed_dirs} directories; "
        "skipped {skipped_recent_files} recent files."
    ).format(
        removed_files=removed_files,
        removed_dirs=removed_dirs,
        skipped_recent_files=skipped_recent_files,
    )
