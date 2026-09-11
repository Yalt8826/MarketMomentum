# 03 — Phase 0: make the P&L economically real

[← Strategy](02-strategy-and-mechanics.md) · [Index](README.md) · Next: [Phase 1 →](04-phase-1-evaluation.md)

**The question Phase 0 answers:** ignoring every feature this project built,
does systematic strangle-selling on NSE stocks earn a positive return *after
costs*?

**Nothing downstream may be built until Gate 0 reports a number.** If the base
strategy loses money, no filter saves it, and the honest output of this branch
is a negative result.

---

## 1. Task 0.1 — `backtest/economics.py`

Pure functions, no I/O, unit-tested standalone — the same pattern as
`options/pricing.py`, and for the same reason: this is where arithmetic errors
silently become financial claims.

```python
def contract_value(points: float, lot_size: int) -> float:
    """Points per share -> rupees for one contract."""

def order_costs(premium: float, lot_size: int, side: Literal["buy", "sell"],
                rates: CostRates) -> float:
    """All-in cost of ONE order. Itemised, never a single fudge factor."""

def trade_costs(legs: Sequence[Leg], rates: CostRates) -> CostBreakdown:
    """Total across 4 + 2*n_adjustments orders, itemised by component."""

def strangle_margin(spot: float, lot_size: int, call_strike: float,
                    put_strike: float, iv: float, model: MarginModel) -> float:
    """SPAN + exposure approximation. AN APPROXIMATION — see section 3."""

def apply_slippage(premium: float, side: Literal["buy", "sell"],
                   fraction: float) -> float:
    """Sell fills lower, buy fills higher. Always against you."""
```

### The cost components

Every one is charged per order, and the strategy places **10–12 orders per
trade** (§2 of [the strategy doc](02-strategy-and-mechanics.md)):

| Component | Applies to | Notes |
|---|---|---|
| Brokerage | every order | Discount brokers charge a flat per-order fee |
| **STT** | **sell side only**, on premium | The largest single component for a writer |
| Exchange transaction charge | every order, on premium | NSE publishes a rate per crore of premium |
| SEBI turnover fee | every order | Small |
| Stamp duty | **buy side only** | Small |
| GST | on (brokerage + transaction + SEBI) | Compounds the rest |
| IPFT | every order | Small |

> ⚠️ **Rates change, and this document must not pretend otherwise.** STT on
> options sales has been revised more than once, and exchange charges were cut
> in recent years. **Do not hard-code rates.** Put them in a `CostRates`
> dataclass with an `effective_from` date and a source note, default them from
> config, and have the write-up state the rate set used. A backtest run under
> stale rates is not wrong so much as *undated*, which is worse.

**Acceptance:** costs must be reported **itemised**, not as a lump. When Gate 0
is marginal — and it may well be — the question "which component killed it" has
to be answerable. The expected answer is STT plus the adjustment order count.

## 2. Task 0.2 — schema

```sql
ALTER TABLE backtest_results ADD COLUMN variant           TEXT NOT NULL DEFAULT 'base';
ALTER TABLE backtest_results ADD COLUMN lot_size          INTEGER;
ALTER TABLE backtest_results ADD COLUMN n_adjustments     INTEGER;
ALTER TABLE backtest_results ADD COLUMN gross_pnl_rupees  DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN costs_rupees      DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN slippage_rupees   DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN net_pnl_rupees    DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN margin_rupees     DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN return_on_margin  DOUBLE PRECISION;
-- PK becomes (variant, symbol, year, month)
```

`variant` is load-bearing: without it a parameter sweep overwrites itself and
Phase 1 cannot compare anything. Keep `gross`, `costs`, `slippage` and `net`
separate — collapsing them loses the ability to answer §1's question.

Mirror the change in `sql/backtest_results.sql` and re-run `init_db.sql`, which
is idempotent.

## 3. Task 0.3 — the margin model, and its honesty

Exchange margin is SPAN + exposure, computed from a daily risk array that
bhavcopy does not publish. **It cannot be reproduced exactly**, so:

- Implement a documented approximation — a percentage of contract notional, with
  the strangle receiving the benefit that both legs cannot lose simultaneously.
- Expose it as a `MarginModel` with explicit parameters, never as a magic number
  buried in a function.
- **Run the whole of Phase 0 at ±25% on the margin assumption** and report all
  three results. If the verdict flips inside that band, the verdict is the
  margin assumption, not the strategy, and must be reported as such.

