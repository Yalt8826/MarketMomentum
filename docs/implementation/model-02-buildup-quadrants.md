# Stage 1b — Buildup quadrants (the direction feature)

**Read [model-00-overview.md](model-00-overview.md) first.**

**Goal.** Give the flow features a **direction**. Today they have none, and
that is their single largest defect.

**Build target:** extend `src/mktmtm/features/flow.py`, extend
`src/mktmtm/storage/sql/init_flow_features.sql`, extend
`tests/unit/test_flow_features.py`.

---

## 1. Why this is needed

From the header comment of `init_flow_features.sql`, written when the outlier
detector was built:

> an OI build cannot distinguish a buyer from a writer, so none of these
> columns carry a direction. That needs the price-change quadrant (a separate
> feature).

This is that feature. Right now `oi_outlier_build_notional` says "₹400 crore of
new open interest appeared at this strike" and cannot say whether somebody
**bought** those calls (bullish, paying premium) or **wrote** them (bearish or
income-seeking, collecting premium). Those are opposite market views producing
an identical OI print.

Stage 2's HMM cannot learn a meaningful state from directionless observables:
"unusual activity happened" supports at most a two-state quiet/active model,
which is not worth building. **Build this before Stage 2.**

## 2. The method

Open interest change crossed with price change is standard OI analysis. For a
**futures** contract:

| Price | OI | Interpretation | Column |
|---|---|---|---|
| ↑ | ↑ | **Long buildup** — new buyers, bullish | `q_long_buildup_notional` |
| ↓ | ↑ | **Short buildup** — new sellers, bearish | `q_short_buildup_notional` |
| ↑ | ↓ | **Short covering** — shorts closing, bullish | `q_short_cover_notional` |
| ↓ | ↓ | **Long unwinding** — longs closing, bearish | `q_long_unwind_notional` |

For an **option** contract the price is the premium, so the reading changes:

| Premium | OI | Interpretation |
|---|---|---|
| ↑ | ↑ | Buyers are paying up — **buyer-driven** open interest |
| ↓ | ↑ | Sellers are pressing premium down while opening — **writer-driven** |

Writers are the institutional side far more often than the retail side; option
writing needs margin, and the `participant_oi` short columns are dominated by
`Pro` and `FII`. So writer-driven OI is the more institutional signal, and
separating it out is the point of this feature.

### The delta confound — do not skip this

A call premium rises when the underlying rises, whether or not any buyer was
aggressive. **The raw premium change is therefore not a clean measure of buyer
versus writer pressure for options.** It is confounded by the underlying's move,
scaled by the option's delta.

The correction is the **delta-adjusted residual**:

```
residual_premium_chg = (premium_t - premium_{t-1}) - delta_{t-1} * (spot_t - spot_{t-1})
```

Per-strike `delta` is available in `option_metrics.option_chain` — each JSONB
element carries `{iv, oi, chg_oi, delta, gamma, vanna, charm, close, settle,
strike, expiry, type, volume, ...}`. It is **not** in the bhavcopy parquet
cache.

Implement in two passes so the cheap path ships first:

- **Pass A (primary, cheap).** Raw premium quadrants from the parquet cache,
  computed in the existing streaming loop. This is the convention every Indian
  market-data terminal uses, and it ships in minutes.
- **Pass B (validation, bounded cost).** Delta-adjusted quadrants computed
  **only for the flagged outlier strikes** — a few tens of thousands of rows,
  not 49.5M — via a lateral join over `jsonb_array_elements`, the same
  technique `_CHAIN_AGG_SQL` already uses because a Python pass over the full
  chain takes minutes.

Then **measure the disagreement rate** between A and B on the flagged strikes
and record it in §7. If they agree on most flagged strikes, Pass A is
sufficient and the extra column can be dropped. If they disagree materially,
Pass B's version is the correct one and Pass A must not be used. Measure it;
do not assume either way.

## 3. Columns

Append to `flow_features` — same `(symbol, date)` grain, so **do not create a
new table**.

