# forecast-spine

A point-in-time correct evaluation pipeline for ERCOT hourly load forecasts.

The model is a weekly seasonal naive, on purpose. The work is making historical
evaluation faithfully represent what could actually have been known at decision
time.

> **Invariant:** nothing used to produce a forecast for target hour `T` may have
> become available after `T − 24h`.

It is enforced in the schema, the SQL, the model inputs, the tests and the
release gates — not just stated here.

- [`MEMO.md`](MEMO.md) — the reasoning, the measured evidence, and what would
  still get past these gates.
- [`RETRIEVAL.md`](RETRIEVAL.md) — what was and was not retrieved, generated
  from the files on disk.
- [`notebooks/exploration.ipynb`](notebooks/exploration.ipynb) — the same
  argument as figures, committed with outputs so it reads on GitHub.

---

## Run it

Nothing below needs credentials or a network. This is the fastest way to see
the whole thing work:

```bash
./scripts/lab.sh --install-only
```

```bash
uv run forecast-spine demo
```

```bash
uv run pytest -q
```

`demo` builds five synthetic scenarios end to end and asserts the verdict each
one produces — one approval, one 23-hour DST day, and three materially
different blocks. `scripts/lab.sh` with no arguments also opens the notebook in
JupyterLab.

With the public MIS listing (still no credentials — it retains about 7 days):

```bash
uv run forecast-spine acquire
uv run forecast-spine run --processing-date 2026-09-23
uv run python scripts/evidence.py
```

`run` exits **0** when both gates pass, **1** on a data-readiness failure and
**2** on a model-gate failure.

---

## The pipeline

```
ERCOT MIS listing (no credentials)   ERCOT Public API archive (credentials)
        │                                         │
        └──────────────┬──────────────────────────┘
                       ▼
        Immutable raw files        filename + publication_ts + SHA256
                       ▼
        Normalization              every source row gets a disposition
                       │           raw = accepted + duplicate + quarantined
                       ├─► accepted
                       └─► quarantine + reason_code + raw payload
                       ▼
        DuckDB canonical vintages  publication_ts kept apart from target_ts
                       ▼
        sql/asof_join.sql          latest publication ≤ target − 24h
                       ▼
        evaluation_dataset
                       ├─► Gate 1: data readiness  ──(fail)──► BLOCKED, exit 1
                       └─► Gate 2: seasonal naive  ──(fail)──► BLOCKED, exit 2
                                   │
                                   ▼
                              APPROVED, exit 0
```

Three selections run against three different clocks. The forecast and the
seasonal-naive *input* are as-of `T − 24h`; the reported actual is as-of the
processing date, because truth is only known after the hour. The cutoff is per
target hour, not one run timestamp — hour 2 and hour 23 of the same day have
deadlines 21 hours apart.

---

## Sources

| | NP3-565-CD | NP6-345-CD |
| --- | --- | --- |
| Report | Seven-Day Load Forecast by Model and Weather Zone | Actual System Load by Weather Zone |
| reportTypeId | 14837 | 13101 |
| Cadence, *measured* | every 60 min, at HH:30 CPT | every 1440 min, at 05:50 CPT |
| Revised after publication | yes — 99.7% of target zone-hours | never observed |

MIS filename timestamps are **America/Chicago**, confirmed against every
vintage held rather than assumed — see [`MEMO.md` §1](MEMO.md). The two reports
order `SOUTHERN` and `SOUTH_CENTRAL` oppositely, so zones are mapped by name
and headers are fingerprinted per file.

---

## Credentials

**The default path needs none.** `acquire`, `run`, `demo`, the notebook and the
whole test suite use only the public MIS listing and in-process fixtures.

One command needs credentials: `backfill`, which reads archived vintages older
than the public listing's ~7-day retention.

```bash
cp .env.example .env     # .env is gitignored
```

It requires **two** credentials, which is easy to get wrong. An API Explorer
subscription key alone returns `401 Unauthorized. Access token is missing or
invalid.` on every endpoint — the ercot.com account username and password are
what mint the bearer token. Neither is ever logged, echoed or written to the
warehouse, and `Credentials.__repr__` redacts them so they cannot leak into a
traceback or a notebook cell.

Requests use a sliding-window limiter below ERCOT's documented 30/min, with
`Retry-After` honoured on 429 and one silent re-auth on mid-run token expiry.

```bash
# NP3-565 publications posted 21 Feb 00:00 – 23 Mar 23:59 CPT
uv run forecast-spine backfill --report load_forecast \
    --from 2026-02-21 --to 2026-03-23 --requests-per-minute 28

# NP6-345 actuals for target days 22 Feb – 23 Mar, plus the seven-day
# seasonal-naive lookback before the first target day
uv run forecast-spine backfill --report actual_load \
    --from 2026-02-16 --to 2026-03-24 --requests-per-minute 28

# What arrived, and what did not
uv run forecast-spine coverage --from 2026-02-21 --to 2026-03-23
uv run python scripts/retrieval_report.py --verify

# Evaluate the named range rather than a day count
uv run forecast-spine run --processing-date 2026-03-24 \
    --window-start 2026-02-22 --window-end 2026-03-23
```

---

## Layout

```
sql/asof_join.sql             the as-of selection; the heart of the submission
src/forecast_spine/
    ercot.py                  MIS listing, immutable content-addressed download
    ercot_api.py              authenticated archive client, rate-limited
    coverage.py               expected publications vs what is on disk
    time.py                   operating dates, hours ending, DST, DSTFlag
    normalize.py              wide CSV → long rows, one disposition per row
    pipeline.py               run_id, warehouse schema, idempotent load
    seasonal_naive.py         WAPE, rolling-origin folds, peak-hour diagnostics
    gates.py                  the two executable gates
    fixtures.py               synthetic credential-free scenarios
    viz.py                    chart chrome for the notebook
    cli.py                    acquire / backfill / coverage / run / demo
tests/
    test_asof_join.py         cutoff boundaries, latest-eligible, no back-fill
    test_dst.py               23/24/25-hour days, repeated hour, 167-hour lag
    test_accounting.py        row ledger invariant, the zone-order trap
    test_rerun.py             determinism, partition replace, archive drift
    test_gates.py             all five scenarios, tampering, tightened threshold
    test_ercot_api.py         rate limiter, payload shapes, MIS-compatibility
    test_coverage.py          DST-aware expected cadence, named gaps
    test_window.py            explicit date ranges, refused windows
scripts/
    lab.sh                    install deps and open the notebook
    evidence.py               regenerates every factual claim in MEMO.md
    retrieval_report.py       regenerates RETRIEVAL.md
```

`data/raw/` (immutable vintages), `data/warehouse/` and `data/reports/` are
gitignored; the pipeline rebuilds them.

---

## What the figures show

Two carry the argument:

- **the vintage landscape** — every forecast ever published for a zone, plotted
  against what it forecasts, with the `T − 24h` frontier drawn through it;
- **the cost of hindsight** — relax one predicate to take the latest vintage
  instead of the as-of one, and ERCOT's own model scores **1.13%** instead of
  **2.92%**. Nothing errors, no row goes missing, every chart still renders. The
  model just looks 2.6× better than it was at decision time.

That second number is the exercise in one figure: the failure mode is not a
crash, it is a plausible number.
