from services.links.detection import detect_supported_service, extract_supported_link, extract_supported_links


def test_detect_supported_service_covers_all_supported_group_guard_links():
    assert detect_supported_service("https://www.instagram.com/p/demo") == "instagram"
    assert detect_supported_service("https://www.tiktok.com/@demo/video/1") == "tiktok"
    assert detect_supported_service("https://soundcloud.com/artist/track") == "soundcloud"
    assert detect_supported_service("https://pin.it/demo123") == "pinterest"
    assert detect_supported_service("https://youtu.be/demo") == "youtube"
    assert detect_supported_service("https://music.youtube.com/watch?v=abc123") == "youtube"
    assert detect_supported_service("https://x.com/demo/status/1") == "twitter"


def test_extract_supported_links_preserves_order_and_deduplicates():
    text = (
        "first https://youtu.be/demo "
        "then https://www.instagram.com/reel/abc/ "
        "again https://youtu.be/demo"
    )

    assert extract_supported_links(text) == [
        ("youtube", "https://youtu.be/demo"),
        ("instagram", "https://www.instagram.com/reel/abc/"),
    ]


def test_extract_supported_link_strips_trailing_sentence_punctuation():
    assert extract_supported_link("watch this https://youtu.be/demo.") == (
        "youtube",
        "https://youtu.be/demo",
    )


def test_extract_supported_links_canonicalizes_tracking_query_params():
    text = (
        "https://www.instagram.com/reel/abc/?utm_source=ig_web_copy_link "
        "https://www.youtube.com/watch?v=abc123&si=noise&utm_source=test&t=30"
    )

    assert extract_supported_links(text) == [
        ("instagram", "https://www.instagram.com/reel/abc/"),
        ("youtube", "https://www.youtube.com/watch?v=abc123&t=30"),
    ]
