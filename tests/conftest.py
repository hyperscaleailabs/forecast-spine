"""Shared helpers: a warehouse you can hand-populate one row at a time.

The as-of tests deliberately do not go through the acquisition or
normalization layers. They insert exact `publication_ts` / `target_ts` pairs
so that a boundary case is a boundary case, not an artefact of how a fixture
happened to be generated.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from forecast_spine import pipeline
from forecast_spine.time import resolve_hour

CENTRAL = ZoneInfo("America/Chicago")
UTC = dt.UTC

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "asof_join.sql"

RUN_ID = "test-run"
PROCESSING_DATE = dt.date(2026, 9, 23)


@pytest.fixture
def warehouse():
    connection = pipeline.connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def context(
    *,
    window_start: dt.datetime,
    window_end: dt.datetime,
    cutoff_lag_hours: int = 24,
    seasonal_lag_days: int = 7,
    processing_date: dt.date = PROCESSING_DATE,
) -> pipeline.RunContext:
    return pipeline.RunContext(
        run_id=RUN_ID,
        processing_date=processing_date,
        processing_ts_utc=pipeline.processing_cutoff(processing_date),
        pipeline_version=pipeline.PIPELINE_VERSION,
        cutoff_lag_hours=cutoff_lag_hours,
        seasonal_lag_days=seasonal_lag_days,
        window_start_utc=window_start,
        window_end_utc=window_end,
        source_files=(),
    )


def local(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=CENTRAL).astimezone(UTC)


def add_forecast(
    connection,
    *,
    operating_date: dt.date,
    hour_ending: int,
    publication: dt.datetime,
    forecast_mw: float,
    zone: str = "COAST",
    model: str = "E",
    in_use: bool = True,
    dst_flag: str = "N",
) -> None:
    resolved = resolve_hour(operating_date, hour_ending, dst_flag)
    connection.execute(
        "INSERT INTO forecast_vintage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            RUN_ID, PROCESSING_DATE, "src", 1, publication, resolved.target_ts_utc,
            operating_date, hour_ending, dst_flag, zone, model, in_use, forecast_mw,
        ],
    )


def add_actual(
    connection,
    *,
    operating_date: dt.date,
    hour_ending: int,
    publication: dt.datetime,
    actual_mw: float,
    zone: str = "COAST",
    dst_flag: str = "N",
) -> None:
    resolved = resolve_hour(operating_date, hour_ending, dst_flag)
    connection.execute(
        "INSERT INTO actual_vintage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            RUN_ID, PROCESSING_DATE, "src", 1, publication, resolved.target_ts_utc,
            operating_date, hour_ending, dst_flag, zone, actual_mw,
        ],
    )


def asof_rows(connection, run_context: pipeline.RunContext) -> list[dict]:
    pipeline.build_evaluation_dataset(connection, run_context, SQL_PATH)
    cursor = connection.execute("SELECT * FROM evaluation_dataset ORDER BY target_ts_utc, weather_zone")
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def run_scenario(scenario: str, tmp_path: Path, **overrides):
    """Build and gate a fixture scenario end to end, in process."""
    from forecast_spine import fixtures, gates

    raw = fixtures.build(scenario, tmp_path)
    processing_date = fixtures.processing_date_for(scenario)
    run_context = pipeline.build_context(
        processing_date, raw, window_days=overrides.pop("window_days", 1), **overrides
    )
    connection = pipeline.connect(":memory:")
    pipeline.load(connection, run_context)
    pipeline.build_evaluation_dataset(connection, run_context, SQL_PATH)
    readiness = gates.data_readiness(connection, run_context)
    model = gates.seasonal_naive_gate(connection, run_context) if readiness.passed else None
    return connection, run_context, readiness, model


def reason_codes(result) -> set[str]:
    return {reason.code for reason in result.reasons}
