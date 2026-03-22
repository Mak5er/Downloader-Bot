def cancel():
    return "✖️ Cancel"


def welcome_message():
    return (
        '<b>Welcome to MaxLoad <tg-emoji emoji-id="5420141555233071341">❤️</tg-emoji></b>\n\n'
        "Drop a link and I'll download media from:\n"
        '<tg-emoji emoji-id="5233671414023753035">📷</tg-emoji> Instagram\n'
        '<tg-emoji emoji-id="5233597424622144804">🎵</tg-emoji> TikTok\n'
        '<tg-emoji emoji-id="5233311027612913110">▶️</tg-emoji> YouTube\n'
        '<tg-emoji emoji-id="5231309843435919433">🐦</tg-emoji> X (Twitter)\n'
        '<tg-emoji emoji-id="5233448977667492819">🎧</tg-emoji> SoundCloud\n'
        '<tg-emoji emoji-id="5233210422298974231">📌</tg-emoji> Pinterest\n\n'
        "Use /settings to customize captions, buttons, and chat auto-delete."
    )


def settings():
    return (
        "<b>⚙️ Settings</b>\n"
        "Use the buttons below to customize how downloads are sent. "
        "These changes apply only to your account."
    )


def settings_private_only():
    return (
        "Settings are available only in private chat. Open DM with the bot to change preferences."
    )


def get_field_text(field: str):
    texts = {
        "captions": (
            "<b>📝 Descriptions</b>\n"
            "Show or hide post captions in downloaded media. "
            "Some sources may not provide captions."
        ),
        "delete_message": (
            "<b>🗑️ Delete Messages</b>\n"
            "Automatically remove your link once the download is handled."
        ),
        "info_buttons": (
            "<b>ℹ️ Info Buttons</b>\n"
            "Toggle additional info buttons under downloaded media."
        ),
        "url_button": (
            "<b>🔗 URL Button</b>\n"
            "Show or hide a button with the original post link."
        ),
        "audio_button": (
            "<b>🎧 MP3 Button</b>\n"
            "Toggle the Download MP3 button when audio is available."
        ),
    }
    return texts.get(field, "<b>Settings</b>\nThis option doesn't have a description yet.")


def captions(user_captions, post_caption, bot_url, *, limit: int = 1024):
    import html

    def _truncate_escaped(value: str, max_len: int) -> str:
        if max_len <= 0:
            return ""
        if len(value) <= max_len:
            return value
        cut = value[:max_len]
        amp = cut.rfind("&")
        semi = cut.rfind(";")
        if amp > semi:
            cut = cut[:amp]
        return cut

    footer = '<tg-emoji emoji-id="5283080528818360566">🚀</tg-emoji> Powered by <a href="{bot_url}">MaxLoad</a>'.format(bot_url=bot_url)

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
    return "🎧 Downloading audio..."


def downloading_video_status():
    return "<tg-emoji emoji-id='5375464961822695044'>🎬</tg-emoji> Downloading video..."



def uploading_status():
    return "☁️ Uploading file to Telegram..."


def timeout_error():
    return "<tg-emoji emoji-id='5413704112220949842'>⏰</tg-emoji> Request timed out. Please try again later."


def retrying_again_status(next_attempt: int, total_attempts: int):
    return f"Error, trying again... ({next_attempt}/{total_attempts})"


def dm_start_required():
    return "<tg-emoji emoji-id='5472308992514464048'>🔒</tg-emoji> First-time setup needed: open DM, press Start, and resend the link."


def settings_admin_only():
    return "Only group admins can open /settings in group chats."


def join_group(chat_title: str) -> str:
    return (
        "Thanks for adding me to <b>{chat_title}</b> <tg-emoji emoji-id='5280764381804650651'>🌸</tg-emoji>\n"
        "Please grant me <b>admin rights</b> to unlock full functionality 🔓"
    ).format(chat_title=chat_title)


def admin_rights_granted(chat_title: str) -> str:
    return (
        "Thanks for granting admin rights in <b>{chat_title}</b> <tg-emoji emoji-id='5280764381804650651'>🌸</tg-emoji>\n"
        "💻 I'll keep downloads running smoothly."
    ).format(chat_title=chat_title)


def something_went_wrong():
    return "<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Couldn't process this link right now. \nPlease try again later."


def video_too_large():
    return "The video is too large for Telegram."


def audio_too_large():
    return "The audio is too large for Telegram."


def nothing_found():
    return (
        "No media found. Check the link and try again."
    )


def keyboard_removed():
    return "Reply keyboard removed."


def tiktok_live_not_supported():
    return "TikTok LIVE streams aren't supported yet. Send a regular TikTok post link."


def delete_permission_warning():
    return "Auto-delete failed: missing permission to delete messages in this chat. Please grant delete permissions or turn off auto-delete in settings."


def stats_temporarily_unavailable():
    return "Couldn't generate stats right now. Please try again later."


def no_queue_metrics_yet():
    return "No queue metrics yet."


def open_bot_for_audio():
    return "Open the bot in private chat to download audio."


def audio_fetch_failed():
    return "Failed to get audio info. Please try again later."


def audio_download_failed():
    return "Audio download failed. Please try again later."


def inline_album_link_invalid():
    return "This album link is expired or invalid."


def inline_photo_title(service_name: str):
    return f"{service_name} Photo"


def inline_photo_description():
    return "Single photo"


def inline_album_title(service_name: str):
    return f"{service_name} Album"


def inline_album_description():
    return "Open full album in bot"


def inline_open_full_album_button():
    return "Open Full Album"


def inline_photos_title(service_name: str):
    return f"{service_name} Photos"


def inline_photos_not_supported(service_name: str):
    return f"{service_name} photos are not supported inline."


def inline_send_video_button():
    return "Send video inline"


def inline_send_video_prompt(service_name: str):
    return f"{service_name} video is being prepared...\nIf it does not start automatically, tap the button below."


def inline_send_audio_prompt(service_name: str):
    return f"{service_name} audio is being prepared...\nIf it does not start automatically, tap the button below."


def inline_video_already_processing():
    return "This inline video is already being prepared."


def inline_video_already_sent():
    return "This inline video was already sent."
