# 04 — Phase 1: does the research add anything?

> **Status: RUN 2026-09-11 — FAILED on every criterion.**
> Full results: [`../results/gate-1-feature-filters.md`](../results/gate-1-feature-filters.md).
> The prior in §6 was recorded before the run and held. §7 records what actually
> happened against what this document predicted.

[← Phase 0](03-phase-0-economics.md) · [Index](README.md) · Next: [Phase 2 →](05-phase-2-serving.md)

**Reached only if Gate 0 passes.**

**The question:** does conditioning entry on this project's features beat
entering unconditionally — *and* beat the filter every retail vol seller already
uses?

This is the gate that decides whether five stages of research contributed
anything to a tradeable strategy. The honest prior is that it **fails**.

---

## 1. The candidate filters

Attached to each trade **as at the prior close** (see §3 — this is a correctness
requirement, not a detail):

| Feature | Source | Hypothesis |
|---|---|---|
| `ce_ivp` / `ce_ivr` | `ml_dataset` | IV percentile / rank — sell when vol is rich against its own history |
| `ce_iv_30 − rv_yz` | `ml_dataset` | The variance risk premium **directly**: implied minus realised |
| `fut_basis_excess` | `flow_features` | Strongest constructed feature (−0.0516 vs \|return\|) — high leveraged-long demand went with *calmer* next-day moves |
| `dist_to_flip` | `ml_dataset` | Best dealer feature (+0.0210), and invariant to the sign convention that turned out wrong |
| within-symbol `gex` z-score | derived | The magnitude effect that survived (+0.0436 after the IV control) |
| `oi_outlier_share` | `flow_features` | Is something unusual being positioned here? A reason *not* to be short vol |

Note the expected signs differ: some are reasons to **take** the trade, some are
reasons to **skip** it. A filter that can only ever add trades is not a filter.

## 2. The mandatory baselines

This project's history is unambiguous on this point. Stage 1 was defeated by the
expiry calendar; Stage 5 was only credible because it beat price momentum. **The
boring explanation here is IV rank.**

| Baseline | Role |
|---|---|
| **Unfiltered** — every eligible symbol-month | The floor. Gate 0's own result. |
| **IV rank alone** | ⭐ **The one that matters.** Requires none of this research. |
| **Random filter** | The placebo: same trade count, seeded, many draws → the distribution of pure chance |

> "Sell options when implied volatility is high" is what a retail vol seller
> already does with a free screener. If the project's features merely *match*
> that, the research contributed nothing to the strategy and the write-up must
> say so in those words.

The random filter matters more than it looks. Selecting 30% of 13,000 trades
gives enormous freedom to find a subset that performed well. The placebo
establishes how good a *meaningless* filter looks at that trade count, and any
real filter must clear it.

## 3. Look-ahead: the trap specific to this phase

The unfiltered backtest enters on day 1 at `settle`. That is an acceptable fill
assumption. **It becomes look-ahead the instant a filter reads day-1 data**,
because `settle`, IV, and every IV-derived feature are knowable only after day 1
closes.

```
   WRONG                          RIGHT
   features(t) -> trade(t)        features(t-1) -> trade(t)
```

**Required:** a unit test that plants a deliberately look-ahead filter — one
conditioned on the trade's own outcome — and asserts the harness rejects or
flags it. This is the same device as the project's existing leakage tripwire,
which scored a perfect AUC 1.0000 and thereby proved the harness could detect
leakage at all.

## 4. Evaluation design

**Splits.** Reuse `ml/splits.py` — purged walk-forward over unique dates with an
embargo. Any threshold is chosen on train and evaluated on test. Never select and
report on the same fold. Report per-fold results, not only the pooled number.

**Significance.** Trades are monthly and there are ~60 months, so `n` is small
and trades within a month share that month's market. A t-test over 13,000 trades
would treat them as independent and badly overstate significance.

