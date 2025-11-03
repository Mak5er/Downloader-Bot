def admin_panel(total_count, private_count, group_count, active_user_count, inactive_user_count):
    return ("""<b>Hello, this is the admin panel.</b>

👥 Total chats: <b>{total_count}</b>
   • 👤 Private users: <b>{private_count}</b>
   • 🏘️ Groups: <b>{group_count}</b>

✅ Active: <b>{active_user_count}</b>
🚫 Inactive: <b>{inactive_user_count}</b>

<b>Admin commands:</b>
Coming soon...""").format(
        total_count=total_count,
        private_count=private_count,
        group_count=group_count,
        active_user_count=active_user_count,
        inactive_user_count=inactive_user_count,
    )



def not_groups():
    return ("This command cannot be used in a group!")


def finish_mailing():
    return ("Mailing is complete!")


def start_mailing():
    return ("Starting mailing...")


def mailing_message():
    return ('Enter the message to send:')


def search_user_by():
    return ('Search user by:')


def type_user(search):
    return ('Type user {search}:').format(search=search)


def user_not_found():
    return ("User not found!")


def return_user_info(user_name, user_id, user_username, status):
    return ("""<b>USER INFO</b>
<b>Name</b>: {user_name}
<b>ID</b>: {user_id}
<b>Username</b>: {user_username}
<b>Status</b>: {status}""").format(user_name=user_name, user_id=user_id, user_username=user_username, status=status)


def canceled():
    return ('Action canceled!')


def your_message_sent():
    return ('Your message sent!')


def something_went_wrong():
    return ("Something went wrong, see log for more information!")


def enter_ban_reason():
    return ('Enter ban reason:')


def successful_ban(banned_user_id):
    return ("User {banned_user_id} successfully banned!").format(banned_user_id=banned_user_id)


def successful_unban(unbanned_user_id):
    return ("User {unbanned_user_id} successfully unbanned!").format(unbanned_user_id=unbanned_user_id)


def ban_message(reason):
    return ("🚫 You have been banned, contact @mak5er for more information!\nReason: {reason}").format(reason=reason)


def unban_message():
    return ("🎉 You have been unbanned!")


def please_type_message():
    return ('Please type message:')


def log_deleted():
    return ("Log deleted, starting to write a new one.")


def active_users_check_started(total_users):
    return ("🔄 Starting availability check for {total_users} users...").format(total_users=total_users)


def active_users_check_completed(total_users, reachable_users, unreachable_users):
    return ("""<b>✅ Availability check finished.</b>
👥 Total users processed: <b>{total_users}</b>
📬 Reachable: <b>{reachable_users}</b>
🚫 Unreachable: <b>{unreachable_users}</b>""").format(
        total_users=total_users,
        reachable_users=reachable_users,
        unreachable_users=unreachable_users,
    )


def active_users_check_no_targets():
    return ("ℹ️ There are no users available for checking.")

def enter_chat_id():
    return ("🆔 Enter the chat ID (for example, -1001234567890):")


def invalid_chat_id():
    return ("⚠️ Chat ID must be a number like -1001234567890. Try again or tap Cancel.")


def enter_chat_message():
    return ("✉️ Enter the message you want to send to this chat:")


def chat_message_sent(chat_id):
    return ("✅ Message delivered to chat {chat_id}.").format(chat_id=chat_id)


def chat_message_failed(chat_id):
    return ("⚠️ Failed to send message to chat {chat_id}. Make sure the bot is a member and can write there.").format(chat_id=chat_id)


def chat_message_sending():
    return ("⏳ Sending message...")
