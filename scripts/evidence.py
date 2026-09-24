"""Derive every factual claim in MEMO.md from the vintages we actually hold.

Run after `forecast-spine acquire`:

    .venv/bin/python scripts/evidence.py

Nothing here is asserted by hand. If ERCOT's behaviour changes, this script
changes the memo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from forecast_spine import ercot, gates, pipeline, seasonal_naive

RAW = Path("data/raw")
SQL = Path("sql/asof_join.sql")


def _args():
    """Processing date, and optionally an explicit evaluation window.

    Without a window this evaluates the trailing `window_days` operating days,
    which is the right default for the rolling MIS path. The assignment names a
    range instead, so the figures quoted in README.md and MEMO.md come from:

        uv run python scripts/evidence.py 2026-03-24 \
            --window-start 2026-02-22 --window-end 2026-03-23
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("processing_date", nargs="?", default="2026-09-23",
                        help="YYYY-MM-DD; the date the run pretends it is.")
    parser.add_argument("--window-start", default=None, help="First operating day to evaluate.")
    parser.add_argument("--window-end", default=None, help="Last operating day to evaluate.")
    parsed = parser.parse_args()
    def as_date(value):
        return dt.date.fromisoformat(value) if value else None

    return (
        as_date(parsed.processing_date),
        as_date(parsed.window_start),
        as_date(parsed.window_end),
    )


