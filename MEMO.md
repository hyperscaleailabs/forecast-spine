# Memo — forecast-spine

Every figure is generated, not recalled — `scripts/evidence.py` and
`scripts/retrieval_report.py`. Measurement detail behind each claim is in
[`MEMO-long.md`](MEMO-long.md).

## The two feeds

| | NP3-565-CD (forecast) | NP6-345-CD (actual) |
| --- | --- | --- |
| reportTypeId | 14837 | 13101 |
| Cadence, measured | every 60 min, at HH:30 Central | daily, 05:50 Central |
| Lands | hourly | next morning, for the prior operating day |
| Revised after publication | **yes — 99.7%** of target zone-hours | **never observed** |
| **Must not be used for** | scoring itself. A vintage published after `T−24h` is hindsight, including the same file's restatement of hours earlier that day. | *input* to anything. It is evaluation truth only; `actual(T−7d)` is the one exception and must clear the 24h bar like any input. |

Two traps. MIS filename timestamps are **Central, not UTC** — misreading shifts
every cutoff five hours, in the direction that admits late forecasts. And the two
reports order their zone columns differently (`SOUTHERN` and `SOUTH_CENTRAL`
transposed), so a positional parser yields a complete, plausible, entirely wrong
dataset. Zones are mapped by name; headers are fingerprinted.

## As-of join decisions

**Sign convention: `error = forecast − actual`. Positive is an over-forecast.**

- **No publication exactly at `T−24h`?** Take the latest at or before it. A
  day-ahead process runs with the newest file when the deadline passes. The gate
  separately flags when that newest is suspiciously old (`>2h` against an hourly
  cadence) — which is how ERCOT's 2026-03-06 archive gap surfaced as an
  evaluation problem rather than a file count.
- **Spring-forward and fall-back?** Never by hour arithmetic.
  `resolve_hour(operating_date, hour_ending, dst_flag)` maps to an unambiguous
  UTC instant. `DSTFlag` discriminates the *repeated* hour at fall-back; it is
  not "is daylight time" (every September row is `N` while Texas is on CDT).
  Expected zone-hours come from the calendar (23/24/25), and the seasonal lag is
  **seven calendar days, not 168 hours** — across a transition the same local
  hour is 167 or 169 hours away.
- **Same publication timestamp, two different values?** Neither is kept. Both
  raw rows are preserved as `QUARANTINED_KEY_CONFLICT`, the as-of row carries
  `value_count > 1`, and readiness refuses the release. "Last row wins" is a coin
  flip dressed as a rule: it produces a dataset that looks complete and nobody
  downstream learns a choice was made.

The cutoff is **per target hour**, not one run timestamp — hour 2 and hour 23 of
the same day have deadlines 21 hours apart. Three selections run against three
clocks: forecast and naive input at `T−24h`, the reported actual at
`processing_ts`.

## What would make me roll this back at 3am

Observable symptoms, not metrics:

- **Error suddenly much better than its trailing weeks.** The alarm, not good
  news — it is the signature of leakage, and every other system trains operators
  to read improvement as success.
- An evaluation published with a `run_id` absent from the release log: the gate
  was bypassed.
- Two runs for one processing date with **different** `run_id`s: inputs moved
  underneath a frozen date.
- Row count no longer zones × target hours: a join changed grain.
- Quarantine rate dropping to zero after being non-zero: the detector broke.

## Rebuild cost, and what I would cache

Full rebuild from local files: **43s** (775 files, 1.13M source rows, 5,752
evaluated zone-hours). Retrieval dominates wall-clock (~30 min at 28 req/min);
**normalization dominates rebuild**. Cache normalized rows keyed by
`content_sha256` — raw files are immutable, so a rebuild re-reading the same
bytes can skip parsing entirely. Do not cache the as-of join: it is ~2s, and a
stale one is the exact failure this system exists to prevent.

## The dominant uncertainty

**That the MIS filename timestamp is the moment a vintage became available.**
Every cutoff is computed from it. If ERCOT's real posting time differs
materially, every selection is off by that difference, in a direction
undetectable from the files themselves. To shrink it: poll the listing
continuously for a week, record the first instant each filename is observable,
and compare against the stamp — turning an assumption into a distribution.

## How a bad forecast could still pass the model gate

Pooled WAPE is dominated by large zones and cheap overnight hours, so a forecast
excellent at 03:00 in a small zone and catastrophic at the `NORTH_CENTRAL` peak
clears 8% comfortably. That is why a second, independent per-zone-day peak-hour
limit exists; `test_a_good_average_does_not_excuse_a_bad_peak_hour` corrupts one
zone-day peak, shows pooled WAPE still passing, and confirms the guardrail is
what refuses.

**What it does not close:** a *uniformly, mildly* biased forecast. Bias cancels in
an absolute-error metric and nothing here gates on it. The compensating signal is
the signed `bias_mw` already computed per fold — a run of same-signed daily bias
is the observable symptom. Gating on it is the next thing I would build.

## Result on the assignment window, and the thresholds

Readiness **FAILS** on three codes: 8 rows at 2026-02-22 HE 1 (structural — the
cutoff precedes the permitted publication window by 30 min), 8 at 2026-03-15 HE 3
(structural — its week-ago input is the hour spring-forward deletes), and 16 at
2026-03-07 HE 13–14 (real — five publications missing from ERCOT's own archive,
confirmed by `--verify`).

The model gate also fails: WAPE 8.65% vs 8.0%, worst day 15.08% vs 9.0%, worst
peak-hour APE 32.15% vs 20.0%. **The thresholds were fixed against a September
window scoring 6.35%, before this data existed.** March spans the winter–spring
transition where weekly repetition is weak; ERCOT's own model scores 3.39% on the
same rows, so the window is forecastable and seasonal-naive is not the tool. I
left them alone and reported the failure — re-fitting after seeing the result is
fitting the gate to the answer. `forecast-spine demo` shows both gates approving,
so both outcomes are reachable.

## Time and AI use

**Approximate time spent: _[fill in]_.** AI (Claude) was used throughout: SQL
and normalization drafts, the notebooks, prose, and review of my own claims.
Three things it got wrong that I caught — all the same shape, confident and
plausible until checked against the code:

- It stated the error sign convention was documented in `asof_join.sql`'s header.
  It was not; only the arithmetic existed. Now stated.
- It reported "11 readiness reason codes" in an architecture diagram. There are
  **16** — twelve static plus one `QUARANTINED_<disposition>` per quarantine
  reason.
- It wrote a README command using `date +%F -d yesterday`, GNU syntax that fails
  on the macOS this was built on.

The habit that caught all three was the same one the pipeline enforces: derive
the number from the source rather than from memory.

## What I could not establish, and what I cut

- **Whether `DSTFlag` means what I read it to mean.** No fall-back transition
  occurs in the data held. The reading is consistent with all evidence available
  but is not proven; it is isolated in one function.
- **Whether actuals are ever revised.** Not once in the days held — one window's
  observation, not a guarantee.
- **Quarantine on real data.** All 1.13M real rows are `ACCEPTED`; those branches
  are exercised only by fixtures and tests.
- **Cut deliberately:** no orchestrator, containers, feature store, dashboard or
  second candidate model. Thresholds from a year of backtest rather than one
  window is the first thing I would add with more time.
