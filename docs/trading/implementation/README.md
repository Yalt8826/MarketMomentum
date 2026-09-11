# Trading tool — implementation

[← the whole story in one page](../README.md)

**Branch:** `feat/vol-selling-strategy` · **Status:** specification, nothing built.

Turning MktMtm into something a trader can use. This is a **separate line of
work** from the research in `feat/gex-dealer-positioning`: different question,
different cadence, different definition of success. The research stands; it is
not the basis of the tool, and [document 01](01-handoff-from-research.md)
explains why in measured terms.

---

## The pivot, in one line

> **Stop asking which way a stock will go. Sell the magnitude of its movement,
> and use the research to decide where.**

Direction is a closed question here. But every measured forward relationship in
this project is about magnitude — and the participant data says who gets paid
for bearing it: institutional desks are net *writers* of calls on **99.9%** of
days, with retail on the other side.

## Read in order

| # | Document | |
|---|---|---|
| **00** | [The plan](00-plan.md) | The original one-page argument and gate structure |
| 01 | [Handoff from research](01-handoff-from-research.md) | What carries over, what dies, and the measured reasons |
| 02 | [Strategy and mechanics](02-strategy-and-mechanics.md) | The strangle exactly as `backtest/strangle.py` implements it |
| 03 | [Phase 0 — economics](03-phase-0-economics.md) | ⭐ Lot size, costs, margin, slippage. **The critical gap.** |
| 04 | [Phase 1 — evaluation](04-phase-1-evaluation.md) | Filters, baselines, and the gate that decides whether the research matters |
| 05 | [Phase 2 — serving](05-phase-2-serving.md) | The daily screen and its risk controls |
| 06 | [Phase 3 — forward validation](06-phase-3-forward-validation.md) | Frozen monthly records, on the nowcast precedent |
| 07 | [Build order](07-build-order.md) | Task list: files, tests, gates |
| 08 | [Phase 0 as built](08-phase-0-as-built.md) | ✅ What was actually written, and where it deviates from the plan |
| 09 | [Phase 1 as built](09-phase-1-as-built.md) | ✅ The evaluation harness, and the strictly-prior guarantee it rests on |
| 10 | [Phases 2 and 3 as built](10-phase-2-3-as-built.md) | ✅ The screen, its refusals, and the frozen record |

## Results so far

| Gate | Verdict | |
|---|---|---|
| **0** — does the base strategy make money? | ✅ **PASS** | [gate-0-base-strategy.md](../results/gate-0-base-strategy.md) |
| — four silent defects found on the way | fixed | [bugs-and-fixes.md](../results/bugs-and-fixes.md) |
| **1** — do our features beat IV rank? | ❌ **FAIL** | [gate-1-feature-filters.md](../results/gate-1-feature-filters.md) |

Gate 0: +4.56% mean return on margin per trade, positive in 6 of 6 years,
−37.27% max drawdown, breakeven slippage 3.16% per leg. Three silent defects
were found and fixed while producing it — the worst trade fell from −1,677% to
−245.8% while the mean moved less than half a point, because the contamination
was in the tail rather than the average.

## The four gates

Each can stop the work. All criteria are fixed before any number exists.

| Gate | Question | Prior |
|---|---|---|
| **0** | Does strangle-selling make money **net of costs**? | **Likely passes** |
| **1** | Do our features beat **IV rank alone**? | ❌ **Failed, as predicted** |
| 2 | — build only — | ✅ built |
| **3** | Does it hold forward, ≥6 months, losses included? | Unknown by design |

## Why Gate 1 is the one that matters

| | Partial IC vs next-day \|return\| |
|---|---|
| `ce_iv_30` — plain implied volatility | **+0.2114** |
| Every feature this project built | **≤ 0.05** |

"Sell options when implied volatility is high" needs none of this research and is
available in any free screener. **This branch only earns its existence if it
beats that.** The stated prior is that it does not — which is written down in
advance so that a pass means something and a failure is not a surprise.

A Gate 1 failure still leaves a working strategy, a reusable cost/margin/slippage
model, and the measured statement that the research does not improve it. That is
a result, and it goes in `docs/trading/results/` at the same resolution as a
positive one.

## The caveat that governs everything

> **The 2021–2026 sample contains no severe crash.** A short strangle's entire
> risk lives in that tail. Every backtest here **will** flatter the strategy, and
> the size of the flattery cannot be estimated from inside the window.
