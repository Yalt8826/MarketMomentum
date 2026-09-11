# 14 — Results, in Plain Terms

[← The journey](13-the-journey.md) · [Index](README.md) · Next: [Bugs and lessons →](15-bugs-and-lessons.md)

Every number in this document appears in the technical reports. What this
document adds is a translation out of statistics and into English, and an
explicit statement of what may and may not be claimed.

---

## 1. The scoreboard

| Stage | What it tested | Verdict | The deciding number |
|---|---|---|---|
| **1** — market flow | Can we predict tomorrow's market-wide institutional flow? | ❌ **Null** | Best margin over a calendar-only baseline: **+0.002**. Four of 18 combinations significantly *negative*. |
| **1b** — buildup quadrants | Can open-interest builds be given a direction? | ✅ **Success** | Delta adjustment required: raw vs adjusted disagree on **35.8%** of contracts. |
| **2** — regime HMM | Do symbol-days fall into persistent positioning regimes? | ❌ **Negative** | Minimum state persistence **never exceeds 0.5 at any state count**. |
| **3** — attribution | Can a market-wide flow number be split across symbols? | ⚠️ **Mixed** | Decomposes (R² 0.45) but the earnings check **fails** — shuffled labels beat real ones. |
| **4** — flow momentum | Does directional positioning continue? | ⚠️ **Split** | A real futures→options lead (+0.033 at lag 2), but **flat across activity terciles** — microstructure, not whales. |
| **5** — accumulation | Can we detect institutional accumulation per symbol? | ✅ **Positive** | Partial IC **+0.1374**, positive in **18 of 19 quarters**. |
| — GEX | Does dealer gamma predict volatility? | ❌ **Negative** | Fails on sign, on magnitude, and against implied volatility. |

---

## 2. Are these results trustworthy?

This has to come before the results themselves, because it is what separates
*"we measured nothing"* from *"our measuring device was broken."* Both checks
were specified before any model was fitted.

| Check | Expected | Observed | |
|---|---|---|---|
| **Leakage tripwire** — a model deliberately given tomorrow's answer | AUC > 0.95 | **1.0000** for all three participants | ✅ |
| **Placebo** — labels shuffled randomly across dates | R² ≈ 0 | **−0.008 / −0.002 / −0.015** against a real +0.451 | ✅ |

The pipeline demonstrably detects signal when signal is present, and
demonstrably collapses when it is removed. **The nulls are therefore
measurements, not defects.**

---

## 3. The positive result, translated

The technical statement is: *the mean futures basis in excess of carry has a
momentum-controlled cross-sectional rank information coefficient of +0.1374
against quarterly mutual-fund accumulation, positive in 18 of 19 quarters.*

Here is the same thing in terms you can picture. Take the shipped score, rank
about 152 symbols by it each quarter, and look at the extremes:

| | **Top decile** | **Bottom decile** |
|---|---|---|
| Mean mutual-fund accumulation | **+0.679%** of the company | +0.155% |
| Spread | **+0.524 percentage points** | |
| Quarters where top beat bottom | **18 of 19** | |
| Symbols that were accumulated at all | **72.2%** | 56.1% |

Base rate across all symbols: **62.7%** were accumulated to some degree.
19 periods, ~152 symbols each.

**So:** pick the top 10% by this signal and mutual funds bought roughly **four
times as much of those companies** as of the bottom 10% — and it worked in 18 of
19 quarters. The directional hit rate is 72.2% against a 62.7% base rate:
**+9.5 percentage points**, which is modest and honest.

> **Reproduce it:** `uv run python scripts/decile_spread.py`. That script reuses
> `build_accumulation_frame` unchanged and applies the same momentum
> residualisation `nowcast` ships, so it is a restatement of the shipped result
> rather than a second experiment. The numbers move slightly as the database
> gains trading days; these are as at `option_metrics` through **2026-09-04**.

That is the finding. It is real, it is weak, and both halves of that sentence
matter.

### The three things that make it credible

