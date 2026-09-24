# Memo

Every number here is produced by a script, not recalled:
`scripts/evidence.py` for source behaviour and run metrics,
`scripts/retrieval_report.py` for `RETRIEVAL.md`. If ERCOT's behaviour changes,
those scripts change the memo.

## Where the evidence is

| What is being judged | Where to look |
| --- | --- |
| Point-in-time correctness | [`sql/asof_join.sql`](sql/asof_join.sql), [`tests/test_asof_join.py`](tests/test_asof_join.py), §2 below |
| Production judgment on imperfect data | §1, §5, §8; [`ercot_api.py`](src/forecast_spine/ercot_api.py) rate limiting and collision handling |
| Executable gates | [`gates.py`](src/forecast_spine/gates.py), `forecast-spine demo`, §7 |
| Row accountability | [`normalize.py`](src/forecast_spine/normalize.py), [`tests/test_accounting.py`](tests/test_accounting.py), §3 |
| Rerun safety and operability | [`pipeline.py`](src/forecast_spine/pipeline.py), [`tests/test_rerun.py`](tests/test_rerun.py), §6 |
| Source judgment | §1, §4, §9; [`RETRIEVAL.md`](RETRIEVAL.md) |

One sentence, if you only read one: *every output traces to what ERCOT had
published by the decision cutoff, every source row has a disposition, reruns
are deterministic, ambiguous data blocks release, and acceptance is an
executable decision rather than a judgement call in a notebook.*

---

## 0. Retrieval: what was and was not obtained

`RETRIEVAL.md` is generated and carries the counts. The shape of the answer:

- **Forecast publications** — NP3-565-CD, every publication posted
  2026-02-21 00:00 through 2026-03-23 23:59 Central.
- **Actuals** — NP6-345-CD, covering target operating days 2026-02-22 through
  2026-03-23, *plus the seven days before the first target day*, because the
  seasonal-naive forecast for 22 February is built from the actual for
  15 February and that input has to exist.
- **Target hours** — 2026-02-22 through 2026-03-23 inclusive, Central.

Two distinctions the report makes that a raw file count cannot.

**A missing publication is not a missing forecast.** The as-of rule takes the
newest vintage published at or before `T − 24h`. A gap only becomes a missing
*forecast* when it removes the last eligible vintage for a target hour, or
leaves one old enough to trip the staleness check. The gate reports that per
zone-hour; the coverage report does not guess at it.

