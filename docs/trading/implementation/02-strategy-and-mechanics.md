# 02 — The strategy, as actually implemented

[← Handoff](01-handoff-from-research.md) · [Index](README.md) · Next: [Phase 0 →](03-phase-0-economics.md)

`backtest/strangle.py`, 917 lines, ported from a verified legacy script. The
mechanics are complete and unit-tested. This document states precisely what it
does, because Phase 1's filters attach to these decision points and Phase 0's
cost model has to count these orders.

---

## 1. The trade

```
  DAY 1 of the month        SELL  ~20-delta CE  of the ~30-day expiry
  (first trading day)       SELL  ~20-delta PE  of the same expiry
                            → collect both premiums

  END OF EACH WEEK          close the CHEAPER leg (the one that has won)
  (last trading day         re-sell the SAME type near the other leg's price
   of the week)             → re-centres the position, banks the winner

  EXIT DAY                  buy back both legs
  (last trading day on/     - index:  on or before the Tuesday before expiry
   before the target)       - stock:  on or before the Wednesday before expiry
```

Profit if the underlying stays between the strikes. The position is short
volatility: it earns theta and loses on large moves in either direction.

## 2. Every decision point, and where it lives

| Step | Function | Rule |
|---|---|---|
| Entry date | `run_backtest_month` | First trading day of the calendar month |
| Expiry | `get_metrics_for_day` | `expiry_30d` from `option_metrics` |
| **Earnings skip** | `run_backtest_month` | Skip the month if `upcoming_earning_date` falls in `[entry, expiry]` |
| Strike selection | `find_options_near_delta` | Nearest to +0.20 delta (CE) and −0.20 delta (PE) |
| Hedge dates | `find_weekly_hedge_dates` | Last trading day of each week, strictly in `(entry, exit]` |
| Which leg to adjust | adjustment loop | Close whichever is **cheaper to buy back** |
| Replacement strike | `find_option_near_price` | Same type, priced near the *other* leg |
| Exit day | `find_exit_day` | Walks back from expiry to the target weekday |
| P&L | `run_backtest_month` | `entry_credit − exit_cost + adjustment_pnl` |

**The earnings skip is already a filter, and a good one.** Implied volatility
collapses after results; being short a strangle across an earnings date is a
different trade with a different risk profile. It is excluded by construction —
worth knowing before Phase 1 proposes it as a novel idea.

## 3. Order count — the input the cost model needs

Costs are per *order*, so this is the number that matters:

```
   entry            2 orders   (sell CE, sell PE)
   per adjustment   2 orders   (buy back one leg, sell its replacement)
   exit             2 orders   (buy back CE, buy back PE)

   total = 4 + 2 × n_adjustments
```

A typical month has 3–4 hedge dates → **10–12 orders per trade**. A cost model
that charges only for entry and exit understates the true figure by roughly a
factor of three, and the weekly adjustment is the strategy's most expensive
habit.

> This alone can decide Gate 0. The adjustment banks a winning leg, which looks
> good in points and may not survive being charged for.

## 4. What P&L currently means — and does not

```python
result["pnl_points"] = initial_entry_credit - final_exit_cost + total_adjustment_pnl
```

**Points per share.** Not rupees, not a return, and not comparable across
symbols: 20 points on a ₹200 stock is a very different outcome from 20 points on
a ₹4,000 stock, and both are recorded identically.

Missing, in order of severity:

| Missing | Consequence |
|---|---|
| **Lot size** | Cannot convert to rupees at all |
| **Margin** | Cannot compute return on capital, so cannot compare to anything |
| **Transaction costs** | 10–12 orders per trade charged at zero |
| **Slippage** | Fills assumed at `settle` — see §5 |
| **Portfolio construction** | Every symbol-month is an independent trade with no capital constraint |

## 5. Prices: `settle` is a mark, not a fill

The backtester prices every leg at `settle` — the exchange's official daily
settlement price. That is the correct choice for *valuation*, and the project's
own invariants say so, because `settle` exists even on a day the contract never
traded where `CLOSE` would be a stale last-trade.

For *execution* it is optimistic in an unknown direction:

- A 20-delta option on a mid-cap can carry a wide spread.
- Bhavcopy publishes **no bid or ask**, so the spread cannot be measured.
- Settlement for an untraded strike is derived, not transacted.

Phase 0 therefore does not attempt to estimate slippage precisely. It reports
**breakeven slippage** — the per-leg cost at which the strategy's edge reaches
zero — which is a statement that survives not knowing the true spread.

## 6. Data timing — the constraint that rules out day trading

Bhavcopy publishes **after market close**. So:

- The chain used to pick strikes on day `t` is knowable only after `t` closes.
- Any filter computed from day `t` must be traded on **day `t+1`**.
- Nothing here supports intraday decisions at any horizon.

The current entry-on-day-1-at-`settle` convention is acceptable as a *fill
assumption* for the unfiltered strategy. It becomes **look-ahead** the moment
Phase 1 conditions entry on day-1 data. Phase 1 must shift to
`features(t−1) → trade(t)`, and a test must pin it.

## 7. Known gaps in the mechanics themselves

Not blocking, but they should be recorded rather than discovered later:

- **`PRIMARY KEY (symbol, year, month)`** permits one trade per symbol-month, so
  no two parameter variants can coexist. Phase 0.2 adds a `variant` column.
- **No stop loss.** The position runs to exit day regardless of drawdown. Real
  vol sellers do not do this, and a stop changes the return distribution
  substantially — it is a Phase 1 parameter, not a Phase 0 assumption.
- **Adjustment can fail silently.** If the chain lacks a replacement near the
  target price the loop logs and continues, leaving the position uncentred. The
  frequency of this is unmeasured and should be counted in Phase 0.
- **No liquidity filter.** A 20-delta strike with zero volume is selectable
  today. Phase 0 should report how often that happens.

[← Handoff](01-handoff-from-research.md) · [Index](README.md) · Next: [Phase 0 →](03-phase-0-economics.md)
