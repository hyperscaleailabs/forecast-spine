"""Warehouse build: immutable sources -> canonical vintages -> evaluation set.

Rerun safety
------------
A run is identified by content, not by wall clock:

    run_id = SHA256(pipeline_version | processing_date | sorted input hashes)

The set of inputs is itself derived from the processing date -- only files
ERCOT published *during or before* that date are admitted -- so re-running
`--processing-date 2026-09-22` tomorrow consumes exactly the same bytes it
consumed today, even though the archive has moved on. Loading is a single
transaction that deletes the processing date's rows before reinserting them,
so a rerun replaces a partition rather than appending to it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pyarrow as pa

from . import ercot, normalize

PIPELINE_VERSION = "1.0.0"
CENTRAL = ZoneInfo("America/Chicago")
UTC = dt.UTC

DEFAULT_CUTOFF_LAG_HOURS = 24
DEFAULT_SEASONAL_LAG_DAYS = 7
DEFAULT_WINDOW_DAYS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_file (
    run_id             VARCHAR NOT NULL,
    processing_date    DATE    NOT NULL,
    source_file_id     VARCHAR NOT NULL,
    report_key         VARCHAR NOT NULL,
    original_filename  VARCHAR NOT NULL,
    publication_ts_utc TIMESTAMPTZ NOT NULL,
    content_sha256     VARCHAR NOT NULL,
    size_bytes         BIGINT  NOT NULL,
    schema_fingerprint VARCHAR NOT NULL,
    local_path         VARCHAR NOT NULL,
    raw_row_count      BIGINT  NOT NULL
);

-- The row ledger. Every data line of every source file lands here exactly
-- once, accepted or not, with its raw payload preserved.
CREATE TABLE IF NOT EXISTS source_row (
    run_id            VARCHAR NOT NULL,
    processing_date   DATE    NOT NULL,
    source_file_id    VARCHAR NOT NULL,
    source_filename   VARCHAR NOT NULL,
    report_key        VARCHAR NOT NULL,
    source_row_number BIGINT  NOT NULL,
    raw_line          VARCHAR NOT NULL,
    disposition       VARCHAR NOT NULL,
    reason_code       VARCHAR,
    reason            VARCHAR
);

CREATE TABLE IF NOT EXISTS forecast_vintage (
    run_id                VARCHAR NOT NULL,
    processing_date       DATE    NOT NULL,
    source_file_id        VARCHAR NOT NULL,
    source_row_number     BIGINT  NOT NULL,
    publication_ts_utc    TIMESTAMPTZ NOT NULL,
    target_ts_utc         TIMESTAMPTZ NOT NULL,
    operating_date        DATE    NOT NULL,
    hour_ending           INTEGER NOT NULL,
    dst_flag              VARCHAR NOT NULL,
    weather_zone          VARCHAR NOT NULL,
    model                 VARCHAR NOT NULL,
    is_ercot_model_in_use BOOLEAN NOT NULL,
    forecast_mw           DOUBLE  NOT NULL
);

CREATE TABLE IF NOT EXISTS actual_vintage (
    run_id             VARCHAR NOT NULL,
    processing_date    DATE    NOT NULL,
    source_file_id     VARCHAR NOT NULL,
    source_row_number  BIGINT  NOT NULL,
    publication_ts_utc TIMESTAMPTZ NOT NULL,
    target_ts_utc      TIMESTAMPTZ NOT NULL,
    operating_date     DATE    NOT NULL,
    hour_ending        INTEGER NOT NULL,
    dst_flag           VARCHAR NOT NULL,
    weather_zone       VARCHAR NOT NULL,
    actual_mw          DOUBLE  NOT NULL
);
"""


@dataclasses.dataclass(frozen=True)
class RunContext:
    run_id: str
    processing_date: dt.date
    processing_ts_utc: dt.datetime
    pipeline_version: str
    cutoff_lag_hours: int
    seasonal_lag_days: int
    window_start_utc: dt.datetime
    window_end_utc: dt.datetime
    source_files: tuple[ercot.SourceFile, ...]

    @property
    def window_operating_dates(self) -> tuple[dt.date, dt.date]:
        start = self.window_start_utc.astimezone(CENTRAL).date()
        end = (self.window_end_utc.astimezone(CENTRAL) - dt.timedelta(seconds=1)).date()
        return start, end


