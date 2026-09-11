# 00 — Volatility-selling strategy: the plan

[Index](README.md) · Next: [Handoff from research →](01-handoff-from-research.md)

**Status:** specification. Nothing below is built.
**Supersedes nothing.** Stages 1–5 stand; this is a separate line of work with a
different question, a different cadence and a different definition of success.

**Prerequisite reading:** [`../../understanding/v1/14-results.md`](../../understanding/v1/14-results.md)
for what has and has not been established, and
[`model-06-accumulation-detection.md`](../../implementation/model-06-accumulation-detection.md) for the
evaluation discipline this plan inherits.

---

## 0. Why this exists

The project's research question — *can we detect institutional accumulation from
the F&O footprint* — was answered **yes, modestly**: partial IC +0.1374 across 18
of 19 quarters. The follow-up question, *can a normal trader use that*, was then
measured and answered **no**:

| Link in the chain | IC | Quarters + | |
|---|---|---|---|
| our score → MF accumulation | +0.1351 | 18/19 | ✅ works |
| MF accumulation → next quarter's return | +0.0502 | 12/19 | ⚠️ weak |
| **our score → next quarter's return** | **+0.0200** | 11/19 | ❌ nothing |

Decile spread in return terms: **+1.84%/quarter, 95% CI [−1.34%, +5.02%],
t = 1.21, p = 0.24** — not distinguishable from zero.

The break is at the second row, and it is not a defect in the detector. **The
quantity being detected does not predict returns**, so no improvement to
detection quality reaches a trading outcome. Institutions accumulating a name is
simply not the same fact as that name going up — domestic funds are contrarian
buyers (momentum IC −0.064), and quarterly price momentum in this sample is
itself negative (−0.047).

Direction prediction is separately a closed question: the archived XGBoost
next-day classifier found a thin edge **substantially tied to missingness**
(rows where the IV solver failed on illiquid or violently-moving names). See
[`../../models/xgboost-direction-model-card.md`](../../models/xgboost-direction-model-card.md).

### What is left, and why it is worth trying

Every measured forward relationship in this project is about **magnitude**, not
direction:

| Signal | Partial IC vs next-day \|return\|, after the IV control |
|---|---|
| `ce_iv_30` — implied volatility itself | **+0.2114** |
| `fut_basis_excess` | −0.0516 |
| within-symbol gamma z-score | +0.0436 |
| `dist_to_flip` | +0.0210 |

And the project separately measured **who is paid for bearing that magnitude**:
institutional desks are net *writers* of calls on **99.9%** of 1,258 days and net
long puts on **98.3%**, with retail on the other side. Option writers collect the
variance risk premium. That is the profitable side of this market, established
from this project's own participant data rather than assumed.

So the pivot is: **stop asking which way it goes; sell the magnitude, and use the
research to choose where and when.**

---

## 1. The strategy

A short strangle with a weekly delta hedge — already implemented in
`backtest/strangle.py`:

```
   month start   →  SELL the ~20-delta call AND the ~20-delta put
                    of the ~30-day expiry.  Collect two premiums.
                    Profit if the underlying stays between the strikes.

   each week     →  close the cheaper leg, re-sell it near the price
                    of the other leg          ← the delta-hedge adjustment

   exit day      →  buy back both legs (last trading day on or before
                    the index-Tuesday / stock-Wednesday before expiry)
```

Already present and correct: 20-delta strike selection, the 30-day expiry pick,
weekly hedge dates, exit-day resolution, and an **earnings filter** that skips
any month where results fall between entry and expiry.

**Cadence.** Entries are monthly; management is weekly. This is *not* day
trading — the data is end-of-day, so the earliest action on any signal is the
next morning's open. A plan that promises intraday signals from bhavcopy is
lying about the data.

---

## 2. What exists vs what is missing

