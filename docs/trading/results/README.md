# Trading tool — results

**Branch:** `feat/vol-selling-strategy`

Outcomes from the volatility-selling work. Negative results are recorded at the
same resolution as positive ones, following the research branch's practice.

---

## Where things stand

| Gate | Question | Verdict | |
|---|---|---|---|
| **0** | Does strangle-selling make money net of costs? | ✅ **PASS** | [gate-0-base-strategy.md](gate-0-base-strategy.md) |
| **1** | Do this project's features beat IV rank alone? | ❌ **FAIL** | [gate-1-feature-filters.md](gate-1-feature-filters.md) |
| **2** | Ship the daily screen | ✅ **built** | [`mktmtm screen`](screens/) |
| **3** | Does it hold forward, ≥ 6 months? | ⏳ recording | [screens/](screens/) |

## Gate 0 in one table

7,706 trades · 217 symbols · 61 months · 2021-08 → 2026-09

| | |
|---|---|
| Mean return on margin | **+4.56%** per trade |
| Win rate | 67.1% (win +16.73%, loss −20.26%) |
| Positive years | **6 of 6** |
| Max drawdown | **−37.27%** |
| Worst month | −16.12% |
| Breakeven slippage | 3.16% per leg |
| Months that trade | **51%** of candidates |

**Read the caveats before the headline.** The sample contains no severe crash,
which is precisely where a short strangle's risk lives, and the yearly edge is
decaying monotonically — 8.30% in 2021 to 1.09% in 2025.

## Documents

| | |
|---|---|
| [Gate 0 — base strategy](gate-0-base-strategy.md) | The verdict, the four mandatory disclosures, and five reasons to discount it |
| [Gate 1 — feature filters](gate-1-feature-filters.md) | The research features do not improve the strategy. Neither does IV rank. |
| [The stop loss](stop-loss.md) | Measured at 1.5x/2x/3x. It costs return and buys no tail protection. |
| [Bugs and fixes](bugs-and-fixes.md) | Four silent defects found in Phase 0, how they were caught, and what they cost |

## The finding most worth carrying

Gate 0's first run reported a worst trade of **−1,677%** — a loss of 16× the
margin posted. Every headline statistic looked plausible: mean +4.79%, win rate
68.3%, Sharpe 2.22, six positive years.

Chasing that one impossible number found three separate data defects. Fixing
them moved the mean by **less than half a percentage point** while the worst
trade fell to −245.8%.

> The mean was never the contaminated statistic. The tail was — and in a
> premium-selling strategy the tail is the part that decides whether the book is
> usable at all.

See [bugs-and-fixes.md](bugs-and-fixes.md).
