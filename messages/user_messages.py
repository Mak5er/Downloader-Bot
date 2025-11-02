def cancel():
    return "❌ Cancel"


def welcome_message():
    return "Welcome to MaxLoad Downloader! Send me a link to download the video."


def settings():
    return (
        "<b>⚙️ Settings</b>\n"
        "Using the buttons below, you can customize the bot's functionalities. "
        "Keep in mind that all the changes made will only apply to you."
    )


def get_field_text(field: str):
    texts = {
        "captions": (
            "<b>📝 Descriptions</b>\n"
            "Choose if you want to add a short description to downloaded content. "
            "Keep in mind that some extractors still don't support this feature."
        ),
        "delete_message": (
            "<b>🗑️ Delete Messages</b>\n"
            "Automatically delete URLs after they are processed. "
            "Useful if you want to keep your chat clean."
        ),
        "info_buttons": (
            "<b>ℹ️ Info Buttons</b>\n"
            "Show or hide additional info buttons in messages."
        ),
        "url_button": (
            "<b>🔗 URL Button</b>\n"
            "Enable or disable a button with the direct URL to the downloaded content."
        ),
    }
    return texts.get(field, "<b>Settings</b>\nNo description available for this option.")


def captions(user_captions, post_caption, bot_url):
    footer = '🚀 Powered by <a href="{bot_url}">MaxLoad</a>'.format(bot_url=bot_url)
    if user_captions == "on" and post_caption:
        return "{post_caption}\n\n{footer}".format(post_caption=post_caption, footer=footer)
    return footer


def join_group(chat_title):
    return "Hi! Thank you for adding me to <b>{chat_title}</b>!\nHave a nice day!".format(chat_title=chat_title)


def something_went_wrong():
    return "Something went wrong :(\nPlease try again later."


def video_too_large():
    return "The video is too large."


def audio_too_large():
    return "The audio is too large."


def nothing_found():
    return None


def keyboard_removed():
    return "Keyboard removed."