**1. It survives the control that kills everything else.** The raw correlation
is +0.140 and the momentum-controlled one is +0.137 — **98% retained.** Compare
`oi_outlier_moneyness`, which went from +0.0368 to +0.0004 under the same kind of
control.

**2. It is consistent, not large.** 18 of 19 quarters (binomial p = 0.000038;
p = 0.0046 after Bonferroni-correcting for ~120 examined cells). Bootstrap over
periods: **95% CI [+0.0902, +0.1801]**, excluding zero. It holds in both time
halves and both size halves.

**3. The counterparty structure is coherent.** The same feature carries
*opposite* signs for foreign funds (−0.054) and mutual funds (+0.137), and the
two accumulation series correlate at **−0.4807, negative in 19 of 19 quarters.**
One trade, both sides. And unlike Stage 1's analogous check, these two series are
filed independently by different holders — nothing forces them to oppose.

### Timing: it detects, it does not forecast

| Window | Partial IC |
|---|---|
| The **prior** quarter (pure forecasting) | nothing |
| First **30 days** of the quarter | **+0.085** |
| The full quarter | **+0.137** |

This is exactly the intended shape. And because the filing publishes ~6 weeks
after quarter end, a detector firing 30 days in **leads public disclosure by
about four months.**

---

## 4. The negative results, and why they are worth having

### FII accumulation is price momentum

Foreign institutional accumulation correlates with price momentum at **+0.360** —
the largest number in the project. No derivatives feature survives its control.

This is a *more robustly established* finding than the positive one. It says:
foreign funds buy what is going up, and their buying carries no derivatives
signature beyond that. Anyone building a "smart money detector" that finds FII
flow is at serious risk of having built a momentum detector.

### Nothing predicts tomorrow's flow better than the calendar

Five calendar features — days to expiry plus day-of-week — reach AUC 0.61–0.72
on next-day institutional flow. Nothing built here beats it, and four of 18
combinations are significantly *worse* than it.

**Reading:** short-horizon institutional flow at market level is dominated by
mechanical roll scheduling. There is little left to predict once you know the
date.

### There are no positioning regimes

Not a single state configuration produces states lasting more than ~1.6 days.
Three independent signatures agree, and the conclusion is that the observables
**vary continuously** — an HMM asked to find discrete modes in a continuum will
always return something and always mean nothing.

### Dealer gamma does not predict volatility

Three failures: the sign runs the wrong way (1.360% vs 1.547%), the sign carries
no information anyway (ranking on `−|gex|` scores better), and implied volatility
already contains everything it has (+0.0099 partial against IV's own +0.2114).

**What survives, narrower and defensible:** when a symbol carries unusually
little dealer gamma *relative to its own history*, its next-day move is larger —
after controlling for the symbol's own IV (ρ = +0.0436, same sign in all six
years). Note what changed: comparing a symbol against *itself*. The pooled
version was ranking stocks by size.

### The futures→options lead is real but not institutional

+0.033 at lag 2 with 84% of symbols agreeing, and it survives partialling out the
same-day relationship. But split by activity:

| Activity | Partial lead |
|---|---|
| Low (quiet days) | +0.0306 |
| Mid | +0.0347 |
| High | +0.0285 |

**Flat.** A whale-driven effect would concentrate where whales are active. This
is a pervasive microstructure property.

---

## 5. Two overclaims that were withdrawn

Both were written up as strong evidence before being checked properly. They are
recorded here because catching them is part of the result.

**Stage 1's Pro/Client opposition** (prediction correlation −0.897) and **Stage
2's "8 of 8 states show opposed FII/Client signs"** were both presented as the
models independently recovering market structure.

They are near-tautological. The labels themselves are already correlated by
construction:

| | FII | Pro | Client | DII |
|---|---|---|---|---|
| **Client** | **−0.757** | **−0.802** | 1.000 | **−0.835** |

The derivatives book is closed — every long is somebody's short — so Client is
mechanically the other side of every institutional category. Any variable
correlating with FII *must* correlate oppositely with Client.

