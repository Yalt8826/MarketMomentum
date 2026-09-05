# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.12, managed with `uv`. Everything runs through `uv run`.

```bash
uv sync                                   # install deps into .venv
uv run pytest                             # full suite
uv run pytest -m "not integration"        # skip tests needing a live PostgreSQL
uv run pytest tests/unit/test_pricing.py::test_name -v   # single test
uv run mypy src/mktmtm                    # strict; frontend_app.py is excluded
uv run ruff check src/mktmtm              # lint (E,F,I,UP,B,SIM; line-length 100)
```

Database bootstrap (idempotent, safe to re-run):

```bash
psql "$PG_DSN" -f src/mktmtm/storage/sql/init_db.sql   # runs all DDLs in dependency order
uv run python scripts/verify_db_health.py              # health gate; exits 1 on structural flaws
```

Full pipeline order (ML training path):

```bash
uv run mktmtm fetch-bhavcopy        # NSE F&O bhavcopy → data/raw/bhavcopy/
uv run mktmtm fetch-yahoo           # daily closes → data/raw/yahoo_finance/
uv run mktmtm fetch-participant-oi  # NSE participant-wise OI (FII/DII/Pro/Client) → participant_oi
uv run mktmtm fetch-deals           # NSE bulk/block deals → deals (see the caveats below)
uv run mktmtm fetch-shareholding    # SEBI quarterly FII/DII holdings → shareholding
uv run mktmtm preparse-bhavcopy     # parquet cache; re-run after EVERY fetch (speeds etl ~4x)
uv run mktmtm etl                   # threaded per-symbol ETL → option_metrics
uv run mktmtm make-dataset          # features + targets → ml_dataset (+ parquet)
uv run mktmtm flow-features         # outlier strikes + futures basis + buildup quadrants → flow_features
uv run mktmtm backtest              # optional
```

Modelling stages (all read the tables above; none is part of the ETL path):

```bash
uv run mktmtm market-flow           # Stage 1: market-level flow model → models/evaluation/
uv run mktmtm flow-regimes          # Stage 2: pooled HMM → flow_regimes  (~11 min)
uv run mktmtm flow-attribution      # Stage 3: LLP attribution → flow_attribution
uv run mktmtm flow-momentum         # Stage 4: does positioning continue? (self-supervised)
uv run mktmtm accumulation          # Stage 5: detect accumulation vs shareholding label
```

Smoke runs: `make-dataset --limit-symbols RELIANCE,TCS --limit-dates 30`, `process --limit-days 5`,
`backtest --symbols X --years 2024 --months 1`.

`MKTMTM_NO_PROGRESS=1` forces log-line progress instead of rich bars (also automatic when
stderr is not a TTY, so `mktmtm etl > run.log` stays readable).

## Architecture

Data flows **raw files → PostgreSQL fact table → ML matrix**, with pure-math modules at the bottom
that never touch I/O.

```
data/          bhavcopy (NSE), yahoo (spot), symbols (shipped 222-symbol catalog),
               rates (MIFOR CSV), earnings (NSE board-meeting dates)
options/       pricing (Black-Scholes + bisection IV), volatility (Yang-Zhang), chain
               (ATM strike pick, time-to-expiry) — pure functions, unit-tested standalone
storage/       db.DBWriter (pooled, batched UPSERT, NaN scrub) → option_metrics
               etl.py        threaded per-symbol ETL — THE path that feeds the ML dataset
               etl_chunked.py single-pass sharded variant (ETL_CHUNKS/ETL_CHUNK env), no ivp/ivr
ml/dataset.py  option_metrics → ml_dataset (features + supervised targets)
ml/market_flow.py    Stage 1 — market-level flow model; market_metrics.py holds its
               metrics because ml/metrics.py is cross-sectional and undefined on a
               one-observation-per-date series
ml/regimes.py  Stage 2 — pooled GaussianHMM over per-symbol flow observables
ml/attribution.py    Stage 3 — Learning from Label Proportions bridge
ml/flow_momentum.py  Stage 4 — cross-instrument lead-lag in positioning direction
ml/accumulation.py   Stage 5 — nowcasting institutional accumulation vs shareholding
pipeline/      process.py — LEGACY JSON twin of etl.py; feeds only the old UI, not the ML path
backtest/      short-strangle + weekly delta hedge → backtest_results
ui/            Streamlit control panel; pipeline steps run as detached subprocesses (ui/jobs.py)
```

`etl.py` and `pipeline/process.py` compute the same enrichment two ways (PG rows vs JSON file).
**Any change to the enrichment logic must land in both**, or they silently diverge — the `_IV_WINDOW
= 252` / `_IV_MIN_PERIODS = 60` constants are duplicated in each file precisely because they must match.

### Invariants that are easy to break

