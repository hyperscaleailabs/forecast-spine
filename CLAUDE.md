# forecast-spine — working notes

Point-in-time correct evaluation of ERCOT hourly load forecasts. Built as an
interview take-home. Read [`README.md`](README.md) to run it and
[`MEMO.md`](MEMO.md) for the reasoning; this file is the operating context a
fresh session needs.

## The one rule

> Nothing used to produce a forecast for target hour `T` may have become
> available after `T − 24h`.

It lives in [`sql/asof_join.sql`](sql/asof_join.sql) and is re-checked
independently by the readiness gate, which deliberately does not trust that
query. If you change the cutoff predicate, expect
`test_readiness_gate_does_not_trust_the_query_it_gates` to catch you.

What is being graded: point-in-time correctness, production judgment on
imperfect data, executable gates, row accountability, rerun safety, and source
judgment. **Not** forecast sophistication. Resist adding models.

## Environment

```bash
./scripts/lab.sh --install-only     # venv + deps, idempotent
uv run pytest -q                    # no credentials, no network
uv run forecast-spine demo          # five scenarios, asserts each verdict
```

`.venv/` is a uv venv on Python 3.12. `uv run ...` and `.venv/bin/...` are
equivalent.

## Data on disk (not in git)

- `data/raw/` — immutable ERCOT vintages, content-hashed. **Perishable and
  irreplaceable**: ERCOT's public MIS listing retains only ~7 days of NP3-565,
  so the September 2026 window cannot be re-downloaded. Back it up before
  wiping anything. The March 2026 window *can* be re-fetched from the
  authenticated archive.
- `.env` — credentials, mode 600, gitignored. See `.env.example`.
- `data/warehouse/`, `data/reports/` — rebuildable.

## Gotchas that have already bitten

1. **Two credentials, not one.** A subscription key alone returns 401 on every
   Public API endpoint; the bearer token needs the ercot.com account login.
2. **MIS filename timestamps are Central, not UTC.** Misreading shifts every
   cutoff by five hours.
3. **The two reports order zone columns differently** — `SOUTHERN` and
   `SOUTH_CENTRAL` are swapped. Map by name, never by position.
4. **`DSTFlag` is not "is daylight time."** It discriminates the repeated hour
   at fall-back. September rows are all `N` while Texas is on CDT.
5. **A local operating day is 23, 24 or 25 hours.** Never hard-code 24 —
   coverage, the readiness gate and the seasonal-naive lag all derive it.
6. **The seasonal-naive lag is seven calendar days, not 168 hours.** Join on
   `(operating_date − 7, hour_ending, dst_flag)`.
7. **Rate limiting is counted, not slept.** A fixed sleep controls the gap, not
   the count. If you touch `RateLimiter`, run its tests — one earlier version
   throttled 4x tighter than configured and the first regression test missed it.

## Conventions

- Numbers in `README.md` / `MEMO.md` / `RETRIEVAL.md` come from
  `scripts/evidence.py` and `scripts/retrieval_report.py`. Regenerate rather
  than hand-edit figures.
- The notebook is committed **with outputs** so it reads on GitHub. Regenerate
  with
  `uv run jupyter nbconvert --to notebook --execute --inplace notebooks/exploration.ipynb`.
- Chart conventions are in `src/forecast_spine/viz.py`: one y-axis, colour
  follows the entity, dashing only for labelled thresholds.
- `ruff check src tests scripts` must pass.

## State as of 2026-09-23

- Committed and pushed through the README/memo restructure. 74 tests green.
- A **detached backfill** may still be running; it writes to `/tmp/fs/bf_fc.log`
  and `/tmp/fs/bf_act.log`, ending with `ALLDONE`. Check with
  `pgrep -f "forecast-spine backfill"`. It is idempotent — safe to re-run.
- Target windows from the assignment: forecast publications
  **2026-02-21 → 2026-03-23**, actuals **2026-02-16 → 2026-03-24** (targets plus
  the seven-day naive lookback), evaluating target hours
  **2026-02-22 → 2026-03-23**.

## Next steps

1. When the backfill finishes:
   `uv run forecast-spine coverage --from 2026-02-21 --to 2026-03-23` and
   `uv run python scripts/retrieval_report.py --verify` to produce
   `RETRIEVAL.md`, which separates gaps we caused from gaps in ERCOT's archive.
2. Run the assignment window:
   `uv run forecast-spine run --processing-date 2026-03-24 --window-start 2026-02-22 --window-end 2026-03-23`.
   Expect readiness to **fail** on 8 rows: target 2026-02-22 hour ending 01:00
   has a cutoff of 2026-02-21 00:00, publications post at HH:30, so the earliest
   the permitted window allows is 30 minutes too late. That is structural, not a
   defect — report it rather than widening the window.
3. `MEMO.md` is the concise two-page memo (all Part 4 bullets);
   `MEMO-long.md` keeps the measurement detail. Regenerate figures with
   `scripts/evidence.py <date> --window-start .. --window-end ..`.
4. Re-execute the notebook against the March window.
