# 18 — What a Trade Actually Costs

[← Selling volatility](17-selling-volatility.md) · [Index](README.md) · Next: [The four gates →](19-the-four-gates.md)

The research half of this project never had to think about money. It computed
correlations between features and outcomes; a correlation has no brokerage.

The moment you claim a *strategy*, four things you could previously ignore
become the whole argument: **lot size, costs, margin, and slippage.** This
document explains each from scratch, because a reader who skips them will
misread every number in [document 20](20-does-it-make-money.md).

The code is `backtest/economics.py` — pure functions, no database, unit-tested
standalone, in the same spirit as `options/pricing.py`. Its own module docstring
says why: *"this is where an arithmetic slip silently becomes a financial
claim."*

---

## 1. The problem: "points" are not money

The backtester originally reported this:

```python
pnl_points = entry_credit - exit_cost + adjustment_pnl
```

That is **points per share** — the premium collected per single share, in
rupees. It is not a profit, and worse, it is **not comparable between stocks**:

> 20 points on a ₹200 stock and 20 points on a ₹4,000 stock are wholly different
> trades, and the old code recorded them identically.

The first is a 10% move's worth of premium; the second is 0.5%. Averaging them
together produces a number that means nothing at all. Four pieces are needed to
turn points into a return.

```mermaid
flowchart LR
    P["points<br/>per share"] -->|"× lot size"| R["rupees<br/>per contract"]
    R -->|"− costs"| N["net rupees"]
    N -->|"÷ margin"| RET["<b>return on<br/>capital</b>"]

    style P fill:#3d2f00,stroke:#ffc107,color:#fff
    style RET fill:#1a3a1a,stroke:#4caf50,color:#fff
```

---

## 2. Lot size — you cannot buy one option

Indian derivatives are not traded one share at a time. Every contract covers a
fixed bundle of shares called the **lot size**, set by the exchange per symbol
and revised periodically. RELIANCE might trade in lots of 500; a cheaper stock
might trade in lots of 5,000. The exchange sets it so that one contract is worth
roughly ₹5–10 lakh, which is why cheap stocks get big lots.

So:

```
   27 points collected  ×  lot size 500  =  ₹13,500 actually received
```

Two consequences that matter:

- **You cannot take a small position.** The minimum trade in a ₹1,000 stock with
  a 500 lot commits you to ₹500,000 of exposure. This is why the screen
  ([document 21](21-the-daily-screen.md)) refuses any symbol whose lot size it
  cannot resolve: guessing mis-scales the entire position, and a position that
  is twice the intended size is not a small error.
- **Lot sizes change over time.** The project carries a schedule with **852
  change-points** across the symbol universe. Using today's lot size on a 2022
  trade would silently mis-state that trade's rupee outcome.

> This is also the source of one of the project's classic bugs: bhavcopy reports
> open interest in **shares**, not contracts, so the lot size *divides* to
> recover a contract count — it never multiplies. The reference file asserted
> the reverse, and the raw sum was off by a factor of **2,158**. See
> [document 15 §3](15-bugs-and-lessons.md).

---

## 3. Costs — five statutory charges plus brokerage

Every order in India carries a stack of charges. They are small individually and
decisive in aggregate, because this strategy places a lot of orders.

| Charge | Rate | Applies to |
|---|---|---|
| **Brokerage** | ₹20 flat **per order** | Every order |
| **STT** (Securities Transaction Tax) | 0.1% of premium turnover | **Sell side only** |
| Exchange transaction | 0.03503% | Both sides |
| GST | 18% | On brokerage + exchange + SEBI + IPFT — *not* on STT or stamp duty |
| Stamp duty | 0.003% | **Buy side only** |
| SEBI turnover fee | ₹10 per crore | Both sides |
| IPFT (investor protection levy) | ₹50 per crore | Both sides |

Note "turnover" here means **premium turnover** — `premium × lot_size`, the
money that actually changed hands — not the notional value of the underlying
shares.

### The order count is the thing that bites

This is the non-obvious part, and it nearly decided Gate 0 on its own.

The strategy does not place two orders. It re-centres itself every week (a
**delta hedge** — see [document 20 §1](20-does-it-make-money.md)), and each
adjustment is two more orders:

