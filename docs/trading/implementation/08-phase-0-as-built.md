# 08 — Phase 0 as built

[← Build order](07-build-order.md) · [Index](README.md) · [Gate 0 verdict →](../results/gate-0-base-strategy.md)

What was actually written, how it differs from the specification, and where each
piece lives. Documents 03 and 07 are the *plan*; this is the record.

**Status: Phase 0 complete.** Gate 0 passed on 2026-09-11.

---

## 1. What was built

| Module | Lines | Role |
|---|---|---|
| `backtest/economics.py` | 570 | **New.** Costs, margin, slippage, whole-trade pricing |
| `backtest/analysis.py` | 355 | **New.** The Gate 0 report |
| `backtest/strangle.py` | +408 | Economics wiring, three defect fixes |
| `storage/sql/backtest_results.sql` | +95 | Variant key, rupee columns, provenance |
| `config/settings.py` | +4 | `backtest_brokerage_per_order` |
| `tests/unit/test_economics.py` | 440 | **New.** 40 tests |
| `tests/backtest/test_strangle.py` | +272 | 41 tests (12 new) |

**2,120 lines added. 515 tests pass**, none requiring a database.

## 2. `economics.py` — the four missing pieces

`strangle.py` reported `pnl_points`: points per share. Not a return, and not
comparable across symbols — 20 points on a ₹200 stock and on a ₹4,000 stock were
recorded identically.

### 2.1 Rates, dated and resolved

```python
@dataclass(frozen=True)
class CostRates:
    effective_from: date
    source: str
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.001          # sell side only
    exchange_txn_pct: float = 0.0003503
    sebi_turnover_pct: float = 0.000001
    ipft_pct: float = 0.000005
    stamp_duty_buy_pct: float = 0.00003  # buy side only
    gst_pct: float = 0.18                # on brokerage + exchange + SEBI + IPFT
```

**Three STT regimes**, verified against published sources rather than recalled:

| From | STT on sale | Exchange txn | Authority |
|---|---|---|---|
| — | 0.05% | 0.0495% | pre-2023 |
| 2023-04-01 | 0.0625% | 0.0495% | Finance Act 2023, NSE FATAX56235 |
| 2024-10-01 | **0.1%** | **0.03503%** | Finance Act 2024; SEBI uniform fees |

> **October 2024 moves two rates in opposite directions** — STT up, exchange
> charges down. A test pins that, because a single blended rate would be wrong
> in both directions at once.

`resolve_rates(day)` picks the regime in force and **raises** rather than falling
back when a date precedes every known set: a trade priced under rates that did
not yet exist is a fabricated number.

### 2.2 Brokerage is configuration, not a constant

**Deviation from spec.** Document 03 treated brokerage as one more rate. The
first magnitude check showed it dominates — **57.9% of all costs** — and it is
broker-specific, so pinning a value would have silently decided Gate 0.

```python
BROKERAGE_SCENARIOS = {"zero": 0.0, "discount": 20.0, "premium": 40.0}
rate_sets(brokerage_per_order=None)   # None reads Settings
```

Statutory rates are facts and get pinned. The thing that varies gets swept —
the same treatment margin gets (±25%) and slippage gets (breakeven).

### 2.3 Order count — the number the model exists to get right

```python
def strangle_orders(entry_call, entry_put, exit_call, exit_put,
                    lot_size, adjustments) -> list[Order]:
    # 4 + 2 * n_adjustments
```

Each weekly hedge **buys one leg back and sells a replacement** — two orders, not
zero. A typical month is 10–12 orders, so charging only entry and exit
understates roughly threefold. Pinned by a test across `n = 0..4`.

### 2.4 Margin — an approximation that says so

```python
@dataclass(frozen=True)
class MarginModel:
    span_pct: float = 0.09
    exposure_pct: float = 0.03
    note: str = "approximation; not exchange SPAN."

    def scaled(self, factor: float) -> MarginModel: ...
```

Real SPAN comes from a daily risk array bhavcopy does not carry. `scaled()` is
the sensitivity hook; Gate 0 reports 0.75× / 1.00× / 1.25× and all three are
positive.

### 2.5 Slippage — handled by not pretending

```python
def apply_slippage(premium, side, fraction) -> float:
    # sell fills lower, buy fills higher — always against the trader
```

No bid/ask exists in the data, so the analysis layer reports **breakeven
slippage** instead of a point estimate. "It earns X% and breaks even at 3.16%"
survives not knowing the true spread; "it earns X%" does not.