These encode past production bugs; the audit trail is in `docs/data/iv-integrity-audit.md` and
`docs/data/ml-dataset-report.md`.

- **Two price series, two purposes.** `option_metrics.underlying_price` is the **raw/unadjusted**
  Yahoo close and is the only correct basis for ATM strike selection, IV, Greeks, and the
  nearest-strike lookups in `_CHAIN_AGG_SQL` — NSE bhavcopy strikes and settles are unadjusted.
  `underlying_price_adj` is the **adjusted** close and is the only correct basis for returns,
  momentum and labels (`ml/dataset.py` aliases it to `underlying_price` when loading). Mixing them
  corrupted ~10% of IV rows before this was fixed.
- **`implied_volatility_bisection` returns `None`, never a sentinel.** Degenerate or non-converged
  inputs must stay `None` → SQL `NULL`. The old `0.0` / `500.0` returns were garbage wearing a
  number's clothes and propagated into every derived vol feature.
- **Row order is load-bearing.** Every momentum feature is a `groupby().pct_change()`. The SQL
  `ORDER BY symbol, date` and the explicit re-sorts in `_add_derived` / `_add_targets` /
  `screen_corporate_actions` are not cosmetic. `validate_no_lookahead()` runs inside `build_dataset`
  and raises rather than writing a corrupt table — don't weaken or skip it.
- **`DATASET_COLUMNS` in `ml/dataset.py` is the single source of truth** for column identity *and
  order*; `sql/init_ml_dataset.sql` mirrors it. Adding a feature means editing both.
- **The bhavcopy parquet cache is held as one sorted DataFrame + a `{symbol: (start, stop)}`
  `searchsorted` offset map** (`etl._load_cache`). A per-symbol dict of frames OOM-killed the ETL at
  ~12.5 GB; boolean-masking per symbol was memory-safe but re-scanned 60M rows 222 times.
- **Bhavcopy `OPEN_INT`/`CHG_IN_OI` are in SHARES, not contracts.** The lot size divides to
  recover a contract count; it never multiplies. `data/lots.py` asserted the reverse until
  2026-09-04. Verified against `participant_oi` (which does report contracts):
  `SUM(OPEN_INT / lot)` over all stock futures matches its `TOTAL` to within 0.02%, where the
  raw sum is off by ~2,158x. Notional is `oi * spot`.
- **Targets are NULL on each symbol's most recent day** (no "tomorrow"). Those are inference rows,
  never training rows.
- **The bhavcopy parquet cache's row groups are NOT in chronological order.** One row group per
  trade date, but the frame is sorted by symbol, so the groups come out with ~293 backward steps
  over five years. Anything needing "yesterday" must sort them first (`flow._ordered_row_groups`);
  streaming in file order silently joins each day against an arbitrary other day and *nothing
  fails* — the same contract exists on both days, so the merge succeeds.
- **Option buildup quadrants must use the delta-adjusted premium residual**, never the raw
  premium change: a call premium rises when the underlying rises regardless of who traded. Raw
  and adjusted disagree on 35.8% of contracts (`scripts/measure_delta_adjustment.py`), and the
  aggregate does not reveal it — the writer share barely moves while individual assignments differ.
- **The pre-2023 shareholding XBRL keeps DEAD CATEGORIES as explicit zeros.**
  `ForeignInstitutionsMember` is the legacy FII bucket, emptied when SEBI folded FII into FPI in
  2014, and it sits in the same filing as `InstitutionsForeignPortfolioInvestorMember` carrying the
  real holding. Resolving aliases by first-present picked the zero and reported **no foreign
  holding for every filing before 2023** — and inflated DII by the same amount through the
  institutions-minus-foreign fallback. `_pick` prefers the first **non-zero** alias for this
  reason. Sanity-check any new holder column for a discontinuity at the 2023 boundary.
- **`shareholding` is the per-symbol institutional label; use SHARE COUNTS, not percentages.**
  A holding percentage moves on a buyback or an issue with nobody having traded; the count only
  moves when somebody buys or sells. The XBRL stores percentages as *fractions* (0.1720 = 17.20%),
  converted on ingest with the scale inferred from the data rather than hard-coded, so a taxonomy
  change cannot silently multiply every holding by 100. `total_shares` is derived as
  `shares / pct` across categories and returns NULL when they disagree by more than 1%.
- **NSE bulk/block deal history cannot be fetched programmatically.** `bulk.csv`/`block.csv` serve
  only the latest trading day, and `/api/historical/*-deals` returns HTTP 503 to any programmatic
  client even with a fully warmed session (it is bot detection — the same report downloads fine
  from a browser). History must be downloaded by hand into `data/raw/deals/`. And **bulk deals
  structurally exclude the F&O universe**: the 0.5%-of-listed-shares threshold selects microcaps,
  so 0 of 158 rows on the day measured were in our 217 symbols. Block deals reach large caps but
  are rare (~2/day). See `docs/implementation/deals-label.md`.
