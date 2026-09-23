# forecast-spine

A point-in-time correct evaluation pipeline for ERCOT hourly load forecasts.

The model is a weekly seasonal naive, on purpose. The work is in making
historical evaluation faithfully represent what could actually have been known
at decision time.

> **Invariant:** nothing used to produce a forecast for target hour `T` may
> have become available after `T − 24h`.

It is enforced in the schema, the SQL, the model inputs, the tests and the
release gates — not just stated here. `MEMO.md` has the reasoning and the
measured evidence; this file is how to run it.

---

## Quick start

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
```

Everything below runs from synthetic fixtures — no credentials, no network:

```bash
uv run forecast-spine demo
```

```bash
uv run pytest -q
```

To use real ERCOT data (public MIS listing, no credentials required):

```bash
uv run forecast-spine acquire
```

```bash
uv run forecast-spine run --processing-date 2026-09-23
```

```bash
uv run python scripts/evidence.py
```

`run` exits **0** when both gates pass, **1** on data-readiness failure and
**2** on model-gate failure.

---

## What it does

```
ERCOT MIS listing  (NP3-565-CD forecast, NP6-345-CD actual)
        |
        v
Immutable raw files          filename + publication_ts + SHA256
        |
        v
Normalization                every source row gets a disposition
        |                    raw = accepted + duplicate + quarantined
        +--> accepted
        +--> quarantine + reason_code + raw payload
        |
        v
DuckDB canonical vintages    publication_ts kept separate from target_ts
        |
        v
sql/asof_join.sql            latest publication <= target - 24h
        |
        v
evaluation_dataset
        |
        +--> Gate 1: data readiness  --(fail)--> BLOCKED, exit 1
        |
        +--> Gate 2: seasonal naive  --(fail)--> BLOCKED, exit 2
                 |
                 v
             APPROVED, exit 0
```

Three selections run against three different clocks. The forecast and the
seasonal-naive *input* are as-of `T − 24h`; the reported actual is as-of the
processing date, because truth is only known after the hour.

The cutoff is per target hour, not one run timestamp — hour 2 and hour 23 of
the same day have deadlines 21 hours apart.

---

## Sources

| | NP3-565-CD | NP6-345-CD |
|---|---|---|
| Seven-Day Load Forecast by Model and Weather Zone | reportTypeId 14837 | |
| Actual System Load by Weather Zone | | reportTypeId 13101 |
| Cadence *measured from vintages* | every 60 min, at HH:30 CPT | every 1440 min, at 05:50 CPT |
| Public retention *measured* | 7.2 days | 31.0 days |
| Revised after publication | yes, 99.7% of target zone-hours | never observed |

Publication timestamps in MIS filenames are **America/Chicago**, confirmed
against all 174 forecast vintages rather than assumed — see `MEMO.md §1`.

The 7.2-day retention of the public listing is why the evaluation window is
2026-09-18 → 2026-09-22 rather than a window crossing spring-forward. DST is
covered by tests and an end-to-end 23-hour fixture day instead. `MEMO.md §0`.

---

## Results on real data

Processing date 2026-09-23, window 2026-09-18 → 2026-09-22, 960 zone-hours.

```
206 source files -> 268,032 source rows -> 268,032 dispositions (all ACCEPTED)
2,405,376 forecast + 6,912 actual observations
full rebuild from local files: 8.4s

data_readiness  PASS
seasonal_naive  PASS   WAPE 6.35%  worst day 7.40%  worst peak-hour APE 16.20%
```

For reference, ERCOT's own in-use model over the same dataset: WAPE 2.92%.
It is *not* a competing candidate — the exercise asks for one model — it is
there because its vintages are what the point-in-time machinery is built on.

---

## Demonstrated failures

`forecast-spine demo` runs all five scenarios and asserts each verdict:

| Scenario | Verdict | Reason |
|---|---|---|
| `pass` | APPROVED | |
| `dst_spring_forward` | APPROVED | 23-hour operating day, 184 rows |
| `missing_forecast` | BLOCKED (1) | `MISSING_ASOF_FORECAST` ×8 — the value exists in a later vintage and is not used |
| `conflicting_duplicate` | BLOCKED (1) | `QUARANTINED_SAME_KEY_DIFFERENT_VALUES` ×2 — no winner is picked |
| `schema_drift` | BLOCKED (1) | `SCHEMA_DRIFT` — a renamed zone column is refused, not parsed positionally |

---

## Layout

```
sql/asof_join.sql             the as-of selection; the heart of the submission
src/forecast_spine/
    ercot.py                  MIS listing, immutable content-addressed download
    time.py                   operating dates, hours ending, DST, DSTFlag
    normalize.py              wide CSV -> long rows, one disposition per row
    pipeline.py               run_id, warehouse schema, idempotent load
    seasonal_naive.py         WAPE, rolling-origin folds, peak-hour diagnostics
    gates.py                  the two executable gates
    fixtures.py               synthetic credential-free scenarios
    cli.py                    acquire / run / demo
tests/
    test_asof_join.py         cutoff boundaries, latest-eligible, no back-fill
    test_dst.py               23/24/25-hour days, repeated hour, 167-hour lag
    test_accounting.py        row ledger invariant, the zone-order trap
    test_rerun.py             determinism, partition replace, archive drift
    test_gates.py             all five scenarios, tampering, tightened threshold
scripts/evidence.py           regenerates every factual claim in MEMO.md
```

`data/raw/` (immutable vintages) and `data/warehouse/` are gitignored; the
pipeline rebuilds them.

## Configuration

Live acquisition uses only the public MIS endpoint. No credentials are read
from anywhere, and fixtures are generated in-process, so nothing in this repo
depends on a secret.
