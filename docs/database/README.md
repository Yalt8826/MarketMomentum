# MktMtm Database Architecture Report

> **Scope:** PostgreSQL schema behind the Market Momentum (MktMtm) NSE options
> pipeline — every table, column, index, JSONB document, relationship, write
> path and operational consideration, with all details.
>
> **Executable schema:** `src/mktmtm/storage/sql/init_db.sql` (idempotent
> bootstrap, runs all three DDL files in dependency order). Individual DDLs:
> `init_option_metrics.sql`, `backtest_results.sql`, `symbol_data.sql`.
> **Code that owns the schema:** `src/mktmtm/storage/db.py` (writer),
> `src/mktmtm/storage/etl.py` / `etl_chunked.py` (loaders),
> `src/mktmtm/backtest/strangle.py` (reader + result writer).

## 1. Overview

MktMtm stores three logical layers in a single PostgreSQL database (default
database name `mktmtm`, see `PG_DB` in `src/mktmtm/config/settings.py`):

| Layer | Table | Role | Grain |
|---|---|---|---|
| Dimension | `symbol_data` | Dhan metadata for the F&O catalog (222 symbols) | 1 row per symbol |
| Fact – market | `option_metrics` | Enriched daily option metrics (IV, Greeks, RV, chain snapshot) | 1 row per `(symbol, trading date)` |
| Fact – strategy | `backtest_results` | Short-strangle backtest outcomes per symbol/month | 1 row per `(symbol, year, month)` |

The pipeline flows through three stages: **raw files → processed JSON →
PostgreSQL**. The database is the serving layer: the backtester reads
`option_metrics` from it and writes `backtest_results` back into it; the
Streamlit UI and FastAPI health endpoint sit on top.

## 2. Entity-Relationship diagram

```mermaid
erDiagram
    symbol_data {
        varchar symbol_name PK
        varchar symbol_title
        varchar symbol_type
        varchar symbol_bse_code
        varchar isin_code
        bigint dhan_underlying_security_id
        varchar underlying_segment
    }
    option_metrics {
        text symbol PK
        date date PK
        float underlying_price
        float interest_rate
        float strike_price
        date expiry_30d
        date expiry_60d
        date expiry_90d
        date upcoming_earning_date
        float rv_yz
        jsonb ce
        jsonb pe
        jsonb option_chain
        jsonb extras
    }
    backtest_results {
        text symbol PK
        date entry_date
        date exit_date
        int year PK
        int month PK
        double entry_credit
        double exit_cost
        double pnl_points
        text skipped_reason
        double call_entry_strike
        double put_entry_strike
        double call_entry_delta
        double put_entry_delta
        double call_entry_price
        double put_entry_price
        double call_exit_price
        double put_exit_price
        double hedge_pnl_points
    }

    symbol_data ||--o{ option_metrics : "symbol (logical)"
    symbol_data ||--o{ backtest_results : "symbol (logical)"
    option_metrics ||--o{ backtest_results : "reads metrics"
```

**Note on arrows:** relationships are **logical only** — there are **no
foreign-key constraints** in the schema by design (see §7). `symbol_data` is
the reference dimension; `option_metrics.symbol` and
`backtest_results.symbol` reference it at the application layer.

## 3. Table: `symbol_data` — F&O symbol dimension (Dhan metadata)

Source of the 222 seed rows: `nse_fno_scripts.json` catalog (5 indices +
217 individual securities). Seeds are shipped in `symbol_data.sql` and loaded
by the consolidated `init_db.sql`.

| Column | Type | Null | Key | Description |
|---|---|---|---|---|
| `symbol_name` | `VARCHAR(255)` | NOT NULL | PK | NSE ticker, e.g. `NIFTY`, `RELIANCE`, `SBIN` |
| `symbol_title` | `VARCHAR(255)` | yes | | Human-readable name, e.g. `Nifty 50`, `State Bank of India` |
| `symbol_type` | `VARCHAR(50)` | yes | | `index_futures` (indices) or `individual_securities` (stocks) |
| `symbol_bse_code` | `VARCHAR(50)` | yes | | BSE scrip code (`NA_INDEX` for indices) |
| `isin_code` | `VARCHAR(50)` | yes | | ISIN of the underlying (`NA_INDEX` for indices) |
| `dhan_underlying_security_id` | `BIGINT` | yes | | Dhan broker security ID of the underlying (0 for NIFTY, NULL for a few) |
| `underlying_segment` | `VARCHAR(50)` | yes | | Reserved; NULL in all current seeds |

