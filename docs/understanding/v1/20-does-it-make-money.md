# 20 — Does It Make Money?

[← The four gates](19-the-four-gates.md) · [Index](README.md) · Next: [The daily screen →](21-the-daily-screen.md)

Gates 0 and 1, and the stop loss that was measured and rejected. This is the
results document for Part III — the analogue of
[document 14](14-results.md) for the research half.

Short version: **the strategy makes money, the research does not improve it, and
the risk is not fixable by any means tested.**

---

## 1. The strategy, precisely

Before the numbers, exactly what was simulated. `backtest/strangle.py`, 917
lines, ported from a verified legacy script.

```
  DAY 1 OF THE MONTH        SELL the ~20-delta call of the ~30-day expiry
  (first trading day)       SELL the ~20-delta put  of the same expiry
                            → collect both premiums

  END OF EACH WEEK          close the CHEAPER leg — the one that has won
                            re-sell the same type near the other leg's price
                            → re-centres the position, banks the winner

  EXIT DAY                  buy back both legs
                            (last trading day on or before the Wednesday
                             before expiry, for a stock)
```

Three details a beginner should not skip:

**"20-delta" means roughly a 20% chance of finishing in the money.** Delta
([document 05](05-pricing-iv-and-greeks.md)) approximates the probability that
an option ends up worth something. Selling the 20-delta call and put picks
strikes far enough out that the stock has to move substantially before either
hurts — you are insuring against a fairly unusual event, and being paid less for
it than you would be at a closer strike.

**The weekly adjustment is a delta hedge.** If the stock drifts up, the call
becomes dangerous and the put becomes nearly worthless. Closing the won leg and
re-selling it nearer the money re-centres the position around the new price.
This is the most expensive habit in the strategy — it is why a trade places
10–12 orders rather than 4 ([document 18 §3](18-what-a-trade-costs.md)).

**Earnings months are skipped entirely.** If a company's results fall between
entry and expiry, the month is not traded. Implied volatility collapses after
results, so being short a strangle across an earnings date is a different trade
with a different risk profile. This skip is not a clever filter added later — it
is in the strategy by construction, and it costs one month in six.

---

## 2. Gate 0 — PASS

**7,706 trades · 217 symbols · 61 months · 2021-08 → 2026-09**

| Per trade | |
|---|---|
| **Mean return on margin** | **+4.56%** |
| Median | +9.51% |
| Win rate | 67.1% |
| Mean win / mean loss | +16.73% / −20.26% |
| Worst single trade | **−245.8%** |
| 1st / 5th percentile | −74.6% / −36.2% |

| Portfolio, pooled by month | |
|---|---|
| Mean monthly return | +4.93% |
| Worst month | −16.12% |
| **Max drawdown** | **−37.27%** |
| Sharpe (annualised) | 2.16 |

Both pass marks were cleared: mean net return > 0 (**+4.56%**), and positive in
at least 4 of 5 years (**6 of 6**).

### Reading that table properly

Three of those rows are routinely misread, so here is what each actually says.

**The win rate is the least informative number on the page.** 67.1% sounds
excellent. But the mean loss (−20.26%) is *larger* than the mean win (+16.73%),
which means winning two trades in three is a requirement, not an achievement.
Work it through: 0.671 × 16.73 − 0.329 × 20.26 = +4.56. The win rate and the
average sizes are two halves of one number, and quoting either alone is
misleading. **A strategy can win 90% of the time and still lose money.**

**The median is double the mean.** Most trades do better than the average trade.
That gap is the signature of a **left-skewed** distribution — a pile of modest
wins with a long tail of large losses dragging the average down. This is the
characteristic shape of premium selling, and it is exactly what you would expect
from selling insurance.

**The worst trade lost 245.8% of the capital posted for it.** Not 245.8% of the
premium — of the *margin*. One position, one month, losing nearly two and a half
times the money committed to it. This is what "open-ended risk" means in
practice, and it is why the strategy is never described here as safe.

### Max drawdown, and why it is the binding number

**A drawdown is the peak-to-trough fall in cumulative value.** If the book grows
to ₹1,000,000 and then falls to ₹627,300 before recovering, that is a −37.27%
drawdown, and it is what the trader actually lives through.

It is a more honest risk measure than volatility or Sharpe because it is the
number that makes people stop. A strategy that eventually recovers is worthless
if you abandoned it at the bottom, and a −37% fall is well within the range that
causes that.

**The Sharpe ratio** (2.16) is return divided by volatility — a "how much bumpiness
per unit of gain" measure. It is quoted here for completeness and should be
discounted: Sharpe assumes a roughly bell-shaped return distribution, and this
one has a long left tail by construction. **Sharpe systematically flatters
strategies that sell insurance**, because their risk is concentrated in rare
events that a volatility measure barely registers.

### The trend matters more than the headline

