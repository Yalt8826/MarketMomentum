# Participant-level data: acquisition, and what it says about the dealer sign

**As of 2026-09-03, commits `67c83b7` and `c28fc7b`, branch `feat/gex-dealer-positioning`.**

Continues `docs/pipeline/README.md` (the state of the pipeline as of `44bc89a`), whose
open item #1 was *"No institutional-flow label — this is the defining gap."* This report
covers closing the acquisition half of that gap, and the first result it produced.

Companion documents: `docs/models/gex-dealer-positioning-findings.md` (the five-year GEX
result), `docs/implementation/gex-dealer-positioning.md` (the plan this branch
implements), `docs/database/README.md` (schema).

## At a glance

| | |
|---|---|
| New table | `participant_oi` — **6,290 rows**, 1,258 trading days × 5 participants, 2.5 MB |
| Coverage | 2021-08-02 → 2026-09-03; **zero `ml_dataset` days lack a participant row** |
| Raw CSVs | `data/raw/participant_oi/`, 5.2 MB |
| New code | `data/participant_oi.py` (526 LOC), DDL (43), tests (254) |
| Tests | **24 new**; suite now 263 unit, all passing; `ruff` and `mypy` clean |
| Headline result | `DEALER_SIGN` is **inverted for stock options** (99.9% of 1,258 days) and **regime-dependent for index options** |

## What changed

Two commits since `44bc89a`:

- **`67c83b7`** — `data/participant_oi.py`, the `participant_oi` table and DDL, the
  `fetch-participant-oi` CLI command, three real NSE fixtures, 21 unit tests.
- **`c28fc7b`** — fixed an over-strict integrity gate that silently rejected 14
  legitimate trading days, and backfilled them plus 23 more at the head of the window.

Before either, the existing database was verified against the requirements of a
supervised model (below), which is what established that acquisition — not modelling —
was the binding constraint.

## Training readiness: why more data, not a better model

The question put to the database was whether it could train a neural model for
institutional entry and exit. It cannot, and the reason is not data quality.

**There is no label.** No column in `ml_dataset` matches any institutional or flow
keyword; all six targets are price or volatility outcomes (`next_day_return`,
`next_day_direction`, `next_day_abs_return`, `next_day_rv_5d`, `next_day_iv_chg`,
`next_day_vrp`). A supervised model trained on any of them is a volatility model. It
cannot learn that an institution entered, because nothing in the 212,441 rows ever says
one did.

This matters more than it sounds. The Phase-6 XGBoost run scores IC 0.2365 while
`ce_iv_30` alone scores 0.2114 — almost all of it is the IV feature. A higher-capacity
model has *more* room to find that shortcut, not less.

Separately, the matrix is not in a state any dense network can consume as-is:

| check | value | consequence |
|---|---|---|
| Complete-case rows | 168,100 / 212,441 (79.1%) | dense nets reject NaN; impute or lose 21% |
| Features with zero NaN | 18 / 61 | worst are `ce_ivp`/`ce_ivr` at 11.8% |
| Scale spread | **13.2 orders of magnitude** | `gex_chg` reaches 1.62e12, `dist_to_flip` 0.1 |
| Heavy tails | 29 / 61 features with kurtosis > 100 (max 4,129) | scaling recentres outliers, it does not remove them |
| Collinear pairs (\|r\| > 0.95) | 21 | e.g. `ce_vega ~ pe_vega` at 0.9999 |
| `±inf` cells | 0 | clean |
| Class balance | 0.5090 up | fine |

A per-date rank transform fixes the tails, and is the same normalization the GEX work
arrived at independently:

```
raw                       max kurtosis  4,129    features >100: 29/61
cross-sectional z-score   max kurtosis    136    features >100: 10/61
cross-sectional rank      max kurtosis    554    features >100:  1/61
```

The panel structure is sound: 1,254 dates, 135–217 symbols per date (median 157), no
thin dates, median 1,253 rows per symbol, only 8 of 217 symbols under a year of history.

## The data

NSE's clearing arm publishes one CSV per trading day at
`nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv`, giving open
interest split by **who holds it**: `Client / DII / FII / Pro / TOTAL` against
`{Future, Option} × {Index, Stock} × {Long, Short}`. It is the only public ground truth
on institutional positioning in the Indian F&O market.

`Pro` — broker proprietary desks — is the dealer/market-maker proxy. `Client` is retail
and non-institutional. This is what makes `DEALER_SIGN` testable at all.

### The structural constraint

**This data is market-wide: one row per participant per date, not per symbol.** Joined
onto `ml_dataset` by date alone it is constant within a date, and therefore contributes
**exactly zero** to any cross-sectional (within-date) rank IC — the metric the whole
Phase-6 evaluation is built on.

It cannot be a per-symbol feature. Its three legitimate uses are:

1. validating `DEALER_SIGN` (done — see below),
2. supplying a market-level regime target (~1,258 rows, which is an HMM-sized problem,
   not a deep-network-sized one),
