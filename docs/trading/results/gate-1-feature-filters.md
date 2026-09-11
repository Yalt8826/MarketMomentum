# Gate 1 — do this project's features improve the strategy?

**Run 2026-09-11** · commit `a7bba6e` · 7,706 trades, 61 months
**Verdict: FAIL.** No feature beats chance. Neither does IV rank.

Specification: [`../implementation/04-phase-1-evaluation.md`](../implementation/04-phase-1-evaluation.md).

---

## 1. The verdict

> **Selectivity does not help this strategy.** Trading every eligible
> symbol-month is as good as any filter tested, including the obvious one that
> needs no research at all.

This was the [recorded prior](../implementation/04-phase-1-evaluation.md#6-the-honest-prior),
written before the run: every constructed feature in this project sits at ≤0.05
partial IC against implied volatility's own +0.2114, which is a large head start
to overcome. It failed as predicted, and more completely than predicted — the
boring explanation failed too.

| Criterion | Result |
|---|---|
| 1 — beats unfiltered | ❌ marginal at best, none significant |
| 2 — **beats IV rank alone** | ❌ none |
| 3 — outside the random-filter distribution | ❌ none |
| 4 — holds in both halves | ❌ every feature flips sign across years |
| 5 — look-ahead tripwire fires | ✅ +19.1pp |

## 2. The harness is sound

Established before reading any result, because a null is only a measurement if
the apparatus works.

| Check | Expected | Observed |
|---|---|---|
| **Feature lag** — strictly prior to entry | all ≥ 1 day | min 1, median 3, **max 4, none ≤ 0** ✅ |
| **Leakage tripwire** — a filter given the outcome | must dominate | 4.56% → **23.64%**, +19.1pp ✅ |

The tripwire is the important one: it proves the comparison machinery *can*
detect an edge of this kind. The nulls below are therefore measurements, not
defects.

## 3. The comparison

Unfiltered mean return on margin: **+4.56%**.

### Keeping the best 30% of trades each month

| Feature | Direction | Trades | Mean | vs unfiltered | vs IV rank | Beats chance? |
|---|---|---|---|---|---|---|
| **`ce_ivr`** (the baseline) | high | 1,662 | +4.76% | +0.20pp | — | ❌ |
| `iv_rv_spread` | high | 1,724 | +4.04% | −0.52pp | −0.72pp | ❌ |
| `fut_basis_excess` | low | 1,039 | **+0.96%** | **−3.59pp** | −3.80pp | ❌ |
| `dist_to_flip` | high | 1,856 | +4.15% | −0.41pp | −0.61pp | ❌ |
| `gex_z` | high | 2,136 | +3.76% | −0.80pp | −1.00pp | ❌ |
| `oi_outlier_share` | low | 375 | +4.42% | −0.14pp | −0.34pp | ❌ |

Chance p95 at this selectivity: **+5.20%**. **Nothing reaches it — including
IV rank at +4.76%.**

Selecting a random 30% of trades beats every real filter 5% of the time. No
filter here clears that bar.

### The one apparent exception, and why it is not one

At keep = 20%, `oi_outlier_share` shows +7.02% and appears to beat everything.
It selects **one trade**. Reported for completeness; it means nothing.

## 4. Significance — nothing excludes zero

Monthly block bootstrap, 1,500 draws, paired within each resample so filtered
and unfiltered always come from the same months:

| Feature | Edge | 95% CI | |
|---|---|---|---|
| `ce_ivr` | +0.23pp | [−1.27, +1.56] | straddles zero |
| `iv_rv_spread` | −0.52pp | [−2.21, +1.04] | straddles zero |
| **`fut_basis_excess`** | **−3.61pp** | **[−6.33, −1.21]** | **significantly negative** |
| `dist_to_flip` | −0.42pp | [−1.40, +0.58] | straddles zero |
| `gex_z` | −0.78pp | [−1.77, +0.11] | straddles zero |
| `oi_outlier_share` | −0.07pp | [−3.78, +4.20] | straddles zero |

`fut_basis_excess` is the only significant result in the table and it is
**significantly harmful**. Filtering on the project's strongest constructed
feature makes the strategy materially worse.

## 5. Walk-forward — selectivity chosen on the past, scored on the next month

37 folds, 24-month warm-up. Threshold picked on every prior month, applied
unchanged forward.

| Feature | Mean edge | Folds positive |
|---|---|---|
| `dist_to_flip` | +0.53pp | 20/37 (54%) |
| `oi_outlier_share` | −0.08pp | 16/37 |
| `gex_z` | −0.45pp | 12/37 |
| **`ce_ivr`** | **−0.79pp** | 20/37 |
| `iv_rv_spread` | −1.27pp | 18/37 |
| `fut_basis_excess` | −2.46pp | 13/37 |

Five of six are negative out of sample. The best, `dist_to_flip`, wins 54% of
folds — indistinguishable from a coin flip.

**IV rank is negative out of sample too**, despite being marginally positive
in-sample. That gap is what walk-forward exists to expose.

## 6. The decisive table — every feature flips sign across years

Edge in percentage points, keep = 30%:

| Year | `ce_ivr` | `iv_rv_spread` | `fut_basis_excess` | `dist_to_flip` | `gex_z` | `oi_outlier_share` |
|---|---|---|---|---|---|---|
| 2021 | +6.47 | +6.97 | **+12.76** | +2.84 | −0.94 | −8.30 |
| 2022 | −1.29 | −2.48 | **−11.02** | −0.48 | −0.94 | −6.92 |
| 2023 | +2.15 | +1.62 | +0.08 | +0.23 | −0.82 | −2.09 |
| 2024 | +1.82 | +0.90 | −2.83 | +0.56 | −0.94 | −1.59 |
| 2025 | −1.97 | −2.81 | −1.40 | −1.77 | +0.12 | −1.09 |
| 2026 | +2.80 | +1.11 | +1.29 | +1.24 | −0.34 | +9.10 |

`fut_basis_excess` swings from **+12.76pp to −11.02pp in consecutive years**.
That is not a signal with unstable magnitude; it is noise.

Only `gex_z` is consistent — consistently, mildly **harmful**.

**This table is why Gate 1 had to run per year.** Pooled over the full sample,
`ce_ivr` and `iv_rv_spread` show positive mean edges (+1.66pp, +0.88pp) that
come almost entirely from 2021. A filter fitted on the whole window would have
inherited that and looked usable.

## 7. Does anything work in the recent, harder regime?

Gate 0 found the strategy's edge decaying from 8.30% (2021) to 1.09% (2025).
Restricting to 2024–2026 (4,096 trades, unfiltered +2.25%):

| Feature | Edge |
|---|---|
| `oi_outlier_share` | +1.89pp |
| `ce_ivr` | +0.35pp |
| `dist_to_flip` | −0.30pp |
| `gex_z` | −0.37pp |
| `iv_rv_spread` | −0.77pp |
| `fut_basis_excess` | −1.32pp |

`oi_outlier_share` leads here after being the *worst* feature in 2021–2023.
Given §6, the honest reading is that this is the same noise viewed from a
different window, not a regime-specific signal.

## 8. What this establishes

**The research features do not improve the strategy.** Measured against three
baselines, at three selectivities, with walk-forward selection and a monthly
block bootstrap. Not one criterion passes.

**Neither does implied volatility rank**, which is the more surprising half.
"Sell options when IV is historically high" is the standard retail heuristic,
and on this data at this cadence it does not beat trading everything.

Two readings, and the evidence does not separate them:

1. **Monthly entry is too coarse.** IV rank measured at one point per month,
   held for a month, may simply be too slow. A daily-entry strategy could find
   what this cannot.
2. **The premium is not concentrated.** If the variance risk premium is paid
   roughly evenly across names, there is nothing for a cross-sectional filter to
   select, and breadth is the only lever.

The second is consistent with Gate 0's finding that the strategy earns its
return from a **high win rate on many small trades**, not from a subset of
exceptional ones.

## 9. Limits of this null

- **Only six features, one form.** Rank-and-keep on a single feature at a time.
  Combinations, interactions and non-monotone relationships are untested.
- **Only monthly entry.** Everything here inherits Gate 0's cadence.
- **Coverage is imperfect.** `ce_ivr` is non-null on 70.8% of trades, so the
  IV-rank baseline is computed on a subset. The other features range 73–97%.
- **`fut_basis_excess` has a structural hole** — NULL in every expiry week,
  which is 36% of dates and not at random.
- Selectivity was the only lever tested. **Position sizing, stop losses and
  strike selection were not**, and §10 argues the first two matter more.

## 10. What follows

Phase 2 should ship, but as the honest version described in
[`05-phase-2-serving.md` §6](../implementation/05-phase-2-serving.md#6-if-gate-1-failed--ship-the-screen-anyway):

- Rank by **breadth and tradeability**, not by a validated filter.
- Show the research features as **context columns**, labelled as not having
  beaten the baseline.
- Keep every risk control and the liquidity gate.

That is a defensible product: *"here is what the position would cost and risk,
here is what else is unusual about this name."* It claims nothing the evidence
cannot support.

The two levers Gate 1 did not test are more promising than another feature
sweep:

1. **A stop loss.** Max drawdown is −37.27% and positions currently run to
   expiry regardless. This changes the return distribution far more than any
   entry filter.
2. **Position sizing and correlation limits.** Gate 0's portfolio assumes every
   eligible trade is funded, which no real book can do.

Both act on the tail, which is where this strategy's risk actually lives — and
Phase 0 demonstrated that the tail, not the mean, is the part worth working on.
