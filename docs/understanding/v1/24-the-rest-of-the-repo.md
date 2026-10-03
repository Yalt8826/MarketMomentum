# 24 — The Rest of the Repository

[← Glossary](23-glossary.md) · [Index](README.md)

Documents 01–22 follow the two project stories: the research
([01–16](README.md)) and the trading tool ([17–22](17-selling-volatility.md)).
But a repository is not only its narrative path, and several working components
sit off to the side of both. This document accounts for **everything else**, so
nothing in the codebase is unexplained — and ends with a map from every source
file to the document that covers it.

---

## 1. The three entry points

The package installs three commands, and only the first is on the research path:

| Command | Points at | Status |
|---|---|---|
| **`mktmtm`** | `cli:main` | ⭐ The real one. Every pipeline and modelling stage. |
| `mktmtm-ui` | `frontend:main` | A working Streamlit control panel |
| `mktmtm-api` | `api:main` | A 21-line FastAPI stub |

### `mktmtm-api` — an unfinished stub, named honestly

Twenty-one lines: a FastAPI app with a single `/health` endpoint returning
`{"status": "ok"}`, and a `uvicorn` launcher bound to `127.0.0.1:8000`.

It serves no model, reads no table, and nothing depends on it. It is scaffolding
for a serving layer that was never built — which is the correct outcome, given
that the project's result is a quarterly ranking rather than anything needing a
live API.

Listed here so that finding it in `pyproject.toml` does not suggest a serving
component exists.

---

## 2. The Streamlit UI

A working control panel, built during the `main`-branch phase, that predates the
modelling work and is not part of it.

| Module | Role |
|---|---|
| `frontend.py` | Launcher — resolves the bind address at start-up |
| `frontend_app.py` | The Streamlit app itself (excluded from `mypy`) |
| `ui/jobs.py` | Background job runner |
| `ui/queries.py` | Read queries against PostgreSQL |
| `ui/charts.py` | Altair charts |
| `ui/styles.py` | The dark trading-terminal theme |

**How the job runner works, and why.** Pipeline steps run as **detached
subprocesses**, with state on disk under `<data_dir>/jobs/` — a `.log` per job
capturing output, and a `.json` holding status. So the UI never blocks, and jobs
survive an app restart.

> ⚠️ **A security property that is enforced, not aspirational.** `mktmtm-ui`
> binds the machine's **tailnet** IPv4 by default, resolved at launch rather than
> hard-coded, and falls back to loopback when tailscale is absent.
> `--host 127.0.0.1` gives local-only (SSH-tunnel) mode.
>
> **It has no authentication of any kind. It must never bind `0.0.0.0`.**
> Doing so exposes a database-backed control panel that can start pipeline
> subprocesses to the entire local network.

The UI reads the **legacy** JSON pipeline output, not `ml_dataset` — which is why
`pipeline/process.py` still exists (§4).

---

## 3. The backtester

`backtest/strangle.py` — the one part of the project aimed at **trading** rather
than detection. It is on `main`, predates the reframe, and has been quiet since.

**The strategy.** A short strangle with weekly delta-hedge adjustments:

```
   month start   →  SELL the ~20-delta call AND the ~20-delta put
                    of the ~30-day expiry
                    (collect two premiums; profit if the stock
                     stays between the two strikes)

   each week     →  close the cheaper leg, re-sell it near the
                    price of the other leg          ← the delta hedge

   exit day      →  buy back both legs
                    (last trading day on or before the
                     index-Tuesday / stock-Wednesday before expiry)
```

This is the **writer's** side of document 03 §3.2 — collecting premium, betting
that movement stays small, carrying open-ended risk in exchange. Exactly the
trade the project spends the rest of its time trying to detect other people
doing.

Three deliberate differences from the legacy script it was ported from, each of
which is a testability decision:

- Every connection parameter comes from the pydantic `Settings` object, never
  bare environment variables.
- The symbol catalog is read through `data/symbols.py`, not a filesystem path.
- `find_exit_day` takes a `Callable[[date], bool]` trading-day check rather than
  a live database cursor, **so its logic is unit-testable without a database.**

