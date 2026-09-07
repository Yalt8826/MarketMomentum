# 11 — Machine Learning, Only the Parts We Use

[← The features](10-the-features.md) · [Index](README.md) · Next: [The models →](12-the-models.md)

You do not need a machine-learning course to read document 12. You need about
eight concepts. This document supplies exactly those, in the order the project
uses them, with no mathematics beyond arithmetic.

---

## 1. What a model is

A model is a function that takes numbers in and produces a number out, whose
internal settings are chosen so its output matches known answers as closely as
possible.

```
   features  ──▶  [ model ]  ──▶  prediction     compared against ──▶ truth
   (what we                                                            (label)
    observed)
```

**Training** is choosing the internal settings. **Evaluation** is checking the
output against answers the model was not allowed to see while training.

That last clause is the whole discipline. Everything below is a technique for
enforcing it.

## 2. The three models this project actually uses

No neural networks anywhere, and that is a deliberate choice rather than a
limitation.

| Model | What it is | Where used |
|---|---|---|
| **Ridge regression** | A straight line through the data, with a penalty that stops any one feature dominating | Stages 1 and 3 |
| **XGBoost** | Hundreds of small decision trees, each correcting the previous ones' mistakes | Stage 1, as a comparison |
| **Hidden Markov Model (HMM)** | Assumes the data passes through hidden "states" and tries to infer them | Stage 2 |

**Why nothing bigger?** At market level there are about 1,000 usable rows and 25
features. At symbol level there is no label at all. A neural network needs
hundreds of thousands of labelled examples to beat a regularised linear model,
and would simply memorise noise here.

The project made this a *prediction recorded in advance*: "Ridge will match or
beat XGBoost at n≈556." **Ridge won 2 of 3.** Writing down what you expect before
you run is a cheap and surprisingly effective discipline.

## 3. Rank correlation, and the information coefficient (IC)

This is the metric almost every result in this project is reported in, so it is
worth understanding properly.

Suppose you rank 150 stocks by your signal, from most to least. Then, later, you
rank the same stocks by what actually happened. **How similar are the two
orderings?**

That similarity is the **Spearman rank correlation**, and in finance it is called
the **information coefficient (IC)**.

```
   Your ranking      Reality        agreement?
   ─────────────     ────────       ──────────
   1  RELIANCE       1  RELIANCE       ✓
   2  TCS            3  TCS            close
   3  INFY           2  INFY           close
   ...
   150 IREDA         149 IREDA         ✓          →  IC near +1.0

   perfectly reversed                             →  IC = −1.0
   completely unrelated                           →  IC =  0.0
```

**Why ranks rather than raw values?** Because financial data has extreme
outliers. One stock that moved 40% would dominate an ordinary correlation. Ranks
say "it was the biggest", not "it was 40%", which is both more robust and closer
to what you would actually act on.

### What counts as a good IC?

Not what a beginner expects.

| IC | Interpretation in this domain |
|---|---|
| 0.00 | Nothing |
| 0.02–0.05 | Marginal; needs a lot of observations to be believable |
| **0.05–0.15** | **A real, useful signal in equity research** |
| 0.30+ | Suspect a bug, a leak, or a truism |

The project's best result is **+0.1374**. Its largest number, **+0.360**, is
FII accumulation vs price momentum — and that one is a truism, not a discovery.

### Computed *within* each date

Critically, the IC is computed **per date (or per quarter) and then averaged**,
not once over the whole pooled dataset. Ranking within a date removes everything
common to the whole market that day — if everything fell 2%, that is invisible to
a within-date ranking. What is left is genuinely about telling symbols apart.

## 4. AUC — the metric for yes/no questions

When the question is a yes/no ("will tomorrow's flow be positive?"), the metric
is **AUC** (area under the ROC curve).

Read it as: *pick one actual "yes" and one actual "no" at random. What is the
probability the model scored the "yes" higher?*

| AUC | Meaning |
|---|---|
| 0.50 | A coin flip |
| 0.55–0.60 | Weak but real |
| 0.70 | Strong for financial data |
| 1.00 | Perfect — **and in this domain, almost always a bug** |

## 5. Train/test splits — and why the obvious one is wrong

You cannot evaluate a model on data it was trained on; it will look excellent and
be useless. So you hold data back.

**In time-series data you must split by time**, not randomly. If you shuffle rows
and hold back 20%, the model trains on Wednesday and Friday and is tested on
Thursday — it has effectively seen the future.

This project's splits obey three rules:

**1. Split on unique dates, never on rows.** Every symbol on a given date goes to
the same side. A row-level split would scatter one date's symbols across the
boundary and leak the market-wide factor.

**2. Leave an embargo gap.** The label spans day `t → t+1`, so the last training
day's outcome is realised on the first test day. Purging a day from the training
tail closes that.

```
   TRAIN ██████████████████░ EMBARGO ░████████ VALID ████████
                            ▲
                     one date purged so the label
                     horizon cannot straddle the boundary
```

**3. Walk forward.** Rather than one split, use several expanding ones — train on
the first year, test on the next chunk; train on two years, test on the next; and
so on. This tests whether the model works *repeatedly*, not once.

**4. Normalisation must be causal too.** Every feature is percentile-transformed
against *its own past only*, on an expanding window. Scaling by the whole
dataset's mean and standard deviation would leak the future's distribution into
every training row — a subtle leak that is easy to miss. A unit test appends
future rows and asserts earlier values are bit-identical.

## 6. Partial correlation — the most important tool in the project

