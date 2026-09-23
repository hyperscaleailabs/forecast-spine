"""Normalization: wide ERCOT CSVs -> long canonical rows, with a disposition
for every source row.

Two rules drive this module.

1. **No silent row loss.** Every data line in every source file is given
   exactly one disposition and recorded in the row ledger:

       raw_rows == accepted + duplicate_identical + quarantined

   A quarantined line keeps its source file, its 1-based row number, its raw
   payload, a machine reason code and a human reason. Nothing is dropped.

2. **Join by name, never by position.** The two reports name and *order*
   their weather-zone columns differently:

       NP3-565: ... North, NorthCentral, SouthCentral, Southern, West ...
       NP6-345: ... NORTH,   NORTH_C,    SOUTHERN,     SOUTH_C,  WEST ...

   Columns 7 and 8 are Southern and South Central in opposite order. A
   positional join produces a fully populated, entirely plausible dataset in
   which two large zones are swapped -- the exact failure mode this exercise
   is about. Both reports are therefore mapped through explicit name tables
   and the header is fingerprinted on every file.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import io
from collections import Counter
from collections.abc import Iterable

from . import ercot
from .time import (
    TemporalError,
    parse_dst_flag,
    parse_hour_ending,
    parse_operating_date,
    resolve_hour,
)

ACCEPTED = "ACCEPTED"
DUPLICATE_IDENTICAL = "DUPLICATE_IDENTICAL"
QUARANTINED_PARSE_ERROR = "QUARANTINED_PARSE_ERROR"
QUARANTINED_INVALID_VALUE = "QUARANTINED_INVALID_VALUE"
QUARANTINED_KEY_CONFLICT = "QUARANTINED_KEY_CONFLICT"
QUARANTINED_SCHEMA_ERROR = "QUARANTINED_SCHEMA_ERROR"

QUARANTINE_DISPOSITIONS = frozenset(
    {
        QUARANTINED_PARSE_ERROR,
        QUARANTINED_INVALID_VALUE,
        QUARANTINED_KEY_CONFLICT,
        QUARANTINED_SCHEMA_ERROR,
    }
)

# Canonical weather zones. SYSTEM_TOTAL is reported by both files but is a
# roll-up, not a weather zone; it is kept for cross-checks and excluded from
# the evaluation dataset.
WEATHER_ZONES = (
    "COAST",
    "EAST",
    "FAR_WEST",
    "NORTH",
    "NORTH_CENTRAL",
    "SOUTH_CENTRAL",
    "SOUTHERN",
    "WEST",
)
SYSTEM_TOTAL = "SYSTEM_TOTAL"

FORECAST_HEADER = (
    "DeliveryDate,HourEnding,Coast,East,FarWest,North,NorthCentral,"
    "SouthCentral,Southern,West,SystemTotal,Model,InUseFlag,DSTFlag"
)
ACTUAL_HEADER = (
    "OperDay,HourEnding,COAST,EAST,FAR_WEST,NORTH,NORTH_C,SOUTHERN,"
    "SOUTH_C,WEST,TOTAL,DSTFlag"
)

FORECAST_ZONE_COLUMNS = {
    "Coast": "COAST",
    "East": "EAST",
    "FarWest": "FAR_WEST",
    "North": "NORTH",
    "NorthCentral": "NORTH_CENTRAL",
    "SouthCentral": "SOUTH_CENTRAL",
    "Southern": "SOUTHERN",
    "West": "WEST",
    "SystemTotal": SYSTEM_TOTAL,
}
# Declared in the order each report emits them, so the swap is visible here
# rather than only in the headers above.
ACTUAL_ZONE_COLUMNS = {
    "COAST": "COAST",
    "EAST": "EAST",
    "FAR_WEST": "FAR_WEST",
    "NORTH": "NORTH",
    "NORTH_C": "NORTH_CENTRAL",
    "SOUTHERN": "SOUTHERN",
    "SOUTH_C": "SOUTH_CENTRAL",
    "WEST": "WEST",
    "TOTAL": SYSTEM_TOTAL,
}

# Zone load in MW. ERCOT system peak is ~86 GW; no single weather zone
# approaches 200 GW, and load is never negative at this aggregation.
MIN_PLAUSIBLE_MW = 0.0
MAX_PLAUSIBLE_MW = 200_000.0


@dataclasses.dataclass(frozen=True)
class Observation:
    """One (publication, target hour, zone) load value."""

    source_file_id: str
    source_row_number: int
    report_key: str
    publication_ts_utc: dt.datetime
    target_ts_utc: dt.datetime
    operating_date: dt.date
    hour_ending: int
    dst_flag: str
    weather_zone: str
    value_mw: float
    model: str | None
    is_ercot_model_in_use: bool | None


@dataclasses.dataclass(frozen=True)
class LedgerEntry:
    """The disposition of exactly one source CSV data line."""

    source_file_id: str
    source_filename: str
    report_key: str
    source_row_number: int
    raw_line: str
    disposition: str
    reason_code: str | None
    reason: str | None


@dataclasses.dataclass
class NormalizationResult:
    report_key: str
    source_file_id: str
    source_filename: str
    schema_fingerprint: str
    raw_row_count: int
    observations: list[Observation]
    ledger: list[LedgerEntry]

    def disposition_counts(self) -> Counter[str]:
        return Counter(entry.disposition for entry in self.ledger)

    def check_accounting(self) -> None:
        """The invariant, asserted at the point of production."""
        counts = self.disposition_counts()
        total = sum(counts.values())
        if total != self.raw_row_count:
            raise AssertionError(
                f"{self.source_filename}: row accounting broken -- "
                f"{self.raw_row_count} raw rows but {total} dispositions"
            )


def schema_fingerprint(header: Iterable[str]) -> str:
    return hashlib.sha256(",".join(header).encode()).hexdigest()[:16]


def normalize(source: ercot.SourceFile) -> NormalizationResult:
    if source.report_key == "load_forecast":
        return _normalize(source, FORECAST_HEADER, FORECAST_ZONE_COLUMNS, is_forecast=True)
    if source.report_key == "actual_load":
        return _normalize(source, ACTUAL_HEADER, ACTUAL_ZONE_COLUMNS, is_forecast=False)
    raise ValueError(f"unknown report key {source.report_key!r}")


def _normalize(
    source: ercot.SourceFile,
    expected_header: str,
    zone_columns: dict[str, str],
    *,
    is_forecast: bool,
) -> NormalizationResult:
    text = ercot.read_csv_text(source)
    lines = text.splitlines()
    header_line = lines[0] if lines else ""
    data_lines = lines[1:]

    result = NormalizationResult(
        report_key=source.report_key,
        source_file_id=source.source_file_id,
        source_filename=source.filename,
        schema_fingerprint=schema_fingerprint(header_line.split(",")),
        raw_row_count=len(data_lines),
        observations=[],
        ledger=[],
    )

    def record(row_number: int, raw: str, disposition: str, code: str | None, reason: str | None):
        result.ledger.append(
            LedgerEntry(
                source_file_id=source.source_file_id,
                source_filename=source.filename,
                report_key=source.report_key,
                source_row_number=row_number,
                raw_line=raw,
                disposition=disposition,
                reason_code=code,
                reason=reason,
            )
        )

    # A changed header invalidates every positional assumption below, so the
    # whole file is quarantined rather than parsed on a guess.
    if header_line.strip() != expected_header:
        reason = f"header changed: expected {expected_header!r}, found {header_line.strip()!r}"
        for row_number, raw in enumerate(data_lines, start=2):
            record(row_number, raw, QUARANTINED_SCHEMA_ERROR, "SCHEMA_DRIFT", reason)
        result.check_accounting()
        return result

    columns = header_line.strip().split(",")
    reader = csv.reader(io.StringIO("\n".join(data_lines)))

    # key -> (row_number, comparable payload). Used to separate a harmless
    # exact repeat from a genuine conflict at the same key.
    seen: dict[tuple, tuple[int, tuple]] = {}
    pending: dict[int, list[Observation]] = {}
    conflicted_rows: dict[int, str] = {}

    for offset, fields in enumerate(reader):
        row_number = offset + 2  # 1-based, header is row 1
        raw = data_lines[offset]

        if len(fields) != len(columns):
            record(
                row_number,
                raw,
                QUARANTINED_PARSE_ERROR,
                "FIELD_COUNT_MISMATCH",
                f"expected {len(columns)} fields, found {len(fields)}",
            )
            continue

        row = dict(zip(columns, fields))
        try:
            operating_date = parse_operating_date(
                row["DeliveryDate"] if is_forecast else row["OperDay"]
            )
            hour_ending = parse_hour_ending(row["HourEnding"])
            dst_flag = parse_dst_flag(row["DSTFlag"])
            resolved = resolve_hour(operating_date, hour_ending, dst_flag)
        except TemporalError as exc:
            record(row_number, raw, QUARANTINED_PARSE_ERROR, exc.reason_code, str(exc))
            continue

        model = row["Model"].strip() if is_forecast else None
        in_use: bool | None = None
        if is_forecast:
            flag = row["InUseFlag"].strip().upper()
            if flag not in {"Y", "N"}:
                record(
                    row_number,
                    raw,
                    QUARANTINED_INVALID_VALUE,
                    "INVALID_IN_USE_FLAG",
                    f"unexpected InUseFlag {row['InUseFlag']!r}",
                )
                continue
            in_use = flag == "Y"
            if not model:
                record(row_number, raw, QUARANTINED_INVALID_VALUE, "MISSING_MODEL", "empty Model")
                continue

        values: dict[str, float] = {}
        failure: tuple[str, str] | None = None
        for column, zone in zone_columns.items():
            try:
                value = float(row[column])
            except (TypeError, ValueError):
                failure = ("UNPARSEABLE_MW", f"{column}={row[column]!r} is not a number")
                break
            if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124 -- NaN check
                failure = ("NON_FINITE_MW", f"{column}={row[column]!r} is not finite")
                break
            if not MIN_PLAUSIBLE_MW <= value <= MAX_PLAUSIBLE_MW:
                failure = (
                    "IMPLAUSIBLE_MW",
                    f"{column}={value} outside [{MIN_PLAUSIBLE_MW}, {MAX_PLAUSIBLE_MW}] MW",
                )
                break
            values[zone] = value

        if failure is not None:
            record(row_number, raw, QUARANTINED_INVALID_VALUE, failure[0], failure[1])
            continue

        key = (resolved.target_ts_utc, model)
        payload = tuple(sorted(values.items())) + (in_use,)
        if key in seen:
            first_row_number, first_payload = seen[key]
            if first_payload == payload:
                record(
                    row_number,
                    raw,
                    DUPLICATE_IDENTICAL,
                    "EXACT_REPEAT",
                    f"identical to source row {first_row_number}",
                )
            else:
                # Two different answers for the same key in the same
                # publication. We do not pick a winner; both are quarantined
                # and the readiness gate refuses the release.
                reason = (
                    f"conflicting values for target {resolved.target_ts_utc.isoformat()} "
                    f"model={model!r}: source rows {first_row_number} and {row_number} disagree"
                )
                conflicted_rows[row_number] = reason
                conflicted_rows[first_row_number] = reason
                pending.pop(first_row_number, None)
                record(row_number, raw, QUARANTINED_KEY_CONFLICT, "SAME_KEY_DIFFERENT_VALUES", reason)
            continue

        seen[key] = (row_number, payload)
        pending[row_number] = [
            Observation(
                source_file_id=source.source_file_id,
                source_row_number=row_number,
                report_key=source.report_key,
                publication_ts_utc=source.publication_ts_utc,
                target_ts_utc=resolved.target_ts_utc,
                operating_date=resolved.operating_date,
                hour_ending=resolved.hour_ending,
                dst_flag=resolved.dst_flag,
                weather_zone=zone,
                value_mw=value,
                model=model,
                is_ercot_model_in_use=in_use,
            )
            for zone, value in values.items()
        ]
        record(row_number, raw, ACCEPTED, None, None)

    # Retract the first member of any conflicting pair: it was provisionally
    # accepted before its partner was seen.
    if conflicted_rows:
        rewritten = []
        for entry in result.ledger:
            if entry.disposition == ACCEPTED and entry.source_row_number in conflicted_rows:
                entry = dataclasses.replace(
                    entry,
                    disposition=QUARANTINED_KEY_CONFLICT,
                    reason_code="SAME_KEY_DIFFERENT_VALUES",
                    reason=conflicted_rows[entry.source_row_number],
                )
            rewritten.append(entry)
        result.ledger = rewritten

    for row_number, observations in pending.items():
        if row_number not in conflicted_rows:
            result.observations.extend(observations)

    result.check_accounting()
    return result