Output goes to `backtest_results`.

### It is no longer off to the side

This section described a dormant component when it was written. It is now the
centre of the project's second line of work: `feat/vol-selling-strategy` added
**+518 lines** to `strangle.py` alone (trade economics, a stop loss, and three
defect fixes) and built four modules around it.

**The whole of [Part III](17-selling-volatility.md) is about this code**, and
the numbers it produced — 7,706 trades, +4.56% mean return on margin, a −37.27%
drawdown, and eight silent defects found along the way. What follows there rather
than here:

| | |
|---|---|
| The strategy, precisely | [20 §1](20-does-it-make-money.md) |
| `backtest/economics.py` — costs, margin, slippage | [18](18-what-a-trade-costs.md) |
| `backtest/analysis.py` — the Gate 0 report | [20 §2](20-does-it-make-money.md) |
| `backtest/filters.py` — feature evaluation | [20 §3](20-does-it-make-money.md) |
| `serving/screen.py` — the daily tool | [21](21-the-daily-screen.md) |
| `serving/scoring.py` — forward validation | [22](22-forward-validation.md) |

Three of §3's listed gaps have since been closed by that work — the
`PRIMARY KEY` now carries a `variant` column, a liquidity filter is enforced,
and a stop loss was implemented and measured (and rejected).

---

## 4. The legacy JSON pipeline

`pipeline/process.py` computes the **same enrichment** as the ETL, against JSON
files instead of PostgreSQL. It feeds only the UI, not the modelling path.

It is kept because the UI uses it, and it carries a standing hazard worth
knowing about:

> ⚠️ **Any change to enrichment logic must land in both `process.py` and
> `storage/etl.py`, or they silently diverge.** The constants `_IV_WINDOW = 252`
> and `_IV_MIN_PERIODS = 60` are duplicated in each file *precisely because they
> must match*, and an anti-divergence test keeps the two chain builders
> byte-identical.

That test is the only thing preventing a slow, invisible drift between two
implementations of the same mathematics.

---

## 5. The chunked ETL variant

`storage/etl_chunked.py` — a second ETL implementation, kept alongside the
threaded one.

| | `etl.py` (the real path) | `etl_chunked.py` |
|---|---|---|
| Iteration | Per symbol, threaded | Single pass over files, in date order |
| Memory | Holds a per-symbol time map | Streams straight into the writer buffer |
| IV percentile / rank | ✅ Computed | ❌ **Skipped** |
| Sharding | No | `ETL_CHUNKS` / `ETL_CHUNK` |

Sharding lets several processes split a full backfill:
`ETL_CHUNKS=8 ETL_CHUNK=3` runs only files `[len×3/8, len×4/8)`.

**Do not use it to build the ML dataset.** It skips the rolling IV percentile and
rank pass, so `ml_dataset` built from it would be missing features that the
modelling stages read. It exists for bulk backfills where those columns will be
recomputed anyway.

---

## 6. Testing

**463 tests, all passing**, none requiring a database
(`uv run pytest -m "not integration"`). Integration tests live under
`tests/integration/` and skip cleanly when no credentials resolve.

The project is built test-first, and four categories of test are doing real work
rather than box-ticking:

| Kind | Example | What it protects |
|---|---|---|
| **Pure-math** | `test_pricing.py` | Black-Scholes and the Greeks against known values, no I/O |
| **Anti-divergence** | the two chain builders | The legacy twin cannot drift from the ETL (§4) |
| **Invariant** | causal normalisation | Appends future rows, asserts earlier values are **bit-identical** |
| **Contract** | `test_pipeline_runner.py` | Each pipeline wrapper matches the CLI command it duplicates |

That last category exists because of the bug in
[document 15 §9](15-bugs-and-lessons.md) — a stage that reported success in 58
seconds while its table stayed stale. Every new stage wrapper gets one.