**Indexes:** none beyond the primary key.
**Referenced by:** `option_metrics.symbol`, `backtest_results.symbol` (logical).

> ⚠️ Two catalogs coexist on purpose: the **pipeline driver** is the JSON
> package `src/mktmtm/data/catalog/nse_fno_scripts.json` (loaded via
> `importlib.resources`, never regenerated at runtime), while `symbol_data`
> is the **Dhan mapping mirror** used for broker integration. They must stay
> in sync when the catalog changes.

## 4. Table: `option_metrics` — enriched daily option metrics (fact)

One row per symbol per trading date, written by `storage/etl.py` /
`etl_chunked.py` from `data/processed/processed_data.json`.

| Column | Type | Null | Key | Description |
|---|---|---|---|---|
| `symbol` | `TEXT` | NOT NULL | PK (1/2) | NSE ticker (logical FK → `symbol_data.symbol_name`) |
| `date` | `DATE` | NOT NULL | PK (2/2) | Trading date (parsed from bhavcopy filename, `DD-Mon-YYYY`) |
| `underlying_price` | `FLOAT` | yes | | Spot close from Yahoo Finance for the date |
| `interest_rate` | `FLOAT` | yes | | MIFOR rate for the date, in **percent** (e.g. `5.9`); nearest-available fallback; pricing converts to decimal (`r = rate / 100`) |
| `strike_price` | `FLOAT` | yes | | ATM strike — nearest strike to spot among the next ~3 monthly expiries (`pick_strike_nearest_underlying`) |
| `expiry_30d` | `DATE` | yes | | Nearest future expiry (first FUT/OPT expiry of the day) |
| `expiry_60d` | `DATE` | yes | | Second future expiry |
| `expiry_90d` | `DATE` | yes | | Third future expiry |
| `upcoming_earning_date` | `DATE` | yes | | Next earnings date for the symbol (from `earning_dates.json`), NULL when unknown |
| `rv_yz` | `FLOAT` | yes | | Rolling **Yang–Zhang realized volatility** (annualized, 252 trading periods, 30-day window, 90-day max lookback); linear-interpolated when missing |
| `ce` | `JSONB` | yes | | Call-side summary document (IV 30/60/90, volume, OHLC, IV percentile/rank, Greeks) — see §6 |
| `pe` | `JSONB` | yes | | Put-side summary document, same shape as `ce` |
| `option_chain` | `JSONB` | yes | | Full option-chain snapshot array (every CE/PE row of the day with computed IV + delta) — see §6 |
| `extras` | `JSONB` | yes | | Reserved extensibility bag; always `{}` in the current pipeline |

**Primary key:** `(symbol, date)` — a symbol's day is upserted, never
duplicated.

### 4.1 Indexes

| Index | Type | Purpose |
|---|---|---|
| `idx_symbol_date (symbol, date)` | btree | Range scans per symbol (redundant with PK — legacy artifact, kept for query plans) |
| `idx_rv_yz (rv_yz)` | btree | Filtering/sorting on realized vol |
| `idx_strike_price (strike_price)` | btree | Filtering/sorting on ATM strike |
| `idx_ce_iv30 ((ce->>'iv_30'))` | btree expression | Filter on the call IV-30 value inside the JSONB doc |
| `idx_ce_delta ((ce->'greeks'->>'delta'))` | btree expression | Filter on call delta inside the JSONB doc |
| `idx_option_chain_gin (option_chain)` | GIN (`jsonb_path_ops`) | `@>`, `?`, containment/array queries against the chain snapshot |

The expression indexes are **functionally dependent on the JSON key names**
(`iv_30`, `greeks.delta`): renaming keys in the pipeline silently breaks these
indexes' usefulness. Keep §6 in sync with §4.1.

## 5. Table: `backtest_results` — short-strangle strategy results (fact)