3. validating per-symbol anonymous flow features by aggregating them to market level.

Documented in the module and DDL headers so the constraint travels with the code.

## The fetcher

`mktmtm fetch-participant-oi` — defaults to a five-year window, ~11 min at the default
0.5 s spacing, `--start-date` / `--end-date` / `--delay` / `--no-load` to vary it. Raw
CSVs cache to disk, then parse and upsert into `participant_oi`. Only NSE's raw counts
are stored; nets and day-over-day flows are derived on read via `add_net_positions`, so
the table stays a faithful copy of the source.

### Parsing: three eras, one parser

The parser is pinned against three unmodified NSE files committed as fixtures
(2021-08-02, 2024-01-15, 2026-08-28). This was necessary rather than thorough — the
header differs by era in ways a parser written from one sample would not survive:

| | title row | `Future Stock Short` |
|---|---|---|
| 2021 | `"..."` | quoted, trailing literal tab |
| 2024 | `""..."` | bare, trailing literal tab |
| 2026 | `""..."` | trailing spaces |

`Total Long Contracts` carries the same trailing whitespace. Names are stripped before
mapping, and every parsing test runs against all three fixtures so an era-specific quirk
cannot pass by being handled in one branch only.

The file's own title date is cross-checked against the requested date, so NSE serving a
stale file under today's URL raises rather than silently duplicating a row of history.

### Integrity gates

Two of NSE's own arithmetic identities are enforced per file, because they catch
truncation that a byte count cannot:

1. **In the `TOTAL` row, every long leg equals its short leg** — each open contract is
   held long by one participant and short by another.
2. **`Client + DII + FII + Pro` equals `TOTAL`** in every column.

Both need a tolerance, and both tolerances are measured rather than assumed — see the
failure mode below.

### Deliberate departures from `data/bhavcopy.py`

The existing bhavcopy fetcher works — it produced the 212,441-row dataset — but it was
audited before being used as a template, and five defects were found and not inherited.

| `bhavcopy.py` | `participant_oi.py` |
|---|---|
| 404, non-200, and every exception all return `False` (`:296`, `:298`, `:300`) — a blocked run is indistinguishable from a run of holidays, and the log says "holidays return 404" | typed `FetchOutcome` with an `is_failure` property; holidays and blocking are distinct outcomes |
| failed session warm-up is swallowed by `suppress(Exception)` (`:344`), so every later 403 reads as a holiday | `warm_session()` raises if NSE issues no cookie, before 1,254 wasted requests |
| `time.sleep(1)` sits inside the worker (`:274`) with `max_workers` defaulting to 4 — the real rate is 4/s, not the 1/s the comment claims | single-threaded with a global delay; the rate is what it says |
| cached files are trusted on existence alone (`:270`), so a truncated file is skipped forever | cached files are re-parsed; a bad one is re-fetched |
| no abort on sustained failure | aborts after 20 consecutive failures rather than hammering NSE |

Minor, unfixed in bhavcopy: `filter_and_save_csv`'s return value is discarded at `:291`
while `:293` returns `True` regardless, so a filter failure still counts as success; and
its `is_udiff` parameter (`:232`) is unused.

## Failure mode hit and recorded

**An integrity gate that was stricter than the data.** The balance check asserted that
the `TOTAL` row's long and short legs match *exactly*, reasoning that every open
contract is held long by one party and short by another. That is true of the underlying
positions but **not of NSE's published figures**, which round each leg independently.

The three fixtures happened to balance exactly, so the over-strict gate looked correct
and shipped. The first full run then rejected **14 legitimate trading days** —
2023-08-10, 08-22, 08-31, 09-05, the 2023-10-10…10-26 run, and 2025-03-25 — all of
which return HTTP 200 and parse cleanly.

The drift was measured across all 14 before the bound was set: uniformly **±1, never
larger**, only on `option_index_call_long`, `option_index_put_long` and
`total_long_contracts`. `_BALANCE_TOLERANCE = 2` stays ~6 orders of magnitude tighter
than any real truncation. The participant-sum gate needed the same treatment for the
same reason (`_SUM_TOLERANCE = 4`; observed drift also exactly 1).

Two things worth carrying forward:

- **The typed outcome is what made this findable.** Those 14 days were logged as
  `PARSE_ERROR`, not counted as holidays. Under bhavcopy's bare-`False` convention they
  would have been invisible, and the gap would have been discovered — if ever — as 14
  missing rows in a model.
- **Regression tests pin both sides of each tolerance**: a drift of 1 must pass on every
  affected column, and a large drift must still fail. Neither bound can be quietly
  widened, and the exact gate cannot come back.

Backfill: 37 days fetched (the 14, plus 23 at the head — `ml_dataset` opens 2021-08-02
while a default five-year window began 2021-09-03), **0 failures**, 71 holidays.

