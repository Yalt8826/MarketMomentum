# Five-Year Build — ETL Integrity Change Report

> **Scope:** audit of the completed five-year extraction/ETL build, the two defects it
> exposed, the code changes made in response, and the procedure to re-run.
> **Snapshot:** 2026-08-31. All figures verified live against PostgreSQL and
> `data/processed/ml_dataset.parquet` before any change was made.
> **Server:** PostgreSQL · `options_db` · `localhost:5432` · SSLMODE=disable.
> **Branch:** `fix/etl-cache-offsets-and-spot-basis` — `dd11945` (pre-change checkpoint),
> `1c20789` (fixes).
> **Companion:** `iv-integrity-audit.md` §7 records the same two defects as F8/F9 in the
> numbered fix series; this report covers the change set end to end.

---

## 0. Verdict up front

The five-year run completed and `scripts/verify_db_health.py` returned **PASS**. The build
was **not trainable**. Two defects, neither visible to the gate as it stood:

| # | Defect | Effect on the delivered build | Requires |
|---|---|---|---|
| **F8** | ETL cache offset map computed with `np.searchsorted` over an unsorted array | 75 of 222 symbols silently dropped; 3 built from *other companies'* option chains | ETL re-run |
| **F9** | Yahoo's "raw" close is itself split/bonus-adjusted, so F2/F3 removed the wrong adjustment | strike/spot basis mismatch persists for pre-corporate-action history; 17.7% of `ce_iv_30` unusable | ETL re-run |

Both are baked into `option_metrics` at ETL time, so `make-dataset` alone cannot repair
them. The raw bhavcopy and Yahoo files on disk are sound — no re-download is needed.

What was **not** wrong: the dataset's structural integrity held up completely. No
look-ahead, no duplicate keys, the corporate-action screen working, sane target
distribution. Those are recorded in §4.

---

## 1. What the build delivered

| Metric | Previous build (audit §0) | This build | |
|---|---|---|---|
| `option_metrics` symbols | 217 | **143** | −74 |
| `option_metrics` rows | 212,479 | **169,974** | −42,505 |
| Date range | — | 2021-08-02 → 2026-08-28 | 1,254 trading days |
| `ml_dataset` rows | 212,479 | 169,974 | matches parquet (51 cols) |
| Labeled rows | — | 169,810 (99.9%) | 91 last-day + 73 screened |

The health gate compared `ml_dataset` against `option_metrics` — both produced by the same
run — and never against the catalog those symbols were supposed to come from. A uniform
34% shortfall therefore looked internally consistent.

74 of the 79 absent symbols had complete Yahoo history on disk, including TCS, WIPRO,
TITAN, ULTRACEMCO, TRENT, DMART, VEDL and SHRIRAMFIN. Traced end to end, TCS had 398,491
cache rows across 1,254 days, **all 1,254 fully processable**. It was dropped anyway.

The remaining 5 (FINNIFTY, MIDCPNIFTY, LTIM, TATAMOTORS, PEL) have no
`_processed.json` and are legitimately absent — Yahoo serves no working ticker for them.

---

## 2. F8 — the cache offset map silently dropped 75 symbols

### Root cause

`build_bhavcopy_cache` writes the text columns as pandas `category` (dictionary encoding
keeps the 881 MB cache small), and each of the 1,254 row groups carries its own dictionary.
Reading that back yields a Categorical whose categories follow **first-appearance order
across row groups**, not alphabetical order:

```
arrow schema SYMBOL type: dictionary<values=string, indices=int16, ordered=0>
pandas dtype after read:  category
categories alphabetically sorted?  False
```

`_load_cache` then did:

```python
df = df.sort_values(["SYMBOL", "DATE"])     # orders by category CODE, not alphabetically
sym_col = df["SYMBOL"].to_numpy()           # -> array is NOT alphabetically sorted
starts = np.searchsorted(sym_col, syms)     # searchsorted is only defined on sorted input
```

`np.searchsorted` on unsorted input returns arbitrary positions. Because the category order
happens to be *nearly* alphabetical, most symbols landed correctly and the rest failed
silently — which is why the run looked successful.

