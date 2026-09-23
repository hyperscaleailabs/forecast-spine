"""Weekly seasonal-naive forecast and its evaluation.

Definition
----------
    y_hat(zone, T) = actual(zone, same local hour, T - 7 operating days)

Seven days rather than one because it carries hour-of-day *and* day-of-week
together: a Saturday is predicted from a Saturday.

The forecast itself is produced by `sql/asof_join.sql`, not here, because the
only hard part is point-in-time selection: the source actual must have been
published by the *forecast's* cutoff (T - 24h), not merely exist today. This
module scores what that query produced.

Metric
------
WAPE, sum |error| / sum actual, rather than MAE or MAPE.

    - The system runs at multi-GW scale, so a scale-free number is needed to
      compare zones; MAE is not comparable across Far West and North Central.
    - MAPE divides per row, so a single low-load hour dominates the average.
      WAPE's single denominator is the fraction of delivered demand missed,
      which is what an operator actually cares about.

Evaluation is sequential by operating day (rolling origin). A random
train/test split would be meaningless here: every fold's inputs must precede
its target.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import duckdb

FORECAST_COLUMNS = {
    "naive": ("naive_forecast_mw", "naive_error_mw"),
    "ercot": ("ercot_forecast_mw", "ercot_error_mw"),
}


@dataclasses.dataclass(frozen=True)
class Fold:
    """One rolling-origin fold: a single operating day."""

    operating_date: dt.date
    rows: int
    wape_pct: float
    mae_mw: float
    bias_mw: float


@dataclasses.dataclass(frozen=True)
class ZoneScore:
    weather_zone: str
    rows: int
    wape_pct: float
    peak_hour_abs_error_mw: float
    peak_hour_ape_pct: float


@dataclasses.dataclass(frozen=True)
class Evaluation:
    model: str
    rows: int
    wape_pct: float
    mae_mw: float
    bias_mw: float
    p95_ape_pct: float
    worst_fold_wape_pct: float
    peak_hour_wape_pct: float
    max_peak_hour_ape_pct: float
    folds: tuple[Fold, ...]
    zones: tuple[ZoneScore, ...]

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "rows": self.rows,
            "wape_pct": round(self.wape_pct, 4),
            "mae_mw": round(self.mae_mw, 3),
            "bias_mw": round(self.bias_mw, 3),
            "p95_ape_pct": round(self.p95_ape_pct, 4),
            "worst_fold_wape_pct": round(self.worst_fold_wape_pct, 4),
            "peak_hour_wape_pct": round(self.peak_hour_wape_pct, 4),
            "max_peak_hour_ape_pct": round(self.max_peak_hour_ape_pct, 4),
            "folds": [
                {
                    "operating_date": f.operating_date.isoformat(),
                    "rows": f.rows,
                    "wape_pct": round(f.wape_pct, 4),
                    "mae_mw": round(f.mae_mw, 3),
                    "bias_mw": round(f.bias_mw, 3),
                }
                for f in self.folds
            ],
            "zones": [
                {
                    "weather_zone": z.weather_zone,
                    "rows": z.rows,
                    "wape_pct": round(z.wape_pct, 4),
                    "peak_hour_abs_error_mw": round(z.peak_hour_abs_error_mw, 3),
                    "peak_hour_ape_pct": round(z.peak_hour_ape_pct, 4),
                }
                for z in self.zones
            ],
        }


def _scored(model: str) -> str:
    """Rows that can be scored at all: both a forecast and an actual."""
    forecast_column, error_column = FORECAST_COLUMNS[model]
    return f"""
        SELECT
            weather_zone,
            operating_date,
            target_ts_utc,
            actual_mw,
            {forecast_column} AS forecast_mw,
            {error_column}    AS error_mw,
            abs({error_column}) AS abs_error_mw
        FROM evaluation_dataset
        WHERE actual_mw IS NOT NULL
          AND {forecast_column} IS NOT NULL
    """


def evaluate(con: duckdb.DuckDBPyConnection, model: str = "naive") -> Evaluation:
    """Score `model` over the run's evaluation dataset.

    Rows with a missing forecast or actual are *excluded here and counted by
    the readiness gate*. Scoring must never quietly shrink its own
    denominator -- that is how a pipeline reports an excellent metric on the
    handful of hours that happened to work.
    """
    if model not in FORECAST_COLUMNS:
        raise ValueError(f"unknown model {model!r}")
    scored = _scored(model)

    overall = con.execute(
        f"""
        SELECT count(*),
               100.0 * sum(abs_error_mw) / nullif(sum(actual_mw), 0),
               avg(abs_error_mw),
               avg(error_mw),
               quantile_cont(100.0 * abs_error_mw / nullif(actual_mw, 0), 0.95)
        FROM ({scored})
        """
    ).fetchone()

    folds = tuple(
        Fold(operating_date=row[0], rows=row[1], wape_pct=row[2], mae_mw=row[3], bias_mw=row[4])
        for row in con.execute(
            f"""
            SELECT operating_date, count(*),
                   100.0 * sum(abs_error_mw) / nullif(sum(actual_mw), 0),
                   avg(abs_error_mw), avg(error_mw)
            FROM ({scored})
            GROUP BY operating_date
            ORDER BY operating_date
            """
        ).fetchall()
    )

    # Peak hours: the highest-actual hour of each zone-day. A model can post a
    # fine overall WAPE while being badly wrong exactly here, which is the
    # only place the number matters operationally.
    peak = f"""
        SELECT * FROM (
            SELECT *, row_number() OVER (
                PARTITION BY weather_zone, operating_date ORDER BY actual_mw DESC
            ) AS peak_rank
            FROM ({scored})
        ) WHERE peak_rank = 1
    """

    zones = tuple(
        ZoneScore(
            weather_zone=row[0],
            rows=row[1],
            wape_pct=row[2],
            peak_hour_abs_error_mw=row[3],
            peak_hour_ape_pct=row[4],
        )
        for row in con.execute(
            f"""
            SELECT z.weather_zone, z.rows, z.wape_pct,
                   p.max_abs_error_mw, p.max_ape_pct
            FROM (
                SELECT weather_zone, count(*) AS rows,
                       100.0 * sum(abs_error_mw) / nullif(sum(actual_mw), 0) AS wape_pct
                FROM ({scored}) GROUP BY weather_zone
            ) z
            JOIN (
                SELECT weather_zone,
                       max(abs_error_mw) AS max_abs_error_mw,
                       max(100.0 * abs_error_mw / nullif(actual_mw, 0)) AS max_ape_pct
                FROM ({peak}) GROUP BY weather_zone
            ) p USING (weather_zone)
            ORDER BY z.weather_zone
            """
        ).fetchall()
    )

    peak_overall = con.execute(
        f"""
        SELECT 100.0 * sum(abs_error_mw) / nullif(sum(actual_mw), 0),
               max(100.0 * abs_error_mw / nullif(actual_mw, 0))
        FROM ({peak})
        """
    ).fetchone()

    return Evaluation(
        model=model,
        rows=overall[0],
        wape_pct=overall[1] or 0.0,
        mae_mw=overall[2] or 0.0,
        bias_mw=overall[3] or 0.0,
        p95_ape_pct=overall[4] or 0.0,
        worst_fold_wape_pct=max((f.wape_pct for f in folds), default=0.0),
        peak_hour_wape_pct=peak_overall[0] or 0.0,
        max_peak_hour_ape_pct=peak_overall[1] or 0.0,
        folds=folds,
        zones=zones,
    )