def processing_cutoff(processing_date: dt.date) -> dt.datetime:
    """The instant a run pretends it is: local midnight ending the date.

    Everything ERCOT published during `processing_date` is visible; nothing
    published after it is, no matter what the archive holds when the run
    actually executes. That is what makes a rerun deterministic.
    """
    local_midnight = dt.datetime.combine(processing_date + dt.timedelta(days=1), dt.time())
    return local_midnight.replace(tzinfo=CENTRAL).astimezone(UTC)


def compute_run_id(
    processing_date: dt.date,
    source_files: tuple[ercot.SourceFile, ...],
    pipeline_version: str = PIPELINE_VERSION,
) -> str:
    digest = hashlib.sha256()
    digest.update(pipeline_version.encode())
    digest.update(processing_date.isoformat().encode())
    for content_hash in sorted(f.content_sha256 for f in source_files):
        digest.update(content_hash.encode())
    return digest.hexdigest()[:32]


def build_context(
    processing_date: dt.date,
    raw_root: Path,
    *,
    cutoff_lag_hours: int = DEFAULT_CUTOFF_LAG_HOURS,
    seasonal_lag_days: int = DEFAULT_SEASONAL_LAG_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> RunContext:
    processing_ts = processing_cutoff(processing_date)

    source_files = tuple(
        source
        for report_key in ercot.REPORTS
        for source in ercot.scan_local(report_key, raw_root)
        if source.publication_ts_utc < processing_ts
    )

    # Evaluation window: the `window_days` complete operating days ending the
    # day before the processing date. Actuals for operating day D are
    # published on D+1, so D = processing_date - 1 is the newest day that can
    # have a complete set of actuals.
    last_day = processing_date - dt.timedelta(days=1)
    first_day = last_day - dt.timedelta(days=window_days - 1)
    window_start = dt.datetime.combine(first_day, dt.time()).replace(tzinfo=CENTRAL).astimezone(UTC)
    window_end = (
        dt.datetime.combine(last_day + dt.timedelta(days=1), dt.time())
        .replace(tzinfo=CENTRAL)
        .astimezone(UTC)
    )

    return RunContext(
        run_id=compute_run_id(processing_date, source_files),
        processing_date=processing_date,
        processing_ts_utc=processing_ts,
        pipeline_version=PIPELINE_VERSION,
        cutoff_lag_hours=cutoff_lag_hours,
        seasonal_lag_days=seasonal_lag_days,
        window_start_utc=window_start,
        window_end_utc=window_end,
        source_files=source_files,
    )


def connect(database: Path | str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(database))
    con.execute("SET TimeZone='UTC'")
    con.execute(SCHEMA)
    return con


# Arrow schemas mirroring the DuckDB tables above. Rows are appended
# column-wise and handed to DuckDB as a single Arrow batch: the row-at-a-time
# `executemany` path costs ~10 minutes for a full 2.4M-observation rebuild,
# the columnar path ~2 seconds.
_TS = pa.timestamp("us", tz="UTC")

SOURCE_FILE_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("processing_date", pa.date32()),
        ("source_file_id", pa.string()),
        ("report_key", pa.string()),
        ("original_filename", pa.string()),
        ("publication_ts_utc", _TS),
        ("content_sha256", pa.string()),
        ("size_bytes", pa.int64()),
        ("schema_fingerprint", pa.string()),
        ("local_path", pa.string()),
        ("raw_row_count", pa.int64()),
    ]
)

SOURCE_ROW_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("processing_date", pa.date32()),
        ("source_file_id", pa.string()),
        ("source_filename", pa.string()),
        ("report_key", pa.string()),
        ("source_row_number", pa.int64()),
        ("raw_line", pa.string()),
        ("disposition", pa.string()),
        ("reason_code", pa.string()),
        ("reason", pa.string()),
    ]
)

_VINTAGE_HEAD = [
    ("run_id", pa.string()),
    ("processing_date", pa.date32()),
    ("source_file_id", pa.string()),
    ("source_row_number", pa.int64()),
    ("publication_ts_utc", _TS),
    ("target_ts_utc", _TS),
    ("operating_date", pa.date32()),
    ("hour_ending", pa.int32()),
    ("dst_flag", pa.string()),
    ("weather_zone", pa.string()),
]