## Result: the dealer sign is wrong for stocks, and unstable for indices

`options/dealer_exposure.py` sets `DEALER_SIGN = {"CE": +1.0, "PE": -1.0}` — dealers
long calls, short puts. Until now this was, in the words of the previous report, *"an
assumption, not a measurement."* It is now measured, over all 1,258 days.

### Stock options — inverted, and overwhelmingly so

Share of days on each side:

| participant | net calls < 0 | net puts > 0 |
|---|---|---|
| **Pro** | **99.9%** | **98.3%** |
| Client | 0.0% | 1.9% |
| FII | 99.8% | 74.6% |
| DII | 99.4% | 41.6% |

The Pro book is net **short calls and long puts** — the opposite of `DEALER_SIGN` on
both legs — with only **1 exception day out of 1,258** on calls and 22 on puts. `Client`
is the exact mirror, which is the expected signature of dealers facing retail.

This is not a regime artefact: it holds in every one of the six years.

### Index options — regime-dependent, and it changed sign in 2024–25

The same measurement on index options gives a very different answer. Pro is net short
index calls on only 68.1% of days overall and net long index puts on 49.2% — a coin
flip — because the sign *moved*:

| year | mean net index call | mean net index put | calls < 0 | puts > 0 |
|---|---|---|---|---|
| 2021 | −101,204 | −22,710 | 90.5% | 38.1% |
| 2022 | −138,049 | −79,806 | 92.3% | 21.4% |
| 2023 | −146,107 | −96,762 | 87.3% | 24.5% |
| 2024 | −97,093 | −18,275 | 64.6% | 51.2% |
| 2025 | +16,197 | +70,525 | 39.9% | 79.0% |
| 2026 | +37,600 | +97,295 | 36.7% | 86.7% |

Monotone on both legs across six years — a structural transition centred on 2024, not
noise. By 2025–26 the Pro desks hold a *long* index-options book on both legs.

**A single global `DEALER_SIGN` is therefore wrong regardless of which way it points.**
It is stable and cleanly invertible for stock options, and time-varying for index.

This independently explains a result the previous report could not account for: the
index subsample showed nothing for H1 (p = 0.72), in exactly the segment where
dealer-hedging theory should be *strongest*. If the assumed sign is right only about
half the time there, no signal could survive.

### What the correction costs

Derived previously and unchanged: flipping the sign negates net GEX, and negating a
function does not move its zeros — so **`gamma_flip` and `dist_to_flip` are invariant**
under the correction. For the stock universe it is a one-line change plus a ~12-minute
`gex-features` refresh, with no ETL re-run. `dist_to_flip` was already the strongest
dealer feature after an IV control (+0.0210, t = +7.47), and is unaffected either way.

The index handling is a genuine design question, not a fix: flip only the stock legs, or
derive a time-varying sign from the Pro book itself.

## Open items

Updated from the previous report.

1. ~~Participant-level data unavailable~~ — **acquired.** What remains is the *label*:
   participant OI supplies a market-level regime target (~1,258 rows), not the
   per-symbol institutional label the project's stated goal needs.
2. **The `DEALER_SIGN` correction**, per the section above — stock legs are a one-line
   change; the index sign needs a decision.
3. **Derivable-now per-symbol flow features**, needing no new download: OI-to-volume
   ratio per strike (57.6% of chain elements have both non-zero), buildup-quadrant
   attribution, notional flow via `lot_sizes.csv`, outlier-strike detection, futures
   basis and roll. These are unlabelled, which points at an HMM over latent states
   rather than a supervised model.
4. Carried over unchanged: LTIM / PEL / TATAMOTORS producing zero rows;
   `ProcessPoolExecutor` for the ETL pending a memory test; `backtest_results` empty.
5. `ruff` and `mypy` are still absent from `.venv`, but **do run via `uvx`**
   (`uvx ruff@latest check src/mktmtm`), so the CLAUDE.md gates are enforceable today
   without fixing the dev extras.

## Re-running

```bash
uv run mktmtm fetch-participant-oi                               # 5-year default, ~11 min
uv run mktmtm fetch-participant-oi --start-date 2021-08-01       # match ml_dataset's start
uv run mktmtm fetch-participant-oi --no-load                     # download only
```

Re-runs are cheap: cached files are re-parsed rather than re-fetched, so only genuinely
missing days hit NSE. The backfill of 1,329 weekdays took 1 m 16 s with 1,221 already
cached.

Verifying coverage against the dataset:

```sql
SELECT COUNT(*), COUNT(DISTINCT date), COUNT(DISTINCT client_type) FROM participant_oi;
-- expect 6290 | 1258 | 5

SELECT COUNT(*) FROM (SELECT DISTINCT date FROM ml_dataset) m
  LEFT JOIN (SELECT DISTINCT date FROM participant_oi) p USING (date)
  WHERE p.date IS NULL;  -- expect 0
```
