"""Retrieval coverage: what is on disk versus what should be.

`scan_local` identifies a vintage from its filename, so these tests write
named placeholder files rather than real archives -- the question under test
is which publications exist, not what is inside them.
"""

from __future__ import annotations

import datetime as dt

import pytest

from forecast_spine import coverage, ercot
from forecast_spine.ercot_api import mis_filename
from forecast_spine.time import CENTRAL

FORECAST = ercot.REPORTS["load_forecast"]
ACTUAL = ercot.REPORTS["actual_load"]


def _place(raw_root, report, report_key, day: dt.date, hours, minute=30):
    directory = raw_root / report_key
    directory.mkdir(parents=True, exist_ok=True)
    for hour in hours:
        published = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=CENTRAL)
        name = mis_filename(report, published.astimezone(dt.UTC))
        (directory / name).write_bytes(b"PK placeholder")


def test_a_fully_retrieved_day_reports_complete(tmp_path):
    day = dt.date(2026, 3, 20)
    _place(tmp_path, FORECAST, "load_forecast", day, range(24))
    result = coverage.measure("load_forecast", day, day, tmp_path)
    assert result.complete
    assert (result.held, result.expected) == (24, 24)
    assert result.missing_hours == ()
    assert "complete" in result.summary()


def test_missing_publications_are_named_not_just_counted(tmp_path):
    day = dt.date(2026, 3, 6)
    present = [h for h in range(24) if h not in (4, 8, 10, 11, 12)]
    _place(tmp_path, FORECAST, "load_forecast", day, present)

    result = coverage.measure("load_forecast", day, day, tmp_path)
    assert not result.complete
    assert (result.held, result.expected) == (19, 24)
    assert [m.hour for m in result.missing_hours] == [4, 8, 10, 11, 12]
    assert all(m.date() == day for m in result.missing_hours)


def test_spring_forward_expects_twenty_three_publications(tmp_path):
    """The hour that does not exist cannot have been published."""
    day = dt.date(2026, 3, 8)
    _place(tmp_path, FORECAST, "load_forecast", day, [h for h in range(24) if h != 2])
    result = coverage.measure("load_forecast", day, day, tmp_path)
    assert result.expected == 23, "a 23-hour day must not be reported as missing an hour"
    assert result.complete


def test_fall_back_expects_twenty_five_publications(tmp_path):
    day = dt.date(2026, 11, 1)
    _place(tmp_path, FORECAST, "load_forecast", day, range(24))
    result = coverage.measure("load_forecast", day, day, tmp_path)
    assert result.expected == 25
    assert not result.complete, "the repeated hour is a real, separate publication"


def test_a_daily_report_expects_one_publication_per_day(tmp_path):
    start, end = dt.date(2026, 3, 1), dt.date(2026, 3, 5)
    day = start
    while day <= end:
        _place(tmp_path, ACTUAL, "actual_load", day, [5], minute=50)
        day += dt.timedelta(days=1)
    result = coverage.measure("actual_load", start, end, tmp_path)
    assert (result.held, result.expected) == (5, 5)
    assert result.complete


def test_an_entirely_absent_day_is_reported_rather_than_skipped(tmp_path):
    start, end = dt.date(2026, 3, 1), dt.date(2026, 3, 3)
    _place(tmp_path, FORECAST, "load_forecast", dt.date(2026, 3, 2), range(24))
    result = coverage.measure("load_forecast", start, end, tmp_path)
    assert [d.held for d in result.days] == [0, 24, 0]
    assert len(result.missing_hours) == 48
    assert "2026-03-01" in result.summary() and "2026-03-03" in result.summary()


@pytest.mark.parametrize("report_key", ["load_forecast", "actual_load"])
def test_an_empty_directory_reports_everything_missing(tmp_path, report_key):
    day = dt.date(2026, 3, 20)
    result = coverage.measure(report_key, day, day, tmp_path)
    assert result.held == 0 and not result.complete
