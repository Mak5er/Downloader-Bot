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


def settings_private_only():
    return (
        "Settings are available only in a private chat. "
        "Please message the bot directly to update your preferences."
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
        "audio_button": (
            "<b>🎧 MP3 Button</b>\n"
            "Show or hide the Download MP3 button under videos with audio."
        ),
    }
    return texts.get(field, "<b>Settings</b>\nNo description available for this option.")


def captions(user_captions, post_caption, bot_url, *, limit: int = 1024):
    import html

    def _truncate_escaped(value: str, max_len: int) -> str:
        if max_len <= 0:
            return ""
        if len(value) <= max_len:
            return value
        cut = value[:max_len]
        # Avoid ending in the middle of an HTML entity (e.g. "&amp").
        amp = cut.rfind("&")
        semi = cut.rfind(";")
        if amp > semi:
            cut = cut[:amp]
        return cut

    footer = '🚀 Powered by <a href="{bot_url}">MaxLoad</a>'.format(bot_url=bot_url)

    if user_captions == "on" and post_caption:
        body = html.escape(str(post_caption))
        sep = "\n\n"
        # Keep footer intact; only shrink the body.
        budget = limit - len(sep) - len(footer)
        if budget <= 0:
            return _truncate_escaped(footer, limit)

        if len(body) > budget:
            suffix = "…"
            body = _truncate_escaped(body, max(0, budget - len(suffix))).rstrip() + suffix

        return f"{body}{sep}{footer}"

    return _truncate_escaped(footer, limit)


def downloading_audio_status():
    return "🎧 Downloading audio, please wait..."


def downloading_video_status():
    return "🎬 Downloading video, please wait..."


def fetching_info_status():
    return "🔎 Fetching info..."


def uploading_status():
    return "☁️ Uploading to Telegram..."


def timeout_error():
    return "⏱️ Timed out. Please try again later."


def dm_start_required():
    return "🔒 Please open the bot in private chat and press Start so I can process your link."


def settings_admin_only():
    return "Only group admins can use /settings in group chats."


def join_group(chat_title: str) -> str:
    return (
        "👋 Hi! Thanks for adding me to <b>{chat_title}</b> 🌸\n"
        "Please grant me <b>admin rights</b> to unlock full functionality 🔓"
    ).format(chat_title=chat_title)


def admin_rights_granted(chat_title: str) -> str:
    return (
        "Thanks for granting admin rights in <b>{chat_title}</b> 🌸\n"
        "💻 I'll keep downloads running smoothly."
    ).format(chat_title=chat_title)


def something_went_wrong():
    return "Something went wrong :(\nPlease try again later."


def video_too_large():
    return "The video is too large."


def audio_too_large():
    return "The audio is too large."


def nothing_found():
    return "Nothing found. Please check the link and try again."


def keyboard_removed():
    return "Keyboard removed."


def tiktok_live_not_supported():
    return "TikTok LIVE streams are not supported yet."