One row per symbol per calendar month, written by
`backtest/strangle.py::insert_backtest_result`. A `skipped_reason` row means
the month was screened out (no trade taken).

| Column | Type | Null | Key | Description |
|---|---|---|---|---|
| `symbol` | `TEXT` | NOT NULL | PK (1/3) | NSE ticker (logical FK → `symbol_data.symbol_name`) |
| `entry_date` | `DATE` | yes | | Entry day — first trading day of the month (or of the 30-day expiry window) |
| `exit_date` | `DATE` | yes | | Exit day — last trading day on/before the index-Tuesday / stock-Wednesday before expiry |
| `year` | `INTEGER` | NOT NULL | PK (2/3) | Calendar year of the trade month |
| `month` | `INTEGER` | NOT NULL | PK (3/3) | Calendar month of the trade (1–12) |
| `entry_credit` | `DOUBLE PRECISION` | yes | | Credit received at entry = `call_entry_price + put_entry_price` |
| `exit_cost` | `DOUBLE PRECISION` | yes | | Cost of buying back both legs at exit |
| `pnl_points` | `DOUBLE PRECISION` | yes | | Net PnL in points = `entry_credit − exit_cost + hedge_pnl_points` (**includes** hedge adjustments) |
| `skipped_reason` | `TEXT` | yes | | Why no trade: `No Trading Days`, `Missing Entry Data`, `Earnings on <date>`, `No Suitable Options`, `No Valid Exit Date`, `Failed hedge adjustment on <date>`, `Missing Exit Data`, `Missing Exit Price`; NULL = traded |
| `call_entry_strike` / `put_entry_strike` | `DOUBLE PRECISION` | yes | | ~20-delta strikes of the two legs at entry |
| `call_entry_delta` / `put_entry_delta` | `DOUBLE PRECISION` | yes | | Leg deltas at entry (≈ ±0.20) |
| `call_entry_price` / `put_entry_price` | `DOUBLE PRECISION` | yes | | Leg premiums at entry |
| `call_exit_price` / `put_exit_price` | `DOUBLE PRECISION` | yes | | Leg premiums at exit (buy-back) |
| `hedge_pnl_points` | `DOUBLE PRECISION` | yes | | PnL from the weekly delta-hedge adjustments (`total_adjustment_pnl`), kept separately for attribution; added by `ALTER TABLE` in `backtest_results.sql` |

**Primary key:** `(symbol, year, month)` — one trade per symbol per month;
re-running a month **upserts** the row.

### 5.1 Indexes

| Index | Type | Purpose |
|---|---|---|
| `idx_backtest_results_symbol_date (symbol, entry_date)` | btree | Per-symbol performance timelines |
| `idx_backtest_results_pnl (pnl_points)` | btree | Sorting/filtering winners and losers |

## 6. JSONB document structures

`option_metrics` stores the rich payloads as JSONB so the schema stays stable
while the analytics evolve. `DBWriter` scrubs NaN/Inf → `NULL` recursively
(`clean_for_json`) before serialization; Postgres `Json()` adapter is used on
every document column.

### 6.1 `ce` / `pe` — leg summary documents (identical shape)

```jsonc
{
  "iv_30": 14.23,            // implied vol (%), 30-day expiry, bisection-implied
  "iv_60": 15.01,            // implied vol (%), 60-day expiry
  "iv_90": 15.87,            // implied vol (%), 90-day expiry
  "volume": 312450,          // total contracts traded across all CE/PE rows of the day
  "last_price_30d": 142.35,  // settle price of the 30-day ATM leg
  "close": 142.35,           // OHLC of the 30-day ATM leg (settle/close/open/high/low)
  "open": 138.10,
  "high": 144.20,
  "low": 137.85,
  "ivp": 63.2,               // 30-day rolling IV percentile (0–100; window 30)
  "ivr": 41.7,               // 30-day rolling IV min-max rank (0–100; NULL on flat window)
  "greeks": {                // Black–Scholes Greeks of the 30-day ATM leg
    "delta": 0.498,
    "gamma": 0.0021,
    "theta": -0.084,
    "vega": 0.312,
    "rho": 0.045
  }
}
```