Suppose a feature correlates +0.36 with mutual-fund buying. Impressive?

Not until you know whether it also correlates +0.36 with *the stock going up* —
because "institutions buy stocks that are rising" is a truism, and a feature that
merely detects rising stocks has discovered nothing.

**Partial correlation** answers: *how much correlation is left after the boring
explanation is removed?*

The procedure, in the project's own code:

```
   1. rank the signal, rank the boring explanation
   2. fit a line predicting signal-rank from boring-rank
   3. subtract that line — keep the residual
   4. correlate the residual with the answer
```

Two results this immediately reframes:

| Feature | Raw | Partial | Reading |
|---|---|---|---|
| `oi_outlier_moneyness` vs next-day move | +0.0368 | **+0.0004** | It was measuring implied volatility |
| `basis_excess_mean` vs MF accumulation | +0.140 | **+0.137** | **98% survives** — it is not momentum |

> **The standing rule of this project: never report a raw correlation on its
> own.** Assume any price-derived feature is a momentum proxy until the partial
> says otherwise.

## 7. Placebos, tripwires and baselines — proving the apparatus works

Three checks that answer *"did we measure nothing, or is our measuring device
broken?"* Both were specified before any model was fitted.

**The leakage tripwire.** Deliberately give a model tomorrow's answer as a
feature. It should score near-perfectly. If it does not, the harness is broken.
→ Result: **AUC 1.0000** for all three participants. ✅

**The placebo.** Shuffle the labels randomly across dates and re-run everything.
The score should collapse to zero. If a shuffled model still scores well, the
"signal" was coming from feature structure, not from the labels.
→ Result: R² of **−0.008 / −0.002 / −0.015** against a real +0.451. ✅

**The baseline.** Compare against the dumbest thing that could work. In Stage 1
that is a calendar-only model; in Stage 5 it is price momentum.

Together these establish that the pipeline detects signal when signal is present
and collapses when it is removed. **That is what makes the project's many null
results measurements rather than defects.**

The placebo also caught a real overclaim. Stage 3's "earnings corroboration"
lifts of +0.012 to +0.053 all looked like modest positive results in isolation —
until the placebo, fitted on shuffled labels, clustered around earnings *more
strongly* than the real model did. Without something to compare against, they
would have been written up as weak support.

## 8. Bootstrap confidence intervals

A measured IC of +0.1374 is one number from one dataset. How much would it move
if history had gone slightly differently?

A **bootstrap** answers this by resampling the data thousands of times and
recomputing. The spread of those answers is the confidence interval.

Two refinements this project needs:

**Moving-block bootstrap** for daily series. A daily market series is
autocorrelated — today resembles yesterday — so treating 1,000 days as 1,000
independent observations dramatically overstates significance. Resampling in
21-day *blocks* preserves that structure.

**Bootstrap over periods, not rows**, for Stage 5. About 150 symbols share a
quarter, and they share that quarter's market conditions. Resampling rows would
pretend those 150 observations were independent. Resampling whole quarters does
not.

→ Stage 5's headline: **+0.1374, 95% CI [+0.0902, +0.1801]**, excludes zero.

## 9. Multiple comparisons

If you test 120 different combinations at the usual 5% threshold, you expect
about six "significant" results **from pure chance**.

Stage 5 examined roughly 120 cells. Its binomial p-value of 0.000038 becomes
**0.0046 after a Bonferroni correction** — multiplying by the number of tests.
Still significant, but the correction is applied and reported rather than
quietly skipped.

## 10. Learning from Label Proportions (LLP)

One unusual technique, used in Stage 3, worth a paragraph because it is the
formal name for the project's central problem.

You know the *total* for a group but not the individual values. Each date is a
**bag** of ~170 symbols, and only the bag's aggregate flow is observed. LLP fits
a per-symbol function whose bag-aggregates match the observed totals.

For a linear function this reduces exactly to ridge regression on the
weighted-mean feature vector, so the project uses that closed form.

**Its fundamental limitation, which no amount of technique removes:** with ~170
symbols and one equation per day, the system is **underdetermined by
construction.** Many different per-symbol allocations reproduce the same
aggregate exactly. What picks one is the functional form, the regularisation and
the weighting — all assumptions, not data.

## 11. The vocabulary, collected

| Term | Meaning |
|---|---|
| **Feature** | An input number computed from raw data |
| **Label / target** | The answer we are trying to match |
| **Training / evaluation** | Fitting the model / testing it on unseen data |
| **IC** | Information coefficient — rank correlation between prediction and outcome |
| **ICIR** | mean(IC) ÷ std(IC) — the signal-to-noise of the signal itself |
| **AUC** | Ranking quality for a yes/no question; 0.5 is a coin flip |
| **Walk-forward** | Repeated expanding train/test splits through time |
| **Embargo** | A purged gap so the label horizon cannot straddle the split |
| **Causal normalisation** | Scaling using only each row's own past |
| **Partial correlation** | What is left after the boring explanation is removed |
| **Placebo** | Re-run on shuffled labels; should collapse to zero |
| **Tripwire** | A model given the answer; should score perfectly |
| **Bootstrap** | Resampling to get a confidence interval |
| **Bonferroni** | Correcting for having tested many hypotheses |
| **LLP** | Learning from Label Proportions — only group totals are known |
| **Decile spread** | Average outcome of the top 10% minus the bottom 10% by your ranking |

[← The features](10-the-features.md) · [Index](README.md) · Next: [The models →](12-the-models.md)
