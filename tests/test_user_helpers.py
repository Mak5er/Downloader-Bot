import datetime
from unittest.mock import AsyncMock

import pytest

from handlers import user
from services.db import StatsSnapshot


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2025, 1, 2, 15, 30, 0, tzinfo=tz)


@pytest.fixture(autouse=True)
def clear_stats_caches():
    user._stats_snapshot_cache.clear()
    user._stats_chart_cache.clear()
    yield
    user._stats_snapshot_cache.clear()
    user._stats_chart_cache.clear()


@pytest.mark.parametrize(
    "data,expected",
    [
        (
            {"2025-01-02": 3, "2025-01-01": 1},
            [
                (datetime.datetime(2025, 1, 1), 1),
                (datetime.datetime(2025, 1, 2), 3),
            ],
        ),
        ({}, []),
    ],
)
def test_prepare_series_sorts_dates(data, expected):
    assert user._prepare_series(data) == expected


def test_decimate_series_limits_points():
    dates = [datetime.datetime(2025, 1, day + 1) for day in range(10)]
    counts = list(range(10))

    sampled_dates, sampled_counts = user._decimate_series(dates, counts, max_points=4)

    assert len(sampled_dates) < len(dates)
    assert sampled_dates[-1] == dates[-1]
    assert sampled_counts[-1] == counts[-1]


def test_prepare_series_for_year_aggregates_fixed_month_axis(monkeypatch):
    monkeypatch.setattr(user.datetime, "datetime", FixedDateTime)
    dates, counts = user._prepare_series_for_period(
        {
            "2024-02-01": 2,
            "2024-02-12": 3,
            "2025-01-01": 5,
        },
        "Year",
    )

    assert len(dates) == 12
    assert dates[-1] == datetime.datetime(2025, 1, 1)
    assert counts[-1] == 5
    assert 5 in counts


def test_build_stats_caption_empty():
    caption = user.build_stats_caption("Week", StatsSnapshot())
    assert "Statistics for Week" in caption
    assert "No downloads recorded" in caption


def test_build_stats_caption_split_includes_top_platforms():
    snapshot = StatsSnapshot(
        totals_by_date={"2025-01-01": 6, "2025-01-02": 4},
        by_service={
            "Instagram": {"2025-01-01": 4},
            "TikTok": {"2025-01-01": 3},
            "YouTube": {"2025-01-02": 2},
            "Other": {"2025-01-02": 1},
        },
        service_totals={"Instagram": 4, "TikTok": 3, "YouTube": 2, "Other": 1},
        total_downloads=10,
    )

    caption = user.build_stats_caption("Month", snapshot, "split")

    assert "Total downloads: <b>10</b>" in caption
    assert "<b>Top platforms</b>" in caption
    assert "Instagram: <b>4</b> (40%)" in caption


@pytest.mark.asyncio
async def test_fetch_stats_snapshot_uses_cache(monkeypatch):
    snapshot = StatsSnapshot(total_downloads=3)
    fake_db = type("FakeDb", (), {"get_download_stats": AsyncMock(return_value=snapshot)})()
    monkeypatch.setattr(user, "db", fake_db)

    first = await user.fetch_stats_snapshot("Week")
    second = await user.fetch_stats_snapshot("Week")

    assert first is snapshot
    assert second is snapshot
    fake_db.get_download_stats.assert_awaited_once_with("Week")


@pytest.mark.asyncio
async def test_fetch_stats_snapshot_refreshes_and_clears_chart_cache(monkeypatch):
    fake_db = type(
        "FakeDb",
        (),
        {"get_download_stats": AsyncMock(return_value=StatsSnapshot(total_downloads=1))},
    )()
    monkeypatch.setattr(user, "db", fake_db)
    user._stats_snapshot_cache["Week"] = (0.0, StatsSnapshot(total_downloads=9))
    user._stats_chart_cache[("Week", "total")] = (user.time.monotonic(), b"stale")

    await user.fetch_stats_snapshot("Week")

    assert ("Week", "total") not in user._stats_chart_cache
    fake_db.get_download_stats.assert_awaited_once_with("Week")


def test_render_stats_chart_returns_png_bytes_without_filesystem_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(user.datetime, "datetime", FixedDateTime)
    user.plt.switch_backend("Agg")
    snapshot = StatsSnapshot(
        totals_by_date={"2025-01-01": 3, "2025-01-02": 5},
        total_downloads=8,
    )

    chart_bytes = user.render_stats_chart(snapshot, "Week", "total")

    assert chart_bytes.startswith(b"\x89PNG")
    assert list(tmp_path.iterdir()) == []


def test_render_stats_chart_uses_cache(monkeypatch):
    monkeypatch.setattr(user.datetime, "datetime", FixedDateTime)
    user.plt.switch_backend("Agg")
    snapshot = StatsSnapshot(
        totals_by_date={"2025-01-01": 3, "2025-01-02": 5},
        total_downloads=8,
    )

    first = user.render_stats_chart(snapshot, "Week", "total")
    second = user.render_stats_chart(snapshot, "Week", "total")

    assert first == second