| Component | State |
|---|---|
| Strike selection, expiry pick, hedge dates, exit day | ✅ built, unit-tested |
| Earnings-conflict skip | ✅ built |
| `backtest_results` table | ✅ exists, **0 rows — never run at scale** |
| P&L in points per share | ✅ computed |
| **Lot size → rupee P&L** | ❌ **missing** |
| **Margin model → return on capital** | ❌ **missing** |
| **Transaction costs** | ❌ **missing** |
| **Slippage / executable price** | ❌ **missing** |
| **Portfolio construction, position sizing** | ❌ **missing** |
| Feature join at entry | ❌ missing |
| Walk-forward evaluation of filters | ❌ missing |
| Daily serving path | ❌ missing |

> **The headline gap.** `pnl_points = entry_credit − exit_cost + adjustment_pnl`
> is measured in **points per share**. It is not a return, it is not in rupees,
> and it cannot be compared across symbols — a 20-point gain on a ₹200 stock and
> on a ₹4,000 stock are wholly different trades. **Every economic claim this plan
> makes depends on Phase 0.1 and nothing can be concluded before it.**

### Four traps, each of which would invalidate a result

**Settlement prices are not executable prices.** The backtester marks entry at
`settle`, which exists even on a day the contract never traded. A 20-delta option
on a mid-cap can have a wide spread. Selling at the settlement mark is optimistic
and the size of the optimism is unknown.

**Feature timing.** Features computed from day `t`'s close (including anything
IV-derived) are not knowable until after day `t` closes. **A filter using day `t`
data must trade on day `t+1`.** The current entry-on-day-1 logic is fine as a
fill assumption but becomes look-ahead the moment a filter conditions on the same
day's chain.

**`PRIMARY KEY (symbol, year, month)`** permits exactly one trade per symbol-month,
so two parameter variants cannot coexist in the table. A variant column is
required before any sweep.

**The sample contains no severe crash.** 2021–2026 has drawdowns but nothing like
2008 or March 2020. A short-strangle backtest over this window **will** flatter
the strategy, because the strategy's entire risk is in the tail that is absent.

---

## 3. Phase 0 — does the base strategy make money at all?

**Question:** ignoring every feature this project built, does systematic
strangle-selling on NSE stocks earn a positive net-of-cost return?

If the answer is no, nothing downstream matters and the work stops here.

### 0.1 Make the P&L economically real  *(the critical task)*

New module `src/mktmtm/backtest/economics.py`, pure functions, no I/O:

```python
def contract_value(points: float, lot_size: int) -> float
def round_trip_costs(premium_in: float, premium_out: float,
                     lot_size: int, n_legs: int, n_adjustments: int) -> float
def strangle_margin(spot: float, lot_size: int, ...) -> float
def slippage(premium: float, model: str, ...) -> float
```

- **Lot size** from `data/lots.py`, resolved at the **entry date** — it is a
  change-point schedule, not a constant (median 3 revisions per symbol, max 8).
- **Costs**, itemised rather than a single fudge factor: brokerage per order,
  STT on the sell side of premium, exchange transaction charges, SEBI turnover
  fee, stamp duty, GST on (brokerage + charges). Count **every** order —
  2 legs in, 2 out, plus 2 per weekly adjustment.
- **Margin**: SPAN + exposure for a short strangle. Bhavcopy does not publish it,
  so this is an approximation and must be declared as one, with a sensitivity
  run at ±25%.
- **Slippage**: no bid-ask in the data. Implement as a configurable fraction of
  premium and **report the breakeven slippage** — the level at which the
  strategy's edge disappears. That number is more honest than any point estimate.

### 0.2 Schema

```sql
ALTER TABLE backtest_results ADD COLUMN variant TEXT NOT NULL DEFAULT 'base';
-- PK becomes (variant, symbol, year, month)
ALTER TABLE backtest_results ADD COLUMN lot_size INTEGER;
ALTER TABLE backtest_results ADD COLUMN pnl_rupees DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN costs_rupees DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN margin_rupees DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN return_on_margin DOUBLE PRECISION;
```

### 0.3 Run at scale