```sql
ALTER TABLE flow_features
    -- Futures quadrants (rupee notional, oi * spot, NO lot factor)
    ADD COLUMN IF NOT EXISTS q_long_buildup_notional  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS q_short_buildup_notional DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS q_short_cover_notional   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS q_long_unwind_notional   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS q_fut_direction          DOUBLE PRECISION,

    -- Option writer/buyer split (share of traded |chg_oi| notional)
    ADD COLUMN IF NOT EXISTS ce_writer_frac           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS pe_writer_frac           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS opt_direction            DOUBLE PRECISION,

    -- The outlier strikes, given a direction at last
    ADD COLUMN IF NOT EXISTS oi_outlier_writer_frac   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS oi_outlier_direction     DOUBLE PRECISION;
```

Also add the identical column list to `init_flow_features.sql`'s `CREATE TABLE`
so a fresh bootstrap and a migrated database converge. Keep both; the `ALTER`
is for existing installs, the `CREATE` for new ones. Both are idempotent.

### Derived scores

`q_fut_direction` ∈ [−1, 1], the bullish share of futures quadrant notional:

```
bull = q_long_buildup + q_short_cover
bear = q_short_buildup + q_long_unwind
q_fut_direction = (bull - bear) / (bull + bear)     -- NULL when denominator is 0
```

`opt_direction` ∈ [−1, 1] combines the four option views. Call buying and put
writing are bullish; put buying and call writing are bearish:

```
bull = ce_buyer_notional + pe_writer_notional
bear = pe_buyer_notional + ce_writer_notional
opt_direction = (bull - bear) / (bull + bear)       -- NULL when denominator is 0
```

`oi_outlier_direction` applies the same formula restricted to flagged strikes.
**This is the headline output of this stage** — it is what turns
`oi_outlier_build_notional` from "something happened" into "somebody took a
bullish/bearish position".

`NULL` when the denominator is zero. Never `0.0` — a symbol-day with no traded
option OI change is *unmeasured*, not *neutral*, and the distinction matters
because the two produce opposite conclusions when averaged.

## 4. Implementation notes

### The two-day join

`data/processed/bhavcopy_cache.parquet` holds **60,609,884 rows in 1,254 row
groups, one per trade date**, sorted. `_outlier_pass` already streams it one
row group at a time and skips groups by date using
`_row_group_date(reader, rg)`, which reads statistics rather than data.

Extend that loop to retain the previous row group's frame and merge:

```python
KEY = ["SYMBOL", "INSTRUMENT", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP"]
merged = today.merge(prev, on=KEY, how="left", suffixes=("", "_prev"))
```

Required cache columns: the existing `_OPTION_COLUMNS` plus `CLOSE` and
`SETTLE_PR`.

Traps:

- **Memory.** Hold exactly one previous frame. Do not accumulate a list of
  frames — a per-symbol dict of frames OOM-killed the ETL at ~12.5 GB once
  already (`CLAUDE.md`, cache invariant).
- **`EXPIRY_DT` is a dictionary-encoded string** in the cache, not a date. Join
  on it as a string — that is cheaper and avoids parsing. If you must parse it,
  use `_parse_expiry`, never bare `pd.to_datetime`: "May" is identical under
  `%b` and `%B`, pandas infers `%B` from a May-only chunk, and the next June
  chunk raises. That bug killed a full build once; there is a regression test.
- **New listings have no previous row.** `how="left"` leaves `NaN`; those
  contracts get `NULL`, not a zero price change.
- **Expiry rolls.** Contracts that expired vanish from today's group and simply
  drop out of the left join. Correct — do not reintroduce them.
- **Date gaps.** The "previous row group" is the previous *trading* date, which
  may be several calendar days back across weekends and holidays. That is the
  right comparison. Do not attempt calendar-day alignment.

### Only traded contracts get a quadrant

Gate on `CONTRACTS > 0`.

