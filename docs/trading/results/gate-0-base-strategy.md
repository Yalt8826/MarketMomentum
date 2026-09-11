# Gate 0 — does strangle-selling make money at all?

**Run 2026-09-11** · commit `0d1af6d` · `variant = 'base'`
**Verdict: PASS**, with four disclosures and one large unquantifiable risk.

Specification: [`../implementation/03-phase-0-economics.md`](../implementation/03-phase-0-economics.md).
Reproduce: `uv run python -c "from mktmtm.backtest.strangle import run_backtest; run_backtest(years=[2021,2022,2023,2024,2025,2026])"`

---

## 1. The question

Ignoring every feature this project's research built, does systematic
short-strangle selling on NSE stocks earn a positive return **after costs**?

Nothing in Phase 1 matters if the answer is no.

## 2. The result

7,706 trades · 217 symbols · 61 calendar months · 2021-08 → 2026-09

| | Per trade |
|---|---|
| **Mean return on margin** | **+4.56%** |
| Median | +9.51% |
| Win rate | 67.1% |
| Mean win / mean loss | +16.73% / −20.26% |
| Worst single trade | −245.8% |
| 1st / 5th percentile | −74.6% / −36.2% |

| | Portfolio, pooled by month |
|---|---|
| Mean monthly return | +4.93% |
| Worst month | **−16.12%** |
| **Max drawdown** | **−37.27%** |
| Sharpe (annualised) | 2.16 |

The median is double the mean and the mean loss exceeds the mean win: this is the
characteristic shape of premium selling — win often, lose large. **The win rate
is the least informative statistic on the page.**

### Criterion 1 — mean net return > 0 · **PASS** (+4.56%)

### Criterion 2 — positive in ≥ 4 of 5 years · **PASS** (6 of 6)

| Year | Trades | Mean return | Win rate |
|---|---|---|---|
| 2021 | 734 | +8.30% | 73.3% |
| 2022 | 1,629 | +6.92% | 67.8% |
| 2023 | 1,247 | +6.86% | 74.9% |
| 2024 | 1,460 | +3.65% | 65.0% |
| 2025 | 1,671 | **+1.09%** | 61.9% |
| 2026 | 965 | +2.15% | 63.4% |

**The trend is the most important thing in this table.** The edge falls by
roughly 8× from 2021 to 2025. Two candidate explanations, neither tested here:
the October 2024 STT increase (0.0625% → 0.1%) raised the cost of every sold
leg, and India's options market has grown enormously more competitive over the
window. A strategy whose edge is decaying monotonically is not the same
proposition as one earning a stable 4.56%.

---

## 3. The four mandatory disclosures

### 3.1 Margin sensitivity — verdict stable

| Margin assumption | Mean return |
|---|---|
| 0.75× (9% of notional) | +6.08% |
| **1.00× (12%)** | **+4.56%** |
| 1.25× (15%) | +3.65% |

Positive throughout, so the verdict does not rest on the margin model. But the
*size* does: this is an approximation, not exchange SPAN, which bhavcopy does not
publish.

### 3.2 Breakeven slippage — **3.16% per leg**

The strategy's edge reaches zero if each leg fills 3.16% worse than its
settlement mark. Reported instead of a point estimate because bhavcopy carries
no bid/ask.

Whether 3.16% is comfortable depends on the name: it is generous for a liquid
large-cap option and thin for the illiquid tail below.

### 3.3 Liquidity of the selected strikes

| | |
|---|---|
| Zero-volume entry legs | **0.0%** (enforced — see §4) |
| Median entry-leg volume | 223 contracts |
| **10th percentile** | **12 contracts** |

Zero by construction now, but the p10 is the number to hold onto: **one entry leg
in ten traded 12 contracts or fewer on the day it was sold.** At that depth the
settlement mark is a weak guide to an executable price, and the 3.16% breakeven
is correspondingly less comfortable.

### 3.4 Skip taxonomy — only **51%** of candidate months trade

| Reason | Months | Share |
|---|---|---|
| **Traded** | 7,706 | **51.0%** |
| No trading days | 4,685 | 31.0% |
| Earnings conflict | 2,505 | 16.6% |
| Missing exit price | 191 | 1.3% |
| No suitable options | 5 | 0.03% |
| Degenerate exit chain | 2 | 0.01% |
| Failed hedge adjustment | 2 | 0.01% |

"No trading days" is mostly symbols that had not yet joined the F&O universe in
the earlier years, not a defect. The earnings filter costs one month in six and
is the strategy's own design — implied volatility collapses after results, so
being short a strangle across one is a different trade.

### 3.5 Cost composition

