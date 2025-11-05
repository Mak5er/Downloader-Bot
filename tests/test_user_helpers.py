import datetime
from pathlib import Path

import pytest

from handlers import user


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2025, 1, 2, 15, 30, 0, tzinfo=tz)


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"2025-01-02": 3, "2025-01-01": 1}, [
            (datetime.datetime(2025, 1, 1), 1),
            (datetime.datetime(2025, 1, 2), 3),
        ]),
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


@pytest.mark.parametrize(
    "period,data,expected_contains",
    [
        ("Week", {}, "Statistics for Week"),
        ("Month", {"2025-01-01": 5}, "Total downloads:"),
    ],
)
def test_build_stats_caption(period, data, expected_contains):
    caption = user.build_stats_caption(period, data)
    assert expected_contains in caption


@pytest.mark.asyncio
async def test_create_and_save_chart(tmp_path, monkeypatch):
    monkeypatch.setattr(user, "Path", lambda p: tmp_path / p)
    monkeypatch.setattr(user.datetime, "datetime", FixedDateTime)
    user.plt.switch_backend("Agg")

    chart_path = user.create_and_save_chart({"2025-01-01": 3, "2025-01-02": 5}, "Week")

    path_obj = Path(chart_path)
    assert path_obj.exists()
    assert path_obj.read_bytes()

    path_obj.unlink()