### Blast radius (reproduced against the real cache)

| Span outcome | Symbols |
|---|---|
| Correct | 144 |
| **Empty → silently dropped** | **75** |
| **Wrong → holds other symbols' rows** | **3** |

| Symbol | True rows | Rows the ETL read | Of which its own | Foreign symbols pulled in |
|---|---|---|---|---|
| TATASTEEL | 306,540 | 36,985 | **0** | ATHERENERG, GVT&D, MAHABANK, NIFTYFPI |
| TATAPOWER | 251,413 | 227,602 | **0** | ADANIPOWER, BAJAJHLDNG, COCHINSHIP |
| TATAMOTORS | 232,916 | 9,871,526 | 232,916 | 360ONE, ADANIENSOL, AMBER |

TATASTEEL's 103 delivered rows were built entirely from other companies' option chains
priced against TATASTEEL's spot — 100% of them show `|strike/spot−1| > 10%`. TATAPOWER
delivered 209 such rows. The failure was invisible because `_iter_symbol_days` simply
`return`s on an empty span and the worker never raises.

### Fix

- Cast `SYMBOL` to `str` before sorting so the sort order and the search order agree.
- Assert the array is sorted before `searchsorted`; raise loudly otherwise.
- `_iter_symbol_days` raises if a slice holds foreign symbols, rather than writing mixed data.
- `_process_symbol` returns its row count; `main` names any zero-row symbol at **WARNING**
  and logs a `wrote rows for N/M symbols` summary. A silent debug line is how 75 dropped
  symbols survived a full five-year run.

The cache *file* is fine — only the reader was broken.

---

## 3. F9 — the "raw" price series is still split/bonus-adjusted

### Root cause

F2/F3 introduced a dual price series: `underlying_price` (raw, for strike/IV) and
`underlying_price_adj` (adjusted, for returns/labels). The plumbing landed correctly —
`underlying_price_adj` is populated and differs from `underlying_price` on 92.5% of rows.

But Yahoo's `indicators.quote.close`, stored as the raw series, is **itself adjusted for
splits and bonuses**. Only the *dividend* adjustment separates it from `adjclose`. F2/F3
therefore removed the adjustment that does not matter for strike matching and left the one
that does.

### Evidence — against the genuinely unadjusted NSE futures close, 2021-08-02

| Symbol | Stored "raw" spot | NSE FUT close | Ratio | Corporate action |
|---|---|---|---|---|
| RELIANCE | 956.46 | 2,093.40 | **2.19×** | 1:1 bonus |
| KOTAKBANK | 333.02 | 1,680.40 | **5.05×** | 1:5 split |
| NESTLEIND | 885.73 | 18,084.85 | **20.42×** | 1:10 split |
| ITC | 199.61 | 209.50 | 1.05× | none — carry + dividend only |

The original monotone-decay-backward signature was intact:

| Year | `\|strike/spot−1\| > 10%` | `ce_iv_30` NULL | Usable `ce_iv_30` |
|---|---|---|---|
| 2021 | 18.12% | 24.9% | 72.1% |
| 2022 | 15.62% | 21.3% | 75.9% |
| 2023 | 14.27% | 16.8% | 80.2% |
| 2024 | 11.10% | 14.2% | 84.0% |
| 2025 | 5.54% | 11.8% | 87.2% |
| 2026 | 0.63% | 7.0% | 93.0% |
| **All** | **10.98%** | **15.8%** | **82.3%** |

### Why the gate missed it

F1 (IV solver returns `None` instead of `0.0`/`500.0` sentinels) works as designed, so
corruption now surfaces as `NULL`. The gate's hygiene test counted only
`ce_iv_30 <= 0 OR ce_iv_30 > 200` — and `NULL` matches neither. It reported 2.0% garbage
against a true 17.7% unusable. The 2026-08-30 one-year validation looked perfect for the
same reason: 2026 has almost no corporate actions behind it.

### Fix

New `options.chain.unadjusted_spot_from_bhavcopy` takes spot from the bhavcopy itself, so
spot and strikes share one basis by construction:

1. `UNDERLYING_PRICE` — NSE's own underlying value, the UDiFF `UndrlygPric` column
   (post 2024-07-08);
2. near-month futures close discounted by carry, `S = F·exp(−rT)` — the legacy era has no
   underlying column, and the futures close is the only unadjusted proxy in the file.

Returns `None` when neither is available; the caller skips the day rather than fall back to
an adjusted price. Yahoo `adjclose` still supplies `underlying_price_adj` for returns and
labels — that part was always correct.

`storage.etl` and `pipeline.process` both call it, keeping the two enrichment paths in step
as CLAUDE.md requires.

---

## 4. What was already healthy (unchanged)

Verified on the delivered parquet before any change:

| Check | Result |
|---|---|
| `validate_no_lookahead` | **PASS** |
| `corr(ret_1d, next_day_return)` | **−0.0022** (want ~0) |
| Duplicate `(symbol, date)` | **0** |
| `\|next_day_return\| > 0.30` | **0** — corporate-action screen working |
| Target distribution | mean +0.00070, std 0.01909, min −0.199, max +0.193 |
| Class balance | 51.0% up; by year 0.499–0.531 |
| Parquet vs DB | 169,974 rows both sides, 51 columns |

The structural work from the earlier fix series holds. Both defects here are upstream of it.

---

## 5. Secondary changes

| Item | Change |
|---|---|
| **6 `Decimal`/object columns** — `put_call_vol_ratio`, `put_call_oi_ratio`, `chain_volume`, `chain_oi`, `chain_oi_chg`, `chain_breadth` | `compute_chain_aggregates` casts every non-key column with `pd.to_numeric`. psycopg2 maps `SUM(bigint)`/`AVG()` to `decimal.Decimal`, which lands as an object column and propagates into every derived ratio. |
| **Health gate blindness** | Now **fails** on catalog symbols absent from `option_metrics`; counts `ce_iv_30 IS NULL` as unusable per year alongside the range test; **fails** when `\|strike/spot−1\| > 10%` on more than 2% of rows. Either defect would have been caught by these. |
| **Stale test assertion** | `test_process.py::test_pipeline_produces_populated_nifty_timestamp` expected `ivp == 100.0`. F6 raised the IV window to 252 with 60 min-periods, so a single-day fixture is correctly `None`. Confirmed failing at the checkpoint commit — pre-existing, not introduced here. |
| **Type annotation** | `_process_symbol`'s `spot_price_map` was annotated `dict[str, dict[str, float]]` but receives the nested `{"adjusted", "raw"}` map from F2/F3. Corrected. |

---

## 6. Code changes

| File | Change |
|---|---|
| `src/mktmtm/storage/etl.py` | `_load_cache` str-cast + sortedness assertion; `_iter_symbol_days` foreign-symbol guard; `_process_symbol` returns row count; `main` reports zero-row symbols at WARNING; spot from the shared helper |
| `src/mktmtm/options/chain.py` | new `unadjusted_spot_from_bhavcopy` |
| `src/mktmtm/data/bhavcopy.py` | `UndrlygPric` → `UNDERLYING_PRICE` in `UDIFF_COLUMN_MAP` and `_CACHE_COLUMNS` |
| `src/mktmtm/pipeline/process.py` | same spot source, keeping the legacy path in step |
| `src/mktmtm/ml/dataset.py` | chain-aggregate `Decimal` → float |
| `scripts/verify_db_health.py` | three new gate checks (§5) |
| `tests/unit/test_etl_cache.py` | **new** — 10 regression tests |
| `tests/pipeline/test_process.py` | stale `ivp` assertion corrected |

---

## 7. Verification performed

- **Full suite:** 139 tests pass (`pytest -m "not integration"`).
- **New tests are meaningful:** reverting the `astype(str)` line fails 4 of them.
- **Lint:** `ruff check src/mktmtm tests` clean.
- **Types:** mypy 20 → 16 pre-existing errors; **zero introduced**. The 16 remaining sit in
  `etl_chunked.py` and two untouched call sites.