IV values are stored in **percent** (`iv * 100`); `ivp`/`ivr` are also
0–100 scale (percentile/rank × 100). `greeks` is zero-filled when IV or T is
invalid.

### 6.2 `option_chain` — daily chain snapshot (array of per-row docs)

```jsonc
[
  {
    "expiry": "27-Aug-2025", // expiry as DD-Mon-YYYY
    "strike": 24500.0,
    "type": "CE",            // CE or PE (OPTSTK / OPTIDX rows only)
    "settle": 142.35,        // SETTLE_PR
    "open": 138.10,          // bhavcopy OHLC of the row
    "high": 144.20,
    "low": 137.85,
    "close": 142.35,
    "volume": 31890,         // CONTRACTS
    "iv": 14.23,             // bisection implied vol (%), 0.0 when uncomputable
    "delta": 0.498           // Black–Scholes delta
  }
  // ...one element per option row of the symbol that day
]
```

Malformed rows (missing/NaN fields) are skipped at build time, so the array
contains only computable rows.

### 6.3 `extras` — reserved

Always `{}` today. Intended as the escape hatch for future analytics fields
without a migration. Query with `extras->>'key'`; add a btree expression
index when a key becomes a hot filter (see commented example in
`init_option_metrics.sql`).

## 7. Relationships & integrity model

- **No foreign keys — by design.** The repo's database rules prohibit
  database-level FKs and cascades; relationships are enforced in the
  application layer (`ON CONFLICT` upserts, catalog-driven loops in
  `etl.py` / `strangle.py`). This keeps bulk loads fast and avoids
  accidental cascade behavior on a ~multi-GB metrics table.
- **Logical references:**
  - `option_metrics.symbol` → `symbol_data.symbol_name`
  - `backtest_results.symbol` → `symbol_data.symbol_name`
  - `backtest_results` reads `option_metrics` (via
    `strangle.get_metrics_for_day` / `get_option_data`) — no DB link, joined
    on `(symbol, date)`.
- **Uniqueness is enforced** by the primary keys — that is the real integrity
  guarantee, and every writer upserts on conflict rather than inserting
  duplicates.
- **Nullability is deliberately loose** on fact columns: market-data gaps
  (missing spot, missing expiry, skipped months) are represented as `NULL`,
  not sentinel values. `skipped_reason` rows in `backtest_results` carry
  NULLs for all price fields.

## 8. End-to-end data flow

```
NSE F&O bhavcopy (CSV)  ─┐
Yahoo Finance (JSON)     ─┼─►  pipeline/process.py  ──►  data/processed/processed_data.json
MIFOR rates (CSV)        ─┤        (options/* math)         (historical.scripts.<sym>.timestamps)
earning_dates (JSON)     ─┘
                                              │  storage/etl.py | etl_chunked.py
                                              ▼
                              ┌──────────────────────────────┐
                              │        PostgreSQL            │
                              │  symbol_data  (dimension)    │
                              │  option_metrics (facts)      │
                              │  backtest_results (facts)    │
                              └──────────────────────────────┘
                                              ▲
                          backtest/strangle.py │ (reads option_metrics,
                              writes backtest_results)
                                              │
                          Streamlit UI / FastAPI (serving layer)
```

1. `fetch-bhavcopy` / `fetch-yahoo` download raw data into `data/raw/`.
2. `process` enriches into per-symbol, per-day timestamps (spot, rates,
   expiries, chain snapshot, IV/Greeks, YZ RV, IV percentile/rank, earnings)
   and writes `processed_data.json`.
3. `etl` (threaded per-symbol) or `etl_chunked` (chunked/sharded) streams
   those records into `option_metrics` via the pooled `DBWriter`.
4. `backtest` reads `option_metrics`, runs the short-strangle + weekly
   delta-hedge simulation per symbol/month, and upserts
   `backtest_results`.
5. The Streamlit UI (`mktmtm-ui`) and FastAPI health endpoint (`mktmtm-api`)
   consume the database for display and monitoring.

## 9. Write semantics & connection handling

