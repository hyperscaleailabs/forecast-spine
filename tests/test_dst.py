"""Daylight saving transitions, treated as a data problem rather than a note.

The evaluated window in a real deployment crosses spring-forward, so a local
operating date is not reliably 24 hours long. Fall-back is outside any window
we can download today, so it is covered synthetically -- the question is
asked in the memo and must have a tested answer.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import add_actual, add_forecast, asof_rows, context, local

from forecast_spine.time import (
    TemporalError,
    expected_hours,
    hours_in_operating_date,
    resolve_hour,
)

SPRING_FORWARD = dt.date(2026, 3, 8)
FALL_BACK = dt.date(2026, 11, 1)
NORMAL = dt.date(2026, 9, 22)


def test_operating_dates_have_23_24_or_25_hours():
    assert hours_in_operating_date(SPRING_FORWARD) == 23
    assert hours_in_operating_date(NORMAL) == 24
    assert hours_in_operating_date(FALL_BACK) == 25


def test_spring_forward_skips_the_nonexistent_hour():
    hours = [hour for hour, _ in expected_hours(SPRING_FORWARD)]
    assert 3 not in hours, "local 02:00-03:00 does not exist on spring-forward day"
    assert hours == [1, 2] + list(range(4, 25))

    with pytest.raises(TemporalError) as raised:
        resolve_hour(SPRING_FORWARD, 3, "N")
    assert raised.value.reason_code == "NONEXISTENT_LOCAL_HOUR"


def test_fall_back_repeats_one_hour_and_dst_flag_separates_them():
    cdt = resolve_hour(FALL_BACK, 2, "Y")
    cst = resolve_hour(FALL_BACK, 2, "N")

    assert cdt.is_repeated_hour and cst.is_repeated_hour
    assert cdt.target_ts_utc != cst.target_ts_utc
    assert cst.target_ts_utc - cdt.target_ts_utc == dt.timedelta(hours=1)
    # 01:00 local occurs first at UTC-5 (CDT), then again at UTC-6 (CST).
    assert cdt.target_ts_utc == dt.datetime(2026, 11, 1, 6, tzinfo=dt.UTC)
    assert cst.target_ts_utc == dt.datetime(2026, 11, 1, 7, tzinfo=dt.UTC)


def test_dst_flag_on_a_non_repeating_hour_is_rejected():
    with pytest.raises(TemporalError) as raised:
        resolve_hour(NORMAL, 14, "Y")
    assert raised.value.reason_code == "UNEXPECTED_DST_FLAG"


def test_seasonal_naive_crosses_spring_forward_by_calendar_day_not_by_168_hours(warehouse):
    """Seven calendar days at the same local hour is 167 hours here.

    Target: 2026-03-14 hour ending 12 (CDT). Its seasonal-naive source is
    2026-03-07 hour ending 12 (CST), 167 hours earlier. Subtracting a flat
    168 hours from the UTC instant would land on hour ending 11 and silently
    forecast the wrong hour.
    """
    target_day = dt.date(2026, 3, 14)
    source_day = dt.date(2026, 3, 7)
    hour_ending = 12

    target_ts = resolve_hour(target_day, hour_ending, "N").target_ts_utc
    right_ts = resolve_hour(source_day, hour_ending, "N").target_ts_utc
    wrong_ts = resolve_hour(source_day, hour_ending - 1, "N").target_ts_utc
    assert target_ts - right_ts == dt.timedelta(hours=167)
    assert target_ts - wrong_ts == dt.timedelta(hours=168)

    add_actual(
        warehouse, operating_date=target_day, hour_ending=hour_ending,
        publication=local(target_day + dt.timedelta(days=1), 5, 50), actual_mw=1000.0,
    )
    # The correct source hour and its neighbour, distinguishable by value.
    add_actual(
        warehouse, operating_date=source_day, hour_ending=hour_ending,
        publication=local(source_day + dt.timedelta(days=1), 5, 50), actual_mw=777.0,
    )
    add_actual(
        warehouse, operating_date=source_day, hour_ending=hour_ending - 1,
        publication=local(source_day + dt.timedelta(days=1), 5, 50), actual_mw=666.0,
    )
    add_forecast(
        warehouse, operating_date=target_day, hour_ending=hour_ending,
        publication=target_ts - dt.timedelta(hours=25), forecast_mw=1010.0,
    )

    rows = asof_rows(
        warehouse,
        context(
            window_start=local(target_day, 0),
            window_end=local(target_day + dt.timedelta(days=1), 0),
            processing_date=target_day + dt.timedelta(days=1),
        ),
    )
    row = next(r for r in rows if r["hour_ending"] == hour_ending)
    assert row["naive_forecast_mw"] == 777.0
    assert row["naive_source_ts_utc"] == right_ts


def test_repeated_fall_back_hour_produces_two_distinct_evaluation_rows(warehouse):
    """Both occurrences of the repeated hour are evaluated, not collapsed."""
    for dst_flag, actual_mw in (("Y", 1000.0), ("N", 900.0)):
        add_actual(
            warehouse, operating_date=FALL_BACK, hour_ending=2, dst_flag=dst_flag,
            publication=local(FALL_BACK + dt.timedelta(days=1), 5, 50), actual_mw=actual_mw,
        )
        add_actual(
            warehouse, operating_date=FALL_BACK - dt.timedelta(days=7), hour_ending=2,
            dst_flag="N",
            publication=local(FALL_BACK - dt.timedelta(days=6), 5, 50), actual_mw=950.0,
        )
        add_forecast(
            warehouse, operating_date=FALL_BACK, hour_ending=2, dst_flag=dst_flag,
            publication=resolve_hour(FALL_BACK, 2, dst_flag).target_ts_utc
            - dt.timedelta(hours=24),
            forecast_mw=actual_mw + 5,
        )

    rows = [
        r
        for r in asof_rows(
            warehouse,
            context(
                window_start=local(FALL_BACK, 0),
                window_end=local(FALL_BACK + dt.timedelta(days=1), 0),
                processing_date=FALL_BACK + dt.timedelta(days=1),
            ),
        )
        if r["hour_ending"] == 2
    ]
    assert len(rows) == 2
    assert {r["dst_flag"] for r in rows} == {"Y", "N"}
    assert {r["actual_mw"] for r in rows} == {1000.0, 900.0}
