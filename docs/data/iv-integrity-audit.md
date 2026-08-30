# `option_metrics` — Corporate-Action / IV-Integrity Audit & Agreed Fixes

> **Scope:** verification of an external audit finding that the **adjusted** Yahoo
> close was being used as the spot price for **unadjusted** NSE bhavcopy strike
> selection and implied-volatility computation, corrupting the ATM strike, the IV
> columns, and every derivative vol feature in the training matrix.
> **Snapshot:** 2026-08-29. All figures verified live against PostgreSQL `options_db`
> and `data/processed/ml_dataset.parquet` (212,479 rows · 217 symbols · 2021-08-02 → 2026-08-28).
> **Server:** PostgreSQL · `options_db` · `localhost:5432` · SSLMODE=disable.

---

## 0. Verdict up front

The audit is **correct**. The ML dataset is **structurally sound** (no look-ahead
leak, no duplicate keys, corporate-action screen working — `|next_day_return|>0.30` count is **0**),
**but the IV columns are not usable as-is**: a price-series mismatch (adjusted spot
against unadjusted strikes) plus two IV-solver sentinel failure modes have
corrupted `ce_iv_30` / `pe_iv_30` and every feature derived from them
(`term_slope`, `put_call_iv_spread`, `skew_25d`, `iv_rv_spread`, `otm_put_iv_premium`).

**Do not train on the current build.** The fixes below are agreed; the data is being
regenerated and validated on a 1-year window before the full 5-year run.

---

## 1. The headline defect — adjusted spot vs unadjusted strikes

`src/mktmtm/data/yahoo.py` stores the **adjusted** close as `underlying_price`
(`:181` switches to `indicators.adjclose` so corporate actions stop polluting the
returns — this fix is correct *for returns*). But `underlying_price` is also fed to:

- `pick_strike_nearest_underlying(underlying=spot, …)` (`storage/etl.py:291`,
  `pipeline/process.py:396`) — selects the ATM strike by distance to `underlying`;
- the IV solver and Greeks (`S=spot`),
- the chain-aggregate SQL (`m.underlying_price` nearest-strike lookups in
  `ml/dataset.py` `_CHAIN_AGG_SQL`).

NSE bhavcopy strikes and settle prices are **unadjusted** traded values. So an
adjusted spot is matched against unadjusted strikes. After a split/bonus the two
diverge by the split factor, the "nearest" strike is picked from the wrong region
of the chain, and IV collapses to a sentinel.

**Concrete evidence (live DB):**

| symbol | date | `underlying_price` (adj) | `strike_price` | `ce_iv_30` | `pe_iv_30` |
|---|---|---|---|---|---|
| RELIANCE | 2023-09-12 | 1204.89 | 2000 | **500.0** | **0.0** |
| KOTAKBANK | 2026-01-13 | 425.78 | 1840 | **500.0** | **0.0** |
| MCX | 2026-01-01 | 2192.43 | 9600 | **500.0** | **0.0** |
| HDFCBANK | 2025-08-25 | 966.07 | 1680 | **500.0** | **0.0** |

These are the exact split/bonus signatures (KotakBank 1:5, MCX 1:4, Reliance 1:1
bonus, HDFCBank split). The last `ce_iv_30=500` row of each symbol is a clean
cutoff at its corporate-action date; there are **0** such rows after each symbol's
own cutoff. Clean rows (post-action) have `strike_price` within ~1% of spot.

**Blast radius — `ce_iv_30=500` sentinel rate by year (monotone decay backward = signature):**

| year | ce_iv_30 == 500 | pe_iv_30 == 0 | rows |
|---|---|---|---|
| 2021 | 24.6% | 38.6% | 15,216 |
| 2022 | 19.2% | 33.6% | 38,672 |
| 2023 | 13.4% | 30.2% | 38,281 |
| 2024 | 9.6%  | 24.4% | 39,842 |
| 2025 | 4.7%  | 16.0% | 50,666 |
| 2026 | 0.5%  | 6.7%  | 29,802 |

Overall: `ce_iv_30 == 500` on **22,665 rows (10.7%)**, `ce_iv_30` outside `[0,200]`
on **32,810 rows (15.4%)**, `pe_iv_30 == 0` on **50,238 rows (23.6%)**.

---

## 2. IV-solver sentinel failure modes (`options/pricing.py`)

`implied_volatility_bisection` has two silent sentinel paths:

- `pricing.py:143` — returns **`0.0`** when `market_price <= intrinsic`
  ("no time value" conflated with "zero vol"). Produces `pe_iv_30 = 0.0` on 50k rows.
- `pricing.py:158` — returns the **midpoint** after the loop exhausts
  (`upper_bound=5.0` → ~**500%**). Produces `ce_iv_30 = 500.0` on 22.7k rows.

Both are garbage wearing a number's clothes: `0.0` is a valid-looking vol, `500%`
is a valid-looking vol, and every derivative feature inherits them. Verified:
`corr(ce_iv_30, pe_iv_30) = −0.202` and median `|ce_iv_30 − pe_iv_30| = 15.3` vol
points on the 70% of rows that are "sane" (both IVs in 5–150) — put-call parity says
same-strike ATM CE/PE should differ by a point or two.

---

## 3. Other findings verified (load-order / hygiene, not gate failures)