- **Real-data smoke test** — cache built from 5 real bhavcopy files spanning both schema
  eras, exercising `build_bhavcopy_cache` → `_load_cache` → `unadjusted_spot_from_bhavcopy`:

```
cache: 212,329 rows, 260 symbols
1) offset map: 260/260 symbols exact, 0 broken
2) derived spot vs traded strike range:
   KOTAKBANK  02-Aug-2021   1660.4  futures  [1280 .. 2420]  near=1660   OK
   NESTLEIND  02-Aug-2021  17689.6  futures [14100 .. 20200] near=17700  OK
   RELIANCE   02-Aug-2021   2067.7  futures  [1360 .. 2680]  near=2060   OK
   NIFTY      04-Aug-2025  24722.8  undrlyg [12000 .. 31000] near=24700  OK
```

Every derived spot lands within 2% of an actually-traded strike, in both eras and through
both source paths — against Yahoo's 333.02 for KOTAKBANK and 885.73 for NESTLEIND.

---

## 8. Re-run procedure

The tables are **truncated, not upserted over**: every row's strike, chain, IV and Greeks
change under the new spot basis, and TATASTEEL/TATAPOWER's corrupt rows would otherwise
survive at any date the correct run does not reproduce.

```bash
cd "/mnt/storage/Study/Major Project/Code/MktMtm"

# 1. clear the derived tables (symbol_data / backtest_results untouched)
psql -h localhost -U yashas -d options_db -c "TRUNCATE option_metrics, ml_dataset;"

# 2. rebuild the parquet cache — REQUIRED, the schema gained UNDERLYING_PRICE (~10 min)
uv run mktmtm preparse-bhavcopy

# 3. ETL (3-5 h for five years)
uv run mktmtm etl 2>&1 | tee etl_rerun.log

# 4. rebuild the training matrix
uv run mktmtm make-dataset

# 5. gate
uv run python scripts/verify_db_health.py
```

No `fetch-bhavcopy` / `fetch-yahoo` — the raw files on disk are sound.

## 9. Acceptance criteria

| Check | Expected |
|---|---|
| ETL summary line | `ETL wrote rows for 217/222 symbols` |
| Zero-row symbols | Exactly 5 — FINNIFTY, MIDCPNIFTY, LTIM, TATAMOTORS, PEL (no Yahoo `_processed.json`, so no `underlying_price_adj` for labels) |
| `option_metrics` | ~217 symbols / ~212k rows |
| `\|strike/spot−1\| > 10%` | Near 0 in **every** year, not just recent ones |
| Usable `ce_iv_30` | ~93% flat across years, not sloping 72% → 93% |
| `corr(ce_iv_30, pe_iv_30)` | Strongly positive; median CE/PE gap ~2 vol points |
| Health gate | PASS, with the three new checks armed |

A residual slope in the per-year `ce_iv_30` or strike/spot figures means the spot basis is
still wrong somewhere — that shape is the signature, and it is the thing to look at first.

---

## 10. Not addressed (deliberate)

These are modelling decisions or pre-existing debt, left for the training loader:

- **`interest_rate` is constant within 100% of dates** — zero cross-sectional signal. Drop
  it for cross-sectional models, or convert to a level-vs-trailing-history feature.
  `days_to_expiry_30d` is flat on 83.6% of dates (fine time-series, useless cross-sectionally).
- **Non-stationary levels** — `underlying_price`, `atm_straddle` raw; needs per-symbol
  normalization or cross-sectional ranking.
- **Index rows mixed with stocks** — 1.47% of rows, different vol regime; segment or exclude.
- **PE-side NULLs ran 17–22%** — the F4 IV gate should cover both legs, not just `ce_iv_30`.
- **16 pre-existing mypy errors**, mostly in `etl_chunked.py` (the unused sharded variant).
- **`rv_yz` is computed from near-month futures OHLC**, which rolls monthly and is
  unadjusted — both introduce discontinuities. Pre-existing behaviour, unchanged here, but
  worth revisiting if realized-vol features underperform.