217 symbols × ~60 months ≈ **13,000 candidate trades**. Time it; add a progress
bar via `progress.py` like every other long stage.

### 0.4 Analysis harness — `src/mktmtm/backtest/analysis.py`

Per-trade return on margin, then: mean, median, win rate, Sharpe, **worst single
month**, **max drawdown of the equity curve**, and the distribution's left tail
(p1, p5). Report the equity curve, not just the average — a strategy with a good
mean and a −60% month is not usable.

### Acceptance criteria — the Phase 0 gate

| Criterion | Threshold |
|---|---|
| Mean return on margin, **net of costs** | > 0 |
| Positive in a majority of years | ≥ 4 of 5 |
| Max drawdown | stated, and survivable at the intended sizing |
| Breakeven slippage | stated explicitly |

**If net-of-cost return is ≤ 0, stop and write it up as a negative result.**
That is a publishable finding and it is the honest end of this line of work.

---

## 4. Phase 1 — does *this project's research* add anything?

Only reached if Phase 0 passes.

**Question:** does conditioning entry on the project's features beat entering
unconditionally — and beat the obvious filter everyone already uses?

### 4.1 Join features at entry

For each trade, attach the feature vector **as at the prior close** (§2, trap 2):

| Feature | Rationale |
|---|---|
| `ce_ivp` / `ce_ivr` | IV percentile / rank — sell when vol is historically rich |
| `ce_iv_30 − rv_yz` | The variance risk premium itself, directly |
| `fut_basis_excess` | Strongest constructed feature (−0.0516 vs \|return\|) |
| `dist_to_flip` | Best dealer feature; invariant to the sign convention |
| within-symbol `gex` z-score | The magnitude effect that survived (+0.0436) |
| `oi_outlier_share` | Is something unusual being positioned here? |

### 4.2 The mandatory baselines

This is the part the project's own history insists on. Stage 1 had the expiry
calendar; Stage 5 had price momentum. **Here the boring explanation is IV rank** —
"sell options when implied volatility is high" is what every retail vol seller
already does, requires none of this research, and must be beaten rather than
matched.

| Baseline | Role |
|---|---|
| **Unfiltered** — trade every eligible symbol-month | The floor |
| **IV rank alone** | The boring explanation. ⭐ The one that matters. |
| **Random filter**, same trade count, seeded | The placebo — establishes the spread of pure chance |

### 4.3 Evaluation

Walk-forward with the existing purged splitters (`ml/splits.py`), selecting any
threshold on train and evaluating on test. Never select and report on the same
fold. Report per-fold, not just pooled.

Because trades are monthly and there are ~60 months, **n is small**. Use the
moving-block bootstrap over months, not a t-test over trades — trades within a
month share that month's market and are not independent.

### Acceptance criteria — the Phase 1 gate

| Criterion | Threshold |
|---|---|
| Filtered beats **unfiltered** out-of-sample | required |
| Filtered beats **IV rank alone** out-of-sample | ⭐ **required** |
| Improvement lies outside the random-filter distribution | required |
| Holds in both halves of the sample | required |

> **If the features only match IV rank, the research contributed nothing to the
> strategy and the write-up must say exactly that.** You would still have a
> working strategy — it just would not need this project.

Honest prior: **this gate is unlikely to pass.** Every constructed feature in
this project sits at ≤0.05 partial IC after the IV control, while IV alone is
+0.2114. That is a large head start to overcome.

---

## 5. Phase 2 — the daily tool

Only reached if Phase 1 passes.

### 5.1 Serving path

Serving does **not** need the 24 GB `option_metrics` fact table — only the
current chain and the feature row. Daily job:

```bash
uv run mktmtm run-pipeline --mode no-etl   # incremental; minutes, not hours
uv run mktmtm screen                       # NEW
```

### 5.2 `mktmtm screen` — the deliverable

New module `src/mktmtm/serving/screen.py` (the `serving/` package exists and is
empty). Output per candidate:

