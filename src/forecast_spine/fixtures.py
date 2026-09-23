"""Synthetic, credential-free ERCOT vintages for demonstrations and tests.

Fixtures are generated rather than committed as binaries so that every byte a
test depends on is reviewable as source. They reproduce the real file layout
exactly -- MIS filename grammar, zipped single CSV member, both headers with
their differing zone names *and differing zone order* -- so the same parser,
the same as-of SQL and the same gates run against them.

Scenarios:

    pass                  a clean operating day; both gates PASS
    dst_spring_forward    2026-03-08, a 23-hour operating day; both gates
                          PASS, demonstrating DST-aware coverage
    missing_forecast      one target hour has no forecast published by its
                          cutoff; readiness FAILs, never back-fills
    conflicting_duplicate one publication carries two different values for
                          the same key; readiness FAILs without guessing
    schema_drift          a weather-zone column is renamed upstream;
                          readiness FAILs rather than parsing positionally
"""

from __future__ import annotations

import datetime as dt
import io
import math
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from .normalize import ACTUAL_HEADER, FORECAST_HEADER
from .time import expected_hours

CENTRAL = ZoneInfo("America/Chicago")

SCENARIOS = (
    "pass",
    "dst_spring_forward",
    "missing_forecast",
    "conflicting_duplicate",
    "schema_drift",
)

# Zone columns in the order each real report emits them. Note that Southern
# and South Central appear in opposite positions -- a positional parser would
# swap them and produce an entirely plausible dataset.
_FORECAST_ZONE_ORDER = (
    "COAST", "EAST", "FAR_WEST", "NORTH", "NORTH_CENTRAL", "SOUTH_CENTRAL", "SOUTHERN", "WEST",
)
_ACTUAL_ZONE_ORDER = (
    "COAST", "EAST", "FAR_WEST", "NORTH", "NORTH_CENTRAL", "SOUTHERN", "SOUTH_CENTRAL", "WEST",
)

_ZONE_SCALE = {
    "COAST": 15000.0, "EAST": 1900.0, "FAR_WEST": 7300.0, "NORTH": 1700.0,
    "NORTH_CENTRAL": 17000.0, "SOUTH_CENTRAL": 10000.0, "SOUTHERN": 5000.0, "WEST": 1900.0,
}

MODELS = ("A3", "E")
IN_USE_MODEL = "E"


def _shape(hour_ending: int) -> float:
    """A smooth diurnal profile; overnight trough, late-afternoon peak."""
    return 0.82 + 0.18 * math.sin((hour_ending - 4) / 24.0 * 2 * math.pi)


def _actual_mw(zone: str, day: dt.date, hour_ending: int) -> float:
    # Week-over-week drift so the seasonal-naive forecast is close but not
    # exact; without it WAPE would be identically zero and prove nothing.
    drift = 1.0 + 0.02 * ((day.toordinal() % 14) / 14.0)
    return round(_ZONE_SCALE[zone] * _shape(hour_ending) * drift, 4)


def _forecast_mw(zone: str, day: dt.date, hour_ending: int, model: str) -> float:
    bias = 1.008 if model == IN_USE_MODEL else 1.03
    return round(_actual_mw(zone, day, hour_ending) * bias, 4)


def _filename(report_type_id: int, marker: str, published: dt.datetime) -> str:
    stamp = published.strftime("%Y%m%d.%H%M%S") + f"{published.microsecond // 1000:03d}"
    return f"cdr.{report_type_id:08d}.0000000000000000.{stamp}.{marker}_csv.zip"


def _write_zip(path: Path, member: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, text)
    path.write_bytes(buffer.getvalue())


def _forecast_csv(day: dt.date, *, header: str = FORECAST_HEADER, omit_hour: int | None = None,
                  conflict_hour: int | None = None) -> str:
    lines = [header]
    for hour_ending, dst_flag in expected_hours(day):
        if hour_ending == omit_hour:
            continue
        for model in MODELS:
            values = [f"{_forecast_mw(z, day, hour_ending, model):.4f}" for z in _FORECAST_ZONE_ORDER]
            total = f"{sum(float(v) for v in values):.4f}"
            in_use = "Y" if model == IN_USE_MODEL else "N"
            lines.append(
                f"{day.strftime('%m/%d/%Y')},{hour_ending}:00,"
                + ",".join(values)
                + f",{total},{model},{in_use},{dst_flag}"
            )
            if hour_ending == conflict_hour and model == IN_USE_MODEL:
                # Same publication, same (target hour, model), different MW.
                bumped = [f"{float(v) * 1.05:.4f}" for v in values]
                bumped_total = f"{sum(float(v) for v in bumped):.4f}"
                lines.append(
                    f"{day.strftime('%m/%d/%Y')},{hour_ending}:00,"
                    + ",".join(bumped)
                    + f",{bumped_total},{model},{in_use},{dst_flag}"
                )
    return "\n".join(lines) + "\n"


