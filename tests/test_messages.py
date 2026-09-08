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
        (bm.spotify_metadata_failed, "Spotify"),
        (bm.spotify_source_not_found, "Spotify"),
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
        (admin_bm.canceled, "canceled"),
        (admin_bm.your_message_sent, "sent"),
        (admin_bm.something_went_wrong, "Something went wrong"),
        (admin_bm.please_type_message, "Please type message"),
        (admin_bm.log_deleted, "Log deleted"),
        (admin_bm.active_users_check_no_targets, "no users"),
        (admin_bm.enter_chat_id, "chat ID"),
        (admin_bm.invalid_chat_id, "must be a number"),
        (admin_bm.enter_chat_message, "Enter the message"),
        (admin_bm.chat_message_sending, "Sending message"),
        (lambda: admin_bm.unknown_chat_target(77), "not in the local database"),
        (lambda: admin_bm.downloads_cleanup_blocked(1, 2), "downloads are still running"),
        (lambda: admin_bm.downloads_cleanup_finished(3, 4, 5), "Removed 3 files"),
        (admin_bm.check_groups_no_targets, "no active groups"),
        (admin_bm.mailing_select_audience, "Select the target audience"),
    ],
)
def test_admin_message_factories_return_expected_text(factory, expected):
    assert expected in factory()


def test_admin_message_formatters_include_dynamic_values():
    panel = admin_bm.admin_panel(10, 7, 3, 8, 2)
    assert "10" in panel
    assert "7" in panel
    assert "3" in panel
    assert "55" in admin_bm.active_users_check_started(55)
    completed = admin_bm.active_users_check_completed(12, 10, 2)
    assert "12" in completed
    assert "10" in completed
    assert "2" in completed
    assert "77" in admin_bm.chat_message_sent(77)
    assert "77" in admin_bm.chat_message_failed(77)
    preview = admin_bm.mailing_audience_preview(10, 7, 2, 1, 6, 4)
    assert "10" in preview
    assert "7" in preview
    known = admin_bm.known_chat_target(88, "Ops Chat", "@ops", "active")
    assert "Ops Chat" in known
    assert "@ops" in known


def test_admin_panel_supports_all_calling_conventions():
    # 9 positional arguments
    panel_9 = admin_bm.admin_panel(100, 80, 15, 5, 10, 8, 2, 5000, 120)
    assert "Private Chats (DM):" in panel_9
    assert "100" in panel_9
    assert "80" in panel_9
    assert "15" in panel_9
    assert "5" in panel_9
    assert "10" in panel_9
    assert "8" in panel_9
    assert "2" in panel_9
    assert "~5,000 members" in panel_9
    assert "120" in panel_9

    # 5 positional arguments (legacy fallback)
    panel_5 = admin_bm.admin_panel(10, 7, 3, 8, 2)
    assert "Total chats: <b>10</b>" in panel_5
    assert "Private users: <b>7</b>" in panel_5
    assert "Private Chats (DM):" not in panel_5

    # Dict argument
    panel_dict = admin_bm.admin_panel({"dm_total": 50, "total_reach": 1000, "dm_active": 45})
    assert "Total with DM: <b>50</b>" in panel_dict
    assert "~1,000 members" in panel_dict
    assert "Active (reachable): <b>45</b>" in panel_dict

    # Keyword arguments
    panel_kw = admin_bm.admin_panel(dm_total=200, groups_total=5, total_reach=3000)
    assert "Total with DM: <b>200</b>" in panel_kw
    assert "Total groups: <b>5</b>" in panel_kw
    assert "~3,000 members" in panel_kw


def test_check_groups_and_segmented_mailing_templates():
    started = admin_bm.check_groups_started(5)
    assert "5 groups" in started

    # Test with reachable_groups and unreachable_groups
    completed_pos = admin_bm.check_groups_completed(5, 4, 1, 1200)
    assert "Group check finished." in completed_pos
    assert "Reachable (bot member): <b>4</b>" in completed_pos
    assert "Unreachable (kicked/left): <b>1</b>" in completed_pos
    assert "~1,200 members" in completed_pos

    # Test with active_groups and kicked_groups keyword arguments
    completed_kw = admin_bm.check_groups_completed(5, active_groups=3, kicked_groups=2, total_reach=950)
    assert "Reachable (bot member): <b>3</b>" in completed_kw
    assert "Unreachable (kicked/left): <b>2</b>" in completed_kw
    assert "~950 members" in completed_kw

    # Backward compatibility aliases
    assert admin_bm.active_groups_check_started is admin_bm.check_groups_started
    assert admin_bm.active_groups_check_completed is admin_bm.check_groups_completed
    assert admin_bm.active_groups_check_no_targets is admin_bm.check_groups_no_targets

    # Segmented mailing preview
    preview = admin_bm.mailing_audience_preview_segmented(
        audience_name="Groups only",
        total_recipients=12,
        active_count=10,
        inactive_count=2,
        estimated_reach=3400,
    )
    assert "Mailing audience preview" in preview
    assert "Groups only" in preview
    assert "12" in preview
    assert "Active: <b>10</b>" in preview
    assert "Inactive: <b>2</b>" in preview
    assert "~3,400 members" in preview
