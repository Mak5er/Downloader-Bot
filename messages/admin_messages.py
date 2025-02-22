def admin_panel(user_count, active_user_count, inactive_user_count):
    return ("""<b>Hello, this is the admin panel.</b>

🪪Number of bot users: <b>{user_count}</b>
📱Number of active users: <b>{active_user_count}</b>
📵Number of inactive users: <b>{inactive_user_count}</b>

<b>Admin commands:</b>
Coming soon...""").format(user_count=user_count,
                          active_user_count=active_user_count,
                          inactive_user_count=inactive_user_count, )


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
    return ("🚫You have been banned, contact @mak5er for more information!\nReason: {reason}").format(reason=reason)


def unban_message():
    return ("🎉You have been unbanned!")


def please_type_message():
    return ('Please type message:')


def log_deleted():
    return ("Log deleted, starting to write a new one.")
