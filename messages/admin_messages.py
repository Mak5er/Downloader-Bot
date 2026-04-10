def admin_panel(total_count, private_count, group_count, active_user_count, inactive_user_count):
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


def mailing_message():
    return "Enter the message to send:"


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


def search_user_by():
    return "Search user by:"


def type_user(search):
    return f"Type user {search}:"


def user_not_found():
    return "User not found!"


def return_user_info(user_name, user_id, user_username, status):
    return ("""<b>USER INFO</b>
<b>Name</b>: {user_name}
<b>ID</b>: {user_id}
<b>Username</b>: {user_username}
<b>Status</b>: {status}""").format(
        user_name=user_name,
        user_id=user_id,
        user_username=user_username,
        status=status,
    )


def canceled():
    return "Action canceled!"


def your_message_sent():
    return "Your message sent!"


def something_went_wrong():
    return "Something went wrong, see log for more information!"


def enter_ban_reason():
    return "Enter ban reason:"


def successful_ban(banned_user_id):
    return f"User {banned_user_id} successfully banned!"


def successful_unban(unbanned_user_id):
    return f"User {unbanned_user_id} successfully unbanned!"


def ban_message(reason):
    return f"You have been banned, contact @mak5er for more information!\nReason: {reason}"


def unban_message():
    return "You have been unbanned!"


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
