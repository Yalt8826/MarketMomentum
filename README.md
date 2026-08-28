# MktMtm — Market Momentum

NSE options market-research pipeline: historical F&O bhavcopy + Yahoo Finance
spot data are enriched into per-symbol, per-timestamp option metrics (implied
volatility, IV percentile/rank, realized volatility, Greeks), stored in
PostgreSQL, and consumed by a short-strangle delta-hedged backtester.

Built as a typed, test-driven port of the athena-verified `data_preprocessing`
scripts. Python 3.12, managed with [uv](https://docs.astral.sh/uv/).

## Features

- **Data acquisition** — NSE F&O bhavcopy downloads (legacy + UDiFF formats,
  5-year default range, symbol-filtered) and Yahoo Finance daily closes for all
  F&O catalog symbols.
- **Option analytics** — Black–Scholes pricing/Greeks, bisection implied
  volatility, Yang–Zhang realized volatility, ATM IV percentile/rank, strike
  selection and time-to-expiry helpers.
- **Processing engine** — combines bhavcopy, spot prices, interest rates
  (MIFOR) and earnings dates into enriched timestamp JSON.
- **Storage** — PostgreSQL `DBWriter` (pooled, batched, NaN-scrubbed) with a
  threaded per-symbol ETL and a chunked/sharded ETL variant; SQL DDLs included.
- **Backtesting** — short-strangle engine with 20-delta entry, weekly delta
  hedges and exit-day logic; results persisted to PostgreSQL.
- **ML dataset preparation** — `make-dataset` derives the supervised targets
  (next-day return / direction) and ~45 features per symbol-day (IV levels,
  percentile/rank, Greeks, momentum, vol spreads, chain aggregates incl.
  put-call volume/OI ratios) into the `ml_dataset` table + a parquet export.
- **Interfaces** — Typer CLI, FastAPI health API (`mktmtm-api`), Streamlit UI
  (`mktmtm-ui`).

## Project layout

```
src/mktmtm/
├── cli.py               # Typer CLI (status, fetch-bhavcopy, fetch-yahoo, process, etl, backtest)
├── api.py               # FastAPI app
├── frontend.py          # Streamlit launcher (tailnet bind; --host/--port)
├── frontend_app.py      # Streamlit control panel (dashboard, pipeline, explorer, analytics)
├── config/              # pydantic Settings + logging
├── options/             # pure math: pricing, volatility, chain helpers
├── data/                # bhavcopy, yahoo, symbols catalog, rates, earnings
├── pipeline/            # processing engine → enriched timestamps JSON
├── storage/             # DBWriter, ETL (threaded + chunked), SQL DDLs
├── backtest/            # strangle backtester
└── models/              # (placeholders) serving-ready model artifacts
```

Design and reference material live under `docs/`:
`docs/backtest/` (strategy design), `docs/database/` (database architecture
report — full schema, JSONB shapes, data flow, operations),
`docs/reference/` (formula reference PDF), `docs/architecture/` (pipeline
diagram).

## Quickstart

```bash
uv sync                    # install deps into .venv
cp .env.example .env       # then edit PG_* to point at your PostgreSQL
uv run mktmtm status       # sanity check
```

Pipeline order for a fresh run:

```bash
uv run mktmtm fetch-bhavcopy                  # 5 years of NSE F&O bhavcopy → data/raw/bhavcopy/
uv run mktmtm fetch-yahoo                     # daily closes for catalog symbols → data/raw/yahoo_finance/
uv run mktmtm process                         # enrich → data/processed/processed_data.json
uv run mktmtm etl                             # threaded per-symbol ETL into PostgreSQL
uv run mktmtm make-dataset                    # features + targets → ml_dataset (+ parquet)
uv run mktmtm backtest                        # short-strangle backtest (4y × all symbols) → PG
```

## CLI

| Command              | Description                                                        |
| -------------------- | ------------------------------------------------------------------ |
| `mktmtm status`      | Show environment, data dirs and DB target                          |
| `mktmtm fetch-bhavcopy` | Download + filter NSE F&O bhavcopy (`--start-date`, `--end-date`, `--max-workers`) |
| `mktmtm fetch-yahoo` | Download daily closes from Yahoo (`--days`, `--output-dir`)        |
| `mktmtm process`     | Run the processing engine (`--limit-days` for bounded smoke runs)  |
| `mktmtm etl`         | Threaded per-symbol ETL into PostgreSQL                            |
| `mktmtm make-dataset`| Build the ML matrix (features + targets) into `ml_dataset` (+ parquet export; `--limit-symbols`, `--limit-dates` for smoke runs) |
| `mktmtm backtest`    | Short-strangle backtester (persists results to PostgreSQL)         |

All commands are thin wrappers over the corresponding `mktmtm.*` modules;
see `uv run mktmtm --help`.

## UI

Streamlit control panel — pipeline job runner, data explorer, analytics.
It runs on athena (jobs survive UI restarts; scope defaults match the CLI).

```bash
uv run mktmtm-ui                        # binds athena tailnet IP:8501 by default
uv run mktmtm-ui --host 127.0.0.1       # local-only mode (SSH-tunnel access)
```

Access from any tailnet device:

- `http://athena:8501` (magicDNS)
- `http://100.82.96.116:8501` (athena's tailnet IP)

> **Security note:** the app has no auth; it is reachable only by devices on
> the tailnet (Tailscale ACLs). Do **NOT** bind `0.0.0.0` — that would expose
> it on every interface. If local-only binding is preferred, SSH-tunnel access
> is the fallback: `ssh -N -L 8501:localhost:8501 athena`.

## Configuration

Settings are loaded from `.env` (see `.env.example`) via pydantic-settings:

| Variable      | Default       | Purpose                          |
| ------------- | ------------- | -------------------------------- |
| `ENVIRONMENT` | `development` | Runtime environment label        |
| `API_HOST`    | `127.0.0.1`   | API bind host                    |
| `API_PORT`    | `8000`        | API bind port                    |
| `LOG_LEVEL`   | `INFO`        | Logging level                    |
| `PG_HOST`     | `localhost`   | PostgreSQL host                  |
| `PG_PORT`     | `5432`        | PostgreSQL port                  |
| `PG_DB`       | `mktmtm`      | PostgreSQL database              |
| `PG_USER`     | `postgres`    | PostgreSQL user                  |
| `PG_PASSWORD` | *(empty)*     | PostgreSQL password              |
| `PG_SSLMODE`  | `disable`     | PostgreSQL SSL mode              |

`PG_DSN` overrides the individual `PG_*` fields when set. Raw/processed data
directories and pipeline tuning (`batch_size`, `max_workers`) are also
configurable; see `src/mktmtm/config/settings.py`.

## Development

```bash
uv run pytest                          # unit + pipeline + backtest tests (integration marked)
uv run pytest -m "not integration"     # skip tests needing a live PostgreSQL
uv run mypy src/mktmtm                 # strict type checking
uv run ruff check src/mktmtm           # lint
```

Integration tests (`tests/integration/`) require a reachable PostgreSQL
matching the `PG_*` configuration and are deselected by default.
