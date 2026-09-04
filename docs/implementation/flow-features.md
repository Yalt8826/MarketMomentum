# Per-Symbol Flow Features — Outlier Strikes and Futures Basis

> **Goal:** Recover a per-symbol *fingerprint* of institutional activity from the anonymous
> option chain, then check it against `participant_oi` — the one series that actually names
> the participants, but only market-wide.
> **Branch:** `feat/gex-dealer-positioning`.
> **Data:** the bhavcopy parquet cache (60.6M rows) + `option_metrics`. No new download,
> no ETL re-run.
> **Built:** 2026-09-04. `flow_features`, 212,441 rows × 20 features, full five-year build
> in **2m01s**.
> **Status:** BUILT AND EVALUATED. One feature survives; the other family does not
> predict returns but does track institutional flow. See §5.

---

## 0. Why these two, and what the join makes possible

`participant_oi` (acquired 2026-09-04, see `docs/data/participant-oi-report.md`) says what
institutions did, but it is market-wide: one row per participant type per day, no symbols.
Joined to `ml_dataset` on date alone it is constant within a date and contributes exactly
zero cross-sectional signal. The option chain has per-symbol, per-strike detail but nobody's
name attached.

Neither alone answers *which symbol* institutions are in. The design here is to build
per-symbol features from the anonymous chain, then **aggregate them to market level and
correlate against the named series** — the one join the market-wide data legitimately
supports. That is not proof (an aggregate can work while the per-symbol version fails), but
it is a real external check against data that was never used to construct the feature.

Two families were built:

**`oi_outlier_*`** — strikes whose open-interest change is extreme against the other strikes
of the same expiry that day. Every existing chain feature (`chain_oi_chg`, `oi_hhi`,
`otm_put_oi_build`) is a sum or an average over ~300 strikes, and averaging is precisely what
destroys a localised signal: nobody accumulates 50,000 contracts of a 15%-OTM call by
accident, but that build is invisible once summed alongside the at-the-money churn. The
output is deliberately sparse and *pointable* — a date, a symbol, a strike.

**`fut_*`** — near-month futures basis measured against cost of carry, plus the near-to-next
roll. This is the family with a direct external counterpart: `participant_oi.net_fut_stock`
is literally FII and Pro futures positioning, the same quantity from an independent source.

## 1. Where it lives

| | |
|---|---|
| Module | `src/mktmtm/features/flow.py` |
| Table | `flow_features`, PK `(symbol, date)` — `src/mktmtm/storage/sql/init_flow_features.sql` |
| Parquet | `data/processed/flow_features.parquet`, written from the same frame as the table |
| CLI | `mktmtm flow-features [--limit-dates N] [--z-threshold Z] [--min-share S] [--no-parquet]` |
| Tests | `tests/unit/test_flow_features.py` — 25 tests |

`flow_features` is deliberately **not** new `DATASET_COLUMNS`. These are candidates under
evaluation; promoting one into `ml_dataset` means editing `DATASET_COLUMNS` *and*
`init_ml_dataset.sql` and re-running `make-dataset`, which should happen once a feature has
earned it. The table is at `ml_dataset`'s exact grain — the build produced 212,441 rows,
matching `ml_dataset` row-for-row — so it joins one-to-one on `(symbol, date)`.

`FLOW_COLUMNS` is the single source of truth for column identity *and* order and the DDL
mirrors it, the same contract `DATASET_COLUMNS` has with `init_ml_dataset.sql`. A test
parses the DDL and asserts the two agree.

## 2. How it reads 60.6M rows without an OOM

The cache has previously OOM-killed the ETL on this host (`storage/etl.py::_load_cache`,
12.5 GB peak). Neither pass here loads it whole:

- **Outlier pass.** Detection is entirely local to one `(symbol, date, expiry)`, and
  `build_bhavcopy_cache` writes **one row group per trade date** (1,254 groups, ~48k rows
  each). The pass streams row group by row group, so peak memory is one trading day. Row
  groups outside the requested window are skipped using the DATE column's min/max statistics
  without decoding them, which is what makes `--limit-dates` a genuinely fast smoke run.
