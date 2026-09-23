"""Canonical time handling for ERCOT operating dates and hours ending.

ERCOT publishes load data in *local operating time*: an operating date, an
hour ending 1..24, and a `DSTFlag`. A local operating date does not always
have 24 hours, so a naive `date + hour` construction is wrong twice a year.

Observed convention (verified against the vintages in `data/raw`): rows in
September 2026 -- squarely inside US Central Daylight Time -- all carry
`DSTFlag = 'N'`. The flag is therefore *not* "this hour is daylight time".
It is a discriminator for the hour that repeats when DST ends:

    DSTFlag = 'Y'  ->  the CDT (first, UTC-5) occurrence of the repeated hour
    DSTFlag = 'N'  ->  everything else, including the CST (second, UTC-6)
                       occurrence of the repeated hour

This reading is consistent with all evidence we hold, but our vintage window
(2026-08-23 .. 2026-09-23) contains no DST transition, so it cannot be
confirmed from data and is recorded as an assumption in `MEMO.md`. The
transition logic is exercised by synthetic fixtures in `tests/test_dst.py`
instead, and it is isolated in this one module so a correction is one edit.

Canonical instant: an hour is identified by the UTC instant at which its
local interval *begins*. Hour ending 1:00 on 2026-03-08 is the interval
[00:00, 01:00) local, keyed by the UTC instant of 00:00 local.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
UTC = dt.UTC

_HOUR_ENDING_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_OPERATING_DATE_RE = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$")


class TemporalError(ValueError):
    """A row whose local timestamp cannot be resolved to a real instant."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclasses.dataclass(frozen=True)
class ResolvedHour:
    operating_date: dt.date
    hour_ending: int
    dst_flag: str
    target_ts_utc: dt.datetime
    is_repeated_hour: bool


def parse_operating_date(value: str) -> dt.date:
    """`MM/DD/YYYY` as published by both NP3-565 and NP6-345."""
    m = _OPERATING_DATE_RE.match(value)
    if m is None:
        raise TemporalError("INVALID_OPERATING_DATE", f"unparseable operating date {value!r}")
    month, day, year = (int(g) for g in m.groups())
    try:
        return dt.date(year, month, day)
    except ValueError as exc:
        raise TemporalError("INVALID_OPERATING_DATE", f"{value!r}: {exc}") from exc


def parse_hour_ending(value: str) -> int:
    """`1:00`..`24:00` (NP3-565) and `01:00`..`24:00` (NP6-345).

    The two reports disagree on zero-padding, which is exactly the kind of
    difference that a naive string join would silently drop.
    """
    m = _HOUR_ENDING_RE.match(value)
    if m is None:
        raise TemporalError("INVALID_HOUR_ENDING", f"unparseable hour ending {value!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute != 0:
        raise TemporalError("INVALID_HOUR_ENDING", f"hour ending {value!r} is not on the hour")
    if not 1 <= hour <= 25:
        raise TemporalError("INVALID_HOUR_ENDING", f"hour ending {value!r} out of range")
    return hour


def parse_dst_flag(value: str) -> str:
    flag = value.strip().upper()
    if flag not in {"Y", "N"}:
        raise TemporalError("INVALID_DST_FLAG", f"unexpected DSTFlag {value!r}")
    return flag


def _is_nonexistent(naive: dt.datetime) -> bool:
    """True for a local wall-clock time skipped by spring-forward."""
    aware = naive.replace(tzinfo=CENTRAL)
    return aware.astimezone(UTC).astimezone(CENTRAL).replace(tzinfo=None) != naive


def _is_ambiguous(naive: dt.datetime) -> bool:
    """True for a local wall-clock time that occurs twice at fall-back."""
    return naive.replace(tzinfo=CENTRAL, fold=0).utcoffset() != naive.replace(
        tzinfo=CENTRAL, fold=1
    ).utcoffset()


def resolve_hour(operating_date: dt.date, hour_ending: int, dst_flag: str) -> ResolvedHour:
    """Resolve an ERCOT local hour to a single unambiguous UTC instant."""
    naive = dt.datetime.combine(operating_date, dt.time()) + dt.timedelta(hours=hour_ending - 1)

    if _is_nonexistent(naive):
        raise TemporalError(
            "NONEXISTENT_LOCAL_HOUR",
            f"{operating_date} hour ending {hour_ending}: local time {naive} does not exist "
            f"(skipped by spring-forward)",
        )

    ambiguous = _is_ambiguous(naive)
    if ambiguous:
        # DSTFlag disambiguates: 'Y' is the CDT occurrence, 'N' the CST one.
        fold = 0 if dst_flag == "Y" else 1
    else:
        if dst_flag == "Y":
            raise TemporalError(
                "UNEXPECTED_DST_FLAG",
                f"{operating_date} hour ending {hour_ending}: DSTFlag='Y' on an hour that does "
                f"not repeat",
            )
        fold = 0

    target_ts_utc = naive.replace(tzinfo=CENTRAL, fold=fold).astimezone(UTC)
    return ResolvedHour(
        operating_date=operating_date,
        hour_ending=hour_ending,
        dst_flag=dst_flag,
        target_ts_utc=target_ts_utc,
        is_repeated_hour=ambiguous,
    )


def expected_hours(operating_date: dt.date) -> list[tuple[int, str]]:
    """The (hour_ending, dst_flag) pairs a complete operating date must have.

    23 on spring-forward, 25 on fall-back, 24 otherwise. The readiness gate
    uses this instead of hard-coding 24, so a DST day is not reported as
    missing data and a DST day with 24 rows *is*.
    """
    hours: list[tuple[int, str]] = []
    for hour_ending in range(1, 25):
        naive = dt.datetime.combine(operating_date, dt.time()) + dt.timedelta(
            hours=hour_ending - 1
        )
        if _is_nonexistent(naive):
            continue
        if _is_ambiguous(naive):
            hours.append((hour_ending, "Y"))
            hours.append((hour_ending, "N"))
        else:
            hours.append((hour_ending, "N"))
    return hours


def hours_in_operating_date(operating_date: dt.date) -> int:
    return len(expected_hours(operating_date))