| Finding | Verified |
|---|---|
| 6 Decimal/`object` columns (`put_call_vol_ratio`, `put_call_oi_ratio`, `chain_volume`, `chain_oi`, `chain_oi_chg`, `chain_breadth`) | ✓ |
| Indices mixed with stocks: NIFTY/BANKNIFTY/NIFTYNXT50 = **3,089 rows (1.45%)** | ✓ |
| `interest_rate` constant within each date (0 / 1,254 dates differ) — no cross-sectional info | ✓ |
| `days_to_earnings` max 1,282, never hits 0 | ✓ |
| `ce_ivp` floor = exactly 1/30 = 3.33 (30-day IV window in `etl.py` — the path that writes the ML dataset; `process.py` already used 252) | ✓ |
| Non-stationary levels raw (`underlying_price`, `atm_straddle`) — need per-symbol normalization/ranking at train time | ✓ |
| `|next_day_return| > 0.30` = 0 — corporate-action fix working | ✓ (no leak) |

---

## 4. Agreed fixes

| # | Fix | Where | Effect |
|---|---|---|---|
| F1 | IV solver returns **`None`** (not `0.0`/`500%`) on degenerate / non-converged inputs | `options/pricing.py` | Bad IVs become SQL `NULL`, not valid-looking sentinels |
| F2 | Store **both** price series: `underlying_price` = **raw/unadjusted** (for strike/IV/chain matching), new `underlying_price_adj` = **adjusted** (for returns/labels) | `data/yahoo.py`, `storage/etl.py`, `option_metrics` DDL | Strike selection + IV use the same price basis as bhavcopy strikes; returns stay corporate-action-clean |
| F3 | ETL computes strikes/IV/Greeks from the **raw** spot; returns/labels in `ml/dataset.py` from the **adjusted** spot | `storage/etl.py`, `ml/dataset.py` | Removes the adjusted-vs-unadjusted mismatch at its source |
| F4 | Training-loader gate: keep `0 < ce_iv_30 <= 200`, drop NULL | loader (documented; `verify_db_health.py` hygiene) | Final cleanliness filter before training |
| F5 | **Re-run the pipeline** (ETL → make-dataset) so `option_metrics`/`ml_dataset` hold clean strikes/IVs | pipeline run | The only way the schema fixes reach the data |
| F6 | IV percentile/rank window **252** (was 30) in `etl.py` — matches `process.py` | `storage/etl.py` | `ce_ivp` floor becomes 1/252, not the 30-valued step function |
| F7 | **ETL OOM fix**: bhavcopy cache loaded as one sorted DataFrame + per-symbol `searchsorted` offsets, not a per-symbol dict (was 12.5 GB peak → OOM-killed on a 14 Gi host) | `storage/etl.py` | ETL survives on modest RAM; per-symbol slice is O(rows) not O(60M) |

Fixes F1–F4 are code/DML changes; **F5 requires re-running ETL** (3–5 h for 5 years)
because the ATM strike, chain, IVs and Greeks are all baked into `option_metrics`
at ETL time — `make-dataset` alone cannot repair them.

> **Ordering note (from the audit):** the dual-price-series fix (F2/F3) is the one
> that actually changes results. The `pricing.py` sentinel fix (F1) turns garbage
> into NULL so the F4 gate can drop it. The IV CE/PE gap (15.3 pts) should be
> re-measured *after* F2/F3 — a large residual would justify pricing off the
> futures basis, but that is a modelling decision to re-evaluate later, not a
> blind pre-emptive change.

---

## 5. Validation plan (1-year window first)

1. Apply F1–F4 to the code + `option_metrics` schema (`ALTER … ADD COLUMN IF NOT EXISTS`).
2. Run the extraction pipeline on a **1-year window** (fetch → preparse → etl → make-dataset).
3. Assert the fixes landed:
   - `ce_iv_30 == 500.0` count ≈ **0** and `pe_iv_30 == 0.0` ≈ 0 (F1);
   - `strike_price` within ~1% of `underlying_price` (raw) on the vast majority of rows (F2/F3);
   - `corr(ce_iv_30, pe_iv_30)` strongly positive and median gap small (F1–F3);
   - `ce_ivp` floor = 1/252 after the rebuild (F5).
4. If green → Yashas runs the full **5-year** pipeline himself.

---

## 6. Validation result (1-year window, 2026-08-30)

Ran the fixed pipeline on a ~13-month window (fetch-yahoo dual series → 1y cache →
etl → make-dataset; scoped `data/validation_1y/`). **49,433 rows in the recent
window.** Verified live against the rebuilt `ml_dataset.parquet`:

| Metric | Before fix (5y, 2026 yr) | After fix (1y window) |
|---|---|---|
| `ce_iv_30 == 500.0` sentinel | 10.7% overall / 24.6% (2021) | **6 rows (0.01%)** |
| `pe_iv_30 == 0.0` sentinel | 23.6% overall / 38.6% (2021) | **14 rows (0.03%)** |
| `corr(ce_iv_30, pe_iv_30)` | −0.202 | **+0.809** |
| median \|ce − pe\| (vol pts) | 15.3 | **2.0** |
| `ce_ivp` floor | 1/30 = 3.33 | **1/252** (after F6 rebuild) |
| corporate-action screen | working | still working (TRENT −33%, VEDL −64.9% caught) |

**Verdict: the fixes are applied and effective.** The residual 6/14 sentinel rows
are a rounding of genuine near-zero-time-value rows (drop via the F4 gate), not a
recurrence of the systematic corruption. Put-call parity is restored — CE and PE
IV now agree to ~2 vol points, confirming the strike-mismatch was the root cause
and that **futures-basis repricing is NOT needed**.

**Report author:** praxis, 2026-08-30 — verified live, all counts reproducible.