| Component | Mean ₹/trade | Share |
|---|---|---|
| **Brokerage** | 213.19 | **57.9%** |
| Exchange transaction | 56.13 | 15.2% |
| GST | 48.62 | 13.2% |
| STT | 47.84 | 13.0% |
| Stamp duty, IPFT, SEBI | 2.62 | 0.7% |

**Brokerage dominates**, because the strategy places 10–12 orders per trade
(`4 + 2 × adjustments`) and it is charged flat per order. At ₹0 brokerage —
offered by some brokers — total costs fall by more than half. This is why
brokerage is a swept scenario (`BROKERAGE_SCENARIOS`) rather than a constant.

---

## 4. Three defects found while producing this number

The first verdict reported a worst trade of **−1,677%** — a loss of 16× the
margin posted, which this strategy cannot produce. Chasing it found three
defects, two pre-existing and all three silent.

| # | Defect | Extent |
|---|---|---|
| 1 | Untraded strikes priced anyway — NSE marks every listed strike, and the weekly hedge selected them | 32 of 7,683 trades, −₹5.7M |
| 2 | **Degenerate chains** — the underlying's price written into every option's premium | 47 of 213,379 symbol-days (0.022%) |
| 3 | **3,308 rows never persisted**, in every run | skip taxonomy under-reported |

**Defect 2 is the instructive one.** On NMDC 2022-10-25 all 206 chain elements
carried an identical settle of ₹130.75 against a spot of ₹130.71, every delta
0.0. Neither obvious filter catches it: volume does not, because affected strikes
include one with 2,492 lots traded; and the no-arbitrage bound (a call cannot
exceed the stock) does not, because the corrupt value sits at **99.97% of spot**,
inside any tolerance loose enough for a legitimate deep-ITM call.

The check must also be **per expiry**. IOC 2022-06-29 is the same defect in
partial form — 146 of 436 rows at the spot price, all in the expiring series,
while July and August price normally. A whole-chain test sees 193 distinct
settles and passes it. That single trade was the −1,677%.

**Defect 3** had been dropping data since before this branch: `entry_date` was
`NOT NULL`, a month with no trading days has none, and the writer logs the
rejection and continues. The run reported success while 3,308 months vanished.

### What the fixes did to the answer

| Stage | Mean return | Worst trade |
|---|---|---|
| Contaminated | +4.79% | −1,677% |
| + liquidity gate | +4.15% | −1,677% |
| + degeneracy gate, + persistence fix | **+4.56%** | **−245.8%** |

The headline moved by less than half a percentage point while the worst trade
fell by a factor of seven. **The mean was never the contaminated statistic — the
tail was**, which is exactly the part of a premium-selling distribution that
decides whether the strategy is usable.

---

## 5. What this does not establish

**The sample contains no severe crash.** 2021–2026 has drawdowns but nothing
like 2008 or March 2020, and a short strangle's entire risk lives in that tail.
This backtest **will** flatter the strategy and the size of the flattery cannot
be estimated from inside the window. This is the single largest known weakness
of the result.

Also outstanding:

- **Settlement is a mark, not a fill.** §3.2 and §3.3 bound this; they do not
  remove it.
- **The margin model is an approximation**, not exchange SPAN.
- **No capital constraint.** The monthly portfolio assumes every eligible trade
  is funded. A real book has finite margin and would hold a subset.
- **No stop loss.** Positions run to exit day regardless of drawdown, which real
  vol sellers do not do. A stop is a Phase 1 parameter.
- **Rates are as-published, not from a contract note.** STT and exchange charges
  are pinned to their statutory regimes; brokerage is swept.

A mean monthly portfolio return of +4.93% is, on its face, too good — it would
compound to roughly +78% a year. The caveats above are the reasons to discount
it, and the −37.27% drawdown is the reason to size it carefully even if it holds.

---

## 6. Gate 0 verdict and what follows

**PASS.** Systematic strangle-selling on NSE stocks earned a positive
net-of-cost return in every year of the sample, and the verdict survives the
margin sensitivity band.

That licenses Phase 1, and only Phase 1:

> **Do this project's research features beat selling on IV rank alone?**

Gate 0 says there is a strategy. It says nothing about whether the research
improves it, and [the prior remains that it does
not](../implementation/04-phase-1-evaluation.md#6-the-honest-prior) — every
constructed feature sits at ≤0.05 partial IC against implied volatility's own
+0.2114.

The decaying yearly edge (§2) is the first thing Phase 1 should condition on.
If the effect is concentrated in 2021–2023 and gone by 2025, a filter fitted on
the full sample will look better than anything a trader could have used.