**A gap of ours is not a gap of ERCOT's.** Disk coverage alone conflates "we
failed to download it" with "it was never posted". `retrieval_report.py
--verify` asks the archive listing what ERCOT holds for each day and labels
each gap accordingly. ERCOT's archive does have real holes — 2026-03-06 lists
19 of the 24 publications an hourly cadence implies, while 2026-03-21 to 03-23
are complete.

**One boundary is structural, not a failure.** The first target hour,
2026-02-22 hour ending 01:00, has a cutoff of 2026-02-21 00:00. Publications
post at HH:30, so the earliest one the assignment's window permits is
2026-02-21 00:30 — after the cutoff. That hour cannot be served from the
permitted publication window by anyone. Reaching back to a 20 February vintage
would fix it and would also break the stated window, so the gate reports it
instead.

---

## 1. Source judgment

Measured from the vintages held, not taken from the documentation.

| | NP3-565-CD (forecast) | NP6-345-CD (actual) |
| --- | --- | --- |
| reportTypeId | 14837 | 13101 |
| Cadence observed | every **60 min**, at HH:30 local | every **1440 min**, at 05:50 local |
| Publication time | hourly | next morning, for the previous operating day |
| Revised after publication? | **yes — 99.7%** of target zone-hours differ across vintages | **no** — not once in the days held |

The 99.7% revision rate is why this is a vintage problem at all. If forecasts
never changed, one snapshot would do and "as of when?" would have no
consequences.

**Publication timestamps are Central, not UTC.** Misreading this shifts every
cutoff by five hours and silently admits forecasts published *after* their
deadline. The discriminator is the vintages stamped `00:30`: read as Central, a
seven-day forecast published at 00:30 must begin on that same local
DeliveryDate; read as UTC it would be 19:30 the previous Central day and would
have to begin on the previous DeliveryDate. All 174 of the vintages checked
begin on the local date. Verified, not assumed.

**Model selection is unambiguous.** Each publication carries 8 models
(A3, A6, E, E1, E2, E3, M, X) per target zone-hour, and exactly **one** carries
`InUseFlag = 'Y'`. The pipeline selects on that flag, and the readiness gate
fails if the count is ever not 1 rather than picking a model by name.

**The trap worth naming.** The two reports order their weather-zone columns
differently:

```
NP3-565: ... North, NorthCentral, SouthCentral, Southern, West ...
NP6-345: ... NORTH,   NORTH_C,    SOUTHERN,     SOUTH_C,  WEST ...
```

Positions 7 and 8 are swapped; the other six line up. A positional parser gets
six of eight zones right and produces a fully populated, entirely plausible
dataset with two multi-GW zones transposed. Both reports go through explicit
name→zone tables, the header is fingerprinted per file, and a test pins it.

---

## 2. Point-in-time correctness

The invariant, enforced in `sql/asof_join.sql` and re-checked independently by
the readiness gate:

> Nothing used to produce a forecast for target hour `T` may have become
> available after `T − 24h`.

Three selections run against three different clocks:

| Selection | Cutoff | Why |
| --- | --- | --- |
| ERCOT forecast | `publication_ts ≤ T − 24h` | what was knowable at decision time |
| Seasonal-naive input | `publication_ts ≤ T − 24h` | the model's input is also a point-in-time value |
| Reported actual | `publication_ts ≤ processing_ts` | truth, known only after the hour |

Two details that are easy to get wrong:

- **The cutoff is per target hour, not one run timestamp.** Hour 2 and hour 23
  of the same operating day have deadlines 21 hours apart. Collapsing them to a
  single "as of yesterday" is the most common silent failure.
- **The naive model's input needs the same discipline as the forecast.** The
  actual for `T − 7d` exists in the warehouse today, but may only be used if it
  had been *published* by `T − 24h`.

Boundary behaviour is pinned by tests: publication exactly at `T − 24h` is
eligible; one second later is not; among eligible vintages the latest wins; and
a later, more accurate vintage is never substituted, not even for an hour that
would otherwise be missing.

**What hindsight is worth.** Relaxing that one predicate — take the latest
vintage rather than the latest eligible one — and rescoring the same data
scores ERCOT's own model at **1.13% WAPE instead of 2.92%** over the September
sample. Nothing errors, no row goes missing, every chart still renders. The
failure mode is not a crash; it is a plausible number. The notebook shows this
as a figure.

---

## 3. Row accountability

The unit is one source CSV data line. Every line gets exactly one disposition:

```
ACCEPTED | DUPLICATE_IDENTICAL | QUARANTINED_PARSE_ERROR
QUARANTINED_INVALID_VALUE | QUARANTINED_KEY_CONFLICT | QUARANTINED_SCHEMA_ERROR
```

and the pipeline asserts, at the point of production and again in the gate:

```
raw_rows == accepted + duplicate_identical + quarantined
```

Quarantined rows keep their source file, 1-based row number, raw payload,
machine reason code and a human reason, so a failure is actionable without
opening the database.

A whole-row policy is deliberate: if any zone value on a line is unparseable or
implausible, the entire line is quarantined rather than half-admitted. A row
with a bad Coast value is not trustworthy for North.

---

## 4. Timezone and DST

ERCOT publishes local operating time: an operating date, `HourEnding` 1..24 and
a `DSTFlag`. Both representations are stored — the ERCOT local triple and a
canonical `target_ts_utc` — and an hour is keyed by the UTC instant at which its
local interval *begins*.

**What `DSTFlag` actually means.** Every row in the September sample carries
`DSTFlag = 'N'` while Texas is on CDT throughout, so the flag is demonstrably
**not** "this hour is daylight time". The reading consistent with the evidence
is that it discriminates the hour that repeats when DST ends: `'Y'` marks the
CDT (first, UTC−5) occurrence, `'N'` everything else. Isolated in one function
so a correction is one edit.

Handling:

- **Spring forward** — 23 hours; `HourEnding 3:00` does not exist and ERCOT
  omits it. A row claiming it is quarantined as `NONEXISTENT_LOCAL_HOUR` rather
  than coerced.
- **Fall back** — 25 hours; `HourEnding 2:00` appears twice and `DSTFlag`
  separates the two into instants an hour apart. Both are evaluated as distinct
  rows, not collapsed.
- **Coverage is DST-aware** throughout. The readiness gate derives expected
  zone-hours from the calendar (23/24/25), so a DST day is not reported as
  missing data — and a DST day with 24 rows *is*. The same applies to retrieval:
  `coverage.py` expects 23, 24 or 25 publications per local day.
- **The seasonal-naive lag is seven *calendar days*, not 168 hours.** Across a
  transition the same local hour a week earlier is 167 or 169 hours away, so the
  join is on `(operating_date − 7, hour_ending, dst_flag)` — correct by
  construction rather than by arithmetic.

**A limitation, handled rather than hidden.** The MIS filename grammar carries a
local timestamp and no DST flag, so a publication inside the repeated hour at
fall-back is genuinely ambiguous: two instants an hour apart reconstruct to the
same filename. The information is not recoverable. `backfill` therefore never
overwrites on a name collision — it keeps both payloads under distinct sequence
numbers, so the pipeline sees two vintages at one publication timestamp, the
as-of query reports `value_count > 1`, and the readiness gate blocks. Ambiguous
input stopping the line is correct; a silently dropped vintage is not.

---

## 5. Conflicting duplicates

*What if the same publication timestamp appears twice with different values?*

Both raw records are preserved. Neither is admitted. Both source rows are marked
`QUARANTINED_KEY_CONFLICT` and the readiness gate fails the release. An exact
repeat is different and harmless: `DUPLICATE_IDENTICAL`, which blocks nothing.

"Last row wins" would be the wrong call. It is a coin flip dressed as a rule, it
produces a dataset that looks complete, and nobody downstream learns a choice
was made. Blocking is recoverable; a silently wrong number propagates.

The same reasoning applies one level up: if two vintages tie at the winning
publication timestamp with different values, `asof_join.sql` emits
`value_count > 1` instead of breaking the tie, and the gate refuses.

---

## 6. Rerun safety and operability

```
run_id = SHA256(pipeline_version | processing_date | sorted input content hashes)
```

The input set is itself a function of the processing date — only files published
on or before local midnight ending that date are admitted — so re-running
`--processing-date 2026-03-10` next week consumes exactly the bytes it consumed
today, even though the archive has moved on.

Loading is one transaction that deletes the processing date's rows before
reinserting them, so a rerun replaces a partition rather than appending, and a
crash mid-load leaves the previous run intact rather than a mixture.

Raw downloads are immutable and content-hashed. If ERCOT reissues a file under
the same name with different bytes, the new content lands beside the old one;
historical evidence is never rewritten in place.

Pinned by tests: identical `run_id` across builds, byte-identical tables across
independent builds, no row duplication on reload, a *later* publication not
disturbing an earlier processing date, and `run_id` changing when source bytes
change.

**Operability.** One command installs and opens the notebook
(`scripts/lab.sh`); `forecast-spine run` exits 0/1/2 so a scheduler can act on
the verdict; `forecast-spine coverage` exits non-zero on any retrieval gap;
gate verdicts are written to `data/reports/` as JSON with machine-readable
reason codes.

---

## 7. The gates

Two executable stages, each returning structured JSON with reason codes, counts
and sample keys.

**Gate 1 — data readiness** (may this dataset judge anything?): row accounting
intact; nothing quarantined; no schema drift; exactly one in-use model per
target zone-hour; DST-aware zone-hour coverage complete; no missing as-of
forecast, actual or naive input; no conflicting values at a winning publication;
no cutoff violations; no unexpectedly stale vintage.

Two deserve comment.

- **Staleness.** An as-of forecast can be perfectly legal and still wrong to
  use. NP3-565 publishes hourly, so the newest forecast publishable by a `T−24h`
  cutoff should be well under an hour old. A six-hour-old vintage passes the
  cutoff rule and signals that *acquisition* missed files. This is where the
  archive's real gaps surface as an evaluation problem rather than a file count.
- **The gate does not trust the query it gates.** The cutoff predicate lives in
  `asof_join.sql`; if someone relaxes it, every downstream number stays
  plausible. The gate independently re-checks
  `ercot_publication_ts_utc ≤ cutoff_ts_utc` on the output.

Gate 1 failing short-circuits Gate 2. Scoring a model on data that failed
readiness is precisely the outcome the pipeline exists to prevent.

**Gate 2 — seasonal naive.** WAPE ≤ 8%, no single operating day above 9%, no
zone's peak-hour APE above 20%, and 100% of the dataset scored. Metric choice:
WAPE = Σ|error| / Σactual. MAE is not comparable across Far West and North
Central; MAPE divides per row, so one low-load overnight hour dominates. WAPE
reads as the fraction of delivered demand missed.

The thresholds are **operational assumptions for this exercise, not tuned
numbers**. They were chosen before searching for a passing configuration. In
production they would come from the cost of being wrong, not the distribution.

Evaluation is sequential by operating day (rolling origin), never a random
split.

Demonstrated outcomes — `forecast-spine demo` runs all five and asserts each:

| Scenario | Verdict |
| --- | --- |
| `pass` | APPROVED |
| `dst_spring_forward` | APPROVED (23-hour day, 184 rows) |
| `missing_forecast` | BLOCKED — `MISSING_ASOF_FORECAST`; the value exists in a later vintage and is not used |
| `conflicting_duplicate` | BLOCKED — `QUARANTINED_SAME_KEY_DIFFERENT_VALUES`; no winner picked |
| `schema_drift` | BLOCKED — a renamed zone column is refused, not parsed positionally |

---

## 8. Live retrieval

`forecast-spine backfill` reads the authenticated Public API; `acquire` reads
the credential-free MIS listing. Both write the same `data/raw/` layout, so
normalization, the as-of SQL and the gates consume either without knowing which
ran. Tests, `demo` and the notebook's fallback need no credentials at all.

**Two credentials, not one.** An API Explorer subscription key alone returns
`401 Unauthorized. Access token is missing or invalid.` on every endpoint. The
API also wants a bearer token minted by ERCOT's Azure B2C flow from the
*account* username and password. Confirmed by probing rather than assumed: a
deliberately invalid login reaches credential validation and returns
`AADB2C90225`, rather than a 404 or a malformed-request error. Both come from
the environment or a gitignored `.env`, are never logged, and `Credentials`
redacts its own `repr` so they cannot leak into a traceback or a notebook cell.

**Rate limiting is counted, not slept.** ERCOT documents 30 requests/minute. A
fixed sleep between requests controls the gap, not the count, so a retry or a
burst of small pages can still cross the ceiling. A sliding window over the last
60 seconds counts what was actually sent. Default 24/min, with `Retry-After`
honoured on 429 and one silent re-auth on mid-run token expiry.

**Two bugs found by measuring rather than assuming.** Both are the kind this
exercise is about — code that looked right and produced plausible behaviour.

1. *The limiter throttled ~4× tighter than configured.* Re-reading the clock
   after sleeping is not the same as adding the pause to a freshly read clock;
   the latter double-counts, timestamps drift into the future, the window stops
   draining, and each cycle compounds. At a configured 24/min, 144 requests took
   898s before the fix and 300s after. Caught because the observed download rate
   (~6/min) did not match the configuration. The first regression test *passed*
   against the buggy code — the drift only bites from the second throttle cycle
   and the test ran one window. It now spans six and asserts a floor as well as
   a ceiling: a limiter that is merely "safe" is also broken.
2. *Coverage collapsed expected publication hours into a set*, reporting a
   25-hour fall-back day as 24 — it would have hidden a genuinely missing
   vintage on exactly the day this exercise cares about. It now counts
   multiplicity per hour, and surplus files for one hour are never credited
   against an absence elsewhere in the day.

---

## 9. How a bad forecast could still pass these gates

Honestly: several ways.

1. **Peak hours.** 23 good hours and one 2 GW miss at system peak can leave
   overall WAPE comfortably inside 8%. That is why the peak-hour guardrail
   exists, and a test corrupts exactly one zone-day peak, shows overall WAPE
   still passing, and confirms the guardrail is what refuses. It is still only a
   guardrail: a model mildly wrong at every peak, rather than badly wrong at
   one, would pass both.
2. **A benign window.** A month of ordinary late-winter load is not a winter
   storm. A seasonal-naive model holds up when load is regular and collapses on
   a front, a holiday, or an extreme ramp. A good WAPE here says nothing about
   February 2021.
3. **Correlated zone error.** WAPE pools absolute error. Eight zones each 6% low
   sum to a system forecast 6% low — one large reserve error that looks
   identical to uncorrelated noise in this metric.
4. **A wrong-but-consistent upstream mapping.** If both reports were transposed
   the same way, every check here would pass. Name mapping and header
   fingerprinting defend against drift, not against being wrong from the start.
5. **`DSTFlag` being the opposite of my reading.** Nothing in the data held
   would catch it; one hour twice a year would be an hour off.

Compensating controls I would add next, in order: absolute MW error at system
peak as a hard limit rather than a percentage; a bias check on summed zone
error; and a seasonal backtest over at least a year before trusting any
threshold.

---

## 10. What I deliberately did not build

No orchestrator, no dashboard, no containers, no feature store, no experiment
tracker, no second candidate model, no ML beyond a weekly seasonal naive. The
brief asks for something local, narrow and reproducible that another person can
run; an Airflow DAG and a Kubernetes manifest would be scope judgment failing,
not engineering maturity.

Effort went roughly 70% into temporal correctness, ingestion and accountability,
tests and gates, and about 10% into the model — which matches what breaks in
production.

Next, given more time:

1. Confirm `DSTFlag` semantics against a November vintage.
2. Peak-MW and bias guardrails; thresholds from a year of backtest rather than
   from this window.
3. Quarantine triage. Today any quarantined row blocks release, which is right
   for this dataset and too blunt for a large one. A per-reason-code tolerance
   budget, scoped to the evaluation window, is the production shape.
4. Reconcile the archive's own gaps against a second source, so "never
   published" can be asserted rather than inferred from one listing.