- **Futures pass.** A predicate push-down on `INSTRUMENT` selects 752,399 of the 60.6M rows
  (733,488 `FUTSTK` + 18,911 `FUTIDX`) at read time.

Spot comes from `option_metrics.underlying_price` — the **raw, unadjusted** close. That is
the basis the ETL used to pick strikes and solve IV, so moneyness and basis computed here
line up with the rest of the pipeline; `underlying_price_adj` would report a basis equal to
the corporate-action factor. Using `option_metrics` rather than the cache's own
`UNDERLYING_PRICE` also pins this table's universe to the pipeline's.

## 3. The outlier detector, and how its thresholds were chosen

A contract is flagged only when it clears **both** gates:

1. `|z| >= 25` — a robust z-score of `chg_oi` **within `(symbol, date, expiry)`**.
2. `share >= 0.05` — it holds at least 5% of the symbol-day's entire `|chg_oi|`.

Three decisions here were measured rather than assumed, and each of the first two started out
wrong.

### 3.1 The scale must be estimated from contracts that traded

A standard deviation is inflated by exactly the large build being hunted, which shrinks its
own z below any threshold — the estimator has to be robust or the detector cannot fire. That
much was anticipated. What was not: estimating the median absolute deviation over the *whole*
chain fails just as badly, for the opposite reason. An option chain is heavily zero-inflated —
most listed strikes see no OI change on a given day — so the median and the MAD both collapse
toward zero and every contract that moved at all scores an enormous z.

Measured on 24 days spread across the five years, the initial implementation put **22% of all
contracts over z = 4** and flagged **99.4% of symbol-days**. It was reporting the chain's
noise floor. Restricting the estimate to contracts with a non-zero change compares a strike
against the others that actually traded, which is the intended cross-section, and drops the
same flag rate to ~13%.

### 3.2 The thresholds

Against the corrected scale, the traded-contract `|z|` distribution runs:

| quantile | p50 | p90 | p99 | p99.9 |
|---|---|---|---|---|
| `\|z\|` | 0.7 | 3.1 | 18.4 | 53.0 |

So a nominal-looking `z >= 4` sits near the 92nd percentile and still fires on 95% of
symbol-days. **25** is roughly the 99.4th percentile. The share distribution is far better
behaved (p95 3.8%, p99 11.1%) and **5%** is what turns "largest change of the day" into
"materially concentrated". Neither gate alone is sufficient and both have a dedicated test.

### 3.3 The flag rate is stable across the five years

This is the check that matters. F&O liquidity grew several-fold over the window, so a
detector that merely tracked volume would show a monotone drift — the way the index
`DEALER_SIGN` measurement did (90.5% → 36.7%). It does not:

| year | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|
| symbol-days flagged | 12.7% | 11.9% | 16.6% | 14.0% | 10.8% | 13.0% |

Full-build result: **34,288 of 212,441 symbol-days (16.1%)**, 46,762 outlier strikes.

### 3.4 What the detector deliberately does not claim

**Direction.** An OI build cannot distinguish a buyer from a writer — an OTM call build is
bullish if someone bought it and bearish if someone wrote it. No column here carries a
direction; that needs the price-change quadrant, which is a separate feature. `call_frac` and
`moneyness` are descriptive only.

**Comparability across expiries.** The z is computed per expiry (a far-month strike judged
against near-month churn would never clear it) but the share gate's denominator is the whole
symbol-day. That is the materiality test — "5% of everything that moved today" — and it
biases toward the front month. The alternative, a per-expiry denominator, admits a dead far
expiry where one strike holds 80% of a trivial total. The bias is accepted and recorded here.

## 4. Two defects found along the way

### 4.1 `EXPIRY_DT` format inference (crash)

`EXPIRY_DT` is `%d-%b-%Y` text. **"May" is spelled identically under `%b` and `%B`**, so
pandas infers `%d-%B-%Y` from a chunk whose first expiry falls in May and then raises on the
next `27-Jun-2024`. This killed the first full five-year build. `storage/etl.py` passes the
format explicitly for the same reason; `_parse_expiry` now does too, with a regression test.

