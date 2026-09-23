"""Command line entry points.

    forecast-spine acquire                      # download MIS vintages
    forecast-spine run --processing-date DATE   # build + gate a run
    forecast-spine demo                         # every fixture scenario

`run` exits 0 when both gates pass, 1 when data readiness fails and 2 when
the model gate fails. Failure is a normal, reportable outcome here, not an
exception: the gates print a structured verdict and the exit code carries it
to whatever scheduled the run.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import time
from pathlib import Path

import typer

from . import ercot, fixtures, gates, pipeline

app = typer.Typer(add_completion=False, help="Point-in-time ERCOT load forecast evaluation.")

DEFAULT_RAW = Path("data/raw")
DEFAULT_DB = Path("data/warehouse/forecast_spine.duckdb")
DEFAULT_SQL = Path("sql/asof_join.sql")
DEFAULT_REPORTS = Path("data/reports")

EXIT_OK, EXIT_READINESS_FAILED, EXIT_MODEL_FAILED = 0, 1, 2


def _echo(message: str) -> None:
    typer.echo(message, err=False)


@app.command()
def acquire(
    report: str = typer.Option("all", help="all | load_forecast | actual_load"),
    raw_dir: Path = typer.Option(DEFAULT_RAW),
    limit: int = typer.Option(0, help="Newest N vintages only; 0 means every one advertised."),
    delay: float = typer.Option(0.3, help="Seconds between requests; ERCOT documents 30/min."),
) -> None:
    """Download MIS vintages. Already-held files are not re-fetched."""
    keys = list(ercot.REPORTS) if report == "all" else [report]
    for key in keys:
        definition = ercot.REPORTS[key]
        vintages = ercot.list_vintages(definition)
        if limit:
            vintages = vintages[:limit]
        _echo(f"{key} ({definition.product_id}): {len(vintages)} vintages advertised")
        fetched = held = 0
        for vintage in vintages:
            existed = (raw_dir / key / vintage.filename).exists()
            ercot.download_vintage(vintage, raw_dir)
            if existed:
                held += 1
            else:
                fetched += 1
                time.sleep(delay)
        _echo(f"  fetched {fetched}, already held {held}")


@app.command()
def run(
    processing_date: str = typer.Option(..., help="YYYY-MM-DD; the date the run pretends it is."),
    source: str = typer.Option("local", help="local | fixtures"),
    scenario: str = typer.Option("pass", help=f"With --source fixtures: {' | '.join(fixtures.SCENARIOS)}"),
    raw_dir: Path = typer.Option(DEFAULT_RAW),
    database: Path = typer.Option(DEFAULT_DB),
    sql: Path = typer.Option(DEFAULT_SQL),
    reports_dir: Path = typer.Option(DEFAULT_REPORTS),
    window_days: int = typer.Option(pipeline.DEFAULT_WINDOW_DAYS),
    cutoff_lag_hours: int = typer.Option(pipeline.DEFAULT_CUTOFF_LAG_HOURS),
) -> None:
    """Build the warehouse for a processing date and run both gates."""
    date = dt.date.fromisoformat(processing_date)
    temporary: Path | None = None
    if source == "fixtures":
        temporary = Path(tempfile.mkdtemp(prefix="forecast-spine-fixtures-"))
        raw_dir = fixtures.build(scenario, temporary)
        window_days = 1
    try:
        code = _execute(date, raw_dir, database, sql, reports_dir, window_days, cutoff_lag_hours)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
    raise typer.Exit(code)


def _execute(
    date: dt.date,
    raw_dir: Path,
    database: Path,
    sql: Path,
    reports_dir: Path,
    window_days: int,
    cutoff_lag_hours: int,
) -> int:
    started = time.monotonic()
    context = pipeline.build_context(
        date, raw_dir, cutoff_lag_hours=cutoff_lag_hours, window_days=window_days
    )
    if not context.source_files:
        _echo(f"no source files published on or before {date} under {raw_dir}")
        return EXIT_READINESS_FAILED

    database.parent.mkdir(parents=True, exist_ok=True)
    connection = pipeline.connect(database)
    pipeline.load(connection, context)
    rows = pipeline.build_evaluation_dataset(connection, context, sql)

    first_day, last_day = context.window_operating_dates
    _echo(
        f"run {context.run_id}  processing_date={date}  "
        f"files={len(context.source_files)}  window={first_day}..{last_day}  "
        f"evaluation_rows={rows}  elapsed={time.monotonic() - started:.1f}s"
    )

    readiness = gates.data_readiness(connection, context)
    _report(readiness, reports_dir)
    if not readiness.passed:
        _echo("RELEASE BLOCKED: data readiness failed; the model gate was not run.")
        return EXIT_READINESS_FAILED

    model = gates.seasonal_naive_gate(connection, context)
    _report(model, reports_dir)
    if not model.passed:
        _echo("RELEASE BLOCKED: seasonal-naive gate failed.")
        return EXIT_MODEL_FAILED

    _echo("RELEASE APPROVED: both gates passed.")
    return EXIT_OK


def _report(result: gates.GateResult, reports_dir: Path) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{result.processing_date}_{result.gate}.json"
    path.write_text(result.to_json())
    _echo(f"\n[{result.status}] {result.gate}  -> {path}")
    for reason in result.reasons:
        _echo(f"  {reason.code} (count={reason.count}): {reason.detail}")
        for key in reason.sample_keys:
            _echo(f"      e.g. {key}")
    if result.gate == "seasonal_naive":
        metrics = result.metrics
        _echo(
            f"  WAPE {metrics['wape_pct']:.2f}%  worst day {metrics['worst_fold_wape_pct']:.2f}%  "
            f"peak-hour WAPE {metrics['peak_hour_wape_pct']:.2f}%  "
            f"worst peak-hour APE {metrics['max_peak_hour_ape_pct']:.2f}%"
        )


@app.command()
def demo(
    database: Path = typer.Option(Path("data/warehouse/demo.duckdb")),
    sql: Path = typer.Option(DEFAULT_SQL),
    reports_dir: Path = typer.Option(Path("data/reports/demo")),
) -> None:
    """Run every fixture scenario and show the verdict each one produces."""
    expected = {
        "pass": EXIT_OK,
        "dst_spring_forward": EXIT_OK,
        "missing_forecast": EXIT_READINESS_FAILED,
        "conflicting_duplicate": EXIT_READINESS_FAILED,
        "schema_drift": EXIT_READINESS_FAILED,
    }
    results: dict[str, int] = {}
    for scenario in fixtures.SCENARIOS:
        _echo(f"\n{'=' * 72}\nscenario: {scenario}\n{'=' * 72}")
        temporary = Path(tempfile.mkdtemp(prefix="forecast-spine-demo-"))
        try:
            raw = fixtures.build(scenario, temporary)
            database.parent.mkdir(parents=True, exist_ok=True)
            if database.exists():
                database.unlink()
            results[scenario] = _execute(
                fixtures.processing_date_for(scenario),
                raw,
                database,
                sql,
                reports_dir / scenario,
                window_days=1,
                cutoff_lag_hours=pipeline.DEFAULT_CUTOFF_LAG_HOURS,
            )
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    _echo(f"\n{'=' * 72}\nsummary\n{'=' * 72}")
    mismatched = []
    for scenario, code in results.items():
        verdict = {0: "APPROVED", 1: "BLOCKED (readiness)", 2: "BLOCKED (model)"}[code]
        marker = "ok " if code == expected[scenario] else "BAD"
        if code != expected[scenario]:
            mismatched.append(scenario)
        _echo(f"  {marker} {scenario:<22} {verdict}")
    raise typer.Exit(1 if mismatched else 0)


if __name__ == "__main__":
    app()