On an untraded strike, `SETTLE_PR` is a theoretical settlement value, not a
transacted price. A "premium change" derived from two consecutive theoretical
settles is model noise, and assigning it a quadrant would manufacture signal
out of arithmetic. This matches `_robust_scale`, which already estimates scale
from traded contracts only — that fix is why the outlier thresholds moved from
z=4.0 to z=25.0.

### Price basis

Use `SETTLE_PR` with a `CLOSE` fallback, matching `futures_basis`. Apply the
same rule to both instruments so the two families stay comparable.

### Threading it through

- Add a `_QUADRANT_COLUMNS` tuple and extend
  `FLOW_COLUMNS = ("symbol", "date", *_OUTLIER_COLUMNS, *_FUTURES_COLUMNS, *_QUADRANT_COLUMNS)`.
- `test_flow_columns_mirror_the_ddl` parses the DDL and compares it to
  `FLOW_COLUMNS`. **It will fail until both sides match** — that is the test
  doing its job, so fix the mismatch rather than relaxing the test.
- Add integer columns to `_INT_COLUMNS` only if any are integers; all ten new
  columns above are `DOUBLE PRECISION`, so none are.
- `write_flow_features` and `export_flow_parquet` iterate `FLOW_COLUMNS` and
  need no change beyond that.

## 5. Tests

Add to `tests/unit/test_flow_features.py`:

| Test | Asserts |
|---|---|
| `test_futures_quadrants_classify_the_four_cases` | Hand-built 2-day frame hits each quadrant exactly once |
| `test_untraded_contracts_get_no_quadrant` | `CONTRACTS == 0` contributes to no quadrant |
| `test_new_listings_have_null_not_zero_price_change` | Absent previous day → `NULL` |
| `test_direction_score_is_null_when_no_traded_flow` | Zero denominator → `NULL`, not 0.0 |
| `test_direction_score_is_bounded` | Random inputs stay within [−1, 1] |
| `test_call_writing_and_put_writing_have_opposite_signs` | The bullish/bearish mapping is not transposed |
| `test_delta_adjustment_removes_a_pure_spot_move` | Premium move exactly `delta * d_spot` → residual 0 → no quadrant |
| `test_expiry_roll_drops_vanished_contracts` | Expired contracts do not resurrect |

`test_call_writing_and_put_writing_have_opposite_signs` is the one that catches
the likeliest bug in this stage. A sign transposition here is invisible in
aggregate — every downstream number stays plausible while meaning the reverse —
and it would silently invert Stage 2's states and Stage 3's attribution.

## 6. Rebuild and acceptance

```bash
psql "$PG_DSN" -f src/mktmtm/storage/sql/init_flow_features.sql   # idempotent
uv run mktmtm flow-features --limit-dates 30                      # smoke
uv run mktmtm flow-features                                       # full, ~2-4 min
uv run python scripts/verify_db_health.py
```

The full run took **2m01s** before the two-day join. Expect 3–5 minutes after;
if it exceeds 15 minutes, the previous-frame retention is leaking memory or the
join is not on the dictionary-encoded key.

- [ ] `flow_features` still has exactly **212,441 rows** — the join must not
      duplicate or drop rows. Verify parity against `ml_dataset` explicitly.
- [ ] `q_fut_direction` non-null on ≥ 80% of symbol-days
- [ ] `oi_outlier_direction` non-null on ~16% (the flagged rate)
- [ ] All direction scores within [−1, 1]
- [ ] `mypy --strict`, `ruff`, and the full test suite pass
- [ ] Pass A / Pass B disagreement rate measured and recorded in §7

## 7. Results

**Built 2026-09-05.** All ten columns are live in `flow_features`; row count is
unchanged at **212,441**, joining `ml_dataset` 1:1. Full build now takes
**6m44s** (was 2m01s before quadrants; the delta lookup below is most of the
increase).

### 7.1 The delta adjustment is required, not optional

`scripts/measure_delta_adjustment.py`, over **163,383 traded contracts** on 47
sampled days spread across the five years:

| Measure | Value |
|---|---|
| Disagreement rate, raw vs delta-adjusted | **35.8%** |
| Disagreement rate, notional-weighted | **36.9%** |
| Median \|raw premium move\| | 4.20 |
| Median \|delta x spot move\| | 3.12 |
| Writer share, raw | 63.4% |
| Writer share, adjusted | 61.0% |

The confound is as large as the signal: the median move explained by the
underlying alone (3.12) is comparable to the median premium move itself
(4.20), and the two readings put **more than a third of contracts on opposite
sides**. Classifying on the raw premium change would have been wrong about a
third of the time, in a way that no aggregate check would have caught — the
writer share barely moves (63.4% vs 61.0%) even though the individual
assignments differ wildly.

**Decision: the delta-adjusted residual is what ships.** §2's staged plan had
the raw pass as primary with the adjusted one as validation; the measurement
reversed that. `option_quadrants` now classifies on

```
(mark - mark_prev) - delta_prev * (spot - spot_prev)
```

and **drops** contracts with no `delta_prev` rather than falling back to the
raw change — a third-wrong reading is not a usable fallback. Per-strike delta
comes from `option_metrics.option_chain` via `_load_day_delta`, fetched one day
at a time inside the existing row-group stream (~0.15 s per date).

### 7.2 A latent defect this stage exposed

The bhavcopy cache's row groups are one per trade date but are **not stored in
chronological order** — the frame is sorted by symbol, so the groups come out
with **293 backward steps** over the five years. The first implementation
streamed them in file order and treated the previous group as "yesterday",
which silently joined each day against an arbitrary other day.

Nothing failed. The same contract exists on both days, so the merge succeeded
and returned a price change spanning months. It surfaced only as a coverage
discontinuity — `opt_direction` non-null on 6.2% of 2023 rows but 99.0% of
2025 rows — which initially looked like the UDiFF schema cutoff (2024-07-08)
and was not.

The outlier detector never noticed because it is purely within-day; the
quadrants are the first thing here that needs an actual yesterday. Fixed in
`_ordered_row_groups`, pinned by `test_row_groups_stream_in_trade_date_order`.

### 7.3 Coverage and distributions

| Year | Rows | `q_fut_direction` | `opt_direction` | `oi_outlier_direction` | mean `opt_direction` | mean `ce_writer_frac` |
|---|---|---|---|---|---|---|
| 2021 | 15,216 | 98.9% | 97.2% | 9.0% | −0.089 | 0.646 |
| 2022 | 38,672 | 100.0% | 99.5% | 9.2% | −0.085 | 0.657 |
| 2023 | 38,281 | 100.0% | 98.9% | 11.5% | −0.106 | 0.673 |
| 2024 | 39,842 | 99.9% | 97.3% | 10.9% | −0.098 | 0.650 |
| 2025 | 50,666 | 100.0% | 99.0% | 10.2% | −0.081 | 0.654 |
| 2026 | 29,764 | 100.0% | 99.8% | 15.1% | −0.067 | 0.621 |

Coverage is flat across the five years — the check that matters, since F&O
liquidity grew several-fold over the window and a drifting rate would mean the
feature was tracking volume. `oi_outlier_direction`'s rate tracks the flagged
rate by construction. All direction scores lie within [−1, 1].

`ce_writer_frac` sits near **0.65** and `pe_writer_frac` near **0.62** in every
year: writers take the majority of newly opened option interest on both sides
of the chain, consistently, which is the expected structural fact (writing
needs margin, so it skews institutional). `opt_direction` is mildly negative
throughout — the market's newly opened option interest leans bearish on
average, which is what a persistent call-writing/covered-call bias looks like.

### 7.4 Not yet done

The §4 acceptance item "correlation of the new direction scores against
`participant_oi` flows at market level" is **not** measured here. It is the
same protocol Stage 1 runs, on the same aggregates, so it is folded into
[Stage 1](model-01-market-flow.md) rather than duplicated — see its results
section for whether `opt_direction` and `oi_outlier_direction` carry anything
the directionless columns did not.