def _actual_csv(day: dt.date) -> str:
    lines = [ACTUAL_HEADER]
    for hour_ending, dst_flag in expected_hours(day):
        values = [f"{_actual_mw(z, day, hour_ending):.2f}" for z in _ACTUAL_ZONE_ORDER]
        total = f"{sum(float(v) for v in values):.2f}"
        lines.append(
            f"{day.strftime('%m/%d/%Y')},{hour_ending:02d}:00,"
            + ",".join(values)
            + f",{total},{dst_flag}"
        )
    return "\n".join(lines) + "\n"


def _local(day: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=CENTRAL)


def build(scenario: str, root: Path, target_day: dt.date | None = None) -> Path:
    """Materialize `scenario` under `root` and return the raw directory.

    The layout matches `data/raw/<report_key>/<mis filename>.zip`, so a
    fixture run and a live run differ only in which directory they read.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
    if target_day is None:
        target_day = dt.date(2026, 3, 8) if scenario == "dst_spring_forward" else dt.date(2026, 9, 22)

    raw = root / scenario
    forecast_dir, actual_dir = raw / "load_forecast", raw / "actual_load"

    # Actuals: the evaluated day, and the same weekday one week earlier as
    # the seasonal-naive input. Each is published at 05:50 the next morning.
    for day in (target_day - dt.timedelta(days=7), target_day):
        published = _local(day + dt.timedelta(days=1), 5, 50)
        _write_zip(
            actual_dir / _filename(13101, "ACTUALSYSLOADWZNP6345", published),
            f"cdr.13101.{day:%Y%m%d}.ACTUALSYSLOADWZNP6345.csv",
            _actual_csv(day),
        )

    # Forecasts: hourly at HH:30, spanning the two days before the target so
    # that every hour of the target day has a vintage published at or before
    # its own T-24h cutoff.
    for offset in (2, 1):
        publish_day = target_day - dt.timedelta(days=offset)
        for hour in range(24):
            published = _local(publish_day, hour, 30)
            omit_hour = conflict_hour = None
            header = FORECAST_HEADER
            if scenario == "missing_forecast":
                # Hour ending 13 is absent from every vintage that could have
                # been published in time; a later vintage does hold it, and
                # must not be used.
                omit_hour = 13
            elif scenario == "conflicting_duplicate" and (offset, hour) == (1, 11):
                conflict_hour = 13
            elif scenario == "schema_drift" and (offset, hour) == (1, 11):
                header = FORECAST_HEADER.replace("SouthCentral", "SouthCentralZone")
            _write_zip(
                forecast_dir / _filename(14837, "LFMODWEATHERNP3565", published),
                f"cdr.14837.{publish_day:%Y%m%d}{hour:02d}.LFMODWEATHERNP3565.csv",
                _forecast_csv(target_day, header=header, omit_hour=omit_hour,
                              conflict_hour=conflict_hour),
            )

    if scenario == "missing_forecast":
        # The hour exists in a vintage published *after* the cutoff. The
        # as-of query must leave the row NULL rather than reach for it.
        published = _local(target_day, 6, 30)
        _write_zip(
            forecast_dir / _filename(14837, "LFMODWEATHERNP3565", published),
            f"cdr.14837.{target_day:%Y%m%d}06.LFMODWEATHERNP3565.csv",
            _forecast_csv(target_day),
        )

    return raw


def processing_date_for(scenario: str, target_day: dt.date | None = None) -> dt.date:
    if target_day is None:
        target_day = dt.date(2026, 3, 8) if scenario == "dst_spring_forward" else dt.date(2026, 9, 22)
    return target_day + dt.timedelta(days=1)
