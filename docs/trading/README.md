# MktMtm — the trading tool

**Branch:** `feat/vol-selling-strategy` · 14 commits · 4,532 lines of code, 568 tests
**Built:** 2026-09-11 → 2026-09-12

A separate line of work from the research in `feat/gex-dealer-positioning`:
different question, different cadence, different definition of success. This
document is the whole story in one place. The detail is in
[`implementation/`](implementation/README.md) and [`results/`](results/README.md).

---

## 1. The question, and why it changed

The research branch asked *can we detect institutional positioning?* and
answered **yes, modestly** — Stage 5 detects mutual-fund accumulation at partial
IC +0.1374 across 18 of 19 quarters.

Then it was asked whether a normal trader could use that. Measured:

| Link | IC | Quarters + |
|---|---|---|
| our score → MF accumulation | **+0.1351** | 18/19 |
| MF accumulation → next quarter's return | +0.0502 | 12/19 |
| **our score → next quarter's return** | **+0.0200** | 11/19 |

**The chain breaks at the second link, and it is not the detector's fault.** The
detected quantity barely predicts returns — domestic funds are contrarian
(−0.064) and quarterly momentum is itself negative (−0.047). No improvement in
detection reaches a trading outcome.

So this branch stops asking *which way* and asks *how much*: sell the magnitude,
since that is where every measured forward relationship in the project already
lives (IV +0.2114, `fut_basis_excess` −0.0516, gamma z +0.0436), and since the
participant data already says who is paid for bearing it — **institutional desks
are net writers of calls on 99.9% of days.**

## 2. What was built

| | |
|---|---|
| `backtest/economics.py` | Costs, margin, slippage, whole-trade pricing |
| `backtest/analysis.py` | The Gate 0 report |
| `backtest/filters.py` | Feature join, baselines, significance |
| `serving/screen.py` | The daily tool, and its frozen record |
| `serving/scoring.py` | Forward validation |
| `backtest/strangle.py` | +518 lines: economics, stop loss, three defect fixes |

Two new commands: **`mktmtm screen`** and **`mktmtm score-screens`**.

## 3. The four gates

Every criterion was fixed before the number existed.

| Gate | Question | Verdict |
|---|---|---|
| **0** | Does strangle-selling make money net of costs? | ✅ **PASS** |
| **1** | Do our research features beat IV rank alone? | ❌ **FAIL** |
| **2** | Build the tool | ✅ Built |
| **3** | Does it hold forward, ≥6 months? | ⏳ **0 of 6** |

### Gate 0 — PASS

7,706 trades · 217 symbols · 61 months · 2021–2026

| | |
|---|---|
| Mean return on margin | **+4.56%** per trade |
| Win rate | 67.1% (win +16.73%, loss −20.26%) |
| Positive years | **6 of 6** |
| **Max drawdown** | **−37.27%** |
| Worst month | −16.12% |
| Breakeven slippage | 3.16% per leg |
| Months that trade | 51% of candidates |

**The yearly trend matters more than the headline**: +8.30% (2021) → **+1.09%
(2025)**, an eightfold decay. Untested candidates: the October 2024 STT rise
(0.0625% → 0.1%), and a maturing options market.

→ [`results/gate-0-base-strategy.md`](results/gate-0-base-strategy.md)

### Gate 1 — FAIL, as the recorded prior predicted

Six features tested at three selectivities against three baselines, with
walk-forward selection and a monthly block bootstrap.

**Nothing beat the random-filter p95 (+5.20%) — including IV rank (+4.76%).**
Unfiltered is +4.56%.

So the finding is not "our features lost to the standard heuristic":

> **Selectivity does not help this strategy.** Trading every eligible
> symbol-month is as good as any filter tested.

The only significant bootstrap result is harmful: `fut_basis_excess` at −3.61pp,
CI [−6.33, −1.21]. Walk-forward is negative for five of six. And the per-year
table settles it — `fut_basis_excess` swings **+12.76pp → −11.02pp in
consecutive years**.

→ [`results/gate-1-feature-filters.md`](results/gate-1-feature-filters.md)

### The stop loss — measured, rejected

The obvious remaining lever, given a −37.27% drawdown. Tested at 1.5×/2×/3× the
entry credit on the common set of 7,706 trades:

| | Mean | 5th pct | Worst trade | Max DD | Sharpe |
|---|---|---|---|---|---|
| **no stop** | **+4.56%** | **−36.25%** | −245.84% | **−37.27%** | **2.16** |
| 1.5× credit | +4.25% | −37.94% | −245.56% | −38.88% | 2.00 |

