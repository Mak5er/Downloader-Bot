import keyboards.inline_keyboards as inline_kb


def _flatten_callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_admin_keyboard_collapses_top_sections_and_restores_refresh():
    markup = inline_kb.admin_keyboard()

    callbacks = _flatten_callbacks(markup)

    assert "admin_ops" in callbacks
    assert "admin_runtime_storage" in callbacks
    assert "admin_refresh" in callbacks
    assert "admin_health" not in callbacks
    assert "admin_session" not in callbacks
    assert "admin_perf" not in callbacks
    assert "admin_downloads" not in callbacks


def test_admin_detail_keyboard_uses_custom_refresh_callback():
    markup = inline_kb.admin_detail_keyboard("admin_ops")

    callbacks = _flatten_callbacks(markup)

    assert callbacks == ["admin_ops", "back_to_admin"]


def test_downloads_admin_keyboard_supports_custom_refresh_callback():
    markup = inline_kb.downloads_admin_keyboard(can_cleanup=True, refresh_callback="admin_runtime_storage")

    callbacks = _flatten_callbacks(markup)

    assert callbacks == ["admin_runtime_storage", "admin_cleanup_downloads", "back_to_admin"]