| Year | Trades | Mean return | Win rate |
|---|---|---|---|
| 2021 | 734 | **+8.30%** | 73.3% |
| 2022 | 1,629 | +6.92% | 67.8% |
| 2023 | 1,247 | +6.86% | 74.9% |
| 2024 | 1,460 | +3.65% | 65.0% |
| 2025 | 1,671 | **+1.09%** | 61.9% |
| 2026 | 965 | +2.15% | 63.4% |

**The edge falls roughly eightfold from 2021 to 2025.** Six positive years is
the pass mark, and it was met — but a strategy whose edge is decaying
monotonically is not the same proposition as one earning a stable 4.56%.

Two candidate explanations, neither tested:

1. **The October 2024 STT increase** (0.0625% → 0.1% on sold options) raised the
   cost of every leg the strategy sells.
2. **India's options market has grown enormously more competitive** over the
   window. More sellers chasing the same premium compresses it — which is what
   you would expect to happen to any published edge.

The second is the more worrying, because it has no floor.

### Only half of candidate months actually trade

| Reason | Months | Share |
|---|---|---|
| **Traded** | 7,706 | **51.0%** |
| No trading days | 4,685 | 31.0% |
| Earnings conflict | 2,505 | 16.6% |
| Missing exit price | 191 | 1.3% |
| No suitable options | 5 | 0.03% |
| Degenerate exit chain | 2 | 0.01% |
| Failed hedge adjustment | 2 | 0.01% |

Publishing this table was a *mandatory disclosure fixed in advance*, and it is
worth understanding why that mattered. "No trading days" is mostly symbols that
had not yet joined the F&O universe in the early years — not a defect. But a
skip taxonomy is exactly where silent data loss hides, and **this project lost
3,308 rows to a silent write failure that this table is what exposed**
([document 15](15-bugs-and-lessons.md)).

---

## 3. Gate 1 — FAIL

The question: do this project's research features improve the strategy, compared
to selling on implied-volatility rank alone?

**Answer: no. And neither does IV rank.**

### The comparison

Unfiltered, trading every eligible symbol-month: **+4.56%**. Keeping the best
30% of trades each month by each feature:

| Feature | Trades | Mean | vs unfiltered | Beats chance? |
|---|---|---|---|---|
| **`ce_ivr`** — IV rank, the baseline | 1,662 | +4.76% | +0.20pp | ❌ |
| `iv_rv_spread` | 1,724 | +4.04% | −0.52pp | ❌ |
| **`fut_basis_excess`** | 1,039 | **+0.96%** | **−3.59pp** | ❌ |
| `dist_to_flip` | 1,856 | +4.15% | −0.41pp | ❌ |
| `gex_z` | 2,136 | +3.76% | −0.80pp | ❌ |
| `oi_outlier_share` | 375 | +4.42% | −0.14pp | ❌ |

**Chance p95 at this selectivity: +5.20%.** That is the bar — pick a *random*
30% of trades and you beat +5.20% one time in twenty. **Nothing here reaches
it**, including IV rank.

So the finding is broader and more useful than "our features lost":

> **Selectivity does not help this strategy.** Trading every eligible
> symbol-month is as good as any filter tested.

### The only significant result is a harmful one

Monthly block bootstrap, 1,500 draws, paired so the filtered and unfiltered
versions always come from the same months:

| Feature | Edge | 95% CI | |
|---|---|---|---|
| `ce_ivr` | +0.23pp | [−1.27, +1.56] | straddles zero |
| **`fut_basis_excess`** | **−3.61pp** | **[−6.33, −1.21]** | **significantly negative** |
| `gex_z` | −0.78pp | [−1.77, +0.11] | straddles zero |
| `dist_to_flip` | −0.42pp | [−1.40, +0.58] | straddles zero |
| `iv_rv_spread` | −0.52pp | [−2.21, +1.04] | straddles zero |
| `oi_outlier_share` | −0.07pp | [−3.78, +4.20] | straddles zero |

A confidence interval that straddles zero means the data cannot distinguish the
feature from no effect at all. Five of six do.

The sixth is **`fut_basis_excess`** — the project's strongest constructed
feature, the one that detects mutual-fund accumulation at +0.1374. Filtering on
it makes the strategy **materially worse**, significantly so.

That is not a contradiction. It is a reminder of the lesson from
[document 17](17-selling-volatility.md): the feature detects something real
about *who is accumulating*, and that is simply not the same quantity as *which
strangles are safe to sell*. A good detector pointed at the wrong question is a
bad filter.

### Two further checks, both negative

**Walk-forward** — pick the threshold using only the past, trade the next month,
repeat across 37 folds. **Negative for five of the six features.** Any filter a
trader could actually have selected in real time would have hurt them.

**Stability across years** — every feature flips sign. `fut_basis_excess` swings
from **+12.76pp to −11.02pp in consecutive years.** A filter that helps by 12
points one year and hurts by 11 the next has no stable relationship with the
outcome; it is fitting noise.

### The harness works — which is what makes the null a measurement