The **leakage tripwire** ([document 11 §7](11-machine-learning-basics.md)) is the
same idea applied to the research harness rather than the code: a check that
*should* score perfectly, which is a stronger statement about an evaluation
pipeline than any passing unit test.

---

## 7. Configuration

Everything goes through a single pydantic `Settings` singleton
(`config/settings.py`).

> **Never read `PG_*` or any other environment variable directly.** `PG_DSN`
> overrides the individual `PG_*` fields. `MKTMTM_PG_ENV_FILE` points the
> integration tests at an env file.

`config/logging.py` sets up logging; `progress.py` provides the rich progress
bars, which automatically degrade to plain log lines when output is not a
terminal (or when `MKTMTM_NO_PROGRESS=1` is set), so a redirected overnight log
stays readable.

`ranking/` and `models/` are **empty placeholder packages** — an `__init__.py`
each and nothing else. `serving/` is no longer one: it holds `screen.py` (the
daily trading tool) and `scoring.py` (forward validation), covered in
[21](21-the-daily-screen.md) and [22](22-forward-validation.md).

---

## 8. Code map — which document explains which file

| Source | Explained in |
|---|---|
| `options/pricing.py`, `volatility.py`, `chain.py` | [05 — Pricing, IV and the Greeks](05-pricing-iv-and-greeks.md) |
| `options/dealer_exposure.py`, `gex_features.py`, `gex_pipeline.py` | [06 §2](06-how-derivatives-move-markets.md), [10 §5](10-the-features.md) |
| `data/bhavcopy.py`, `yahoo.py`, `rates.py`, `earnings.py`, `symbols.py`, `lots.py` | [08 — The data](08-the-data.md) |
| `data/participant_oi.py` | [08 §3](08-the-data.md) |
| `data/shareholding.py` | [08 §4](08-the-data.md), [07 §4](07-the-project-idea.md) |
| `data/deals.py` | [08 §5](08-the-data.md) |
| `storage/etl.py`, `db.py`, `sql/` | [09 — The pipeline](09-the-pipeline.md) |
| `storage/etl_chunked.py` | **§5, this document** |
| `ml/dataset.py` | [09 §4](09-the-pipeline.md) |
| `features/flow.py` | [10 — The features](10-the-features.md) |
| `ml/metrics.py`, `market_metrics.py`, `splits.py`, `baselines.py` | [11 — ML basics](11-machine-learning-basics.md) |
| `ml/market_flow.py` | [12 — Stage 1](12-the-models.md) |
| `ml/regimes.py` | [12 — Stage 2](12-the-models.md) |
| `ml/attribution.py` | [12 — Stage 3](12-the-models.md) |
| `ml/flow_momentum.py` | [12 — Stage 4](12-the-models.md) |
| `ml/accumulation.py` | [12 — Stage 5 + nowcast](12-the-models.md) |
| `ml/gex_hypothesis.py` | [14 §4](14-results.md) |
| `pipeline_runner.py`, `progress.py` | [16 — Running it](16-running-it.md) |
| `scripts/verify_db_health.py` | [09 §6](09-the-pipeline.md) |
| `scripts/decile_spread.py` | [14 §3](14-results.md) |
| `scripts/measure_delta_adjustment.py` | [10 §3](10-the-features.md) |
| `backtest/strangle.py` | **§3, this document** + [20 §1](20-does-it-make-money.md) |
| `backtest/economics.py` | [18 — What a trade costs](18-what-a-trade-costs.md) |
| `backtest/analysis.py` | [20 §2 — Gate 0](20-does-it-make-money.md) |
| `backtest/filters.py` | [20 §3 — Gate 1](20-does-it-make-money.md) |
| `serving/screen.py` | [21 — The daily screen](21-the-daily-screen.md) |
| `serving/scoring.py` | [22 — Forward validation](22-forward-validation.md) |
| `pipeline/process.py` | **§4, this document** |
| `frontend*.py`, `ui/` | **§2, this document** |
| `api.py` | **§1, this document** |
| `config/`, `ranking/`, `models/` | **§7, this document** |

[← Glossary](23-glossary.md) · [Index](README.md)
