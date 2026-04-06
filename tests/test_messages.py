import messages as bm
import messages.admin_messages as admin_bm
import pytest


def test_captions_truncates_to_telegram_media_limit():
    long_text = "A" * 5000
    out = bm.captions("on", long_text, "t.me/testbot")
    assert isinstance(out, str)
    assert len(out) <= 1024
    assert "Powered by" in out


def test_captions_escapes_html_in_user_text():
    text = "<b>bold</b> & <i>italics</i>"
    out = bm.captions("on", text, "t.me/testbot")
    # Footer is HTML, user content must be escaped.
    assert "&lt;b&gt;bold&lt;/b&gt;" in out
    assert "&amp;" in out


def test_captions_supports_larger_limits_for_plain_messages():
    long_text = "B" * 8000
    out = bm.captions("on", long_text, "t.me/testbot", limit=4096)
    assert len(out) <= 4096


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (bm.cancel, "Cancel"),
        (bm.welcome_message, "SoundCloud"),
        (bm.settings, "Settings"),
        (bm.settings_private_only, "private chat"),
        (bm.downloading_audio_status, "Downloading audio"),
        (bm.downloading_video_status, "Downloading video"),
        (bm.uploading_status, "Uploading"),
        (bm.timeout_error, "timed out"),
        (bm.dm_start_required, "First-time setup"),
        (bm.settings_admin_only, "group admins"),
        (bm.video_too_large, "too large"),
        (bm.audio_too_large, "too large"),
        (bm.nothing_found, "No media found"),
        (bm.keyboard_removed, "removed"),
        (bm.tiktok_live_not_supported, "LIVE"),
        (bm.delete_permission_warning, "Auto-delete"),
        (bm.stats_temporarily_unavailable, "stats"),
        (bm.no_queue_metrics_yet, "queue metrics"),
        (bm.open_bot_for_audio, "private chat"),
        (bm.audio_fetch_failed, "audio info"),
        (bm.audio_download_failed, "Audio download failed"),
        (bm.inline_album_link_invalid, "expired"),
        (bm.inline_photo_description, "Single photo"),
        (bm.inline_album_description, "album"),
        (bm.inline_open_full_album_button, "Open Full Album"),
        (bm.inline_send_video_button, "Send video inline"),
        (bm.inline_video_already_processing, "already being prepared"),
        (bm.inline_video_already_sent, "already sent"),
    ],
)
def test_user_message_factories_return_expected_text(factory, expected):
    assert expected in factory()


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("captions", "Descriptions"),
        ("delete_message", "Delete Messages"),
        ("info_buttons", "Info Buttons"),
        ("url_button", "URL Button"),
        ("audio_button", "MP3 Button"),
    ],
)
def test_get_field_text_returns_expected_descriptions(field, expected):
    assert expected in bm.get_field_text(field)


def test_user_message_formatters_include_dynamic_values():
    assert "3/5" in bm.retrying_again_status(3, 5)
    assert "My Chat" in bm.join_group("My Chat")
    assert "My Chat" in bm.admin_rights_granted("My Chat")
    assert bm.inline_photo_title("TikTok") == "TikTok Photo"
    assert bm.inline_album_title("Instagram") == "Instagram Album"
    assert bm.inline_photos_title("Pinterest") == "Pinterest Photos"
    assert "TikTok photos" in bm.inline_photos_not_supported("TikTok")
    assert "YouTube video" in bm.inline_send_video_prompt("YouTube")
    assert "SoundCloud audio" in bm.inline_send_audio_prompt("SoundCloud")


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (admin_bm.not_groups, "group"),
        (admin_bm.finish_mailing, "complete"),
        (admin_bm.start_mailing, "Starting"),
        (admin_bm.mailing_message, "Enter the message"),
        (admin_bm.search_user_by, "Search user"),
        (admin_bm.user_not_found, "not found"),
        (admin_bm.canceled, "canceled"),
        (admin_bm.your_message_sent, "sent"),
        (admin_bm.something_went_wrong, "Something went wrong"),
        (admin_bm.enter_ban_reason, "ban reason"),
        (admin_bm.unban_message, "unbanned"),
        (admin_bm.please_type_message, "Please type message"),
        (admin_bm.log_deleted, "Log deleted"),
        (admin_bm.active_users_check_no_targets, "no users"),
        (admin_bm.enter_chat_id, "chat ID"),
        (admin_bm.invalid_chat_id, "must be a number"),
        (admin_bm.enter_chat_message, "Enter the message"),
        (admin_bm.chat_message_sending, "Sending message"),
    ],
)
def test_admin_message_factories_return_expected_text(factory, expected):
    assert expected in factory()


def test_admin_message_formatters_include_dynamic_values():
    panel = admin_bm.admin_panel(10, 7, 3, 8, 2)
    assert "10" in panel
    assert "7" in panel
    assert "3" in panel
    assert admin_bm.type_user("username") == "Type user username:"
    info = admin_bm.return_user_info("Alice", 42, "@alice", "active")
    assert "Alice" in info
    assert "42" in info
    assert "@alice" in info
    assert "active" in info
    assert "99" in admin_bm.successful_ban(99)
    assert "11" in admin_bm.successful_unban(11)
    assert "spam" in admin_bm.ban_message("spam")
    assert "55" in admin_bm.active_users_check_started(55)
    completed = admin_bm.active_users_check_completed(12, 10, 2)
    assert "12" in completed
    assert "10" in completed
    assert "2" in completed
    assert "77" in admin_bm.chat_message_sent(77)
    assert "77" in admin_bm.chat_message_failed(77)