> **Lesson: check the correlation between your labels before citing an
> opposition result as validation.**

This is also precisely why Stage 5's FII/MF counterparty check *does* count:
those two series are filed independently and nothing forces them to oppose.

---

## 6. Limitations, stated plainly

- **Nothing here is a trading edge.** Measured partial correlations around 0.17
  imply an AUC ceiling near 0.55–0.60. This has since been tested directly
  rather than inferred: the detector's score predicts the next quarter's return
  at **+0.0200** (11 of 19 quarters), and the features were separately tested as
  entry filters for an options strategy where **none beat a random filter** —
  see [`docs/trading/results/`](../../trading/results/README.md).
- **No true out-of-sample test exists yet.** Subset stability is weaker than an
  independent dataset. The frozen nowcasts are the first genuine one, and they
  are not scorable until November 2026.
- **19 quarters is few**, and ~120 cells were examined. The Bonferroni
  correction is applied; the original feature selection was not pre-registered.
- **`fut_basis_excess` has a 36% structural hole.** It is NULL in every expiry
  week — precisely when positioning is most active. Unresolved, and it materially
  limits what Stage 1's null can be said to have tested.
- **One market, one regime.** NSE F&O, 2021–2026, a period when retail
  participation grew several-fold. Untested anywhere else.
- **The effect nearly triples** between the first and second halves of the
  sample. The best available explanation is that it scales with mutual-fund flow
  volume (+0.369 partial), but at 19 periods that gives p = 0.13 — an
  explanation, not an established fact.
- **The earnings calendar was a weak instrument.** Every symbol behaves unusually
  near earnings, so it cannot separate institutional activity from ordinary
  event-driven activity. This is what sank Stage 3's validation.

---

## 7. When the next real test happens

The 19 quarters above are *closed* — the filings exist and the correlation was
measured against them. Careful, controlled, and still a fit.

The genuinely forward test is the two frozen records:

| Record | Window | Predicted IC |
|---|---|---|
| `nowcast-2026-08-28.md` | 59 days | +0.1145 |
| `nowcast-2026-09-06.md` | 66 days | +0.1266 |

Both predict the quarter ending **30 September 2026**. SEBI allows 21 days to
file and NSE has run a few weeks behind that, so **~14 November 2026** is when
they become scorable. The next fully independent test after that is the
October–December quarter, around **mid-February 2027**.

To score: re-run the pipeline, then rank-correlate the record's `score` column
against the realised `mf_shares` change, applying the same corporate-action
filter the evaluation uses. Expect ≈ +0.11. The null is 0.

**A caveat to hold onto:** 18-of-19 implies roughly one negative quarter in ten by
chance. A single bad November falsifies nothing; a run of them would. That is
written into the record itself so nobody over-reacts to one number — including
whoever is reading it.

---

## 8. What would change the answers

In descending order of value per unit of effort:

1. **Scrape monthly mutual-fund portfolios** (~40 asset-manager websites). This
   is the only true out-of-sample test available: monthly rather than quarterly,
   and a completely independent disclosure route.
2. **Replace `basis_excess_med` with un-annualised `fut_basis_pct` in Stage 1.**
   The latter is well-behaved at every days-to-expiry and is always populated,
   recovering the 448 lost dates. A one-feature change and a ~4-minute re-run —
   it directly tests whether Stage 1's null is an artefact of the expiry-week
   hole.
3. **Replace Stage 2's HMM with a continuous latent-state model.** If the
   observables are a continuum, a linear-Gaussian state-space model matches the
   data's shape where a discrete-state model cannot.
4. **Replace Stage 3's earnings instrument with index inclusion/exclusion
   events.** These force large, externally dated, unambiguously institutional
   trades in named symbols — a genuine natural experiment rather than a period of
   general unusualness.
5. **Aggregate Stage 2's observables to weekly bars.** Positioning may be
   persistent weekly and not daily.

[← The journey](13-the-journey.md) · [Index](README.md) · Next: [Bugs and lessons →](15-bugs-and-lessons.md)