**Every column worsens.** Short vol mean-reverts, so a stop crystallises losses
the position would have recovered; and it fires on a weekly mark, so the one
genuine catastrophe gaps straight through it.

> It catches the recoverable losses and misses the unrecoverable one.

→ [`results/stop-loss.md`](results/stop-loss.md)

### Gate 3 — 0 of 6

A record counts only if it entered on the month's **first trading day** and was
**committed to git before its outcome existed**.

While validating the harness, eight screens generated for Feb–Sep 2026 returned
a mean monthly portfolio return of +11.80% and a cumulative **+94.55%**. The
harness excluded all eight — *"not committed before its outcome"*. A
near-doubling the tool refuses to count is the best demonstration available that
the check works.

The one live record is also excluded: it entered 2026-09-04 when the month's
first trading day was 2026-09-01.

→ [`results/gate-3-forward-validation.md`](results/gate-3-forward-validation.md)

## 4. The product

```bash
uv run mktmtm screen --capital 1000000      # on the month's FIRST TRADING DAY
uv run mktmtm score-screens                 # any time after an expiry
```

**It does not rank by conviction, because three measurements say there is
nothing to rank by.** It orders by net credit per rupee of margin — an
efficiency question, not a prediction. The research features appear as `ctx_*`
columns labelled *"tested in Gate 1; did not beat chance"*.

What it spends its effort on instead:

- **Refusing** what it cannot price honestly, each refusal typed.
- **Sizing**: credit, costs, margin, and a modelled 2σ loss. That column earned
  its place immediately — on the first live run **IDEA showed ₹133,937 of
  modelled loss against ₹24,220 of credit**.
- **Caps**: position count, margin budget, sector concentration, leg liquidity.

It states its own limits in its output, not only in these docs.

## 5. Six silent defects

All six produced plausible output and raised nothing. Two predated this branch.

| # | Defect | Extent |
|---|---|---|
| 1 | P&L dropped the replacement leg's credit after each hedge | every adjusted month |
| 2 | Untraded strikes priced anyway | 32 trades, −₹5.7M |
| 3 | **Degenerate chains** — spot written into every premium | 47 symbol-days; **83% on expiry dates** |
| 4 | **3,308 rows never persisted**, in every run | skip taxonomy wrong by 23pp |
| 5 | Variant unset on skipped months | every variant's skips merged into base |
| 6 | Unequal-sample comparison | a −56.79% month that was an artefact |

**How they were found**: Gate 0's first run reported a worst trade of
**−1,677%** — 16× the margin posted. Every headline statistic looked fine. One
impossible number found three defects.

> The mean was never the contaminated statistic. The tail was — and in a
> premium-selling strategy the tail is what decides whether the book is usable.

→ [`results/bugs-and-fixes.md`](results/bugs-and-fixes.md)

## 6. What this branch established

**Positive:** systematic short-strangle selling on NSE stocks earned a positive
net-of-cost return in every year of 2021–2026, and the verdict survives a ±25%
margin sensitivity band.

**Negative, and more useful:**

- This project's research features do not improve it. Neither does IV rank.
- A stop loss makes every measured outcome worse.
- The edge is decaying eightfold across the sample.

**Unestablished, and the binding constraint:** the **−37.27% drawdown is
unmitigated**, and nothing tested reduces it. The sample contains no severe
crash, which is precisely where a short strangle's risk lives.

A +4.93% mean monthly portfolio return is on its face too good — roughly +78%
annualised. Five reasons to discount it are listed in the Gate 0 report, and the
first is that missing crash.

## 7. Honest limits

| | |
|---|---|
| **No crash in the sample** | The largest known weakness, and unquantifiable from inside the window |
| **Settlement ≠ executable** | Bounded by the 3.16% breakeven, not removed. One entry leg in ten traded ≤12 lots. |
| **Margin is an approximation** | Not exchange SPAN, which bhavcopy does not publish |
| **Cost rates unverified** | Brokerage is 57.9% of costs at an assumed ₹20 flat |
| **No capital constraint in Gate 0** | The monthly portfolio assumes every eligible trade is funded |
| **Sector cap is inert** | Implemented and tested; the catalog carries no sector field |
| **Everything is in-sample** | Until Gate 3 has six months |

## 8. Where to read next

| | |
|---|---|
| [implementation/README.md](implementation/README.md) | The build contract, phase by phase, plus what was actually built |
| [results/README.md](results/README.md) | Every verdict with its numbers |
| [results/bugs-and-fixes.md](results/bugs-and-fixes.md) | The six defects — the most transferable document here |
| [implementation/01-handoff-from-research.md](implementation/01-handoff-from-research.md) | What carried over from the research, and what died |
