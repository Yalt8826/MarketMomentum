# The stop loss — measured, and it does not help

[Results index](README.md) · [Gate 0](gate-0-base-strategy.md) · [Gate 1](gate-1-feature-filters.md)

**Run 2026-09-11** · commit `3725c30` · variants `stop1.5`, `stop2`, `stop3`
**Verdict: do not use one.** It costs return and buys no tail protection.

---

## 1. Why this was tested

Gate 1 removed entry selection as a lever — no feature, and not implied-
volatility rank, beat a random filter. That left **sizing and stops as the whole
remaining design surface**.

A stop was the obvious candidate. Gate 0's max drawdown is **−37.27%**, its
worst single trade **−245.8% of margin**, and positions currently run to the
scheduled exit regardless of how far under water they go. Real volatility
sellers do not do this.

## 2. How it works

A multiple of the **entry credit** — the natural unit, since that is what the
trade was paid to take the risk. Checked at each weekly hedge date:

```python
unrealised = (
    total_adjustment_pnl
    + (current_call_sold_price - current_call_price)
    + (current_put_sold_price - current_put_price)
)
if unrealised <= -stop_multiple * initial_credit:
    close both legs
```

**The check sits before the weekly adjustment, not after.** The adjustment
re-sells into a losing position — doubling down on exactly the move that is
hurting. A stop firing only afterwards has already taken the risk it exists to
avoid. A test pins that ordering.

## 3. The result

Measured on the **common set of 7,706 trades** — every variant scored on
identical months (see §5 for why that matters):

| Variant | Stopped | Mean return | 5th pct | Worst trade | Max drawdown | Sharpe |
|---|---|---|---|---|---|---|
| **base** (no stop) | 0% | **+4.56%** | **−36.25%** | **−245.84%** | **−37.27%** | **2.16** |
| 3× credit | 3.0% | +4.49% | −36.88% | −245.56% | −37.75% | 2.12 |
| 2× credit | 7.4% | +4.35% | −39.32% | −245.56% | −38.72% | 2.04 |
| 1.5× credit | 11.4% | +4.25% | −37.94% | −245.56% | −38.88% | 2.00 |

**Every column gets worse as the stop tightens.** Not one dimension improves —
not the tail percentile, not the worst trade, not the drawdown.

## 4. Why a stop fails here

Two mechanisms, and they compound.

**Short volatility mean-reverts.** Implied volatility spikes and then subsides.
A position deep under water at a weekly mark has a real chance of recovering by
expiry, and Gate 0's 67% win rate is partly made of exactly those recoveries. A
stop converts a temporary drawdown into a realised loss — the one outcome the
position was structured to avoid.

**The stop fires on a weekly mark, so the catastrophe gaps through it.** The
worst trade moves from −245.84% to −245.56%: noise. The loss that actually
mattered happened between two checks, which is what a genuine gap does.

> Together: **the stop catches the recoverable losses and misses the
> unrecoverable one.** That is the worst possible selection, and it is exactly
> what the numbers show — mean return falls by the amount of the recoveries
> forgone, while the tail is untouched.

A daily or intraday stop might behave differently. This data is end-of-day and
the strategy is managed weekly, so that is not testable here.

## 5. The comparison trap, and how it was caught

The first comparison looked dramatic and was wrong. Under a 3× stop, June 2023
showed **−56.79%** against base's **+4.76%** — apparently catastrophic.

It was an artefact of unequal samples:

| | base | stop3 |
|---|---|---|
| Rows for 2023-06 | 222 | 8 |
| **Priced trades** | **2** | **8** |

The stop provides an exit *before* the scheduled exit date, so months whose
exit-day lookup fails under base — 191 "Missing Exit Price" skips — get priced
under the stop variants. base priced 2 trades that month; stop3 priced 8. The
comparison was measuring a different set of trades, not a different strategy.

**This is the same trap the research branch hit in Stage 1**, where a calendar
baseline was scored on 1,004 rows against flow models' 556. The fix is the same:
restrict to the common set. On identical trades the effect vanishes — worst
month −16.12% vs −16.26%.

> A stop that appears to help or harm dramatically is usually changing which
> trades exist, not how they turn out.

## 6. A bug found while reading the variant counts

`result["variant"]` was set inside `price_result`, which a **skipped month never
reaches**. So every variant's skipped months were written under the default
variant, silently merging their skip taxonomies into the base run's.

Visible only as an oddity in a count — stop1.5 showed 7,729 rows of which 7,729
were priced, i.e. no skips at all, which cannot be true. Now set at function
entry, with a regression test.

## 7. What this means for the product

The screen ships **without a stop**, and says so in its own output.

That is an uncomfortable thing for a tool to say, so it is worth being precise:
this is not an argument that the position needs no risk management. It is a
measurement that *this particular* control, at these levels, on this cadence,
made every measured outcome worse. The controls that remain — position count,
margin budget, sector concentration, and a modelled 2σ loss shown before sizing
— act on **how much** is at risk rather than on when to abandon a position, and
none of them was tested here.

The −37.27% drawdown is therefore still the binding constraint, and it is
unmitigated. The honest response is to size for it, not to believe a stop has
removed it.
