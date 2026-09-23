"""Evaluation-window selection.

An assignment names a date range; the default heuristic names a day count.
Both must produce the same window when they describe the same thing, and an
impossible window must be refused rather than silently producing an empty
dataset that every gate then passes.
"""

from __future__ import annotations

import datetime as dt

import pytest

from forecast_spine import fixtures, pipeline


@pytest.fixture
def raw(tmp_path):
    return fixtures.build("pass", tmp_path)


def test_explicit_bounds_select_exactly_the_named_operating_days(raw):
    context = pipeline.build_context(
        dt.date(2026, 3, 24), raw,
        window_start=dt.date(2026, 2, 22), window_end=dt.date(2026, 3, 23),
    )
    assert context.window_operating_dates == (dt.date(2026, 2, 22), dt.date(2026, 3, 23))


def test_a_day_count_and_the_equivalent_range_agree(raw):
    processing_date = dt.date(2026, 3, 24)
    by_count = pipeline.build_context(processing_date, raw, window_days=30)
    by_range = pipeline.build_context(
        processing_date, raw,
        window_start=dt.date(2026, 2, 22), window_end=dt.date(2026, 3, 23),
    )
    assert by_count.window_operating_dates == by_range.window_operating_dates
    assert by_count.window_start_utc == by_range.window_start_utc
    assert by_count.window_end_utc == by_range.window_end_utc


def test_an_inverted_window_is_refused(raw):
    with pytest.raises(ValueError, match="starts after it ends"):
        pipeline.build_context(
            dt.date(2026, 3, 24), raw,
            window_start=dt.date(2026, 3, 23), window_end=dt.date(2026, 2, 22),
        )


def test_a_window_reaching_the_processing_date_is_refused(raw):
    """Actuals for day D are published on D+1, so D is not yet evaluable.

    Allowing it would produce a window whose last day has no actuals, which
    reads downstream as missing data rather than as a bad request.
    """
    with pytest.raises(ValueError, match="not published until the next morning"):
        pipeline.build_context(
            dt.date(2026, 3, 24), raw,
            window_start=dt.date(2026, 3, 1), window_end=dt.date(2026, 3, 24),
        )


def test_window_bounds_are_local_operating_days_not_utc_days(raw):
    """The window is expressed in ERCOT local time, which is not UTC midnight."""
    context = pipeline.build_context(
        dt.date(2026, 3, 24), raw,
        window_start=dt.date(2026, 3, 9), window_end=dt.date(2026, 3, 9),
    )
    # 2026-03-09 is after spring-forward, so local midnight is 05:00 UTC.
    assert context.window_start_utc == dt.datetime(2026, 3, 9, 5, tzinfo=dt.UTC)
    assert context.window_end_utc == dt.datetime(2026, 3, 10, 5, tzinfo=dt.UTC)


def test_a_window_spanning_spring_forward_is_one_hour_short_of_a_round_day_count(raw):
    """23 + 24 hours, not 48: the window is real time, not nominal days."""
    context = pipeline.build_context(
        dt.date(2026, 3, 24), raw,
        window_start=dt.date(2026, 3, 8), window_end=dt.date(2026, 3, 9),
    )
    span = context.window_end_utc - context.window_start_utc
    assert span == dt.timedelta(hours=47)
