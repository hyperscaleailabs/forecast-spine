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

Three paths. The first needs nothing; the third reproduces the assignment.

### 1 · No credentials, no network — the whole system in about a minute

```bash
./scripts/lab.sh --install-only     # venv + deps, idempotent
uv run forecast-spine demo          # five scenarios, each verdict asserted
uv run pytest -q                    # 75 tests
```

`demo` builds five synthetic scenarios end to end and asserts the verdict each
produces: one approval, one 23-hour DST day, and three materially different
blocks. It is the proof that **both gate outcomes are reachable**, and it needs
no credentials, no network and no data on disk.

`scripts/lab.sh` with no arguments also opens JupyterLab.

### 2 · The live rolling window — still no credentials

The public MIS listing retains about seven days, so this always evaluates
roughly *now*:

```bash
uv run forecast-spine acquire        # ~12 MB, a few minutes
uv run forecast-spine run --processing-date YYYY-MM-DD   # yesterday's date
uv run python scripts/evidence.py
```

Use *yesterday* as the processing date: actuals for operating day D publish on
D+1, so today's operating day cannot yet be scored. Passing a date on or after
today is refused with an explanatory error rather than silently evaluating a
partial day.

### 3 · The assignment window — credentials required

`data/raw/` is gitignored, so a fresh clone holds no vintages. February and
March 2026 are older than the public listing's retention, which means this path
needs the authenticated archive. Credentials go in `.env` — see
[Credentials](#credentials) below; if you already have a populated `.env`,
start at step 1.

```bash
# 1. Retrieve. ~30 min total at 28 req/min; idempotent, safe to re-run.
uv run forecast-spine backfill --report load_forecast \
    --from 2026-02-21 --to 2026-03-23 --requests-per-minute 28
uv run forecast-spine backfill --report actual_load \
    --from 2026-02-16 --to 2026-03-24 --requests-per-minute 28

# 2. Check what arrived before trusting it.
uv run forecast-spine coverage --from 2026-02-21 --to 2026-03-23
uv run python scripts/retrieval_report.py --verify

# 3. Evaluate the assignment's named range. ~43s.
uv run forecast-spine run --processing-date 2026-03-24 \
    --window-start 2026-02-22 --window-end 2026-03-23

# 4. Regenerate every figure quoted in this README and in MEMO.md.
uv run python scripts/evidence.py 2026-03-24 \
    --window-start 2026-02-22 --window-end 2026-03-23
```

The publication window starts on **21 February**, a day before the first target
day, because 22 February hour ending 01:00 needs a vintage published before it.
Actuals start on **16 February** for the seven-day seasonal-naive lookback, and
end on **24 March** because actuals for operating day D publish on D+1.

**Step 3 is expected to exit non-zero. That is the result, not a failure to
run.** Abridged — the real output names five sample zone-hours per reason:

```
run ae80e7096413957c6de9fafe9058c20b  processing_date=2026-03-24  files=775  window=2026-02-22..2026-03-23  evaluation_rows=5752  elapsed=42.6s

[FAIL] data_readiness  -> data/reports/2026-03-24_data_readiness.json
  MISSING_ASOF_FORECAST (count=8): 8 target zone-hours where no ERCOT forecast was publishable by the cutoff; ...
  MISSING_NAIVE_INPUT (count=8): 8 target zone-hours where no seasonal-naive input was publishable by the cutoff; ...
  STALE_FORECAST_VINTAGE (count=16): 16 target zone-hours whose newest publishable forecast was up to 3.5h old at its cutoff (limit 2.0h); ...
RELEASE BLOCKED: data readiness failed; the model gate was not run.
```

Three failures, three different causes:

| Reason | Rows | Where | Defect? |
| --- | --- | --- | --- |
| `MISSING_ASOF_FORECAST` | 8 | 2026-02-22 HE 1 | **No — structural.** Its cutoff is 2026-02-21 00:00 and publications post at HH:30, so the assignment's own publication window opens 30 minutes too late. |
| `MISSING_NAIVE_INPUT` | 8 | 2026-03-15 HE 3 | **No — the calendar.** Its week-ago input is 2026-03-08 HE 3, the hour spring-forward deletes. |
| `STALE_FORECAST_VINTAGE` | 16 | 2026-03-07 HE 13–14 | **Yes — a real gap.** Five publications are absent from 2026-03-06, leaving the newest publishable vintage 2.5h and 3.5h old against a 2h limit. |

The first two are reported rather than papered over: reaching back to a
20 February vintage would serve HE 1 and would break the stated publication
window, and imputing 15 March HE 3 would invent a week-over-week comparison
with no left-hand side. [`MEMO.md`](MEMO.md) argues each one.

Run `demo` to see the same gates approve a release.

`elapsed` is from one laptop run; everything else is deterministic for the same
inputs. **Close any SQL client holding `data/warehouse/forecast_spine.duckdb`
first** — DuckDB is single-writer and an open connection will fail the run.

Exit codes: **0** both gates passed · **1** data-readiness failure · **2**
model-gate failure.

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

The error columns use **`error = forecast − actual`**, so a positive error is an
over-forecast.

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
vintage held rather than assumed — see [`MEMO-long.md` §1](MEMO-long.md). The two reports
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

Already have a populated `.env`? Nothing else to configure — go straight to
[Run it §3](#3--the-assignment-window--credentials-required).

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
notebooks/
    exploration.ipynb         the walkthrough, organised as Part 0-4
    playbook_0_acquisition.ipynb   \
    playbook_1_asof_join.ipynb      |  one per stage: what it guarantees,
    playbook_2_pipeline.ipynb       |  what breaks, and what running it in
    playbook_3_gates.ipynb          |  production would require
    playbook_4_operations.ipynb    /
```

`notebooks/exploration.ipynb` is the narrative; the five playbooks are
operational and are described in [`notebooks/README.md`](notebooks/README.md).
Every notebook builds a scratch warehouse under a temporary directory, so none
of them blocks — or is blocked by — a SQL client holding `data/warehouse/`.

`data/raw/` (immutable vintages), `data/warehouse/` and `data/reports/` are
gitignored; the pipeline rebuilds them.

---

## What the figures show

Two carry the argument:

- **the vintage landscape** — every forecast ever published for a zone, plotted
  against what it forecasts, with the `T − 24h` frontier drawn through it;
- **the cost of hindsight** — relax one predicate to take the latest vintage
  instead of the as-of one, and ERCOT's own model scores **1.08%** instead of
  **3.39%**. Nothing errors, no row goes missing, every chart still renders. The
  model just looks **3.1× better** than it was at decision time.

That second number is the exercise in one figure: the failure mode is not a
crash, it is a plausible number.

Both figures are generated, not typed — `scripts/evidence.py §6` over the
assignment window prints them.
