# Memo

Every number below was produced by `scripts/evidence.py` against the 206
vintages in `data/raw`, on processing date **2026-09-23**. Nothing here is
asserted from documentation alone; where a claim could not be verified from
data, it is labelled an assumption and says what would falsify it.

---

## 0. The constraint that shaped the submission

The public ERCOT MIS listing retains only a **7.2-day window** of NP3-565
vintages (174 files, 2026-09-16 00:30 → 2026-09-23 05:30 CDT). Historical
forecast vintages older than that are not served by the public endpoint; they
require the authenticated `api.ercot.com` archive, which needs a subscription
key I do not have.

Consequence: the evaluation window is **2026-09-18 → 2026-09-22**, not a
window crossing the March 2026 spring-forward transition. I did not fabricate
the earlier period or substitute today's files for it — that would be exactly
the hindsight this pipeline exists to prevent.

DST is therefore handled in two places instead: the transition logic is
isolated in `src/forecast_spine/time.py`, and both transitions are covered by
synthetic fixtures and tests, including an end-to-end run over a 23-hour
operating day (`forecast-spine demo`, scenario `dst_spring_forward`). Given
credentials, backfilling March is a one-command change to the acquisition
layer and nothing downstream moves.

---

## 1. Source judgment

| | NP3-565-CD (forecast) | NP6-345-CD (actual) |
|---|---|---|
| reportTypeId | 14837 | 13101 |
| Vintages held | 174 | 32 |
| Retention observed | 7.2 days | 31.0 days |
| Cadence observed | every **60 min**, no other gap | every **1440 min**, no other gap |
| Publication time | HH:30 local | 05:50 local, for the previous operating day |
| Revised after publication? | **yes — 99.7%** of 3,240 target zone-hours differ across vintages | **no** — 0 of 32 operating days republished |

ERCOT's product page calls NP6-345-CD "daily", and the measurements agree. A
related ERCOT weather-zone display is documented as updating hourly at ~20
past the hour; that is a different artefact and does **not** describe this CDR
product. I measured rather than inherited the claim.

The 99.7% revision rate is the reason this is a vintage problem at all. If
forecasts never changed, a single snapshot would do.

**Publication timestamps are Central, not UTC.** This matters more than it
sounds: misreading it shifts every cutoff by five hours and quietly admits
forecasts published *after* their deadline. The discriminator is the eight
vintages stamped `00:30`. Read as Central, a 7-day forecast published at 00:30
must begin on that same local DeliveryDate. Read as UTC, 00:30 is 19:30 the
*previous* Central day and the file would have to begin on the previous
DeliveryDate. All **174/174** vintages begin on the local date. Verified, not
assumed.

**Model selection is unambiguous.** Each publication carries 8 models
(A3, A6, E, E1, E2, E3, M, X) per target zone-hour, and in every one of them
exactly **1** carries `InUseFlag = 'Y'`. The pipeline selects on that flag and
the readiness gate fails if the count is ever not 1, rather than picking a
model by name.

**A trap worth naming.** The two reports order their weather-zone columns
differently:

```
NP3-565: ... North, NorthCentral, SouthCentral, Southern, West ...
NP6-345: ... NORTH,   NORTH_C,    SOUTHERN,     SOUTH_C,  WEST ...
```

Positions 7 and 8 are swapped; the other six zones line up. A positional
parser gets six of eight zones right and produces a fully populated,
entirely plausible dataset in which two multi-GW zones are transposed. Both
reports go through explicit name→zone tables, the header is fingerprinted on
every file, and `test_zone_columns_are_mapped_by_name_because_the_reports_order_them_differently`
pins it.

---

## 2. Point-in-time correctness

The invariant, enforced in `sql/asof_join.sql` and re-checked independently by
the readiness gate:

> Nothing used to produce a forecast for target hour `T` may have become
> available after `T − 24h`.

Three selections run against three different clocks:

| Selection | Cutoff | Why |
|---|---|---|
| ERCOT forecast | `publication_ts ≤ T − 24h` | what was knowable at decision time |
| Seasonal-naive input | `publication_ts ≤ T − 24h` | the model's input is also a point-in-time value |
| Reported actual | `publication_ts ≤ processing_ts` | truth, known only after the hour |

Two details that are easy to get wrong:

- **The cutoff is per target hour, not one run timestamp.** Hour 2 and hour 23
  of the same operating day have deadlines 21 hours apart. Collapsing them to
  a single "as of yesterday" is the most common silent failure.
  (`test_cutoff_is_per_target_hour_not_a_single_run_timestamp`)
- **The seasonal-naive input needs the same discipline as the forecast.** The
  actual for `T − 7d` exists in the warehouse today, but the pipeline may only
  use it if it had been *published* by `T − 24h`.
  (`test_seasonal_naive_input_must_also_respect_the_cutoff`)

Boundary behaviour is pinned by tests: publication exactly at `T − 24h` is
eligible; one second later is not; among eligible vintages the latest wins;
and a later, more accurate vintage is never substituted, not even for an hour
that would otherwise be missing.