```
   entry            2 orders   (sell the call, sell the put)
   per adjustment   2 orders   (buy back one leg, sell a replacement)
   exit             2 orders   (buy back both)

   total = 4 + 2 × n_adjustments
```

A typical month has three or four hedge dates, so **10 to 12 orders per trade.**

> A cost model that charges only for entry and exit understates the real figure
> by roughly a factor of three. The weekly adjustment is the strategy's most
> expensive habit, and it looks free when you measure in points.

And the measured composition confirms it — from the Gate 0 run, mean rupees per
trade:

| Component | ₹/trade | Share |
|---|---|---|
| **Brokerage** | 213.19 | **57.9%** |
| Exchange transaction | 56.13 | 15.2% |
| GST | 48.62 | 13.2% |
| STT | 47.84 | 13.0% |
| Stamp duty, IPFT, SEBI | 2.62 | 0.7% |

**Brokerage is the majority of all costs**, purely because it is a flat fee
charged ten to twelve times. That is why brokerage is a *swept scenario*
(`BROKERAGE_SCENARIOS`) rather than a fixed constant: several Indian brokers
charge ₹0 on options, and at zero brokerage the strategy's total costs fall by
more than half.

### Rates are dated, not hard-coded

Every rate lives in a dated `CostRates` set resolved by the trade's date. This
is not fastidiousness — STT on sold options **rose from 0.0625% to 0.1% in
October 2024**, and that rise is one of the two candidate explanations for the
strategy's decaying edge ([document 20 §3](20-does-it-make-money.md)). A
backtest run under a single undated rate could not even ask the question.

> The module's own docstring is blunt about the limit: the defaults "**must be
> verified against your broker's contract note and the current NSE circulars
> before any result is relied upon** — they are a starting point, not an
> authority." Nobody has checked them against a real contract note. That is
> listed as an honest limit, not quietly assumed away.

---

## 4. Margin — the denominator, and why it is the hard one

### What margin is

When you *buy* an option, your maximum loss is the premium, so you pay the
premium and that is that. When you **write** one, your loss is open-ended — so
the exchange will not let you do it on a promise. It requires you to post
collateral, called **margin**, which it holds for the life of the position and
returns when you close.

Margin is not a cost. You get it back. But it is **capital you cannot use for
anything else while the trade is open**, and that makes it the correct
denominator for measuring performance.

### Why margin and not notional

Suppose a strangle on a ₹1,000 stock with a 500 lot earns ₹12,386.

- Measured against the **notional** value of the shares (₹500,000), that is
  **2.5%** — and meaningless, because you never had to put up ₹500,000.
- Measured against the **margin** actually blocked (about ₹60,000), it is
  **20.6%** — the return on the capital the trade genuinely consumed.

Return on margin is what lets you compare a strangle on a ₹200 stock with one on
a ₹4,000 stock, and it is what every number in [document 20](20-does-it-make-money.md)
is expressed in.

### The approximation, stated as one

Real margin is **SPAN + exposure**, computed by the exchange each day from a
risk array that bhavcopy does not publish. **It cannot be reproduced exactly.**

The project models it as a flat percentage of contract notional:

```
margin ≈ spot × lot_size × (9% SPAN + 3% exposure)  =  12%
```

Because this is an assumption rather than a measurement, every conclusion is run
through a **±25% sensitivity band** and all three results reported:

| Margin assumption | Mean return per trade |
|---|---|
| 0.75× (9% of notional) | +6.08% |
| **1.00× (12%)** | **+4.56%** |
| 1.25× (15%) | +3.65% |

**Positive throughout — so the verdict does not depend on the margin model.**
But the *size* does, and it moves by nearly two and a half points across the
band. Margin only scales a denominator, so it can never turn a loss into a
profit; it entirely decides whether a profit is interesting.

---

## 5. Slippage — the cost that cannot be measured here

### The problem

Every price in this backtest is a **settlement price** — the exchange's official
end-of-day mark for each contract. That is the right number for *valuation*, and
the project's own data invariants insist on it, because a settlement price
exists even for a contract that never traded that day (where the "close" would
be a stale price from days ago).

But a settlement price is **not a price anyone traded at**. In reality:

- Every option has a **bid** (what buyers will pay) and an **ask** (what sellers
  want). You sell at the bid and buy at the ask, and the gap between them is a
  real cost on every order.
- On a thinly traded strike that gap can be enormous.
- **Bhavcopy publishes no bid or ask at all.** The spread is not merely
  unmeasured here — it is unmeasurable from this data.

### The honest answer: breakeven slippage

Rather than inventing a spread estimate, the project reports the level at which
the strategy stops working:

> **Breakeven slippage: 3.16% per leg.**
>
> If every leg fills 3.16% worse than its settlement mark, the entire edge
> reaches zero.

This is a claim that survives not knowing the true spread. It hands the reader
the number they need to judge it for themselves: 3.16% on a ₹15 option is about
47 paise of slack per leg. Generous for a liquid large-cap option; uncomfortably
thin for the illiquid tail.

And the liquidity data says the illiquid tail is real:

| | |
|---|---|
| Median entry-leg volume | 223 contracts |
| **10th percentile** | **12 contracts** |

**One entry leg in ten traded twelve contracts or fewer on the day it was sold.**
At that depth the settlement mark is a weak guide to an executable price, and
3.16% of slack is correspondingly less comfortable. This is why the live screen
enforces a minimum-volume gate and refuses legs that never traded.

---

## 6. A complete worked example

A winning month, priced end to end. Stock at ₹1,000, lot size 500, entry and
exit only (a real trade also carries weekly adjustments — see the note below).

**The trade**

| | |
|---|---|
| Sell 1,150 call | ₹15/share |
| Sell 870 put | ₹12/share |
| Credit collected | 27 × 500 = **₹13,500** |
| Margin blocked | 1,000 × 500 × 12% = **₹60,000** |

**At exit**, the stock sat at ₹1,040 and both legs are nearly worthless; buy
both back for ₹2/share total = ₹1,000.

**The charges**

| Charge | Working | ₹ |
|---|---|---|
| Brokerage | 4 orders × ₹20 | 80.00 |
| STT (sell side only) | 0.1% × 13,500 | 13.50 |
| Exchange transaction | 0.03503% × (13,500 + 1,000) | 5.08 |
| Stamp duty (buy side only) | 0.003% × 1,000 | 0.03 |
| SEBI + IPFT | 0.0006% × 14,500 | 0.09 |
| GST | 18% × (80 + 5.08 + 0.09) | 15.33 |
| **Total costs** | | **114.03** |

**The outcome**

| | |
|---|---|
| Gross P&L | 13,500 − 1,000 = ₹12,500 |
| Net P&L | 12,500 − 114.03 = **₹12,385.97** |
| **Return on margin** | 12,386 ÷ 60,000 = **+20.6%** |

That is a *good* month — comfortably above the +4.56% average, which is what you
would expect from a trade where nothing went wrong. Two adjustments to keep in
view:

- **A real trade places 10–12 orders, not 4.** Brokerage alone would be ₹200–240
  rather than ₹80, and each adjustment adds its own turnover-based charges.
- **Slippage is not in this table.** At the 3.16% breakeven, roughly ₹0.47 per
  share per leg would come off the credit — about ₹470 on this position, which
  is four times the entire statutory cost stack.

> Read those two together and the shape of the problem is clear: **the visible
> charges are small and the invisible one is large.** The 3.16% breakeven is the
> number that actually decides whether this strategy is tradeable, and it is the
> one the data cannot resolve.

---

## 7. What this buys you

With lot size, costs, margin and slippage in place, a statement like *"mean
return +4.56%"* finally has a meaning you can check:

- **+4.56% of what?** Of the margin blocked, per trade, per month.
- **After what?** After brokerage, STT, exchange fees, GST, stamp duty, SEBI and
  IPFT levies, charged per order across all 10–12 orders.
- **Assuming what?** Fills at settlement (optimistic by an unknown amount,
  bounded at 3.16%), and a 12%-of-notional margin approximation (swept ±25%).

Every one of those qualifiers is load-bearing. [Document 19](19-the-four-gates.md)
explains the testing discipline that keeps them attached to the number.

[← Selling volatility](17-selling-volatility.md) · [Index](README.md) · Next: [The four gates →](19-the-four-gates.md)