| Field | |
|---|---|
| symbol, expiry | which contract |
| call strike / put strike | at ~20 delta |
| expected credit | in rupees, at the lot size in force |
| **margin required** | so the trade is sizeable |
| **max loss at a defined move** | e.g. ±2σ; a strangle's true max is unbounded |
| filter values | *why* it was selected — never an unexplained score |
| rank | ordering, not a recommendation |

### 5.3 Risk controls — not optional

A short strangle has **open-ended loss**. These belong in the tool, not in a
disclaimer:

- Max concurrent positions, and max fraction of capital in margin
- Per-trade stop expressed as a multiple of credit received
- Correlation cap — 15 strangles across one sector is one trade
- Hard refusal to emit a candidate whose lot size or margin cannot be resolved

### 5.4 Presentation

The Streamlit UI already exists and already runs jobs as detached subprocesses;
add a page rather than building a new front end. **It binds the tailnet IP and
has no authentication — it must never bind `0.0.0.0`.**

---

## 6. Phase 3 — forward validation

Everything in Phases 0–2 is in-sample: filters selected on the same 2021–2026
window they are then measured on. This project already established the remedy and
should reuse it exactly.

Follow the **nowcast precedent** (`ml/accumulation.write_nowcast_record`):

- Each month, write a **dated, committed record** of the selected trades *before
  the outcome exists*, under `docs/results/screen-YYYY-MM.md`.
- The writer **refuses to overwrite**, for the same reason it does there: a
  prediction silently replaced after the fact is worse than no prediction.
- Score monthly against realised outcomes; report the running record including
  losing months.

**Minimum 6 months of paper trading before any real capital.** One profitable
month is one observation.

---

## 7. Honest expectations

| Phase | Passes? | Why |
|---|---|---|
| 0 — base strategy | **Likely** | The VRP is robust historically, and a retail-heavy options market favours writers |
| 1 — research adds value | **Unlikely** | Features ≤0.05 partial IC against IV's +0.2114 |
| 2 — tool | Mechanical, if 1 passes | |
| 3 — forward validation | Unknown by construction | That is the point of it |

The single most probable outcome is: **"systematic option-selling works in this
market; this project's features do not improve it."** That is a genuine result,
arrived at by measurement, and it should be written up as plainly as the positive
ones — but it means the tradeable component would not depend on the research.

### Risks to state in any write-up

- **No crash in the sample.** The strategy's entire risk lives in a tail this
  window does not contain.
- **Settlement ≠ executable.** Report breakeven slippage, always.
- **The margin model is an approximation**, not exchange SPAN.
- **Short strangles can lose more than the capital allocated to them.**

---

## 8. Task list

| # | Task | Module | Gate |
|---|---|---|---|
| 0.1 | Economics: lot size, costs, margin, slippage | `backtest/economics.py` | — |
| 0.2 | Schema: variant + rupee columns | `sql/backtest_results.sql` | — |
| 0.3 | Run 217 symbols × 5 years | `backtest/strangle.py` | — |
| 0.4 | Analysis: returns, Sharpe, drawdown, tails | `backtest/analysis.py` | **Gate 0** |
| 1.1 | Feature join at prior close | `backtest/analysis.py` | — |
| 1.2 | Baselines: unfiltered, IV rank, random placebo | `backtest/analysis.py` | — |
| 1.3 | Walk-forward + block bootstrap over months | reuse `ml/splits.py` | **Gate 1** |
| 2.1 | `mktmtm screen` | `serving/screen.py` | — |
| 2.2 | Risk controls and sizing | `serving/screen.py` | — |
| 2.3 | UI page | `ui/` | — |
| 3.1 | Frozen monthly records | `serving/screen.py` | — |
| 3.2 | Monthly scoring harness | `backtest/analysis.py` | **Gate 3** |

Phase 0 is a few hours of work and settles the largest question. Nothing beyond
it should be built until Gate 0 reports a number.

[Index](README.md) · Next: [Handoff from research →](01-handoff-from-research.md)