- **`DBWriter`** (`storage/db.py`):
  - `psycopg2.pool.SimpleConnectionPool(min=1, max=8)` keyed by DSN,
    `connect_timeout=10`, TCP keepalives on.
  - Batches rows with `psycopg2.extras.execute_values` (page size =
    `batch_size`, default 1000); `SET statement_timeout = 0` per batch.
  - `INSERT ... ON CONFLICT (symbol, date) DO UPDATE SET ...` — re-running a
    day overwrites, never duplicates.
  - Up to `MAX_RETRY = 6` retries with exponential backoff (cap 30 s) on
    `OperationalError`/`InterfaceError`; poisoned connections are closed
    rather than returned to the pool.
  - NaN/Inf scrub (`clean_for_json`) before every JSONB write.
- **Backtest writes** (`insert_backtest_result`): per-row upsert on
  `(symbol, year, month)`; non-finite numerics coerced to `NULL`; a failed
  insert is logged and the run continues (legacy behaviour).
- **Backtest reads**: separate read pool (min 1 / max 2), `MAX_RETRY = 3`.
- **DSN:** `pg_dsn` overrides the individual `PG_*` settings; otherwise
  `postgresql://user:pass@host:port/db?sslmode=...` built from Settings
  (`PG_SSLMODE` default `disable`).

## 10. Schema management

- **Bootstrap:** `src/mktmtm/storage/sql/init_db.sql` is the one-command
  entry point (psql):
  ```bash
  psql "$PG_DSN" -f src/mktmtm/storage/sql/init_db.sql
  ```
  It includes the three DDL files in dependency order
  (`init_option_metrics.sql` → `backtest_results.sql` → `symbol_data.sql`).
- **Idempotency:** all three DDL files are re-runnable (`CREATE TABLE IF NOT
  EXISTS`, `CREATE INDEX IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT
  EXISTS`, seed `INSERT ... ON CONFLICT DO NOTHING`).
- **Change discipline:** evolve the DDL files, never mutate a live table by
  hand. `backtest_results.hedge_pnl_points` was added this way (the
  `ALTER TABLE` in `backtest_results.sql` is the migration pattern for new
  columns).

## 11. Operational guidance

- **Expected volume:** 222 symbols × ~1,250 trading days (5 years) ≈
  **~277k rows** in `option_metrics` today; each row carries a full chain
  snapshot, so row size is dominated by `option_chain` (typically tens of KB
  JSONB per row — expect a several-GB table at full 5-year scale).
  `backtest_results` stays small (~222 × 60 months ≈ 13k rows).
- **Query patterns:** per-symbol time series (`symbol, date` PK scan),
  IV-level screens (`idx_ce_iv30` / `idx_ce_delta`), vol screens
  (`idx_rv_yz`), chain containment (`idx_option_chain_gin`).
- **When to partition:** when the 5-year `option_metrics` dataset outgrows
  memory-friendly scans, partition by `date` (e.g. monthly ranges) and make
  the PK `(symbol, date)` a unique index on each partition — the writer's
  `ON CONFLICT (symbol, date)` upsert works unchanged under partitioned
  unique indexes.
- **Retention:** raw bhavcopy stays on the storage host (`data/raw/`, not in
  git); the DB is the durable serving copy. Define a retention policy only if
  disk pressure demands it — the pipeline is append-mostly by design.
- **Backup:** standard `pg_dump`/`pg_basebackup`; the JSONB-heavy
  `option_metrics` compresses well.
- **Monitoring:** watch `option_metrics` row count vs. expected trading days
  (gaps = missing bhavcopy/Yahoo data), and the share of
  `backtest_results` rows with `skipped_reason` (a rising share usually
  means upstream data gaps, not strategy changes).

## 12. Quick reference — schema at a glance

| Object | Kind | Grain / key | Written by |
|---|---|---|---|
| `symbol_data` | dimension | `symbol_name` PK | seeds (`init_db.sql`) |
| `option_metrics` | fact | `(symbol, date)` PK | `etl.py`, `etl_chunked.py` |
| `backtest_results` | fact | `(symbol, year, month)` PK | `strangle.insert_backtest_result` |
| 11 indexes | incl. 2 expression + 1 GIN | see §4.1, §5.1 | DDLs |
| `processed_data.json` | staging (not in DB) | per symbol/date | `pipeline/process.py` |
