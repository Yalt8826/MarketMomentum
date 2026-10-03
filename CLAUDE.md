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

**To run everything: `uv run mktmtm run-pipeline`** (`--mode no-etl` / `--mode models`,
or `--only <stage>` to resume). Timed, progress-barred, stops at the first failure.
See `docs/runbook.md`.

Full pipeline order (ML training path):

```bash
uv run mktmtm fetch-bhavcopy        # NSE F&O bhavcopy → data/raw/bhavcopy/
uv run mktmtm fetch-yahoo           # daily closes → data/raw/yahoo_finance/
uv run mktmtm fetch-participant-oi  # NSE participant-wise OI (FII/DII/Pro/Client) → participant_oi
uv run mktmtm fetch-deals           # NSE bulk/block deals → deals (see the caveats below)
uv run mktmtm fetch-shareholding    # SEBI quarterly FII/DII holdings → shareholding
uv run mktmtm preparse-bhavcopy     # parquet cache; re-run after EVERY fetch (speeds etl ~4x)
uv run mktmtm etl                   # threaded per-symbol ETL → option_metrics (INCREMENTAL)
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
uv run mktmtm nowcast               # rank symbols for the quarter SEBI has not disclosed yet
uv run mktmtm screen                # trading tool: today's strangles, priced and risk-capped
uv run mktmtm score-screens         # Phase 3: score the frozen screen records
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
- **The ETL is incremental by default and a full rebuild will OOM a 14 GiB host.** `--full`
  loads the entire ~60M-row cache (~9 GB resident) and was OOM-killed twice on 2026-09-05 —
  silently, since SIGKILL is not catchable and the log just stops. Incremental loads ~10M rows
  (~2.5 GB) and runs in ~1 h instead of 10–12. The write cutoff is **per symbol**, because a
  global one would freeze a partially-updated table permanently; and the recomputed overlap is
  **not** rewritten, because those rows hold IV percentile/rank computed with a full 252-day
  history that a trailing window cannot reproduce.
- **SEBI shareholding publishes ~21 days after quarter end, NOT ~6 weeks.** Measured over 1,087
  filings across 53 symbols: median 17 days, **90th percentile exactly 21 in every quarter**
  (the LODR Reg. 31 deadline), 95% within 26. The long tail (77-143 days) is revisions, not
  initial publication. The docs' "a few weeks behind that" was an assumption and is wrong by
  ~3 weeks, which is where every `~2026-11-14` came from. The master API's **`broadcastDate`**
  field carries the real publication timestamp and `fetch_filing_index` currently discards it.
- **Re-running `make-dataset` alone cannot repair IV/strike/Greek defects** — those are baked into
  `option_metrics` at ETL time, so a fix there requires a full ETL re-run (3–5 h for 5 years).
- **`nsearchives.nseindia.com` static files serve FULL HISTORY; `www.nseindia.com/api/*` is
  bot-blocked.** The deals failure is a property of the API path, not of NSE, and the docs
  generalised it too far. Verified available at 2021/2023/2024/2025/2026 dates with a warmed
  session: participant *volume* (`fao_participant_vol_`), per-symbol daily **delivery %**
  (`archives/equities/mto/MTO_`), 165 index closes with P/E and P/B (`ind_close_all_`), the F&O
  ban list (`archives/fo/sec_ban/fo_secban_` — **not** under `content/`), MWPL, FII stats, the
  equity cash bhavcopy, and sector constituents carrying an `Industry` column. See
  `docs/data/nse-archive-survey.md`. **Always probe an old date**: a latest-day-only report like
  `bulk.csv` is indistinguishable from a real archive if you only ask for today.
- **A symbol in an F&O ban cannot be traded, only closed.** `data/secban.py` gates the screen.
  Its failure semantics are the point: NSE publishes the file on every trading day and writes
  `NIL` when nothing is banned, so `200 + NIL` (authoritatively empty) must never be confused
  with a failed lookup. `fetch_secban` raises on any non-200 including the holiday 404, and the
  screen records `secban_checked=False` rather than quietly proceeding. The live screen selected
  a banned symbol before this existed.
- **The 222-symbol catalog is a FIXED 2021 universe and covers 177 of 213 live F&O names (83%).**
  36 trading symbols are absent (incl. SUZLON — rank 3 by OI — ADANIPOWER and TMPV) and 40
  catalog symbols no longer trade. Three of the five "no Yahoo spot source" symbols were
  *renamed*, not de-sourced: `TATAMOTORS→TMPV`, `LTIM→LTM`, PEL delisted. Keeping it frozen is
  correct for the research (it avoids look-ahead in universe selection and preserves
  comparability) and wrong for the screen; do not change the screen's universe mid-way through
  Gate 3's window. See `docs/data/catalog-drift.md`.
- **A screen record must never claim it was written on its entry date.** Bhavcopy publishes after
  the close, so every screen is generated after the entry it prices. What matters is
  `sessions_after_entry` — trading sessions closed in between — which the record publishes from
  the data. 0 is safe; non-zero prints as hindsight-contaminated.
- **A check that fails closed cannot be validated by anything it rejects.** `committed_at`
  returned `None` for every record ever written (relative pathspec, `cwd` set to the file's own
  parent), so Gate 3 was unpassable while reporting each record as "not committed before its
  outcome". It surfaced only when a record that *should* have passed was refused. Write the
  passing case first.

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

**`docs/trading/README.md`** (**start here for the trading branch** — the whole story in
one page: the four gates, the product, the eight silent defects, and the honest limits),
**`docs/trading/implementation/`** (the volatility-selling pivot, on branch
`feat/vol-selling-strategy` — turning the project into a usable tool. Separate line of
work from the research: the detector cannot be traded (score -> forward return +0.0200,
11/19 quarters), so this sells magnitude instead of predicting direction. Four gates;
Gate 1 — beating IV rank alone — is expected to fail and that prior is recorded.
**Gate 0 PASSED**: +4.56% mean return on margin, 6/6 years positive, -37.27% max
drawdown. **Gate 1 FAILED as predicted**: no feature beats chance, and neither does IV
rank. **A stop loss was measured at 1.5x/2x/3x and makes every outcome worse.**
`mktmtm screen` ships the daily tool — ordered by tradeability, not conviction,
with typed refusals and a frozen dated record; `mktmtm score-screens` scores those
records forward. **Gate 3 has its first eligible record** (2026-10-01, 7 positions,
committed 2026-10-03, scores from ~2026-10-27); five more are needed and the verdict
lands ~March 2027. A record counts only if it entered on the month's FIRST TRADING DAY
and was committed to git BEFORE its outcome existed. **Eight silent defects** found and
fixed on the way — see `docs/trading/results/bugs-and-fixes.md`; note that defect 8
means the provenance check had never actually worked, so the celebrated exclusion of
eight backtested +94.55% screens was right for the wrong reason),
**`docs/understanding/v1/`** (the ground-up explanation of the whole project for a reader
with no market background, in three parts: **I (01-06)** markets → F&O → open interest → the
Greeks; **II (07-16)** the research — project idea → data → pipeline → features → the six
stages → results → the silent defects (19 catalogued); **III (17-22)** the trading tool — why the
detector cannot be traded → selling volatility → trade economics → the four gates → Gate 0/1
and the stop loss → the daily screen → forward validation and its eight further defects;
**23-24** glossary and a file-to-document map. Written from the documents below and consistent
with them; update it when a headline number changes),
`docs/runbook.md` (running the pipeline end to end: order, measured timings, the three
things that bite, and how to score the frozen nowcast),
`docs/implementation/nowcast.md` (the detector run forward against an undisclosed quarter:
window calibration, momentum residualisation, and why the prediction record is committed
rather than left in gitignored `models/`),
`docs/implementation/phase-2-report.md` (implementation + results narrative for the reframe to
detection, the shareholding and deals pipelines, Stages 4-5, and the two silent data defects),
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
**`docs/implementation/pre-submission-audit.md`** (the October 2026 audit, and the single most
useful entry point for what changed last: the day-horizon negative result, the NSE archive
survey, two silent defects — a banned symbol in a live record, and Gate 3's provenance check
having never worked — the catalog-drift finding, Gate 3's first eligible record, and the
constant-universe control that closed the one threat to the headline result),
`docs/results/daily-horizon.md` (is there a day-to-day strategy? No: intraday is structurally
impossible on EOD data, stock options are monthly-only, and the daily short-vol edge went
**gross-negative in 2024** — which promotes "the premium compressed" over "STT rose" as the
explanation for Gate 0's decay. Single-pass measurement, caveated as such),
`docs/data/nse-archive-survey.md` (what is actually fetchable from NSE, the access rule, and
what each source is worth),
`docs/data/catalog-drift.md` (the fixed universe, measured: 177 of 213 live names),
`docs/data/iv-integrity-audit.md` (price-basis and IV-solver defects + verified fixes),
`docs/data/etl-integrity-change-report.md` (five-year build audit: the cache offset-map
and spot-basis defects, the change set, re-run procedure and acceptance criteria),
`docs/data/ml-dataset-report.md` (feature analysis, look-ahead incident),
`docs/database/README.md` (full schema, JSONB shapes, write paths),
`docs/models/xgboost-direction-model-card.md` (the project's superseded first approach —
supervised per-symbol price direction; its code is at the git tag `archive/xgboost-direction`
and **must not be merged**, it predates several measured pipeline fixes),
`docs/backtest/` (strategy design), `docs/reference/` (NSE formula PDF).
