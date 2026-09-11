# Bugs found in Phase 0 — and the fixes applied

[Results index](README.md) · [Gate 0 verdict](gate-0-base-strategy.md)

Four defects were found while making the backtester's P&L economically real.
**All four were silent**: none raised an exception, none failed a run, and two
had been dropping or distorting data since before this branch existed.

They are collected here because the *method* that found them transfers, and
because three of the four are visible only from the tail of a distribution
nobody would have looked at if the headline number had seemed reasonable.

---

## How they were found

One number did not make sense.

> Gate 0's first run reported a worst single trade of **−1,677%** — a loss of
> **16× the margin posted**.

A short strangle on a 12%-margin position cannot lose 16× its margin; that would
require the underlying to move by roughly twice its own price. The mean return
(+4.79%), the win rate (68.3%), the Sharpe (2.22) and the year-by-year table all
looked entirely plausible. Only the tail was absurd.

Chasing that one number found three defects. A fourth had already been found by
inspection while wiring the cost model in.

**The lesson worth carrying:** the headline statistics were not the ones that
revealed the corruption. A backtest that reports only a mean, a win rate and a
Sharpe has no way of telling you it is wrong.

---

## Defect 1 — P&L dropped the replacement leg's credit

**Found by** reading the accounting while wiring costs in, not by a failing test.

### What was wrong

```python
pnl_points = initial_entry_credit - final_exit_cost + total_adjustment_pnl
```

`initial_entry_credit` is the **original** entry premiums, recorded once and
never updated. But `call_exit_price` is the exit price of the **current** leg,
and `current_call_sold_price` *is* updated when the weekly hedge sells a
replacement.

So the formula credits the original sale, debits the replacement's buyback, and
the replacement's own sale credit is never counted anywhere.

### Worked example

Sell CE@50 / PE@45. The hedge buys the call back at 30 and re-sells at 58. Exit
CE@20 / PE@25.

| Leg | P&L |
|---|---|
| CE leg 1: sold 50, bought back 30 | +20 |
| CE leg 2: sold 58, bought back 20 | **+38 ← never counted** |
| PE leg: sold 45, bought back 25 | +20 |
| **Truth** | **78** |
| Old formula | 70 |

The error is exactly `(final sold price − original sold price)` per side.

### Why the direction matters

The hedge re-sells *near the price of the losing leg*, so replacements are
systematically sold **higher** than the original. The bug therefore
**understated** P&L on every month that adjusted — conservative, but wrong, and
Gate 0's entire verdict is that number.

### The fix

Sum leg by leg: every leg ever sold, minus what it cost to close.

```python
pnl_points = (
    total_adjustment_pnl
    + (current_call_sold_price - call_exit_price)
    + (current_put_sold_price - put_exit_price)
)
```

**Pinned by** `test_pnl_counts_the_replacement_legs_credit`, which checks
`strangle.py` against `economics.price_strangle_trade` — the two reach the same
figure by different routes. Two independent implementations agreeing is a
stronger statement than either matching a hand-typed constant.

---

## Defect 2 — untraded strikes were priced anyway

### What was wrong

NSE assigns a settlement price to **every listed strike**, whether or not it
traded. `find_option_near_price` — which the weekly hedge uses to re-sell "near
the price of the other leg" — accepted any contract with a finite positive
settle.

So the hedge could sell a contract that had never traded, at a price nobody had
ever transacted.

### Extent

**32 of 7,683 trades (0.4%), for −₹5.7M.** Removing them moved the mean from
+4.79% to +5.75%.

### The fix

Selection requires evidence of a real transaction:

```python
def _is_tradeable(opt: dict[str, Any]) -> bool:
    volume = opt.get("volume")
    return isinstance(volume, (int, float)) and math.isfinite(volume) and volume > 0
```

Applied in both `find_option_near_price` and `find_options_near_delta`, behind
`require_liquidity=True`. The old behaviour stays reachable as
`require_liquidity=False` so the effect remains measurable rather than merely
asserted.

**Open interest is deliberately not accepted as evidence** — a position can
exist without a price being struck today.

---

## Defect 3 — degenerate chains ⭐

The most instructive of the four.

### What was wrong

On **NMDC, 2022-10-25**, every one of 206 chain elements carried an identical
settle of **₹130.75** against a spot of **₹130.71**. The underlying's price had
been written into every option's premium. Every delta was `0.0`, because the IV
solver cannot converge on prices that make no sense.

```
type  strike  settle   delta  volume       oi
  CE    72.5  130.75     0.0       0        0
  CE   100.0  130.75     0.0       0        0
  CE   135.0  130.75     0.0    2492  2113850   ← heavily traded, still corrupt
  PE   117.5  130.75     0.0      10   150750
  ...all 206 rows identical...
```

### Extent

**47 of 213,379 symbol-days (0.022%)** — and the distribution is the diagnosis:

| | |
|---|---|
| On an **expiry date** | **39 of 47 (83%)** |
| Scattered singletons | 8 (NMDC 2022-10-25, RELIANCE 2023-07-19, ITC 2025-01-03, …) |