Observed result: across all 960 evaluation zone-hours, the newest publishable
forecast was at most **0.5 h** old at its own cutoff — consistent with an
hourly product and complete acquisition.

---

## 3. Row accountability

Unit of accountability is one source CSV data line. Every line gets exactly
one disposition:

```
ACCEPTED | DUPLICATE_IDENTICAL | QUARANTINED_PARSE_ERROR
QUARANTINED_INVALID_VALUE | QUARANTINED_KEY_CONFLICT | QUARANTINED_SCHEMA_ERROR
```

and the pipeline asserts, at the point of production and again in the gate:

```
raw_rows == accepted + duplicate_identical + quarantined
```

For this run: **268,032 source rows → 268,032 ledger dispositions**, all
`ACCEPTED`, expanding to 2,405,376 forecast and 6,912 actual observations.

Quarantined rows keep their source file, 1-based row number, raw payload,
machine reason code and a human reason, so a failure is actionable without
opening the database.

A whole-row policy is used deliberately: if any zone value on a line is
unparseable or implausible, the entire line is quarantined rather than
half-admitted. A row with a bad Coast value is not trustworthy for North.

---

## 4. Timezone and DST

ERCOT publishes local operating time: an operating date, `HourEnding` 1..24,
and a `DSTFlag`. Both representations are stored — the ERCOT local triple and
a canonical `target_ts_utc` — and an hour is keyed by the UTC instant at which
its local interval *begins*.

**What `DSTFlag` actually means.** Every row in the 32 days of September 2026
data carries `DSTFlag = 'N'`, and Texas is on CDT throughout. So the flag is
demonstrably **not** "this hour is daylight time". The reading consistent with
all evidence held is that it discriminates the hour that repeats when DST
ends: `'Y'` marks the CDT (first, UTC−5) occurrence, `'N'` everything else.

This is labelled an **assumption**: the vintage window contains no DST
transition, so it cannot be confirmed from data. It is isolated in one
function so a correction is one edit, and a November vintage would settle it.

Handling:

- **Spring forward** — the local day has 23 hours; `HourEnding 3:00` does not
  exist and ERCOT omits it. A row claiming it is quarantined as
  `NONEXISTENT_LOCAL_HOUR` rather than coerced.
- **Fall back** — the local day has 25 hours; `HourEnding 2:00` appears twice
  and `DSTFlag` separates the two into instants an hour apart. Both are
  evaluated as distinct rows, not collapsed.
- **Coverage is DST-aware.** The readiness gate computes expected zone-hours
  from `hours_in_operating_date()` (23/24/25), so a DST day is not reported as
  missing data — and a DST day with 24 rows *is*. The `dst_spring_forward`
  scenario runs end to end and yields 23 × 8 = 184 rows.
- **The seasonal-naive lag is seven *calendar days*, not 168 hours.** Across a
  transition, the same local hour a week earlier is 167 or 169 hours away. The
  join is on `(operating_date − 7, hour_ending, dst_flag)`, so it is correct by
  construction rather than by arithmetic.

---

## 5. Conflicting duplicates

*What if the same publication timestamp appears twice with different values?*

Both raw records are preserved. Neither is admitted. Both source rows are
marked `QUARANTINED_KEY_CONFLICT`, and the readiness gate fails the release.

An exact repeat is different and harmless: it is recorded as
`DUPLICATE_IDENTICAL` and does not block anything.

"Last row wins" would be the wrong call here. It is a coin flip dressed as a
rule, it produces a dataset that looks complete, and nobody downstream ever
learns a choice was made. Blocking is recoverable; a silently wrong number
propagates.

The same reasoning applies one level up: if two vintages tie at the winning
publication timestamp with different values, `asof_join.sql` emits
`value_count > 1` instead of breaking the tie, and the gate refuses.

---

## 6. Rerun safety

```
run_id = SHA256(pipeline_version | processing_date | sorted input content hashes)
```

The input set is itself a function of the processing date — only files ERCOT
published on or before local midnight ending that date are admitted — so
re-running `--processing-date 2026-09-22` next week consumes exactly the bytes
it consumed today, even though the archive has moved on.

Loading is one transaction that deletes the processing date's rows before
reinserting them, so a rerun replaces a partition rather than appending to it,
and a crash mid-load leaves the previous run intact rather than a mixture.

Raw downloads are immutable and content-hashed. If ERCOT reissues a file under
the same name with different bytes, the new content lands beside the old one;
historical evidence is never rewritten in place.

Pinned by tests: identical `run_id` across builds, byte-identical tables across
independent builds, no row duplication on reload, a *later* publication not
disturbing an earlier processing date, and `run_id` changing when source bytes
change.

Full rebuild from local files: **8.4 s** for 206 files / 2.4 M observations.

---

## 7. Gates and the release decision

Two executable stages, each returning structured JSON with reason codes,
counts and sample keys; the CLI exits 0 / 1 / 2.

**Gate 1 — data readiness** (may this dataset judge anything?): row accounting
intact; nothing quarantined; no schema drift; exactly one in-use model per
target zone-hour; DST-aware zone-hour coverage complete; no missing as-of
forecast, actual or naive input; no conflicting values at a winning
publication; no cutoff violations; no unexpectedly stale vintage.

