"""Rerun safety.

The property under test: for a fixed processing date, the pipeline's output
is a function of the bytes ERCOT had published by that date -- not of when
the pipeline happens to run, and not of how many times it has run before.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from conftest import SQL_PATH

from forecast_spine import ercot, fixtures, pipeline

TABLES = ("source_file", "source_row", "forecast_vintage", "actual_vintage")


def _digest(connection, table: str) -> str:
    """Order-independent fingerprint of a table's contents."""
    columns = [
        row[0]
        for row in connection.execute(f"SELECT name FROM pragma_table_info('{table}')").fetchall()
    ]
    projection = ", ".join(f"CAST({c} AS VARCHAR)" for c in columns)
    return connection.execute(
        f"SELECT md5(string_agg(row_text, '|' ORDER BY row_text)) "
        f"FROM (SELECT concat_ws(chr(31), {projection}) AS row_text FROM {table})"
    ).fetchone()[0]


def _build(raw: Path, processing_date: dt.date, database: str = ":memory:"):
    run_context = pipeline.build_context(processing_date, raw, window_days=1)
    connection = pipeline.connect(database)
    pipeline.load(connection, run_context)
    pipeline.build_evaluation_dataset(connection, run_context, SQL_PATH)
    return connection, run_context


def test_run_id_is_a_function_of_inputs_not_of_wall_clock(tmp_path):
    raw = fixtures.build("pass", tmp_path)
    date = fixtures.processing_date_for("pass")
    first = pipeline.build_context(date, raw, window_days=1)
    second = pipeline.build_context(date, raw, window_days=1)
    assert first.run_id == second.run_id

    # A different processing date is a different run, by construction.
    other = pipeline.build_context(date + dt.timedelta(days=1), raw, window_days=1)
    assert other.run_id != first.run_id


def test_reloading_the_same_run_replaces_rather_than_appends(tmp_path):
    raw = fixtures.build("pass", tmp_path)
    date = fixtures.processing_date_for("pass")
    run_context = pipeline.build_context(date, raw, window_days=1)
    connection = pipeline.connect(":memory:")

    pipeline.load(connection, run_context)
    before = {table: _digest(connection, table) for table in TABLES}
    counts_before = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES
    }

    pipeline.load(connection, run_context)
    after = {table: _digest(connection, table) for table in TABLES}
    counts_after = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES
    }

    assert counts_after == counts_before, "a rerun must not double the partition"
    assert after == before


def test_two_independent_builds_produce_byte_identical_tables(tmp_path):
    raw = fixtures.build("pass", tmp_path)
    date = fixtures.processing_date_for("pass")
    first, _ = _build(raw, date)
    second, _ = _build(raw, date)
    for table in TABLES:
        assert _digest(first, table) == _digest(second, table)
    assert _digest(first, "evaluation_dataset") == _digest(second, "evaluation_dataset")


def test_a_later_publication_does_not_change_an_earlier_processing_date(tmp_path):
    """The archive moves on; a historical run must not.

    A vintage published after the processing date lands in `data/raw`, and
    re-running the earlier date produces the same run_id and the same rows.
    """
    raw = fixtures.build("pass", tmp_path)
    date = fixtures.processing_date_for("pass")
    before_connection, before_context = _build(raw, date)
    before = {table: _digest(before_connection, table) for table in TABLES}

    # Copy an existing vintage forward to a publication timestamp beyond the
    # processing date's cutoff. Same bytes, later filename stamp.
    existing = ercot.scan_local("load_forecast", raw)[-1]
    later = existing.local_path.parent / existing.filename.replace(
        existing.filename.split(".")[3], (date + dt.timedelta(days=2)).strftime("%Y%m%d")
    )
    shutil.copyfile(existing.local_path, later)
    assert ercot.parse_publication_ts(later.name) > pipeline.processing_cutoff(date)

    after_connection, after_context = _build(raw, date)
    assert after_context.run_id == before_context.run_id
    for table in TABLES:
        assert _digest(after_connection, table) == before[table]

    # ...while a run *on* the later date does see it.
    newer = pipeline.build_context(date + dt.timedelta(days=3), raw, window_days=1)
    assert len(newer.source_files) == len(before_context.source_files) + 1


def test_run_id_changes_when_source_bytes_change(tmp_path):
    raw = fixtures.build("pass", tmp_path)
    date = fixtures.processing_date_for("pass")
    original = pipeline.build_context(date, raw, window_days=1).run_id

    victim = ercot.scan_local("load_forecast", raw)[0].local_path
    victim.write_bytes(victim.read_bytes() + b"\x00")
    assert pipeline.build_context(date, raw, window_days=1).run_id != original