FORECAST_VINTAGE_SCHEMA = pa.schema(
    _VINTAGE_HEAD
    + [("model", pa.string()), ("is_ercot_model_in_use", pa.bool_()), ("forecast_mw", pa.float64())]
)
ACTUAL_VINTAGE_SCHEMA = pa.schema(_VINTAGE_HEAD + [("actual_mw", pa.float64())])


class _ColumnBuffer:
    """Column-wise accumulator that materializes as one Arrow table."""

    def __init__(self, schema: pa.Schema) -> None:
        self._schema = schema
        self._columns: list[list] = [[] for _ in schema]

    def append(self, *values) -> None:
        for column, value in zip(self._columns, values):
            column.append(value)

    def __len__(self) -> int:
        return len(self._columns[0]) if self._columns else 0

    def to_arrow(self) -> pa.Table:
        return pa.table(
            [pa.array(column, type=field.type) for column, field in zip(self._columns, self._schema)],
            schema=self._schema,
        )


def load(con: duckdb.DuckDBPyConnection, context: RunContext) -> list[normalize.NormalizationResult]:
    """Normalize and load every source file for this run, atomically.

    The delete-then-insert is scoped to `processing_date` and wrapped in one
    transaction, so a rerun replaces that partition wholesale. A crash
    mid-load leaves the previous run's rows intact rather than a half-written
    mixture of two runs.
    """
    results = [normalize.normalize(source) for source in context.source_files]
    by_id = {source.source_file_id: source for source in context.source_files}

    files = _ColumnBuffer(SOURCE_FILE_SCHEMA)
    ledger = _ColumnBuffer(SOURCE_ROW_SCHEMA)
    forecasts = _ColumnBuffer(FORECAST_VINTAGE_SCHEMA)
    actuals = _ColumnBuffer(ACTUAL_VINTAGE_SCHEMA)

    for result in results:
        source = by_id[result.source_file_id]
        files.append(
            context.run_id,
            context.processing_date,
            result.source_file_id,
            result.report_key,
            result.source_filename,
            source.publication_ts_utc,
            source.content_sha256,
            source.size_bytes,
            result.schema_fingerprint,
            str(source.local_path),
            result.raw_row_count,
        )
        for entry in result.ledger:
            ledger.append(
                context.run_id,
                context.processing_date,
                entry.source_file_id,
                entry.source_filename,
                entry.report_key,
                entry.source_row_number,
                entry.raw_line,
                entry.disposition,
                entry.reason_code,
                entry.reason,
            )
        for obs in result.observations:
            head = (
                context.run_id,
                context.processing_date,
                obs.source_file_id,
                obs.source_row_number,
                obs.publication_ts_utc,
                obs.target_ts_utc,
                obs.operating_date,
                obs.hour_ending,
                obs.dst_flag,
                obs.weather_zone,
            )
            if obs.report_key == "load_forecast":
                forecasts.append(*head, obs.model, obs.is_ercot_model_in_use, obs.value_mw)
            else:
                actuals.append(*head, obs.value_mw)

    con.execute("BEGIN TRANSACTION")
    try:
        for table in ("source_file", "source_row", "forecast_vintage", "actual_vintage"):
            con.execute(f"DELETE FROM {table} WHERE processing_date = ?", [context.processing_date])
        _insert(con, "source_file", files)
        _insert(con, "source_row", ledger)
        _insert(con, "forecast_vintage", forecasts)
        _insert(con, "actual_vintage", actuals)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return results


def _insert(con: duckdb.DuckDBPyConnection, table: str, buffer: _ColumnBuffer) -> None:
    if len(buffer) == 0:
        return
    batch = buffer.to_arrow()
    con.register("_batch", batch)
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM _batch")
    finally:
        con.unregister("_batch")


def build_evaluation_dataset(
    con: duckdb.DuckDBPyConnection, context: RunContext, sql_path: Path
) -> int:
    """Materialize `sql/asof_join.sql` as the run's evaluation dataset."""
    sql = sql_path.read_text()
    con.execute(
        f"CREATE OR REPLACE TABLE evaluation_dataset AS {sql.rstrip().rstrip(';')}",
        {
            "cutoff_lag_hours": context.cutoff_lag_hours,
            "seasonal_lag_days": context.seasonal_lag_days,
            "window_start": context.window_start_utc,
            "window_end": context.window_end_utc,
            "processing_ts": context.processing_ts_utc,
        },
    )
    return con.execute("SELECT COUNT(*) FROM evaluation_dataset").fetchone()[0]