Margin only scales the denominator, so it cannot turn a loss into a profit — but
it entirely determines whether a profit is *interesting*. A 2% return on margin
and a 20% return on margin are different conclusions from the same trades.

## 4. Task 0.4 — lot sizes

`data/lots.py` holds an 852-row change-point schedule. Two rules:

- Resolve the lot **in force on the entry date**, not the latest one. Symbols
  carry a median of 3 revisions and up to 8.
- A symbol-month whose lot cannot be resolved is **skipped with a reason**, never
  defaulted. Guessing a lot size silently mis-scales that trade's entire P&L.

Record the resolved `lot_size` on the row so any result can be audited back.

## 5. Task 0.5 — slippage, handled by not pretending

There is no bid-ask in the data. Rather than invent a spread:

1. Implement slippage as a configurable fraction of premium, applied **against**
   the trader on every order (sell fills lower, buy fills higher).
2. Run Phase 0 across a grid — 0%, 1%, 2%, 5% — and
3. **Report the breakeven slippage**: the level at which net return reaches zero.

That last number is the deliverable. "The strategy earns X% on margin and breaks
even at 3.2% slippage per leg" is a claim that survives not knowing the true
spread; "the strategy earns X%" is not.

Also report, as context for whether those fills are plausible at all:

- Share of selected strikes with **zero volume** on the entry day.
- Median volume and open interest of selected strikes.

If a large fraction of chosen 20-delta strikes never traded, the fill assumption
is fiction regardless of the slippage parameter, and that is a finding.

## 6. Task 0.6 — run it

217 symbols × ~60 months ≈ **13,000 candidate trades**.

- Add a progress bar through `progress.py`, like every other long stage.
- Expect a meaningful share to be skipped — no trading days, missing chain, no
  20-delta strike, earnings conflict. **Report the skip taxonomy**; a strategy
  that trades 40% of the months it wants to is a different proposition from one
  that trades 90%.
- Wire it into `pipeline_runner.py` as an optional stage.

## 7. Task 0.7 — `backtest/analysis.py`

Per-trade return on margin, then the portfolio view. **An average is not enough**
— a short-strangle book's whole character is in its left tail.

| Metric | Why |
|---|---|
| Mean / median return on margin | The headline |
| Win rate | Expect high — that is the nature of premium selling |
| **Mean win vs mean loss** | The counterweight; a 75% win rate with 5× losses is a losing strategy |
| Sharpe (monthly, annualised) | Comparability |
| **Worst single month** | The number that decides sizing |
| **Max drawdown of the equity curve** | The number that decides viability |
| p1 / p5 of the trade distribution | The tail, explicitly |
| Skip taxonomy | §6 |
| Cost breakdown | §1 |

Report an **equity curve**, not only summary statistics. A strategy with an
attractive mean and a −60% month is not usable by the audience this branch is
for.

## 8. Gate 0 — acceptance criteria

Fixed **now**, before any number exists, per this project's standing practice.

| # | Criterion | Threshold |
|---|---|---|
| 1 | Mean return on margin, **net of costs and slippage** | **> 0** |
| 2 | Positive in a majority of years | **≥ 4 of 5** |
| 3 | Verdict stable across the margin sensitivity band | ±25% |
| 4 | Breakeven slippage | stated explicitly |
| 5 | Max drawdown | stated, and survivable at intended sizing |
| 6 | Skip taxonomy and zero-volume share | stated |

**Criteria 1 and 2 are pass/fail. Criteria 3–6 are mandatory disclosures** — a
Phase 0 write-up missing any of them is incomplete even if the strategy wins.

### If Gate 0 fails

Stop, and write it up as a negative result in `docs/trading/results/`. This
project has three of those already and they are among its more useful documents.
Do **not** tune parameters until it passes: that converts a measurement into a
search, and the search will succeed on 13,000 trades whether or not there is
anything there.

## 9. The caveat that must appear in every Phase 0 write-up

> **The 2021–2026 sample contains no severe crash.** It has drawdowns, but
> nothing resembling 2008 or March 2020. A short strangle's entire risk lives in
> exactly that tail. A backtest over this window **will** flatter the strategy,
> and the size of the flattery cannot be estimated from inside the window.

This is not boilerplate. It is the single largest known weakness of the Phase 0
result, whatever that result turns out to be.

[← Strategy](02-strategy-and-mechanics.md) · [Index](README.md) · Next: [Phase 1 →](04-phase-1-evaluation.md)
