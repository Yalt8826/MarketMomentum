# MktMtm pipeline — current state

**As of 2026-09-02, commit `c1a6e84`, branch `feat/gex-dealer-positioning`.**
*Partially superseded 2026-09-03 by `docs/data/participant-oi-report.md`, which covers
participant-level data acquisition and the measured dealer sign.*

A record of what the pipeline is, what it produces, what has been measured, and where
the gaps are. Companion documents: `docs/implementation/gex-dealer-positioning.md`
(the plan this branch implements), `docs/models/gex-dealer-positioning-findings.md`
(what the five-year run concluded), `docs/database/README.md` (full schema).

## At a glance

| | |
|---|---|
| Study window | 2021-08-02 → 2026-08-28, **1,254 trading dates** |
| Symbols | 222 in catalog, **217 produce rows** |
| `option_metrics` | 212,441 rows × 16 columns |
| `ml_dataset` | 212,441 rows × **69 columns** (63 features + 6 targets) |
| Chain elements | ~320M, each carrying oi/chg_oi/iv/delta/**gamma/vanna/charm** |
| Raw bhavcopy on disk | 20 GB, 5,344 CSVs |
| Parquet caches | `bhavcopy_cache.parquet` 843 MB, `ml_dataset.parquet` 93 MB |
| Source | 9,077 LOC across 46 modules |
| Tests | **239 unit + 14 integration** (integration skip without PostgreSQL) |
| Full pipeline runtime | **~3 h 45 min** end to end |

## The pipeline

Four stages, orchestrated by `mktmtm gex-pipeline`, each callable standalone.
Runtimes are measured from the 2026-09-01/02 full run, not estimates.

```
data/raw/bhavcopy/*.csv ─┐
data/raw/yahoo_finance/  ├─► preparse ─► etl ─► make-dataset ─► test-gex
data/reference/*.csv     ┘   (parquet)   (PG)     (PG+parquet)    (JSON)
```

### Stage 1 — `preparse-bhavcopy` · 3 min 57 s

Collapses 1,254 bhavcopy CSVs into one sorted parquet cache
(`data/processed/bhavcopy_cache.parquet`, 60,609,884 rows, 304 symbols).

The cache is held as **one sorted DataFrame plus a `{symbol: (start, stop)}`
`searchsorted` offset map**. This shape is load-bearing: a per-symbol dict of frames
OOM-killed the ETL at ~12.5 GB, and boolean-masking per symbol was memory-safe but
re-scanned 60M rows 222 times. Re-run after every `fetch-bhavcopy`; it speeds the ETL
roughly 4×.

### Stage 2 — `etl` · 3 h 32 min 58 s

Threaded per-symbol ETL into `option_metrics`. Per symbol-day it computes the ATM
strike, Black-Scholes Greeks, bisection IV, Yang-Zhang realized vol, and writes the
full option chain as JSONB with per-strike Greeks baked in.

Wrote rows for 217/222 symbols. The five that produced nothing: **FINNIFTY** and
**MIDCPNIFTY** (no working Yahoo ticker, documented and expected), plus **LTIM, PEL,
TATAMOTORS** (new to this run, not yet diagnosed).

Threading gives roughly 1.4× on 4 workers, not 4×, because the work is CPU-bound pure
Python under the GIL. A `ProcessPoolExecutor` conversion is the obvious speed-up and
has not been attempted — it would need a memory test first, given the 12.5 GB OOM
history.

### Stage 3 — `make-dataset` · ~7 min

`option_metrics` → `ml_dataset` (+ parquet export). Chain aggregates are computed in
**SQL** (`_CHAIN_AGG_SQL`, lateral joins over `jsonb_array_elements`) because a Python
pass over ~320M chain elements takes minutes. Adds momentum features, dealer-exposure
aggregates and the six supervised targets, then runs `validate_no_lookahead()` and
`screen_corporate_actions()` before writing.

The last run blanked 26 rows above |20%| single-day moves (ADANIENT, IDEA, NBCC at
+374%, TORNTPOWER at +181%, VEDL, TRENT and others). The screen **blanks and logs
rather than drops** — a silent filter here is how survivorship bias enters.

> This stage was **absent from the pipeline until 2026-09-02**. `gex-features`
> recomputes only `GEX_COLUMNS` by design, and the Phase-5 volatility targets are
> written solely by `build_dataset`. The result was `next_day_abs_return` populated on
> 295 of 212,441 rows and `test-gex` blocking with "no complete (gex,
> next_day_abs_return) rows". See "Failure modes already hit" below.

### Stage 4 — `test-gex` · ~1 min

Phase-6 hypothesis tests → `models/evaluation/gex_h1.json` plus a results table.
Runs H1 (sign test + rank IC), H2 (positioning-change partial ICs), an optional
supervised model gated on H1 significance, and seven robustness checks.

### `gex-features` — opt-in, ~12 min

Fast refresh of the 14 `GEX_COLUMNS` only, leaving momentum features and targets
untouched. Useful when the aggregate SQL changed but the targets are already good.
**No longer a default pipeline stage** — `build_dataset` writes the GEX columns too, so
running both repeats the 12-minute chain aggregation for nothing.

## Inputs

| Source | Path | Notes |
|---|---|---|
| NSE F&O bhavcopy | `data/raw/bhavcopy/` | Two schemas split at `UDIFF_CUTOFF_DATE = 2024-07-08`; `normalize_bhavcopy_columns` maps UDiFF back to legacy names |
| Yahoo Finance closes | `data/raw/yahoo_finance/` | Supplies `underlying_price_adj` only |
| Symbol catalog | `mktmtm.data.catalog` | 222 F&O symbols, shipped in-package via `importlib.resources`; `data/symbols.py` is load-only |
| MIFOR rates | `data/reference/ADJUSTED_MIFOR.csv` | **Carried forward across gaps** — see limitations |
| Earnings dates | `data/reference/earning_dates.json` | NSE board-meeting dates |
| Lot sizes | `data/reference/lot_sizes.csv` | Historical F&O schedule; bhavcopy reports OI in *contracts* |

**Participant-level data was acquired on 2026-09-03** (`participant_oi`, 1,258 days
of Client/DII/FII/Pro/TOTAL open interest) — see
`docs/data/participant-oi-report.md`. It is market-wide, one row per participant per
date, so it cannot serve as a per-symbol feature. Still not present: delivery
percentages, bulk/block deals, shareholding patterns.

## Storage

| Table | Rows | Purpose |
|---|---|---|
| `option_metrics` | 212,441 × 16 | The fact table. JSONB `option_chain` per symbol-day |
| `ml_dataset` | 212,441 × 69 | The training matrix |
| `symbol_data` | 222 × 7 | Catalog metadata |
| `participant_oi` | 6,290 × 16 | Participant-wise OI, 1,258 days × 5 categories |
| `backtest_results` | 0 × 18 | Short-strangle output; unpopulated on this branch |

`option_metrics` columns: `symbol, date, underlying_price, interest_rate,
strike_price, expiry_30d, expiry_60d, expiry_90d, upcoming_earning_date, rv_yz, ce,
pe, option_chain, extras, underlying_price_adj, gamma_flip`.

Each chain element carries: `strike, type, expiry, open, high, low, close, settle,
volume, oi, chg_oi, iv, delta, gamma, vanna, charm`.

Coverage after the re-run: `gamma`/`vanna`/`charm` on **100%** of elements,
`gamma_flip` resolving on **91.4%** of rows (the remainder have no zero crossing in the
±10% band — a real state, not a failure).

## The 69 columns

| Group | n | Columns |
|---|---|---|
| Identity | 2 | `symbol, date` |
| Level | 3 | `underlying_price, interest_rate, rv_yz` |
| CE/PE | 26 | IV at 30/60/90d, IVP/IVR, five Greeks each leg, close/volume/OI |
| Tier 2 | 9 | `ret_1d/5d/10d/20d, iv_rv_spread, term_slope, put_call_iv_spread, days_to_expiry_30d, days_to_earnings` |
| Tier 3 | 9 | `atm_straddle, skew_25d, put_call_vol_ratio, put_call_oi_ratio, otm_put_iv_premium, chain_volume, chain_oi, chain_oi_chg, chain_breadth` |
| **Dealer exposure** | 14 | `dex, gex, gex_chg, vanna_exp, charm_exp, oi_hhi, max_pain, call_wall, put_wall, dist_to_call_wall, otm_put_oi_build, dex_chg, oi_hhi_chg, dist_to_flip` |
| Targets | 6 | `next_day_return, next_day_direction, next_day_abs_return, next_day_rv_5d, next_day_iv_chg, next_day_vrp` |

`DATASET_COLUMNS` in `ml/dataset.py` is the single source of truth for column identity
*and order*; `sql/init_ml_dataset.sql` mirrors it. Adding a feature means editing both.

Data quality: worst null rate is **11.8%** (`ce_ivp`/`ce_ivr`, which need a 252-day
warm-up). All Greek and IV columns sit at 5.6–5.8%. Targets are NULL on each symbol's
most recent day by construction — those are inference rows, never training rows.

## Module map

```
data/          bhavcopy, yahoo, rates, earnings, lots, symbols, catalog
options/       pricing (Black-Scholes + bisection IV), volatility (Yang-Zhang),
               chain (ATM pick, time-to-expiry), dealer_exposure (GEX/DEX, gamma flip)
storage/       db.DBWriter (pooled, batched UPSERT, NaN scrub), etl.py, etl_chunked.py
ml/            dataset.py (matrix + targets), gex_hypothesis.py (Phase 6),
               splits.py (purged walk-forward, inner holdout), metrics.py, baselines.py
gex_features.py    fast GEX-only refresh
gex_pipeline.py    four-stage orchestrator + progress UI
pipeline/process.py  LEGACY JSON twin of etl.py — feeds only the old UI
backtest/      short-strangle + weekly delta hedge
ui/            Streamlit control panel; steps run as detached subprocesses
```

**`etl.py` and `pipeline/process.py` compute the same enrichment two ways** (PG rows vs
JSON file). Any change to enrichment logic must land in both or they silently diverge;
`_IV_WINDOW = 252` / `_IV_MIN_PERIODS = 60` are duplicated in each precisely because
they must match.

## What the pipeline has established

Full detail in `docs/models/gex-dealer-positioning-findings.md`. In brief:

Numbers are from the 2026-09-04 run, under the **corrected** `DEALER_SIGN`.

- H1 (dealers short gamma precedes higher realized vol) **fails its own pooled test**.
  Dealers are short gamma on 97.3% of symbol-days and the rare *long*-gamma days are
  the volatile ones — 1.547% vs 1.360% mean |return|, Welch t = +8.96, p = 4.3e−19,
  IC(−gex) −0.0674. That is the opposite of what hedging theory predicts.
- It **also fails both confound controls**, independently of direction. Ranking on
  `−|gex|`, discarding the sign the hypothesis is about, scores +0.0723 — better than
  the signed version at either polarity. Residualized against `ce_iv_30` the partial IC
  is +0.0099 against a +0.2114 baseline. GEX ∝ ΣOI·Γ·S², so the pooled test was ranking
  symbols by size and liquidity.
- **A magnitude effect survives, within symbol**: each symbol's gex z-scored against
  its own past (causal expanding window) gives ρ = +0.0748, +0.0436 after controlling
  for the symbol's own IV, same sign in all six years. Since gex is negative almost
  always, this reads as *less dealer gamma outstanding than usual → larger next-day
  move*. That statement is invariant to the sign convention, which is why it is the
  one worth keeping.
- `dist_to_flip` is the strongest dealer feature after an IV control (+0.0210,
  t = +7.47) and is **invariant** under the sign correction; the GEX *level* is near
  the bottom.

## Failure modes already hit

Recorded so they are not repeated.

1. **Pipeline missing its target-writing stage.** `gex-pipeline` ran
   `preparse → etl → gex-features → test-gex`, and no stage called `build_dataset`.
   Targets sat at 295/212,441 rows. Fixed 2026-09-02.
2. **A success counter that counted attempts.** `_update_gex_columns` returned
   `len(rows)`, not rows written, on a strict UPDATE that silently skips missing
   `(symbol, date)` pairs — so it would have reported full success while writing
   nothing. Now accumulates `cur.rowcount`.
3. **Robustness checks that all varied the sample and none the confound.** The original
   four (sign flip, liquidity filter, index vs stock, expiry week) cannot distinguish a
   mechanism from a size proxy. `sign_vs_magnitude`, `iv_control` and
   `within_symbol_test` were added for that.
4. **~99M redundant `strptime` calls.** `get_time_to_expiry_in_years` was called once
   per chain element while a chain holds ~3 distinct expiries. `lru_cache` cut it 30×
   (33.94 ms → 1.15 ms per call), saving ~50 min on a five-year run.
5. **A detached run killed by SIGHUP** on terminal close. Long runs need
   `setsid nohup ... &`.

## Invariants that are easy to break

- **Two price series, two purposes.** `underlying_price` is raw/unadjusted and is the
  only correct basis for ATM strike selection, IV, Greeks and nearest-strike lookups —
  NSE strikes and settles are unadjusted. `underlying_price_adj` is the only correct
  basis for returns, momentum and labels. Mixing them corrupted ~10% of IV rows before
  it was caught.
- **`implied_volatility_bisection` returns `None`, never a sentinel.** The old `0.0` /
  `500.0` returns propagated into every derived vol feature.
- **Row order is load-bearing.** Every momentum feature is a `groupby().pct_change()`;
  the SQL `ORDER BY symbol, date` and the explicit re-sorts are not cosmetic.
- **Re-running `make-dataset` alone cannot repair IV/strike/Greek defects** — those are
  baked into `option_metrics` at ETL time and need a full re-run.

## Known limitations

- **The dealer side is now measured, and `DEALER_SIGN` is wrong.** Across 1,258 days
  the Pro book is net *short* calls and *long* puts on 99.9%/98.3% of days for stock
  options — inverted on both legs. For index options the sign is regime-dependent and
  flipped during 2024–25. Uncorrected as of this writing; see
  `docs/data/participant-oi-report.md`.
- **`interest_rate` is unusable as a feature.** The MIFOR rate is carried forward
  across gaps (e.g. 11 days around 2025-04-15) and is a frozen constant on those dates.
- **Open interest is in contracts, not shares.** Any gap in `lot_sizes.csv`
  mis-scales exposure for the affected symbol-periods.
- **Settlement IV contamination** on untraded deep strikes. Measured: ATM ±5% carries
  83.1% of the gamma×OI weight and strikes beyond 30% moneyness carry 0.0%, so the
  contamination sits where it has no weight.
- **`ruff` and `mypy` are not installed in `.venv`** — the dev extras are missing, but
  both run via `uvx` (`uvx ruff@latest check src/mktmtm`), so the CLAUDE.md gates are
  enforceable without fixing the extras.
- **`backtest_results` is empty** on this branch.

## Open items

1. **No institutional-flow label.** Every target is a price or volatility outcome; none
   says an institution entered. Participant-wise OI was **acquired on 2026-09-03** and
   settles the `DEALER_SIGN` question, but it is market-wide — it supplies a
   market-level regime target (~1,258 rows), not the per-symbol label the project's
   stated goal needs. See `docs/data/participant-oi-report.md`.
2. **Derivable-now flow features**, needing no new data: OI-to-volume ratio per strike
   (57.6% of chain elements have both non-zero), buildup-quadrant attribution, notional
   flow via lot sizes, outlier-strike detection, futures basis and roll.
3. LTIM / PEL / TATAMOTORS producing zero rows.
4. `ProcessPoolExecutor` for the ETL, pending a memory test.

## Re-running

```bash
uv run mktmtm gex-pipeline                    # all four stages, ~3h45m
uv run mktmtm gex-pipeline --skip preparse,etl   # dataset + tests only, ~8 min
uv run mktmtm gex-pipeline --limit-symbols RELIANCE,TCS --limit-dates 30   # smoke
```

Long runs must be detached, or a closing terminal kills them:

```bash
setsid nohup uv run mktmtm gex-pipeline > run.log 2>&1 < /dev/null &
tail -f run.log
```

`MKTMTM_NO_PROGRESS=1` forces log lines instead of rich bars — automatic when stderr is
not a TTY, so redirecting to a file stays readable.
