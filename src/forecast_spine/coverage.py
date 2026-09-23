"""What we hold against what we expected to hold.

The assignment asks for a statement of what could and could not be retrieved.
That is easy to get wrong in prose and easy to get stale, so it is computed
from the files on disk instead.

Expected cadence is derived, not assumed: NP3-565 posts once per wall-clock
hour and NP6-345 once per day, so a local operating day has 23, 24 or 25
expected forecast publications depending on DST. Anything missing is reported
as a concrete hour, not a count.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import Counter
from pathlib import Path

from . import ercot
from .time import CENTRAL, expected_hours

HOURLY = "hourly"
DAILY = "daily"

EXPECTED_CADENCE = {"load_forecast": HOURLY, "actual_load": DAILY}


@dataclasses.dataclass(frozen=True)
class DayCoverage:
    day: dt.date
    expected: int
    held: int

    @property
    def missing(self) -> int:
        return max(0, self.expected - self.held)

    @property
    def complete(self) -> bool:
        return self.held >= self.expected


@dataclasses.dataclass(frozen=True)
class Coverage:
    report_key: str
    start: dt.date
    end: dt.date
    days: tuple[DayCoverage, ...]
    missing_hours: tuple[dt.datetime, ...]

    @property
    def expected(self) -> int:
        return sum(d.expected for d in self.days)

    @property
    def held(self) -> int:
        return sum(d.held for d in self.days)

    @property
    def complete(self) -> bool:
        return all(d.complete for d in self.days)

    def summary(self) -> str:
        pct = 100.0 * self.held / self.expected if self.expected else 100.0
        incomplete = [d for d in self.days if not d.complete]
        headline = (
            f"{self.report_key}  {self.start} .. {self.end}  "
            f"{self.held}/{self.expected} expected publications ({pct:.1f}%)"
        )
        lines = [headline]
        if not incomplete:
            lines.append("  complete — every expected publication is on disk")
            return "\n".join(lines)
        lines.append(f"  {len(incomplete)} incomplete day(s):")
        for day in incomplete:
            lines.append(f"    {day.day}  held {day.held}/{day.expected}")
        return "\n".join(lines)


def _expected_publication_hours(report_key: str, day: dt.date) -> Counter[int]:
    """How many publications each local wall-clock hour should carry.

    A count rather than a set, because at fall-back one wall-clock hour runs
    twice and therefore carries two publications. Collapsing them to distinct
    hours reports a 25-hour day as 24 and hides a genuinely missing vintage.
    """
    if EXPECTED_CADENCE[report_key] == DAILY:
        return Counter({5: 1})  # NP6-345 posts once, around 05:50 local
    # NP3-565 posts once per wall-clock hour: 23, 24 or 25 of them.
    return Counter(hour_ending - 1 for hour_ending, _ in expected_hours(day))


def measure(report_key: str, start: dt.date, end: dt.date, raw_root: Path) -> Coverage:
    held: dict[dt.date, Counter[int]] = {}
    for source in ercot.scan_local(report_key, raw_root):
        local = source.publication_ts_utc.astimezone(CENTRAL)
        if start <= local.date() <= end:
            held.setdefault(local.date(), Counter())[local.hour] += 1

    days: list[DayCoverage] = []
    missing: list[dt.datetime] = []
    day = start
    while day <= end:
        expected = _expected_publication_hours(report_key, day)
        present = held.get(day, Counter())
        # Extra files for an hour are not credited against another hour, so a
        # duplicate cannot mask an absence elsewhere in the day.
        day_held = sum(min(present.get(hour, 0), count) for hour, count in expected.items())
        days.append(DayCoverage(day=day, expected=sum(expected.values()), held=day_held))
        for hour, count in sorted(expected.items()):
            for _ in range(count - min(present.get(hour, 0), count)):
                missing.append(dt.datetime.combine(day, dt.time(hour), tzinfo=CENTRAL))
        day += dt.timedelta(days=1)

    return Coverage(
        report_key=report_key,
        start=start,
        end=end,
        days=tuple(days),
        missing_hours=tuple(missing),
    )