### 2.6 Whole-trade pricing

```python
price_strangle_trade(...) -> TradeEconomics
```

Slippage is applied to the legs **before** costs, because a sold leg that fills
lower owes less STT — which is what a contract note shows. The three money
columns decompose exactly:

```
net = gross − costs − slippage
```

pinned by `test_trade_economics_decomposes_exactly`.

## 3. Schema

```sql
PRIMARY KEY (variant, symbol, year, month)
```

`variant` is load-bearing: without it a parameter sweep overwrites itself and
Phase 1 cannot compare filtered against unfiltered. It let the contaminated run
be kept as `base-noliq` while the fixed run rebuilt `base`.

Money columns stay **separate** (`gross_ / costs_ / slippage_ / net_pnl_rupees`)
rather than collapsing to one, so "which component killed it" stays answerable.
`cost_breakdown` holds the itemisation as JSONB.

**Provenance columns** — `rates_effective_from`, `slippage_fraction`,
`margin_pct`, `lot_size` — because a result is unreproducible without the
assumptions that produced it.

## 4. Wiring into `strangle.py`

| Change | Why |
|---|---|
| `price_result()` | Attaches economics; leaves a month **unpriced with a typed reason** when the lot size or spot cannot be resolved. Defaulting a lot would silently mis-scale that trade's whole P&L. |
| Lot resolved at **entry date** | `data/lots.py` is a change-point schedule — median 3 revisions per symbol, max 8 |
| `adjustment_legs` recorded | `(bought_back, resold)` pairs; the cost model charges two orders per adjustment |
| `call_entry_volume` / `put_entry_volume` | Gate 0 must disclose how often a selected strike never traded |
| `_is_tradeable`, `_is_plausible_price`, `chain_is_degenerate` | The three defect fixes — see [bugs-and-fixes](../results/bugs-and-fixes.md) |

## 5. `analysis.py` — the Gate 0 report

One function per Gate 0 requirement:

| Requirement | Function |
|---|---|
| mean net return > 0 | `summarize` |
| positive in ≥ 4 of 5 years | `by_year` |
| stable across margin ±25% | `margin_sensitivity` |
| breakeven slippage stated | `breakeven_slippage` |
| max drawdown stated | `summarize` → `equity_curve` → `max_drawdown` |
| skip taxonomy + liquidity stated | `skip_taxonomy`, `liquidity` |

**The portfolio is pooled by month, not averaged over trades** (`monthly_portfolio`).
Symbols sharing a month share that month's market, so the month is the
independent unit — the same reasoning the research branch used when it
bootstrapped over quarters rather than rows.

`load_results` keeps skipped months deliberately: the share of months a strategy
*cannot* trade is part of its description, and dropping them would make the skip
taxonomy unreportable.

## 6. Deviations from the plan, and why

| Spec said | Built | Reason |
|---|---|---|
| Brokerage as one more rate | Configuration + swept scenario | It is 57.9% of costs and broker-specific |
| Slippage grid 0/1/2/5% | Breakeven reported instead | A single derived number beats four arbitrary points |
| Task 0.9: wire into `pipeline_runner` | **Not done** | The backtest is not part of the daily path; deferred to Phase 2 |
| — | `chain_is_degenerate`, `_is_tradeable`, `_is_plausible_price` | Three defects the plan did not anticipate |

The plan anticipated that settlement prices would be optimistic *fills*. It did
not anticipate that some would be **fictional prices**. That is the substantive
gap between specification and reality in Phase 0.

## 7. Reproducing Gate 0

```bash
psql "$PG_DSN" -f src/mktmtm/storage/sql/backtest_results.sql   # idempotent

uv run python -c "
from mktmtm.backtest.strangle import run_backtest
run_backtest(years=[2021,2022,2023,2024,2025,2026], variant='base')"   # ~7 min

uv run python -c "
import psycopg2
from mktmtm.storage.db import build_dsn
from mktmtm.backtest.analysis import gate_0_report, load_results
with psycopg2.connect(build_dsn()) as conn:
    r = gate_0_report(load_results(conn, 'base'))
print(r['summary'], r['criterion_1_pass'], r['criterion_2_pass'])"
```

15,096 candidate symbol-months → 7,706 priced trades.

[← Build order](07-build-order.md) · [Index](README.md) · [Gate 0 verdict →](../results/gate-0-base-strategy.md)