> Use the **moving-block bootstrap over months**, not over trades. This is the
> same correction the research branch applied to daily series, for the same
> reason.

**Metric.** Return on margin, net of costs and slippage — the Phase 0 quantity.
Not points, not win rate. A filter that raises the win rate while lowering net
return has made the strategy worse and more comfortable.

## 5. Gate 1 — acceptance criteria

| # | Criterion | |
|---|---|---|
| 1 | Filtered beats **unfiltered** out-of-sample | required |
| 2 | Filtered beats **IV rank alone** out-of-sample | ⭐ **required** |
| 3 | Improvement lies outside the random-filter distribution | required |
| 4 | Holds in **both halves** of the sample | required |
| 5 | Look-ahead tripwire fires as designed | required |

Criterion 2 is the whole phase. Criteria 1 and 3 can be passed by a filter that
is really just IV rank in disguise — several of these features correlate with IV.

## 6. The honest prior

| | Partial IC vs next-day \|return\| |
|---|---|
| `ce_iv_30` alone | **+0.2114** |
| every feature this project built | **≤ 0.05** |

That is a four-fold head start for the boring explanation, measured on 212,441
symbol-days. **Gate 1 is expected to fail.**

Writing that down in advance is the point. If Gate 1 then passes, it passes
against a stated prior rather than into a space of unlimited retrospective
enthusiasm; if it fails, that was predicted and is not a disappointment.

### What "failure" delivers

A Gate 1 failure is not a wasted phase. It yields:

- A **working strategy** (Gate 0 passed) that a trader can use.
- The measured statement that **this project's research does not improve it** —
  which closes a question that would otherwise be asked forever.
- A cost, margin and slippage model that is reusable for any future strategy.

The write-up should present it exactly that way, in `docs/trading/results/`,
alongside the research branch's three other negative results.

[← Phase 0](03-phase-0-economics.md) · [Index](README.md) · Next: [Phase 2 →](05-phase-2-serving.md)

---

## 7. Outcome — recorded against the prediction

**Every criterion failed**, and the failure is broader than §6 predicted.

| # | Criterion | Predicted | Observed |
|---|---|---|---|
| 1 | Beats unfiltered | uncertain | ❌ marginal, never significant |
| 2 | **Beats IV rank alone** | **fail** | ❌ none |
| 3 | Outside the random-filter distribution | uncertain | ❌ none |
| 4 | Holds in both halves | uncertain | ❌ every feature flips sign across years |
| 5 | Look-ahead tripwire fires | must pass | ✅ +19.1pp |

### What §6 got right

The prior — "every constructed feature sits at ≤0.05 partial IC against implied
volatility's own +0.2114, so this gate is expected to fail" — was correct, and
writing it down first is what makes the null a measurement rather than a
disappointment.

### What §2 got wrong

This document framed IV rank as **the bar to clear**: the boring explanation
that works, which our features had to beat. That framing assumed IV rank itself
works.

**It does not.** At keep = 30% it scores +4.76% against +4.56% unfiltered and a
chance p95 of **+5.20%** — inside the noise. Out of sample it is *negative*
(−0.79pp over 37 folds).

So the finding is not "our features lost to the standard heuristic". It is:

> **Selectivity does not help this strategy at all.** Trading every eligible
> symbol-month is as good as any filter tested.

That is a stronger and more useful result than the one this phase was designed
to produce, and it changes what Phase 2 should be — see
[`05-phase-2-serving.md`](05-phase-2-serving.md).

### The check that earned its place

§4 required per-year reporting because Gate 0 found the edge decaying. That
requirement turned out to be decisive rather than cautionary: pooled over the
full sample, `ce_ivr` (+1.66pp) and `iv_rv_spread` (+0.88pp) show positive mean
edges which come **almost entirely from 2021**. A filter fitted on the whole
window would have inherited that and looked usable.

`fut_basis_excess` swings from **+12.76pp to −11.02pp in consecutive years** —
the clearest statement available that these are noise, not unstable signals.