- **Price momentum is the bar for any per-symbol accumulation claim**, the way the calendar is at
  market level. FII accumulation tracks momentum at IC +0.360, and `q_fut_direction` — which has
  price direction baked into its construction (price up + OI up = long buildup) — posts a large raw
  IC that collapses to the shuffled baseline once momentum is partialled out, and flips sign
  between momentum-chasing FII and contrarian mutual funds. Always report the partial, never the
  raw IC alone; assume any price-derived feature is a momentum proxy until the partial says
  otherwise.
- **The calendar-only baseline is the bar for any market-level claim.** The expiry cycle alone
  reaches AUC 0.61–0.72 on next-day participant flow. Stage 1 found no feature set beats it.
- **Never use causal within-symbol z-scoring to remove symbol fixed effects here.** It subtracts a
  *slowly updating* expanding mean, so consecutive z-scores share a baseline error and look
  strongly persistent on a series with no autocorrelation at all — it reported +0.298
  self-persistence where the raw series measures −0.009. Use the raw per-symbol correlation
  (`ml/flow_momentum._lead_lag`) instead. The causal z is still correct for its original purpose
  (comparability across symbols), just not for measuring persistence.
- **Direction scores share construction.** `ce_writer_frac` and `oi_outlier_direction` are built
  from the same buckets as `opt_direction` (same-day correlation −0.75 and +0.84), so each appears
  to "lead" it by inheriting its reversal. Always check the contemporaneous correlation before
  believing a forward one.
- **`screen_corporate_actions` blanks, and logs, rather than drops.** A silent filter here is how
  survivorship bias creeps in.
- **Re-running `make-dataset` alone cannot repair IV/strike/Greek defects** — those are baked into
  `option_metrics` at ETL time, so a fix there requires a full ETL re-run (3–5 h for 5 years).

### Other things worth knowing

- Bhavcopy has two schemas: legacy and UDiFF, split at `UDIFF_CUTOFF_DATE = 2024-07-08`;
  `normalize_bhavcopy_columns` maps UDiFF back to legacy names, and everything downstream assumes
  legacy column names (`SYMBOL`, `INSTRUMENT`, `STRIKE_PR`, `SETTLE_PR`, …).
- The 222-symbol F&O catalog ships inside the package (`mktmtm.data.catalog`) and is loaded via
  `importlib.resources`. `data/symbols.py` is load-only and must never regenerate it.
- FINNIFTY and MIDCPNIFTY have no working Yahoo ticker, so they have no spot source and are
  legitimately absent from the dataset.
- All configuration goes through the pydantic `Settings` singleton (`config/settings.py`), never
  bare `PG_*`/env reads. `PG_DSN` overrides the individual `PG_*` fields.
- Chain aggregates are computed in SQL (`_CHAIN_AGG_SQL` lateral joins over `jsonb_array_elements`)
  because a Python pass over ~320M chain elements takes minutes.
- Integration tests (`tests/integration/`, `pytest.mark.integration`) need a live PostgreSQL and
  skip cleanly when no credentials resolve; `MKTMTM_PG_ENV_FILE` points them at an env file.
- `mktmtm-ui` binds the tailnet IP by default and has no auth — never bind `0.0.0.0`.

### Docs

`docs/implementation/deals-label.md` (the per-symbol institutional label: why every stage so far
lacked one, why NSE's history is not fetchable, and why bulk deals structurally miss the F&O
universe),
`docs/results/README.md` (**start here for outcomes** — consolidated results across all four
modelling stages, with the validity checks that make the negative results credible),
`docs/implementation/model-00-overview.md` (the four-stage
institutional-flow modelling plan; read it before touching `ml/`. Carries the shared no-lookahead
conventions and the inter-stage contracts. Stages 1–3 are **built**; each stage file's Results
section records what was measured, negative results included),
`docs/implementation/flow-features.md` (outlier-strike + futures-basis features: threshold
measurements, the OI-units defect, and the per-symbol vs market-level results),
`docs/data/iv-integrity-audit.md` (price-basis and IV-solver defects + verified fixes),
`docs/data/etl-integrity-change-report.md` (five-year build audit: the cache offset-map
and spot-basis defects, the change set, re-run procedure and acceptance criteria),
`docs/data/ml-dataset-report.md` (feature analysis, look-ahead incident),
`docs/database/README.md` (full schema, JSONB shapes, write paths),
`docs/models/xgboost-direction-model-card.md` (the project's superseded first approach —
supervised per-symbol price direction; its code is at the git tag `archive/xgboost-direction`
and **must not be merged**, it predates several measured pipeline fixes),
`docs/backtest/` (strategy design), `docs/reference/` (NSE formula PDF).
