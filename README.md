# forecast-spine

A point-in-time correct ERCOT load forecast evaluation pipeline.

**Core invariant**

> Nothing used to produce a forecast for target time `T` may have become
> available after `T - 24h`.

That invariant is enforced in the schema, the as-of SQL, the tests, the model,
and the release gates — not just asserted in prose.

## Pipeline

```
ERCOT API / archive
        |
        v
 Immutable raw files  (filename + posted_at + SHA256)
        |
        v
 Normalization ---> accepted rows
        |      \--> quarantine rows + reason_code
        v
 DuckDB canonical tables
        |-- forecast_vintages
        |-- actual_vintages
        v
 AS-OF SQL  (latest publication <= target - 24h)
        |
        v
 evaluation_dataset
        |-- Data Readiness Gate
        |-- Seasonal Naive Gate
        v
 structured PASS / FAIL  (non-zero exit on FAIL)
```

## Sources

| Report | Product | Cadence (claimed) |
| --- | --- | --- |
| Hourly load forecast by weather zone | NP3-565-CD | hourly |
| Actual system load by weather zone | NP6-345-CD | daily (verify empirically) |

Publication cadence and revision behaviour are **measured from downloaded
vintages** and reported in `MEMO.md`, not assumed from the docs.

## Usage

```bash
uv run forecast-spine run --processing-date 2026-03-10
uv run forecast-spine run --processing-date 2026-03-10 --source fixtures
```

Live credentials are supplied via environment variables only; fixtures are
credential-free.

## Layout

```
sql/asof_join.sql          the as-of selection, the heart of the submission
src/forecast_spine/        ercot, normalize, time, pipeline, seasonal_naive, gates
tests/                     asof boundaries, DST, row accounting, rerun, gates
```

## Design notes

- `publication_ts` is kept strictly separate from `target_ts_utc`.
- Every source row ends with a disposition:
  `raw_rows = accepted + duplicate + quarantined`.
- Conflicting duplicates at the same publication timestamp are **not**
  resolved by guessing; the key is marked conflicted and blocks release.
- Reruns are deterministic:
  `run_id = SHA256(processing_date + sorted(input_hashes) + pipeline_version)`.
- Timestamps are stored both as ERCOT local (`operating_date`, `hour_ending`,
  `dst_flag`) and canonical `target_ts_utc` (`America/Chicago`).
