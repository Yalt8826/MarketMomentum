# 05 — Phase 2: the daily tool

[← Phase 1](04-phase-1-evaluation.md) · [Index](README.md) · Next: [Phase 3 →](06-phase-3-forward-validation.md)

> **Rewritten 2026-09-11 after Gate 1 failed.** The original version was written
> as a conditional — "if the features validate, rank by them; if not, fall back
> to IV rank." Both halves are now dead: no feature beat chance, and **neither
> did IV rank**. What follows is the design that the evidence actually supports.
> [Gate 1 results](../results/gate-1-feature-filters.md).

---

## 1. What the evidence permits

Gate 0 established a real result: systematic strangle-selling earned **+4.56%
mean return on margin**, positive in **6 of 6 years**, with a **−37.27% max
drawdown**.

Gate 1 established that **no entry filter improves it** — not the project's
features, not implied-volatility rank, not anything tested. Selecting a random
30% of trades beats every real filter 5% of the time.

Together those say something specific about what the tool can be:

> **The edge is in breadth and discipline, not in selection.**
>
> The strategy earns a high win rate across many small trades. There is no
> subset to find, so the tool's job is not to pick names — it is to make the
> mechanics correct, the costs visible, and the risk survivable.

A product that ranks names by a score would be claiming something Gate 1
refuted. This one does not rank.

## 2. What the tool does

`mktmtm screen` — one command, run after the daily refresh:

```bash
uv run mktmtm run-pipeline --mode no-etl   # incremental; minutes
uv run mktmtm screen                       # NEW
```

It answers: **"which strangles are available today, what would each cost, and
what could each lose?"**

| Field | Why |
|---|---|
| symbol, expiry | Which contract |
| call strike / put strike | At ~20 delta, from today's chain |
| **credit (₹)** | At the lot size in force today |
| **margin required (₹)** | Without it the trade cannot be sized |
| **max loss at ±2σ** | A strangle's true max is unbounded; state the modelled figure and say so |
| entry-leg volume | Can this actually be traded? (§4) |
| days to earnings | Excluded if inside the holding window — see §3 |
| context columns | `ce_ivr`, `fut_basis_excess`, … **labelled as not predictive** |

**Order is by tradeability — liquidity and credit-to-margin — not by a score.**
The context columns are shown because a human looking at a name may know
something the data does not, and suppressing the numbers would be worse than
showing them honestly labelled.

> Every context column carries the note: *"tested in Gate 1; did not beat
> chance."* A number displayed without that would be read as a recommendation.

## 3. What the tool refuses

Gate 0's skip taxonomy is a product feature, not a data-quality footnote. The
screen drops a candidate and **says why**:

| Refusal | Reason |
|---|---|
| Earnings inside the holding window | IV collapses after results; a different trade |
| Lot size unresolvable | Guessing mis-scales the whole position |
| Margin unresolvable | Cannot be sized |
| Entry leg never traded | The mark is not a price (Phase 0, defect 2) |
| **Degenerate chain for that expiry** | The prices are fiction (Phase 0, defect 3) |

That last one is load-bearing: 47 symbol-days in the historical data carried the
underlying's price in every option's premium. A screen that emitted those would
be recommending trades at fabricated prices.

## 4. Liquidity gate

Phase 0 measured the 10th percentile of entry-leg volume at **12 contracts**.
One leg in ten traded 12 lots or fewer on the day it was sold.

At that depth the settlement mark is a weak guide to an executable price, and
Gate 0's **3.16% breakeven slippage** is correspondingly less comfortable. The
screen therefore:

- drops any leg with zero volume outright;
- **shows the volume** for the rest, so a thin name is visible as thin.

## 5. Risk controls — the only lever left

Gate 1 removed entry selection as a lever. That makes sizing and stops the
*whole* remaining design surface, not an afterthought.

| Control | Setting |
|---|---|
| Max concurrent positions | Hard cap |
| Max fraction of capital in margin | Hard cap |
| **Correlation cap** | 15 strangles in one sector is one trade wearing fifteen hats |
| Per-trade stop | A multiple of credit received — **untested; see §7** |

A short strangle can lose more than the capital allocated to it. Gate 0's worst
single trade was **−245.8% of margin**, and its worst month **−16.12%**. These
are product features, not disclaimers.

## 6. Presentation

The Streamlit UI exists, renders charts, and runs jobs as detached subprocesses.
**Add a page; do not build a front end.**

> ⚠️ `mktmtm-ui` binds the tailnet IP and has **no authentication**. It must
> never bind `0.0.0.0`. It is now a panel that suggests trades, which raises the
> cost of getting that wrong.

## 7. What the tool must say about itself

The screen ships with its own limits on its face, the way the nowcast record
does:

- **Backtested return: +4.56% per trade, −37.27% max drawdown.** Both numbers,
  always together.
- **No crash in the sample.** 2021–2026 contains nothing like 2008 or March
  2020, and that is precisely where this strategy's risk lives.
- **The edge is decaying**: 8.30% (2021) → 1.09% (2025). Whether that is the
  October 2024 STT rise or a maturing market is untested.
- **No filter is validated.** Gate 1 tested six and none beat chance.
- **The stop loss is untested.** Gate 0 ran positions to expiry; a stop changes
  the return distribution and has not been measured.

## 8. Acceptance criteria

| # | Criterion |
|---|---|
| 1 | Every candidate carries credit, margin and modelled max loss |
| 2 | Every refusal carries a typed reason |
| 3 | Context columns are labelled as not predictive |
| 4 | Risk caps bind, and are configurable |
| 5 | No candidate is emitted from a degenerate expiry or a zero-volume leg |
| 6 | The self-description in §7 appears in the output, not only the docs |

[← Phase 1](04-phase-1-evaluation.md) · [Index](README.md) · Next: [Phase 3 →](06-phase-3-forward-validation.md)