Two of those deserve comment.

- **Staleness.** An as-of forecast can be perfectly legal and still wrong to
  use. NP3-565 publishes hourly, so the newest forecast publishable by a
  `T−24h` cutoff should be well under an hour old. A six-hour-old vintage
  passes the cutoff rule and signals that *our acquisition* missed files.
  Threshold: 2 h. Observed: 0.5 h.
- **The gate does not trust the query it gates.** The cutoff predicate lives in
  `asof_join.sql`; if someone relaxes it, every downstream number stays
  plausible. The gate independently re-checks
  `ercot_publication_ts_utc ≤ cutoff_ts_utc` on the output.
  (`test_readiness_gate_does_not_trust_the_query_it_gates`)

Gate 1 failing short-circuits Gate 2. Scoring a model on data that failed
readiness is precisely the outcome the pipeline exists to prevent.

**Gate 2 — seasonal naive** (is the model good enough to release?):

| Check | Threshold | Observed |
|---|---|---|
| WAPE | ≤ 8.00% | **6.35%** |
| Worst single operating day | ≤ 9.00% | **7.40%** |
| Worst zone peak-hour APE | ≤ 20.00% | **16.20%** |
| Rows scored | 100% of the dataset | 960 / 960 |

Metric choice: **WAPE** = Σ|error| / Σactual. MAE is not comparable across
Far West and North Central; MAPE divides per row, so one low-load overnight
hour dominates. WAPE reads as the fraction of delivered demand missed.

The thresholds are **operational assumptions for this exercise, not tuned
numbers.** They were chosen before searching for a passing configuration; the
observed 6.35% leaves deliberate but not generous headroom. In production they
would come from the cost of being wrong, not from the distribution.

Evaluation is sequential by operating day (rolling origin) — 4.75 / 7.40 /
7.07 / 5.80 / 6.81 % — never a random split.

Demonstrated outcomes (`forecast-spine demo`, all five automated):

| Scenario | Verdict |
|---|---|
| `pass` | APPROVED |
| `dst_spring_forward` | APPROVED (23-hour day, 184 rows) |
| `missing_forecast` | BLOCKED — `MISSING_ASOF_FORECAST` ×8 |
| `conflicting_duplicate` | BLOCKED — `QUARANTINED_SAME_KEY_DIFFERENT_VALUES` ×2 |
| `schema_drift` | BLOCKED — `SCHEMA_DRIFT`, `QUARANTINED_SCHEMA_DRIFT` ×48 |

In `missing_forecast` the value *does* exist in a later vintage. The gate
blocks rather than reaching for it, which is the whole point.

---

## 8. How a bad forecast could still pass these gates

Honestly: several ways.

1. **Peak hours.** 23 good hours and one 2 GW miss at system peak can leave
   overall WAPE comfortably inside 8%. That is why the peak-hour guardrail
   exists — and `test_a_good_average_does_not_excuse_a_bad_peak_hour` corrupts
   exactly one zone-day peak, shows overall WAPE still passing, and confirms
   the guardrail is what refuses. It is still only a guardrail: a model that is
   mildly wrong at every peak, rather than badly wrong at one, would pass both.
2. **A benign window.** Five mild September days are not a winter storm. A
   seasonal-naive model holds up when load is regular and collapses on a front,
   a holiday, or an extreme-heat ramp. WAPE 6.35% says nothing about
   2021-02-15. The window is too short to claim otherwise.
3. **Correlated zone error.** WAPE is computed on pooled absolute error. Eight
   zones each 6% low sum to a system forecast 6% low — one large reserve error,
   but it looks identical to uncorrelated noise in this metric.
4. **A wrong-but-consistent upstream mapping.** If both reports were transposed
   the same way, every check here would pass. The name-mapping and header
   fingerprint defend against drift, not against being wrong from the start.
5. **`DSTFlag` being the opposite of my reading.** Nothing in the held data
   would catch it; one hour twice a year would be an hour off.

The compensating controls I would add next, in order: absolute MW error at
system peak as a hard limit rather than a percentage; a bias check on summed
zone error; and a seasonal backtest over at least a year before trusting any
threshold.

---

## 9. What I deliberately did not build

No orchestrator, no dashboard, no containers, no feature store, no experiment
tracker, no second candidate model, no ML beyond a weekly seasonal naive. The
exercise asks for something local, narrow and reproducible that another person
can run; an Airflow DAG and a Kubernetes manifest would be scope judgment
failing, not engineering maturity.

Effort went roughly 70% into temporal correctness, ingestion and
accountability, tests and gates, and ~10% into the model — which matches what
is being graded, and what actually breaks in production.

What I would do next, given more time or credentials:

1. Backfill March 2026 from the authenticated archive and run the real
   spring-forward window end to end.
2. Confirm `DSTFlag` semantics against a November vintage.
3. Peak-MW and bias guardrails as above; thresholds from a year of backtest.
4. Quarantine triage: today any quarantined row blocks release, which is right
   for this dataset and too blunt for a large one. A per-reason-code tolerance
   budget, scoped to the evaluation window, is the production shape.
