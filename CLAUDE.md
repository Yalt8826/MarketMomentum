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
uv run mktmtm preparse-bhavcopy     # parquet cache; re-run after EVERY fetch (speeds etl ~4x)
uv run mktmtm etl                   # threaded per-symbol ETL → option_metrics
uv run mktmtm make-dataset          # features + targets → ml_dataset (+ parquet)
uv run mktmtm backtest              # optional
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
- **Targets are NULL on each symbol's most recent day** (no "tomorrow"). Those are inference rows,
  never training rows.
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

`docs/data/iv-integrity-audit.md` (price-basis and IV-solver defects + verified fixes),
`docs/data/ml-dataset-report.md` (feature analysis, look-ahead incident),
`docs/database/README.md` (full schema, JSONB shapes, write paths),
`docs/backtest/` (strategy design), `docs/reference/` (NSE formula PDF).