### 4.2 Bhavcopy open interest is in **shares**, not contracts (silent, and pre-existing)

`data/lots.py` asserted the reverse — *"Bhavcopy reports open interest in contracts; share
exposure requires the lot size"* — and the first notional columns followed it, inflating every
value by the lot factor. RELIANCE's futures book came out at ₹8.5 lakh crore instead of
₹17,102 crore.

Settled two ways:

1. **Divisibility.** `OPEN_INT` divides *exactly* by the lot size on 98.0% of 2026 `FUTSTK`
   rows (82.7% across the whole window; the residue is early years where `lot_sizes.csv` rows
   are `source=derived` and approximate — the rate rises 61% → 98% as the reference improves,
   which is the signature of a reference-quality problem, not a units problem).
2. **Against `participant_oi`, which reports contracts.** Summing `OPEN_INT / lot` over all
   stock futures reproduces its `TOTAL future_stock_long` to **within 0.02%** (7,850,805 vs
   7,849,284 on 2026-08-27), where the un-divided sum is off by a factor of **~2,158**.

So notional is `oi × spot`, with no lot factor, and the `lots.py` docstring is corrected.
This is also the first thing the newly-acquired participant data has been used to settle.

Note this leaves `gex` and the other chain aggregates in *share*-gamma units rather than
contract-gamma. That is arguably the more correct unit for a dealer hedging exposure, and it
is a per-symbol scale factor that cancels under the within-symbol normalisation the GEX
findings already use — so it is a labelling clarification, not a defect in those results.

## 5. Results

### 5.1 Per-symbol: does it predict next-day movement?

Target `next_day_abs_return`. Within-symbol causal z-score (expanding, `shift(1)`, min 60
observations), Spearman, then partial against `ce_iv_30` — the standing evaluation bar, since
raw IC on this dataset mostly rediscovers implied volatility.

| feature | n | within-ρ | partial (vs IV) | t |
|---|---|---|---|---|
| **`fut_basis_excess`** | 161,398 | −0.0396 | **−0.0516** | **−20.7** |
| `fut_basis_pct` | 187,006 | −0.0134 | −0.0236 | −10.2 |
| `fut_oi_chg_notional` | 187,006 | +0.0138 | +0.0040 | 1.7 |
| `oi_outlier_call_frac` | 19,840 | +0.0028 | +0.0101 | 1.4 |
| `oi_outlier_share` | 186,913 | −0.0069 | +0.0078 | 3.4 |
| `oi_outlier_max_absz` | 19,850 | +0.0062 | +0.0055 | 0.8 |
| `oi_outlier_moneyness` | 19,850 | +0.0227 | +0.0036 | 0.5 |
| *`gex` (reference)* | 187,006 | +0.0666 | +0.0407 | 17.6 |

**`fut_basis_excess` is the strongest feature the project has produced.** Its partial IC
(−0.0516) exceeds `gex`'s (+0.0407), which was the previous best surviving result, and it
*strengthens* under the IV control rather than dying. Pooled cross-sectionally the same
feature has a raw IC of +0.006 and a partial of −0.027: the signal is **hidden** by implied
volatility, not explained by it — the opposite of the usual failure mode.

The carry adjustment is doing real work: `fut_basis_excess` (basis minus the MIFOR rate)
roughly doubles `fut_basis_pct` (−0.0516 vs −0.0236). That is evidence the feature measures
what it claims — deviation from fair carry — rather than a price ratio. Independently, its
distribution has a median of +0.005% annualised with a fat left tail (p1 −28.9%), which is
the correct signature: excess-over-carry centred on zero, with futures at a discount ahead of
dividends.

**The outlier family does not predict next-day movement.** Every column lands under
|t| = 3.4 within symbol. This is a clean negative result on the family I expected to be the
more promising of the two.

`oi_outlier_moneyness` is the cautionary case and worth keeping in the write-up: pooled raw
IC **+0.0368**, partial IC **+0.0004**. It dies completely under the IV control — exactly the
failure the standing rule exists to catch.