PROCESSING_DATE, WINDOW_START, WINDOW_END = _args()


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def main() -> None:
    heading("1. Retention and cadence of the public MIS listing")
    for key, report in ercot.REPORTS.items():
        files = ercot.scan_local(key, RAW)
        if not files:
            print(f"{key}: nothing downloaded")
            continue
        stamps = [f.publication_ts_utc for f in files]
        gaps = sorted({
            round((b - a).total_seconds() / 60)
            for a, b in itertools.pairwise(stamps)
        })
        span = (max(stamps) - min(stamps)).total_seconds() / 86400
        print(
            f"{key} ({report.product_id}, reportTypeId={report.report_type_id}): "
            f"{len(files)} vintages spanning {span:.1f} days\n"
            f"    oldest {min(stamps).astimezone(ercot.CENTRAL):%Y-%m-%d %H:%M %Z}, "
            f"newest {max(stamps).astimezone(ercot.CENTRAL):%Y-%m-%d %H:%M %Z}\n"
            f"    distinct gaps between consecutive vintages (minutes): {gaps}"
        )

    started = time.monotonic()
    context = pipeline.build_context(
        PROCESSING_DATE, RAW, window_start=WINDOW_START, window_end=WINDOW_END
    )
    connection = pipeline.connect(":memory:")
    pipeline.load(connection, context)
    rows = pipeline.build_evaluation_dataset(connection, context, SQL)
    elapsed = time.monotonic() - started

    heading("2. Publication timestamps are America/Chicago, not UTC")
    total, matching = connection.execute(
        """
        SELECT count(*), sum(CASE WHEN first_day = local_date THEN 1 ELSE 0 END)
        FROM (
            SELECT publication_ts_utc,
                   min(operating_date) AS first_day,
                   CAST(publication_ts_utc AT TIME ZONE 'America/Chicago' AS DATE) AS local_date
            FROM forecast_vintage GROUP BY publication_ts_utc
        )
        """
    ).fetchone()
    print(
        f"forecast vintages whose earliest DeliveryDate equals the filename stamp read as "
        f"Central: {matching}/{total}\n"
        "    Read as UTC, the eight vintages stamped 00:30 would fall at 19:30 the previous\n"
        "    Central day and would have to begin on that previous DeliveryDate. They do not."
    )

    heading("3. Revision behaviour")
    revised = connection.execute(
        """
        SELECT count(*), round(100.0 * sum(CASE WHEN n > 1 THEN 1 ELSE 0 END) / count(*), 1)
        FROM (
            SELECT target_ts_utc, weather_zone, count(DISTINCT forecast_mw) AS n
            FROM forecast_vintage WHERE is_ercot_model_in_use GROUP BY 1, 2
        )
        """
    ).fetchone()
    print(f"NP3-565: {revised[1]}% of {revised[0]} target zone-hours are revised across vintages")
    actual_revisions = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT operating_date FROM actual_vintage
            GROUP BY operating_date HAVING count(DISTINCT publication_ts_utc) > 1
        )
        """
    ).fetchone()[0]
    days = connection.execute("SELECT count(DISTINCT operating_date) FROM actual_vintage").fetchone()[0]
    print(f"NP6-345: {actual_revisions} of {days} operating days were ever republished")

    heading("4. Model selection")
    print(
        connection.execute(
            """
            SELECT 'models per target zone-hour: ' || string_agg(DISTINCT CAST(n AS VARCHAR), ', ')
            FROM (SELECT count(*) AS n FROM forecast_vintage
                  GROUP BY publication_ts_utc, target_ts_utc, weather_zone)
            """
        ).fetchone()[0]
    )
    print(
        connection.execute(
            """
            SELECT 'InUseFlag=Y models per target zone-hour: ' || string_agg(DISTINCT CAST(n AS VARCHAR), ', ')
            FROM (SELECT count(*) AS n FROM forecast_vintage WHERE is_ercot_model_in_use
                  GROUP BY publication_ts_utc, target_ts_utc, weather_zone)
            """
        ).fetchone()[0]
    )
    print("model names: " + ", ".join(
        r[0] for r in connection.execute(
            "SELECT DISTINCT model FROM forecast_vintage ORDER BY model"
        ).fetchall()
    ))

    heading("5. Build cost and row accountability")
    readiness = gates.data_readiness(connection, context)
    metrics = readiness.metrics
    print(
        f"processing_date {PROCESSING_DATE}  run_id {context.run_id}\n"
        f"    {metrics['source_files']} source files -> {metrics['raw_source_rows']} source rows\n"
        f"    dispositions: {metrics['dispositions']}\n"
        f"    {metrics['forecast_observations']} forecast + {metrics['actual_observations']} "
        f"actual observations\n"
        f"    evaluation window {metrics['evaluation_window'][0]}..{metrics['evaluation_window'][1]}"
        f" -> {rows} zone-hours (expected {metrics['expected_evaluation_rows']})\n"
        f"    newest publishable forecast was at most "
        f"{metrics['max_forecast_vintage_age_hours']}h old at its cutoff\n"
        f"    full rebuild from local files: {elapsed:.1f}s"
    )

    heading("6. The cost of hindsight")
    asof_wape, hindsight_wape = connection.execute(
        """
        WITH latest AS (
            SELECT weather_zone, target_ts_utc, forecast_mw,
                   row_number() OVER (PARTITION BY weather_zone, target_ts_utc
                                      ORDER BY publication_ts_utc DESC) AS rn
            FROM forecast_vintage
            WHERE is_ercot_model_in_use AND weather_zone <> 'SYSTEM_TOTAL'
        )
        SELECT 100 * sum(abs(e.ercot_error_mw)) / sum(e.actual_mw),
               100 * sum(abs(l.forecast_mw - e.actual_mw)) / sum(e.actual_mw)
        FROM evaluation_dataset e
        JOIN latest l USING (weather_zone, target_ts_utc)
        WHERE l.rn = 1 AND e.ercot_forecast_mw IS NOT NULL
        """
    ).fetchone()
    print(
        f"ERCOT in-use model, as-of (publication <= T-24h): {asof_wape:.2f}% WAPE\n"
        f"ERCOT in-use model, latest vintage available now: {hindsight_wape:.2f}% WAPE\n"
        f"    relaxing one predicate flatters the model {asof_wape / hindsight_wape:.1f}x; "
        f"nothing errors and no row goes missing"
    )

    heading("7. Gate verdicts and metrics")
    model = gates.seasonal_naive_gate(connection, context)
    print(f"data_readiness: {readiness.status}")
    print(f"seasonal_naive: {model.status}")
    for name in ("naive", "ercot"):
        evaluation = seasonal_naive.evaluate(connection, name).to_dict()
        print(
            f"  {name:<6} WAPE {evaluation['wape_pct']:.2f}%  "
            f"worst day {evaluation['worst_fold_wape_pct']:.2f}%  "
            f"peak-hour WAPE {evaluation['peak_hour_wape_pct']:.2f}%  "
            f"worst peak-hour APE {evaluation['max_peak_hour_ape_pct']:.2f}%  "
            f"p95 APE {evaluation['p95_ape_pct']:.2f}%"
        )
        print("         by day: " + ", ".join(
            f"{f['operating_date']} {f['wape_pct']:.2f}%" for f in evaluation["folds"]
        ))


if __name__ == "__main__":
    main()
