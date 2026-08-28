# Dataset Preparation Plan — `ml_dataset` for ML Training

> **Goal:** after a full re-download (`fetch-bhavcopy` → `fetch-yahoo` → `process` → `etl`),
> one more step (`make-dataset`) automatically derives the target columns and builds a
> training-ready table `ml_dataset` (PostgreSQL) + a parquet export.
>
> **Locked decisions (Yashas, 2026-08-19):**
> 1. Direction label = **binary** (1 = up tomorrow, 0 = down tomorrow).
> 2. **Capture open interest (OI)** from bhavcopy during the re-download.
> 3. Feature scope = **all 3 tiers** (incl. chain aggregates).
> 4. Output = **PG table + parquet export**.
>
> **Audience:** executor-ready spec (agent-agnostic). All paths relative to repo root
> `/mnt/storage/Study/Major Project/Code/MktMtm` (athena) ≡ `/mnt/arch/Major Project/Code/MktMtm` (olympus).
> Runs happen **on athena** (uv at `~/.local/bin/uv`, PG at 127.0.0.1:5432).

---

## Contract — the `ml_dataset` schema (what gets built)

One row per (symbol, trading day) in `option_metrics`. ~45 columns: identity + features + targets.

```sql
CREATE TABLE IF NOT EXISTS ml_dataset (
    symbol            TEXT PRIMARY KEY NOT NULL,  -- composite with date; see PK below
    ...
);
```

**Full column list** (types FLOAT unless noted):

| Group | Columns |
|---|---|
| Identity | `symbol TEXT`, `date DATE` — **PK (symbol, date)** |
| Level | `underlying_price`, `interest_rate`, `rv_yz` |
| CE / PE (30d ATM) | `ce_iv_30`, `pe_iv_30`, `ce_iv_60`, `pe_iv_60`, `ce_iv_90`, `pe_iv_90`, `ce_ivp`, `pe_ivp`, `ce_ivr`, `pe_ivr`, `ce_delta`, `pe_delta`, `ce_gamma`, `pe_gamma`, `ce_theta`, `pe_theta`, `ce_vega`, `pe_vega`, `ce_rho`, `pe_rho`, `ce_close`, `pe_close`, `ce_volume`, `pe_volume`, `ce_oi`, `pe_oi` |
| Momentum / structure (Tier 2) | `ret_1d`, `ret_5d`, `ret_10d`, `ret_20d`, `iv_rv_spread`, `term_slope`, `put_call_iv_spread`, `days_to_expiry_30d INT`, `days_to_earnings FLOAT NULL` |
| Chain aggregates (Tier 3) | `atm_straddle`, `skew_25d`, `put_call_vol_ratio`, `put_call_oi_ratio`, `otm_put_iv_premium`, `chain_volume`, `chain_oi`, `chain_oi_chg`, `chain_breadth` |
| **Targets** | `next_day_return FLOAT NULL`, `next_day_direction SMALLINT NULL` |

Indexes: `(date)`, `(next_day_direction)` (fast chronological train/test splits).

**Target derivation (the core rule):**
- Per symbol, sort by `date`; `next_day_return = underlying_price.shift(-1) / underlying_price − 1`.
- `next_day_direction = (next_day_return > 0).astype(int)`.
- **Last day of each symbol → both targets NULL** (no tomorrow yet; those rows are future inference data, never training rows).

**Incremental safety:** the builder is idempotent — re-running after new data lands upserts
(`ON CONFLICT (symbol, date) DO UPDATE`), filling targets for rows that were previously the
"last day". This is what makes the flow automatic on every refresh.

---

## Phase 1 — Capture open interest (OI)

**Why:** bhavcopy files (legacy **and** UDiFF) carry `OPEN_INT`/`CHG_IN_OI` per contract;
the loader currently drops them. OI powers the strongest new features (`put_call_oi_ratio`,
`chain_oi`, ATM-contract `oi`).

### Changes

1. **`src/mktmtm/data/bhavcopy.py`**
   - `UDIFF_COLUMN_MAP` (line ~41): add
     `"OpnIntrst": "OPEN_INT"` and `"ChngInOpnIntrst": "CHG_IN_OI"`.
   - Legacy files already have `OPEN_INT`/`CHG_IN_OI` → pass through untouched
     (`normalize_bhavcopy_columns` is a no-op for legacy; `filter_and_save_csv` keeps all columns).
2. **`src/mktmtm/pipeline/process.py`**
   - `_build_chain_rows` (line ~221): add `"oi": int(row["OPEN_INT"])`, `"chg_oi": int(row["CHG_IN_OI"])`.
   - CE/PE block (line ~490): add `"oi"` for the 30d ATM contract (`ce30`/`pe30` rows).
3. **`src/mktmtm/storage/etl.py`** — has its **own** chain builder + CE/PE block
   (lines ~226, ~280). Apply the same two additions so `process` and `etl` outputs stay in sync.
   *(If `etl_chunked.py` shares this code, it follows automatically.)*

### Tests
- `tests/unit/test_bhavcopy.py`: UDiFF fixture gains `OpnIntrst`/`ChngInOpnIntrst` →
  assert mapped to `OPEN_INT`/`CHG_IN_OI`.
- `tests/pipeline/test_process.py`: chain-row fixture asserts `oi`/`chg_oi` present.

### Checkpoint & exit test
`uv run pytest tests/unit/test_bhavcopy.py tests/pipeline/test_process.py` green.
Then one live day: `uv run mktmtm fetch-bhavcopy --start-date 2026-08-14 --end-date 2026-08-14`
→ filtered CSV contains `OPEN_INT`; one symbol re-processed → `option_chain[*].oi` present.