On expiry day the expiring series settles against the underlying, and for these
rows that settlement value has been written into every contract's premium —
which is exactly the shape observed: every settle equal to spot, every delta
0.0. The eight non-expiry cases are unexplained.

**Consequence for the strategy:** the exit day is the last trading day *before*
expiry, so the strategy does not normally touch an expiry-day chain. The
exposure comes from hedge dates and from months whose exit lands on one — which
is why the guard is applied at all three points rather than only at exit.

### Why the obvious filters fail

This is the part worth remembering.

| Filter | Why it misses |
|---|---|
| **Volume > 0** | Affected strikes include one with **2,492 lots traded** |
| **No-arbitrage bound** (a call cannot be worth more than the stock) | The corrupt value sits at **99.97% of spot** — inside any tolerance loose enough to admit a legitimate deep-ITM call |

Both are correct filters. Neither sees this. What identifies the defect is the
**degeneracy itself**, so that is what the check tests.

### And it must be per expiry

**IOC, 2022-06-29** is the same defect in partial form: **146 of 436 rows** carry
the spot price, and they are exactly the contracts of the expiring 30-Jun series,
while July and August price normally.

> A whole-chain test sees **193 distinct settles** and passes it.

That single trade was the −1,677%.

### The fix

```python
def chain_is_degenerate(option_chain, expiry_date=None) -> bool:
    rows = [o for o in option_chain if o.get("expiry") == target] if expiry_date else option_chain
    if len(rows) < 10:
        return False          # too thin to distinguish corruption from a sparse listing
    settles = {o.get("settle") for o in rows if isinstance(o.get("settle"), (int, float))}
    return len(settles) < 2
```

Checked at **entry**, at **each hedge date**, and at **exit**. A month touching
such an expiry is skipped with a typed reason rather than priced.

A secondary no-arbitrage guard (`_is_plausible_price`) was kept as well — it
catches the put side of this defect directly, and other corruption shapes — but
it is not sufficient on its own, for the reason above.

---

## Defect 4 — 3,308 rows never persisted, in every run

### What was wrong

The original table declared:

```sql
entry_date DATE NOT NULL
```

A month with no trading days has no entry date. Every such row was **rejected on
insert** — and `insert_backtest_result` logs the failure and continues:

```python
except Exception:  # noqa: BLE001 - legacy behaviour: log, keep going
    logger.error("Failed to insert/update result for %s %s-%s: %s", ...)
```

So the run reported success while **3,308 months vanished**.

### Why the schema rewrite did not fix it

The Phase 0 schema declared the column nullable — but
`CREATE TABLE IF NOT EXISTS` is a **no-op on an existing table**, so the original
constraint survived untouched. The migration has to drop it explicitly:

```sql
ALTER TABLE backtest_results ALTER COLUMN entry_date DROP NOT NULL;
```

### What it cost

The **skip taxonomy** was computed over a denominator missing its largest
category. It reported 74.0% of months traded; the true figure is **51.0%**.

A Gate 0 disclosure was wrong by 23 percentage points, and nothing indicated it.

---

## What the fixes did to the answer

| Stage | Mean return | Worst trade |
|---|---|---|
| Contaminated | +4.79% | −1,677% |
| + liquidity gate (defect 2) | +4.15% | −1,677% |
| + degeneracy gate (3) + persistence (4) | **+4.56%** | **−245.8%** |

**The headline moved by less than half a percentage point while the worst trade
fell by a factor of seven.**

That is the shape of the whole episode: the mean was never the contaminated
statistic — the tail was, and in a premium-selling strategy the tail is the part
that decides whether the book is usable at all.

Note also that each fix moved the answer *downward* before the final one
recovered part of it. **A data cleanup that improves your result is the
suspicious outcome**; removing fictional prices should remove fictional wins
along with fictional losses.

---

## The transferable lessons

1. **Check the tail, not the mean.** Three of four defects were invisible in
   every headline statistic and obvious in the worst trade.
2. **An impossible number is a gift.** −1,677% could not be explained away, so it
   had to be chased. A merely *disappointing* number would have been accepted.
3. **A successful lookup is not a correct lookup.** Every chain query returned a
   row. The rows were fiction.
4. **Two obviously correct filters can both miss the same defect.** Volume and
   no-arbitrage are each right and each blind here.
5. **Test at the granularity the defect lives at.** Whole-chain degeneracy passes
   IOC; per-expiry catches it.
6. **`CREATE TABLE IF NOT EXISTS` never alters an existing table.** Schema
   intentions expressed only in the `CREATE` are not applied.
7. **A writer that logs and continues will hide a systematic failure.** 3,308
   identical errors scrolled past in every run.
8. **Cross-check independent implementations.** The P&L fix is verified against a
   separate code path rather than a constant.

These extend the [research branch's own catalogue of silent
defects](../../understanding/v1/15-bugs-and-lessons.md), which now stands at
fourteen across the project.
