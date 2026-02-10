import messages as bm


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