### 5.2 Market-level: does it track named institutional flow?

Market aggregate per date (median or sum across the ~170 symbols) against `participant_oi`.
**Both sides differenced** — the level correlations are spurious. On levels,
`basis_excess` correlates −0.42/−0.29/−0.36 with FII/Pro/Client net stock futures, all three
in the same direction, which cannot be a flow relationship since those sides offset; both
series simply trend over five years.

On changes, Spearman, 1,253 days (`*` = |t| > 3):

| market aggregate | FII `d_net_fut` | FII `d_net_call` | Pro `d_net_fut` | Pro `d_net_call` | Client `d_net_fut` | Client `d_net_call` |
|---|---|---|---|---|---|---|
| `fut_oi_chg` | −0.170\* | −0.380\* | +0.228\* | −0.441\* | +0.271\* | **+0.559\*** |
| `d_roll` | +0.060 | +0.372\* | −0.112\* | +0.320\* | −0.171\* | **−0.470\*** |
| `outlier_rate` | −0.081 | +0.225\* | −0.054 | +0.205\* | −0.050 | **−0.281\*** |
| `d_basis_excess` | +0.193\* | +0.063 | −0.001 | +0.240\* | −0.005 | −0.234\* |
| `outlier_notional` | −0.122\* | +0.064 | +0.120\* | −0.034 | +0.043 | +0.000 |
| `d_call_frac` | +0.006 | +0.003 | −0.030 | +0.015 | +0.013 | +0.010 |

The structural check is the **sign opposition between Client and the two institutional
categories**, which holds on three independent aggregates (`fut_oi_chg`, `d_roll`,
`outlier_rate`) with FII and Pro agreeing with each other and Client opposite both. Retail and
institutions are mechanically on opposite sides of the F&O book, so a feature that separates
them is measuring the thing this project is about. A feature correlating with all three the
same way — as the level version did — is measuring a trend.

**This substantially rehabilitates the outlier detector.** `outlier_rate` tracks institutional
option flow at market level (FII +0.225, Pro +0.205, Client −0.281) even though it fails to
predict next-day movement per symbol. Those are different claims, and only the second one
failed. For an HMM over latent positioning states — which does not need to forecast realised
volatility — it remains a live input.

`d_call_frac` is dead everywhere and is the first candidate to drop.

## 6. Limitations

- **Direction is unresolved.** Neither family distinguishes a buyer from a writer. The
  price-change quadrant (buildup / unwinding / short covering) is the missing piece and is the
  natural next feature.
- **The conditional outlier columns are sparse.** `max_absz`, `call_frac`, `moneyness` and
  `dte` are defined only on the 16.1% of symbol-days that flagged, so the within-symbol
  expanding z runs on an intermittent series and retains ~19,850 rows. Their per-symbol
  results are correspondingly weaker evidence than the dense columns'.
- **Front-month bias** in the share gate — see §3.4.
- **The effect sizes are small.** A partial IC of −0.05 is the strongest result here and is
  still a weak signal in absolute terms. It is reported as an effect that survives controls,
  not as a tradeable edge, and it has not yet been run through purged walk-forward CV with
  embargo.
- **`fut_basis_excess` is 82.9% populated** by construction: annualisation is suppressed below
  5 days to expiry, because dividing a 0.5% basis by a 1-day horizon yields 182% "annualised"
  and would dominate every downstream z-score. `fut_basis_pct` is populated on all 212,441
  rows and is the fallback at short horizons.

## 7. Next

1. **Buildup-quadrant attribution** — the direction the outlier detector is missing, and the
   feature that would let "someone loaded up this strike" become "someone *wrote* this strike".
2. **Purged walk-forward CV with embargo** on `fut_basis_excess`, to confirm §5.1 survives
   honest splitting before it is promoted into `DATASET_COLUMNS`.
3. **HMM over latent positioning states**, taking `outlier_rate`, `fut_basis_excess`,
   `d_roll` and the existing dealer aggregates as observables — the unsupervised route that
   the unlabelled nature of this data pointed at from the start.
