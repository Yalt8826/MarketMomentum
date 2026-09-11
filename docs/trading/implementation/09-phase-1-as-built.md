# 09 — Phase 1 as built

[← Phase 0 as built](08-phase-0-as-built.md) · [Index](README.md) · [Gate 1 verdict →](../results/gate-1-feature-filters.md)

**Status: Phase 1 complete. Gate 1 failed on 2026-09-11.**

---

## 1. What was built

| Module | Lines | Role |
|---|---|---|
| `backtest/filters.py` | 400 | Feature join, filters, three baselines, significance |
| `tests/unit/test_filters.py` | 290 | 17 tests |

**532 tests pass**, none requiring a database.

## 2. The load-bearing piece — `attach_features`

Everything Gate 1 concludes rests on features being strictly prior to the trade
they condition, and that property is **invisible in any output**: a leaked
filter simply looks brilliant.

```python
merged = pd.merge_asof(
    work, feat,
    left_on="entry_date", right_on="feature_date", by="symbol",
    direction="backward",
    allow_exact_matches=False,   # ← the whole guarantee
)
```

Gate 0 enters at the first trading day's `settle`. Every IV-derived feature for
that day is knowable only after it closes, so matching it would be trading on
information the position could not have had.

**Verified on the real join:** minimum lag 1 day, median 3, maximum 4, **none
≤ 0**.

One implementation note: the feature date is **renamed rather than suffixed**,
because `merge_asof` suffixes fire only on a column collision and
`backtest_results` has no `date` column. Relying on the suffix would have left
the feature date uninspectable — and the property unassertable.

## 3. Three baselines, not one

| Baseline | Role |
|---|---|
| Unfiltered | The floor — Gate 0's own result |
| **`ce_ivr`** | The boring explanation: needs none of this research |
| Random filter, seeded, 400–500 draws | The distribution of pure chance |

The random baseline did more work than expected. Selecting 30% of 7,700 trades
is enormous freedom to find a subset that did well, and its p95 (+5.20%) turned
out to be the bar **nothing** cleared — including IV rank.

## 4. Ranking is within month, not pooled

```python
pct = values.groupby([frame["year"], frame["month"]]).rank(pct=True)
```

A pooled threshold would silently become a **market-timing rule** — take every
trade in high-IV months, none in calm ones. That is a different strategy from
choosing between symbols, and testing it while believing you were testing
cross-sectional selection would be a category error. Pinned by a test that shifts
one month's whole distribution above the others and asserts no month is skipped.

## 5. Significance resamples months

```python
def monthly_block_bootstrap(frame, mask, draws=2000): ...
```

Trades within a month share that month's market. A t-test over 7,700 of them
would treat them as independent when there are really **61**.

The statistic is the **paired difference** computed inside each resample, so
filtered and unfiltered always come from the same months. Two tests pin it: one
asserts the interval straddles zero on noise, the other that a planted +0.30
edge is recovered.

## 6. Walk-forward and per-year

`walk_forward_filter` picks the selectivity on every prior month and applies it
unchanged to the next — at three candidate thresholds over 61 months, selecting
and reporting on the same rows is a live risk. 37 folds after a 24-month
warm-up.

`regime_split` reports per year. This was specified as a guard and turned out
**decisive**: pooled edges for `ce_ivr` and `iv_rv_spread` come almost entirely
from 2021, and `fut_basis_excess` swings +12.76pp → −11.02pp in consecutive
years.

## 7. The tripwire

`leakage_tripwire` plants a filter conditioned on the trade's own outcome. It
lifts the mean from **4.56% to 23.64%** (+19.1pp).

Same device as the research branch's Stage 1 tripwire, which scored a perfect
AUC of 1.0000. It is the evidence that the comparison machinery can detect an
edge when one exists — without it, six nulls would be uninterpretable.

## 8. Deviations from the plan

| Spec said | Built | Why |
|---|---|---|
| Task 1.6 — optional stop-loss parameter | **Not built** | Gate 1 failed on entry selection; a stop is a *risk* lever, and belongs with Phase 2's sizing work rather than buried in a filter sweep |
| Filters on single features | As specified | Combinations untested — recorded as a limit |

## 9. Reproducing Gate 1

```python
import psycopg2
from mktmtm.storage.db import build_dsn
from mktmtm.backtest.analysis import load_results
from mktmtm.backtest.filters import (
    CANDIDATE_FEATURES, attach_features, load_features,
    compare_to_baselines, leakage_tripwire, monthly_block_bootstrap,
    rank_filter, regime_split, walk_forward_filter,
)

with psycopg2.connect(build_dsn()) as conn:
    frame = attach_features(load_results(conn, "base"), load_features(conn))

assert leakage_tripwire(frame)["lift"] > 0.15        # harness works
for name, spec in CANDIDATE_FEATURES.items():
    print(compare_to_baselines(frame, name, 0.3, spec["higher_is_better"]))
```

[← Phase 0 as built](08-phase-0-as-built.md) · [Index](README.md) · [Gate 1 verdict →](../results/gate-1-feature-filters.md)