### Rollback
Revert the 3 file edits above; no schema change was made.

---

## Phase 2 — `ml_dataset` table + `make-dataset` command

### Changes

1. **`src/mktmtm/storage/sql/init_ml_dataset.sql`** (new) — DDL per the contract above,
   wired into `init_db.sql` (`\ir`).
2. **`src/mktmtm/ml/`** (new package):
   - `__init__.py`
   - `dataset.py` — three functions:
     - `load_option_metrics(conn, symbols=None, limit_dates=None) -> pd.DataFrame`
       (read `option_metrics`; `limit_dates` bounds by most-recent N dates for smoke runs)
     - `compute_chain_aggregates(conn, symbols=None, limit_dates=None) -> pd.DataFrame`
       — **one SQL pass** over `option_chain` via `jsonb_array_elements`, `GROUP BY symbol, date`:
       `atm_straddle` (CE+PE settle at strike nearest spot, 30d expiry), `skew_25d`
       (IV at |delta|≈0.25 OTM put − call), `put_call_vol_ratio`, `put_call_oi_ratio`,
       `otm_put_iv_premium` (5% OTM put IV − ATM IV), `chain_volume`, `chain_oi`,
       `chain_oi_chg`, `chain_breadth`. (SQL = C-speed over ~186K rows × ~1,700 chain elements.)
     - `build_dataset(conn, symbols=None, limit_dates=None) -> pd.DataFrame` — pandas for
       everything else: lift CE/PE JSONB → flat columns; per-symbol `groupby().shift()` for
       `ret_1d/5d/10d/20d` and both **targets**; spreads/term structure; `days_to_*` from
       date columns; merge chain aggregates; return the full frame.
   - `export_parquet(df, path)` — `df.to_parquet(path, index=False)`.
   - `write_dataset(conn, df)` — batched upsert `ON CONFLICT (symbol, date) DO UPDATE`
     (reuse the `DBWriter` pattern in `storage/db.py`).
3. **`src/mktmtm/cli.py`** — new command:
   ```
   uv run mktmtm make-dataset
       [--limit-symbols A,B]        # smoke: subset of symbols
       [--limit-dates N]            # smoke: most recent N dates
       [--export-parquet PATH]      # default data/processed/ml_dataset.parquet
       [--no-export]                # PG only
   ```
   Flow: connect PG → build → upsert → export → echo row/target counts.

### Tests
- `tests/unit/test_ml_dataset.py`: target derivation (correct shift, **symbol isolation** —
  interleaved dates across 2 symbols must not leak, last row NULL, zero-return edge), Tier-2
  features (returns, spreads, `days_to_*`), chain aggregates on a tiny chain fixture
  (`put_call_vol_ratio`, `skew_25d`, `atm_straddle`, breadth).
- `tests/integration/test_ml_dataset.py`: build over `--limit-symbols 2 --limit-dates 10`
  against real PG (same pattern as `test_db_conn.py`); assert rows = Σdays, non-null targets = Σ(days−1).

### Checkpoint & exit test
`uv run pytest` full suite green. Smoke run on athena:
```
uv run mktmtm make-dataset --limit-symbols NIFTY,TATASTEEL --limit-dates 30
```
Exit checks: row count = 30×2 with 29×2 non-null targets; NIFTY last row has NULL targets;
spot-check `next_day_return` for one known date pair against
`underlying_price[t+1]/underlying_price[t] − 1` (manual calc); parquet loads via pandas.

### Rollback
`DROP TABLE IF EXISTS ml_dataset;` + remove the new `ml/` package and CLI command
(no changes to existing tables/files).

---

## Phase 3 — Full re-download, docs, verification

1. **README.md**: add `make-dataset` to Features, pipeline order, and CLI table.
2. **`docs/data/database-report.md`**: add `ml_dataset` to the schema section.
3. **Full run on athena** (this is the expensive step — one shot, order matters):
   ```bash
   uv run mktmtm fetch-bhavcopy                  # full range, now with OI
   uv run mktmtm fetch-yahoo
   uv run mktmtm process
   uv run mktmtm etl
   uv run mktmtm make-dataset                    # full build + parquet
   ```
4. **Verification report:** row count ≈ 186K; non-null targets ≈ 186K − 222;
   target class balance (≈% up days); per-feature null counts (expect: OI nulls only where
   legacy files lacked them → should be 0 after full re-download; `days_to_earnings` nulls
   where no earnings in horizon); parquet shape matches PG row count.

### Rollback
Docs-only phase; revert README/database-report edits if needed.

---

## Notes & risks

- **Order matters:** OI features are only populated if the *bhavcopy re-download* happens
  after Phase 1 lands. Partial re-downloads would mix OI-rich and OI-poor rows
  (`make-dataset` tolerates NULLs, but the full re-download is the clean path).
- `process.py` and `etl.py` both build chain/CE-PE snapshots — keep them in sync (Phase 1
  touches both; Phase 2 reads only `option_metrics`, so it is agnostic to which one ran).
- `days_to_earnings` will be NULL for symbols/dates without an upcoming earnings date —
  impute at training time (e.g. 999 or median), stored NULL.
- `underlying_price` is included for reference/inference display; the models should consume
  `ret_*` for price signal (absolute levels are not comparable across symbols).
- Overnight gap / India VIX / futures basis are **future** additions (noted in the data
  report §8) — out of scope here.
