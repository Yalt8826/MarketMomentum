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

*To be filled in by the implementer.* Report at minimum:

1. Distribution of `q_fut_direction` and `oi_outlier_direction`, and their
   non-null rates.
2. **The raw-vs-delta-adjusted disagreement rate on flagged strikes**, and the
   resulting decision about which column survives.
3. Correlation of the new direction scores against `participant_oi` flows at
   market level, differenced, with the expiry-calendar control applied — the
   same protocol used for the existing flow features. The structural check is
   that `Client` moves opposite to `FII`/`Pro`.
4. Whether `oi_outlier_direction` beats the directionless
   `oi_outlier_build_notional` at anything. If it does not, that is a real
   finding and it means Stage 2's observable set needs rethinking before the
   HMM is built.