Two checks were run before any result was read:

| Check | Expected | Observed |
|---|---|---|
| **Feature lag** — every feature strictly prior to entry | all ≥ 1 day | min 1, median 3, max 4, **none ≤ 0** ✅ |
| **Leakage tripwire** — a filter secretly given the outcome | must dominate | 4.56% → **23.64%** (+19.1pp) ✅ |

The tripwire is the important one. It proves the comparison machinery **can**
detect an edge of this kind when one is present. Without it, "we found nothing"
and "our measuring device is broken" are indistinguishable.

> One apparent exception is worth naming, because it is a trap: at a 20%
> selectivity `oi_outlier_share` shows **+7.02%** and appears to beat
> everything. **It selects one trade.** Reported for completeness; it means
> nothing.

---

## 4. The stop loss — measured, rejected

Given a −37.27% drawdown, the obvious remaining lever is a **stop loss**: close
the position automatically once the loss reaches some multiple of the premium
collected, rather than riding it to expiry.

Nearly every practitioner would tell you to do this. It was tested at 1.5×, 2×
and 3× the entry credit, on the **common set of 7,706 trades**.

| | Mean | 5th pct | Worst trade | Max DD | Sharpe |
|---|---|---|---|---|---|
| **no stop** | **+4.56%** | **−36.25%** | −245.84% | **−37.27%** | **2.16** |
| 1.5× credit | +4.25% | −37.94% | −245.56% | −38.88% | 2.00 |

**Every single column gets worse.** Not a trade-off — less return *and* more
risk.

Two mechanisms explain it:

1. **Short volatility mean-reverts.** A strangle that goes against you often
   recovers as the stock settles back into its range and time decay resumes. A
   stop crystallises exactly those losses — it sells at the worst moment and
   forfeits the recovery.
2. **The stop fires on a weekly mark.** The one genuine catastrophe in the
   sample gapped straight through it. A stop cannot protect you from a move that
   happens between two observations, and that is precisely the move that hurts.

> **It catches the recoverable losses and misses the unrecoverable one.**

That sentence is the result. It is also a small but real contribution: a
plausible, widely-recommended intervention, measured properly, and found to be
harmful in both directions at once.

### Why "the common set of 7,706 trades" is in bold

Because the first version of this comparison was wrong, and wrong in an
instructive way.

It showed June 2023 at **−56.79%** under a 3× stop against +4.76% for base — a
stop turning a positive month into a catastrophe. Striking, publishable, and
false:

| 2023-06 | base | stop3 |
|---|---|---|
| Rows | 222 | 8 |
| **Priced trades** | **2** | **8** |

A stop provides an exit *before* the scheduled exit date, so months whose
exit-day lookup fails under the base strategy get priced under the stop
variants. **The comparison was measuring a different set of trades, not a
different strategy.** On the common set the effect vanishes entirely: worst
month −16.12% against −16.26%.

> **The lesson:** when a change appears to help or harm dramatically, check
> whether it changed *which observations exist* before believing it changed
> their outcomes.

The research branch hit the identical trap in Stage 1, scoring a calendar
baseline on 1,004 rows against flow models' 556. Same trap, same fix, two
branches apart.

---

## 5. What Part III has established

**Positive:**

- Systematic short-strangle selling on NSE stocks earned a positive net-of-cost
  return in **every year** of 2021–2026, and the verdict survives a ±25% margin
  sensitivity band.

**Negative, and more useful:**

- This project's research features do not improve it. **Neither does IV rank.**
- Selectivity itself does not help — trading everything eligible is as good as
  any filter tested.
- A stop loss makes every measured outcome worse.
- The edge is decaying roughly eightfold across the sample.

**Unestablished, and the binding constraint:**

> The **−37.27% drawdown is unmitigated**, and nothing tested reduces it. The
> sample contains **no severe crash**, which is precisely where a short
> strangle's risk lives.

A +4.93% mean monthly portfolio return is on its face too good — roughly +78%
annualised. Five reasons to discount it are recorded, and the first is that
missing crash.

### The honest limits, in full

| | |
|---|---|
| **No crash in the sample** | The largest known weakness, and unquantifiable from inside the window |
| **Settlement ≠ executable** | Bounded by the 3.16% breakeven, not removed. One entry leg in ten traded ≤12 lots. |
| **Margin is an approximation** | Not exchange SPAN, which bhavcopy does not publish |
| **Cost rates unverified** | Brokerage is 57.9% of costs at an assumed ₹20 flat, never checked against a contract note |
| **No capital constraint in Gate 0** | The monthly portfolio assumes every eligible trade is funded |
| **Sector cap is inert** | Implemented and tested, but the symbol catalog carries no sector field |
| **Everything is in-sample** | Until [Gate 3](22-forward-validation.md) has six months |

[← The four gates](19-the-four-gates.md) · [Index](README.md) · Next: [The daily screen →](21-the-daily-screen.md)
