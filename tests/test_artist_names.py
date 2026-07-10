from services.media.artist_names import normalize_artist_names


def test_normalize_artist_names_deduplicates_structured_provider_values():
    assert normalize_artist_names(
        [
            {"name": "SUDNO"},
            " sudno ",
            {"name": "Sudno"},
            "SUDNO",
            "sudno",
        ]
    ) == "SUDNO"


def test_normalize_artist_names_collapses_repeated_delimited_string():
    assert normalize_artist_names("SUDNO, sudno, SUDNO, sudno, SUDNO") == "SUDNO"


def test_normalize_artist_names_preserves_legitimate_comma_in_single_name():
    assert normalize_artist_names("Tyler, The Creator") == "Tyler, The Creator"


def test_normalize_artist_names_preserves_order_for_distinct_collaborators():
    assert normalize_artist_names(["Artist One", "Artist Two", "artist one"]) == (
        "Artist One, Artist Two"
    )
