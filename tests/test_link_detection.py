from services.links.detection import detect_supported_service


def test_detect_supported_service_covers_all_supported_group_guard_links():
    assert detect_supported_service("https://www.instagram.com/p/demo") == "instagram"
    assert detect_supported_service("https://www.tiktok.com/@demo/video/1") == "tiktok"
    assert detect_supported_service("https://soundcloud.com/artist/track") == "soundcloud"
    assert detect_supported_service("https://pin.it/demo123") == "pinterest"
    assert detect_supported_service("https://youtu.be/demo") == "youtube"
    assert detect_supported_service("https://x.com/demo/status/1") == "twitter"
