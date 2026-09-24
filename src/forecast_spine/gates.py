"""Two executable release gates.

They are deliberately shaped like small production APIs rather than test
assertions: each returns a structured verdict with machine-readable reason
codes, counts and sample keys, and the CLI exits non-zero when either fails.
A human reading the failure should know what broke and where without opening
the database.

    1. data_readiness  -- may this dataset be used to judge anything at all?
    2. seasonal_naive  -- given a trustworthy dataset, is the model good
                          enough to release?

Gate 1 runs first and its failure short-circuits gate 2. Scoring a model on
data that failed readiness is precisely the "plausible but wrong" outcome the
pipeline exists to prevent.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any

import duckdb

from . import normalize, seasonal_naive
from .pipeline import RunContext
from .time import hours_in_operating_date

PASS = "PASS"
FAIL = "FAIL"

# Sample keys attached to a failure reason, so a failure is actionable
# without a follow-up query.
SAMPLE_LIMIT = 5


@dataclasses.dataclass(frozen=True)
class Reason:
    code: str
    count: int
    detail: str
    sample_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "count": self.count,
            "detail": self.detail,
            "sample_keys": list(self.sample_keys),
        }


@dataclasses.dataclass(frozen=True)
class GateResult:
    gate: str
    status: str
    run_id: str
    processing_date: dt.date
    metrics: dict[str, Any]
    reasons: tuple[Reason, ...]

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "run_id": self.run_id,
            "processing_date": self.processing_date.isoformat(),
            "metrics": self.metrics,
            "reasons": [r.to_dict() for r in self.reasons],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


@dataclasses.dataclass(frozen=True)
class Thresholds:
    """Operational assumptions for this exercise, not tuned numbers.

    They were chosen before looking for a passing configuration and are
    justified in `MEMO.md`. `max_wape_pct = 8.0` was set against the September
    window, where seasonal-naive scored 6.35% -- deliberate but not generous
    headroom *for that window*.

    On the assignment window (Feb-Mar 2026) seasonal-naive scores 8.65% and all
    three model limits trip. That is left as-is on purpose: the thresholds were
    fixed before the March data existed, which is the only property that makes a
    threshold mean anything, and re-fitting after observing the failure would be
    fitting the gate to the answer. A seasonality-aware limit derived from a
    rolling per-month baseline is the real correction, and is not built.
    """

    # NP3-565 publishes hourly, so the newest forecast publishable by a
    # T-24h cutoff should be under an hour old. Anything materially older means
    # vintages are missing -- ours or ERCOT's. On this window it was ERCOT's:
    # the archive lists 19 publications for 2026-03-06 and we hold all 19.
    max_forecast_vintage_age_hours: float = 2.0
    max_wape_pct: float = 8.0
    # No single operating day may be much worse than the window as a whole.
    max_fold_wape_pct: float = 9.0
    # The compensating guardrail: peak hours are where error costs money.
    max_peak_hour_ape_pct: float = 20.0


def _sample(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> tuple[str, ...]:
    rows = con.execute(sql, params or []).fetchall()
    return tuple(" ".join(str(value) for value in row) for row in rows)


def data_readiness(
    con: duckdb.DuckDBPyConnection,
    context: RunContext,
    thresholds: Thresholds = Thresholds(),
) -> GateResult:
    """Is this dataset fit to judge a forecast?"""
    reasons: list[Reason] = []
    date = context.processing_date

    # --- 1. Every source row is accounted for -------------------------------
    raw_rows = con.execute(
        "SELECT COALESCE(sum(raw_row_count), 0) FROM source_file WHERE processing_date = ?", [date]
    ).fetchone()[0]
    ledger_rows = con.execute(
        "SELECT count(*) FROM source_row WHERE processing_date = ?", [date]
    ).fetchone()[0]
    if raw_rows != ledger_rows:
        reasons.append(
            Reason(
                "SOURCE_ROWS_UNACCOUNTED",
                abs(raw_rows - ledger_rows),
                f"{raw_rows} raw source rows but {ledger_rows} ledger dispositions; "
                f"rows were lost or duplicated during normalization",
            )
        )

    # --- 2. Nothing ambiguous or unparseable got in -------------------------
    quarantined = con.execute(
        """
        SELECT reason_code, count(*), min(source_filename || ' row ' || source_row_number)
        FROM source_row
        WHERE processing_date = ? AND disposition IN ?
        GROUP BY reason_code ORDER BY count(*) DESC
        """,
        [date, list(normalize.QUARANTINE_DISPOSITIONS)],
    ).fetchall()
    for reason_code, count, sample in quarantined:
        reasons.append(
            Reason(
                f"QUARANTINED_{reason_code}",
                count,
                f"{count} source rows quarantined as {reason_code}; "
                f"ambiguous or invalid input blocks release rather than being guessed at",
                (sample,),
            )
        )

    # --- 3. The file schema is what the parser was written against ----------
    drifted = con.execute(
        """
        SELECT count(DISTINCT source_file_id), min(original_filename)
        FROM source_file f
        WHERE f.processing_date = ?
          AND EXISTS (
              SELECT 1 FROM source_row r
              WHERE r.source_file_id = f.source_file_id
                AND r.processing_date = f.processing_date
                AND r.disposition = ?
          )
        """,
        [date, normalize.QUARANTINED_SCHEMA_ERROR],
    ).fetchone()
    if drifted[0]:
        reasons.append(
            Reason("SCHEMA_DRIFT", drifted[0], "source files no longer match the expected header", (drifted[1],))
        )

    # --- 4. Exactly one ERCOT model is flagged in use per target hour -------
    multi_model = con.execute(
        """
        SELECT count(*) FROM (
            SELECT publication_ts_utc, target_ts_utc, weather_zone
            FROM forecast_vintage
            WHERE processing_date = ? AND is_ercot_model_in_use
            GROUP BY 1, 2, 3 HAVING count(*) <> 1
        )
        """,
        [date],
    ).fetchone()[0]
    if multi_model:
        reasons.append(
            Reason(
                "AMBIGUOUS_MODEL_IN_USE",
                multi_model,
                "publication/target/zone keys where the count of InUseFlag='Y' models is not 1; "
                "model selection would be arbitrary",
            )
        )

    # --- 5. Evaluation coverage --------------------------------------------
    first_day, last_day = context.window_operating_dates
    expected = 0
    day = first_day
    while day <= last_day:
        expected += hours_in_operating_date(day) * len(normalize.WEATHER_ZONES)
        day += dt.timedelta(days=1)
    observed = con.execute("SELECT count(*) FROM evaluation_dataset").fetchone()[0]
    if observed != expected:
        reasons.append(
            Reason(
                "INCOMPLETE_ZONE_HOUR_COVERAGE",
                abs(expected - observed),
                f"expected {expected} zone-hours for operating days {first_day}..{last_day} "
                f"(DST-aware), found {observed}",
            )
        )

    # --- 6. Nothing missing in the as-of dataset ----------------------------
    for code, column, description in (
        ("MISSING_ASOF_FORECAST", "ercot_forecast_mw", "no ERCOT forecast was publishable by the cutoff"),
        ("MISSING_ACTUAL", "actual_mw", "no reported actual is available to score against"),
        ("MISSING_NAIVE_INPUT", "naive_forecast_mw", "no seasonal-naive input was publishable by the cutoff"),
    ):
        count = con.execute(
            f"SELECT count(*) FROM evaluation_dataset WHERE {column} IS NULL"
        ).fetchone()[0]
        if count:
            reasons.append(
                Reason(
                    code,
                    count,
                    f"{count} target zone-hours where {description}; "
                    f"these are never back-filled from a later vintage",
                    _sample(
                        con,
                        f"""SELECT weather_zone, strftime(target_ts_utc, '%Y-%m-%dT%H:%MZ')
                            FROM evaluation_dataset WHERE {column} IS NULL
                            ORDER BY target_ts_utc, weather_zone LIMIT {SAMPLE_LIMIT}""",
                    ),
                )
            )

    # --- 7. No unresolved conflicts at the winning publication --------------
    for code, column in (
        ("CONFLICTING_ASOF_FORECAST", "ercot_value_count"),
        ("CONFLICTING_ASOF_ACTUAL", "actual_value_count"),
        ("CONFLICTING_NAIVE_INPUT", "naive_value_count"),
    ):
        count = con.execute(
            f"SELECT count(*) FROM evaluation_dataset WHERE {column} > 1"
        ).fetchone()[0]
        if count:
            reasons.append(
                Reason(
                    code,
                    count,
                    f"{count} target zone-hours where the winning publication carries more than "
                    f"one distinct value; no winner is picked and release is blocked",
                    _sample(
                        con,
                        f"""SELECT weather_zone, strftime(target_ts_utc, '%Y-%m-%dT%H:%MZ')
                            FROM evaluation_dataset WHERE {column} > 1
                            ORDER BY target_ts_utc, weather_zone LIMIT {SAMPLE_LIMIT}""",
                    ),
                )
            )

    # --- 8. The invariant itself, re-asserted against the output ------------
    # This should be impossible given the WHERE clause in asof_join.sql. It is
    # checked anyway: the gate must not trust the query it is gating.
    violations = con.execute(
        "SELECT count(*) FROM evaluation_dataset WHERE ercot_publication_ts_utc > cutoff_ts_utc"
    ).fetchone()[0]
    if violations:
        reasons.append(
            Reason(
                "CUTOFF_VIOLATION",
                violations,
                "as-of rows whose forecast was published after its own T-24h cutoff; "
                "the dataset contains hindsight and must not be used",
                _sample(
                    con,
                    f"""SELECT weather_zone, strftime(target_ts_utc, '%Y-%m-%dT%H:%MZ'),
                               strftime(ercot_publication_ts_utc, '%Y-%m-%dT%H:%MZ')
                        FROM evaluation_dataset WHERE ercot_publication_ts_utc > cutoff_ts_utc
                        LIMIT {SAMPLE_LIMIT}""",
                ),
            )
        )

    # --- 9. The as-of forecast is not unexpectedly stale --------------------
    stale = con.execute(
        "SELECT count(*), max(ercot_vintage_age_hours) FROM evaluation_dataset "
        "WHERE ercot_vintage_age_hours > ?",
        [thresholds.max_forecast_vintage_age_hours],
    ).fetchone()
    if stale[0]:
        reasons.append(
            Reason(
                "STALE_FORECAST_VINTAGE",
                stale[0],
                f"{stale[0]} target zone-hours whose newest publishable forecast was up to "
                f"{stale[1]:.1f}h old at its cutoff (limit "
                f"{thresholds.max_forecast_vintage_age_hours}h). NP3-565 publishes hourly, so "
                f"this means vintages are absent -- either our acquisition missed them or "
                f"ERCOT never posted them. `scripts/retrieval_report.py --verify` "
                f"distinguishes the two by asking the archive listing",
                _sample(
                    con,
                    f"""SELECT weather_zone, strftime(target_ts_utc, '%Y-%m-%dT%H:%MZ'),
                               round(ercot_vintage_age_hours, 2)
                        FROM evaluation_dataset WHERE ercot_vintage_age_hours > ?
                        ORDER BY ercot_vintage_age_hours DESC LIMIT {SAMPLE_LIMIT}""",
                    [thresholds.max_forecast_vintage_age_hours],
                ),
            )
        )

    dispositions = dict(
        con.execute(
            "SELECT disposition, count(*) FROM source_row WHERE processing_date = ? GROUP BY 1",
            [date],
        ).fetchall()
    )
    metrics = {
        "source_files": con.execute(
            "SELECT count(*) FROM source_file WHERE processing_date = ?", [date]
        ).fetchone()[0],
        "raw_source_rows": raw_rows,
        "ledger_rows": ledger_rows,
        "dispositions": dispositions,
        "forecast_observations": con.execute(
            "SELECT count(*) FROM forecast_vintage WHERE processing_date = ?", [date]
        ).fetchone()[0],
        "actual_observations": con.execute(
            "SELECT count(*) FROM actual_vintage WHERE processing_date = ?", [date]
        ).fetchone()[0],
        "evaluation_rows": observed,
        "expected_evaluation_rows": expected,
        "evaluation_window": [str(first_day), str(last_day)],
        "max_forecast_vintage_age_hours": con.execute(
            "SELECT round(max(ercot_vintage_age_hours), 3) FROM evaluation_dataset"
        ).fetchone()[0],
    }

    return GateResult(
        gate="data_readiness",
        status=FAIL if reasons else PASS,
        run_id=context.run_id,
        processing_date=date,
        metrics=metrics,
        reasons=tuple(reasons),
    )


def seasonal_naive_gate(
    con: duckdb.DuckDBPyConnection,
    context: RunContext,
    thresholds: Thresholds = Thresholds(),
) -> GateResult:
    """Is the seasonal-naive forecast good enough to release?"""
    evaluation = seasonal_naive.evaluate(con, "naive")
    reasons: list[Reason] = []

    evaluation_rows = con.execute("SELECT count(*) FROM evaluation_dataset").fetchone()[0]
    if evaluation.rows != evaluation_rows:
        # A model must be scored on every row of the dataset it claims to
        # cover. Silently scoring the easy subset is how a bad forecast
        # acquires a good number.
        reasons.append(
            Reason(
                "INCOMPLETE_SCORING",
                evaluation_rows - evaluation.rows,
                f"scored {evaluation.rows} of {evaluation_rows} evaluation rows; "
                f"the metric does not cover the dataset it reports on",
            )
        )

    if evaluation.wape_pct > thresholds.max_wape_pct:
        reasons.append(
            Reason(
                "WAPE_ABOVE_THRESHOLD",
                1,
                f"WAPE {evaluation.wape_pct:.2f}% exceeds {thresholds.max_wape_pct:.2f}%",
            )
        )

    bad_folds = [f for f in evaluation.folds if f.wape_pct > thresholds.max_fold_wape_pct]
    if bad_folds:
        reasons.append(
            Reason(
                "FOLD_WAPE_ABOVE_THRESHOLD",
                len(bad_folds),
                f"{len(bad_folds)} operating days exceed {thresholds.max_fold_wape_pct:.2f}% WAPE; "
                f"an acceptable window average is hiding an unacceptable day",
                tuple(f"{f.operating_date} {f.wape_pct:.2f}%" for f in bad_folds[:SAMPLE_LIMIT]),
            )
        )

    bad_peaks = [z for z in evaluation.zones if z.peak_hour_ape_pct > thresholds.max_peak_hour_ape_pct]
    if bad_peaks:
        reasons.append(
            Reason(
                "PEAK_HOUR_ERROR_ABOVE_THRESHOLD",
                len(bad_peaks),
                f"{len(bad_peaks)} zones miss their daily peak hour by more than "
                f"{thresholds.max_peak_hour_ape_pct:.2f}%; average accuracy does not compensate "
                f"for error at peak",
                tuple(
                    f"{z.weather_zone} {z.peak_hour_ape_pct:.2f}% "
                    f"({z.peak_hour_abs_error_mw:.0f} MW)"
                    for z in bad_peaks[:SAMPLE_LIMIT]
                ),
            )
        )

    metrics = evaluation.to_dict()
    metrics["thresholds"] = dataclasses.asdict(thresholds)

    return GateResult(
        gate="seasonal_naive",
        status=FAIL if reasons else PASS,
        run_id=context.run_id,
        processing_date=context.processing_date,
        metrics=metrics,
        reasons=tuple(reasons),
    )
