# Results — Institutional Flow Detection

**Run date:** 2026-09-05 · **Data:** 2021-08-02 → 2026-08-28 · **Code:** commit `61e7773`

This is the consolidated results report for the institutional-flow modelling
work. It is self-contained: the per-stage design documents in
[`../implementation/`](../implementation/) carry the specifications and the
full working, but nothing here depends on having read them.

---

## 1. Verdict

Five stages were built and run. **Three returned null or negative results, one
succeeded as a feature, and one produced the project's only surviving positive
finding.**

| Stage | What it tested | Verdict | Deciding number |
|---|---|---|---|
| **1** — market flow | Can our features predict tomorrow's market-wide institutional flow? | **Null** | Best AUC margin over a calendar-only baseline: **+0.002**. Four of 18 combinations significantly *negative*. |
| **1b** — buildup quadrants | Can open-interest builds be given a direction? | **Success** | Delta adjustment required: raw vs adjusted classification disagrees on **35.8%** of contracts. |
| **2** — regime HMM | Do symbol-days fall into persistent latent positioning regimes? | **Negative** | Minimum state persistence **never exceeds 0.5 at any state count** (0.367 → 0.072). |
| **3** — attribution | Can a market-wide flow figure be attributed to individual symbols? | **Mixed, leaning negative** | Decomposes well (R² 0.45) but earnings corroboration **fails**: shuffled labels cluster around earnings *more* than real ones. |
| **4** — flow momentum | Does a directional build continue? | **Split: no momentum, but a real lead** | Positioning direction has ~zero autocorrelation. But futures direction **leads** option direction 1–3 days: +0.030, **77–86% of symbols agree**, reverse ≈ 0. |

**The defensible claims from this work:**

> Market-wide flow labels plus per-symbol option features are sufficient to
> *decompose* an observed aggregate, but not to *identify* which symbols the
> institutional flow went into. Next-day market-wide flow is predicted better
> by the expiry calendar than by any derived feature built here.

> Positioning direction does not persist — but **futures positioning leads
> option positioning by one to three days**, asymmetrically and broadly across
> symbols. This is the one positive result that survives every control applied.

---

## 2. The problem being solved

The project's goal is **detecting institutional positioning, not predicting
price**. The central obstacle is a mismatch between what can be measured and
what needs predicting:

```
What we want to know                What we can actually observe
────────────────────────            ──────────────────────────────────
Institutional flow in               NSE participant-wise open interest:
RELIANCE on 2026-08-28              FII / DII / Pro / Client net positions
                                    for the WHOLE MARKET, one row per day

  ↓ per-symbol                        ↓ market-wide
  217 symbols × 1,254 days            1,258 days, no symbol dimension
  = 212,441 rows                      = 6,290 rows
  NO LABEL EXISTS                     LABEL EXISTS
```

No per-symbol institutional-flow label exists in the data, and none can be
obtained within the project's means. Each stage is a different response to that
single fact — Stage 1 works where the label exists, Stage 2 abandons labels
entirely, Stage 3 bridges the two.

---

## 3. Are these results trustworthy?

Establishing this first, because it is what separates *"we measured nothing"*
from *"our measurement apparatus was broken"*. Both checks were specified
before any model was fitted.

| Check | Expected | Observed | |
|---|---|---|---|
| **Leakage tripwire** (Stage 1) — a model deliberately given tomorrow's answer | AUC > 0.95 | **1.0000** for all three participants | ✅ |
| **Placebo** (Stage 3) — labels shuffled across dates | R² ≈ 0 | **−0.008 / −0.002 / −0.015** against a real 0.451 | ✅ |

The pipeline demonstrably detects signal when signal is present, and
demonstrably collapses when it is removed. The nulls in §5 and §6 are therefore
measurements rather than defects.

### Method notes

- **Splits.** Purged walk-forward over unique dates with a 1-day embargo
  (`ml/splits.py`). Every symbol on a date lands on the same side; a row-level
  split would scatter one date's names across the boundary and leak the market
  factor.
- **Normalization is causal.** Every feature is percentile-transformed against
  its own past only, on an expanding window. A global scaler would leak the
  future distribution into every training row. Enforced by a unit test that
  appends future rows and asserts earlier values are bit-identical.
- **Significance uses a moving-block bootstrap** (21-day blocks, 2,000 draws,
  paired). A daily market series is autocorrelated; a t-test over ~1,000 such
  observations treats them as independent and overstates significance.
- **No neural networks.** At the market level there are ~1,000 usable rows and
  ~25 features; at the per-symbol level there is no label at all. Regularized
  linear models, gradient-boosted trees and an HMM are the honest choices at
  these sample sizes.

---

## 4. Stage 1b — buildup quadrants (the component that worked)

**What it does.** Crosses each contract's open-interest change against its
price change, so a build can be attributed to buyers or to writers. Before
this, an OI build said *"₹400 crore of new interest appeared at this strike"*
and could not say whether it was **bought** (bullish, paying premium) or
**written** (bearish or income-seeking, collecting premium) — opposite market
views producing an identical print.

**The finding that shaped it.** A call premium rises when the underlying rises,
whether or not any buyer was aggressive, so the raw premium change is
confounded. Measured over **163,383 traded contracts** across 47 sampled days:

| Measure | Value |
|---|---|
| Disagreement, raw vs delta-adjusted | **35.8%** |
| Disagreement, notional-weighted | **36.9%** |
| Median \|raw premium move\| | 4.20 |
| Median \|delta × spot move\| | 3.12 |
| Writer share, raw → adjusted | 63.4% → 61.0% |

The confound is as large as the signal, and classification runs on the
delta-adjusted residual as a result:

```
(mark − mark_prev) − delta_prev × (spot − spot_prev)
```

**Why this mattered methodologically.** The aggregate would never have exposed
it. The writer share barely moves (63.4% → 61.0%) while more than a third of
individual assignments flip. A validation that checked only summary statistics
would have passed a feature that was wrong a third of the time.

**Coverage.** 10 new columns on `flow_features`, non-null on 97–100% of
symbol-days, flat across all five years — the check that matters, since F&O
liquidity grew several-fold over the window and a drifting rate would mean the
feature was tracking volume rather than positioning.

→ Detail: [`model-02-buildup-quadrants.md`](../implementation/model-02-buildup-quadrants.md)

---

## 5. Stage 1 — market-level flow model (null)

**Task.** Predict the sign of tomorrow's market-wide participant flow
(`d_net_opt_stock_call`) from today's aggregated option and futures features.
Scored on 806 common rows; 556 validation rows after the walk-forward warm-up.

### 5.1 The calendar wins

| Participant | Calendar-only AUC | Best model AUC | Margin |
|---|---|---|---|
| Client | **0.720** | 0.718 | −0.002 |
| FII | **0.654** | 0.654 | +0.0005 |
| Pro | **0.612** | 0.614 | +0.002 |

Across all 18 (feature set × model × participant) combinations:

- **Zero** margins are significantly positive.
- **Four** are significantly negative — worst is `core+direction:xgboost` on
  Client at −0.043 [−0.089, −0.003].

Adding the flow features to the calendar model does not merely fail to help; it
**actively degrades** it. They behave as noise diluting a real signal.

The baseline itself is the story: five features — days to near-month expiry
plus four day-of-week dummies — reach AUC 0.61–0.72. That is a far larger
effect than anything measured in the per-symbol work, and it is entirely
mechanical. Positions roll on a schedule published years in advance, so a model
that only rediscovers the roll cycle has found nothing. This is why the
calendar comparison was specified as the headline before any model was fitted.

### 5.2 Predictions made in advance, both held

| Prediction (recorded before running) | Outcome |
|---|---|
| Ridge will match or beat XGBoost at n≈556 | ✅ Ridge won 2 of 3 participants |
| Nothing will approach AUC 0.85 (that would signal a leak) | ✅ Range was 0.576–0.720 |

### 5.3 The direction features are the only block that helps

Ridge AUC as feature blocks are added:

| Participant | `core` | `+ direction` | `+ extended` |
|---|---|---|---|
| Pro | 0.5757 | 0.5942 | **0.6137** |
| Client | 0.6846 | 0.7117 | **0.7176** |
| FII | 0.6357 | 0.6208 | 0.6326 |

Stage 1b's direction features roughly **halve** the damage the directionless
core does for Pro and Client — for Pro they close the gap to the calendar
entirely. They do not help FII. This is the empirical justification for the
quadrant work, and it is the only place in the project where a constructed
feature block measurably improved a task.

### 5.4 A correction to a prior expectation

Persistence (predicting tomorrow's flow sign from today's) was expected to score
≈0.50, because the linear autocorrelation of participant flow is ≈0 (FII lag-1
= −0.019). It scored **0.526 / 0.555 / 0.604**. Sign persistence exists where
Pearson correlation does not see it, and FII flow in particular carries real
day-to-day sign persistence.

→ Detail: [`model-01-market-flow.md`](../implementation/model-01-market-flow.md)

---

## 6. Stage 2 — latent regime model (negative)

**Task.** With no per-symbol label available, fit a pooled Gaussian HMM over
six per-symbol flow observables and ask whether symbol-days fall into
persistent latent positioning states. Fitted on training dates only (sealed at
2024-12-31), decoded forward. 161,717 symbol-days retained (76.1%), 211 symbols.

### The persistence test fails at every state count

| States (k) | BIC | **Min transition diagonal** | Restart spread |
|---|---|---|---|
| 2 | −297,668 | **0.367** | 0 |
| 3 | −544,977 | **0.255** | 0 |
| 4 | −605,915 | **0.255** | 29,501 |
| 5 | −643,110 | **0.253** | 18,934 |
| 6 | −645,699 | **0.168** | 6,859 |
| 8 | −892,883 | **0.072** | 112,865 |

The requirement is that every state's self-transition exceeds 0.5 — that a
regime survives to the next day. **No state count satisfies it.** Even the
two-state model has a state with a 0.367 diagonal, implying a mean dwell time
of 1/(1−0.367) ≈ **1.6 days**. That is a day-to-day flicker, not a positioning
regime.

Three signatures point at the same explanation:

1. **Persistence worsens monotonically with k.** A model finding real regimes
   and given too many states usually splits them into sub-states that remain
   persistent. This one produces ever more transient states.
2. **BIC never turns around** — still improving at k=8, the largest tried. A
   selection criterion that does not turn around inside the search grid has not
   selected anything.
3. **The fits are not reproducible.** At k=8, six EM restarts differ by
   **112,865** log-likelihood units.

**Conclusion: these observables vary continuously.** An HMM asked to find
discrete modes in a continuum will always return something while meaning
nothing. Per the pre-registered falsification clause, this is reported rather
than tuned away — the temptation to search for a feature set that makes
persistence appear is exactly what the clause exists to prevent.

→ Detail: [`model-03-regime-hmm.md`](../implementation/model-03-regime-hmm.md)

---

## 7. Stage 3 — attribution bridge (mixed, leaning negative)

**Task.** Attribute the observed market-wide flow across individual symbols.
Formally this is **Learning from Label Proportions**: each date is a *bag* of
~170 symbols and only the bag aggregate is observed. For a linear `f` the bag
loss reduces exactly to ridge regression on the weighted-mean feature vector,
so that closed form is used.

| Participant | OOS R² | Placebo R² | Earnings lift | **Placebo earnings lift** | Identifiability |
|---|---|---|---|---|---|
| Pro | **+0.451** | −0.008 | +0.012 | −0.129 | 0.887 ✅ |
| Client | **+0.432** | −0.002 | +0.047 | **+0.097** | 0.807 ✅ |
| FII | +0.069 | −0.015 | +0.053 | **+0.187** | 0.503 ❌ |

### What works

The aggregate decomposition is real for Pro and Client, and the placebo
confirms it comes from the labels rather than from feature structure.

⚠️ **This R² must not be read as predictive.** It is *contemporaneous*
decomposition — features at `t` explaining the flow at `t` — not forecasting.
It does not contradict Stage 1's null, and presenting it as forecasting
performance would be a serious misreading.

### What fails

**The independent validation.** For **Client and FII, a model fitted on
randomly shuffled labels clusters its scores around earnings *more strongly*
than the real model does** — twice as much for Client, 3.5× for FII. The
earnings clustering therefore comes from the per-symbol features' own structure
(symbols behave unusually near earnings regardless of who traded) and **not**
from the flow labels. The only independent evidence available for the
per-symbol allocation does not support it.

FII additionally fails on two counts: its post-event lift (+0.098) exceeds its
pre-event lift (+0.053), which is reaction rather than positioning; and its
identifiability score of 0.503 means linear and tree-based models rank symbols
almost independently of each other, so its per-symbol ranking is a property of
the model rather than of the data.

**Why the placebo was decisive.** The real lifts (+0.012 to +0.053) all look
like modest positive results in isolation. Without a placebo to compare against
they would have been reported as weak corroboration.

### On identifiability

The per-symbol allocation is **not uniquely determined by the data**. With ~170
symbols and one equation per day the system is underdetermined by construction;
many allocations reproduce the same aggregate exactly. What selects one is the
functional form, the regularization, and the choice of weights — all
assumptions. For Pro and Client the allocation survives a change of functional
form (rank correlation 0.81–0.89), so it is at least not an artefact of
choosing a linear model. For FII it does not.

→ Detail: [`model-04-attribution-bridge.md`](../implementation/model-04-attribution-bridge.md)

---

## 7a. Stage 4 — flow momentum (split result)

**Task.** Does a directional build in a symbol continue? Self-supervised —
signal and target both from `flow_features`, so no per-symbol label is needed.

**Why a cross-sectional rank IC.** All NSE stock futures share one monthly
expiry, so `fut_dte` is near-constant within a date (a **single value on 83.6%
of dates**, median spread **0**). A within-date rank correlation is therefore
neutral to both the market-wide factor and the expiry cycle that dominated
Stage 1.

### No momentum in the signal itself

Raw within-symbol autocorrelation of `q_fut_direction`: **−0.009 / −0.001 /
+0.000 / −0.012 / +0.007** at lags 1/2/3/5/10, positive for only 36–56% of
symbols. `opt_direction` actively **reverses** (−0.092 at lag 1, negative for
**98.6%** of symbols) — the bounded-OI mechanism, since a position opened today
must eventually close.

### But futures lead options

Raw within-symbol correlation, `(c)` = share of symbols agreeing on sign:

| Direction | lag 0 | lag 1 | lag 2 | lag 3 | lag 5 |
|---|---|---|---|---|---|
| **futures → options** | −0.218 (99%) | **+0.030 (77%)** | **+0.035 (86%)** | **+0.022 (79%)** | +0.015 (63%) |
| **options → futures** | −0.218 (99%) | +0.003 (55%) | +0.001 (51%) | +0.002 (55%) | +0.003 (53%) |

**The asymmetry is the result** — symmetric confounds cannot produce it.
Cross-sectionally the same effect is **+0.047 to +0.055, significant at every
horizon**, the largest consistent signal in the project (Stage 1's best margin
was +0.002).

Same-day the relationship is strongly *negative* (−0.218, 99% of symbols):
futures long buildup coincides with writer-dominated option flow, then option
positioning turns bullish over the next one to three days. That sign flip is
what makes it a lead rather than a correlation.

### Two spurious findings caught

`ce_writer_frac` appeared to lead `opt_direction` at **+0.071** — larger than
the futures lead — but its same-day correlation is **−0.75**; they share
construction, so it is a mirror of the target's own reversal. The futures lead
cannot be explained that way (same-day only −0.153).

An intermediate version used causal within-symbol z-scoring to strip symbol
fixed effects and reported self-persistence of **+0.2979** with smooth decay —
by far the project's largest number. It was an artefact: the causal z subtracts
a *slowly updating* expanding mean, so consecutive z-scores share a baseline
error and look persistent on a series with zero autocorrelation. **The standard
repair for symbol fixed effects introduced a worse confound than the one it
removed.**

→ Detail: [`model-05-flow-momentum.md`](../implementation/model-05-flow-momentum.md)

---

## 8. Cross-cutting findings

Results that are not any single stage's output but emerged from the work.

### 8.1 A structural gap in the strongest feature

`fut_basis_excess` is `NULL` whenever days-to-expiry < 5 (annualising a one-day
basis multiplies it by 365). At market level *every* symbol enters that window
simultaneously, so the median vanishes for the whole of every expiry week —
**448 of 1,254 dates (36%)**, and not at random. They are precisely the days
when the calendar baseline is most informative and institutional positioning is
most active.

This is a live threat to Stage 1's null: the strongest flow feature is
systematically absent from the most informative third of the sample.

### 8.2 A silent data-ordering defect

The bhavcopy parquet cache holds one row group per trade date, but the frame is
sorted by symbol, so the row groups are **not in chronological order** — 293
backward steps across five years.

The first quadrant implementation streamed them in file order and treated the
previous group as "yesterday", joining each day against an arbitrary other day.
**Nothing failed.** The same contract exists on both days, so every merge
succeeded and returned a price change spanning months. It surfaced only as a
coverage discontinuity that initially resembled the UDiFF schema cutoff and was
not. The outlier detector had never exposed it because it operates purely
within a single day.

### 8.3 Two structural checks that turned out near-tautological

Both were initially written up as strong evidence and had to be withdrawn.

Correlation of `d_net_opt_stock_call` between participants:

| | FII | Pro | Client | DII |
|---|---|---|---|---|
| **FII** | 1.000 | 0.373 | **−0.757** | 0.696 |
| **Pro** | 0.373 | 1.000 | **−0.802** | 0.376 |
| **Client** | −0.757 | −0.802 | 1.000 | −0.835 |

Client is already −0.76 to −0.84 correlated with every institutional category,
because the book is closed and they are the other side of it. Consequently:

- Stage 1's Pro/Client prediction correlation of −0.897 shows the models track
  their own labels, **not** that they independently recovered market structure.
- Stage 2's "8 of 8 states show opposed FII/Client signs" is close to
  automatic — any variable correlating with FII must correlate oppositely with
  Client.

**Lesson:** check the correlation between labels before citing an opposition
result as validation.

---

## 9. Limitations

- **Effect sizes are small by design of the domain.** Measured partial
  correlations were ≈0.17, implying an AUC ceiling around 0.55–0.60 before the
  calendar's contribution. Nothing here is a trading edge.
- **The earnings calendar is a weak instrument.** Every symbol behaves
  unusually near earnings, so it cannot separate institutional activity from
  ordinary event-driven activity.
- **Stage 3's weights are an assumption.** Symbols are weighted by their share
  of F&O open-interest notional. This is observed data rather than a fitted
  quantity, but the choice still influences the allocation.
- **One market, one regime.** All results are NSE F&O, 2021–2026, a period of
  several-fold retail participation growth. Nothing here has been tested out of
  that sample.
- **`fut_basis_excess`'s expiry-week gap** (§8.1) is unresolved and materially
  limits what Stage 1's null can be said to have tested.

---

## 10. What would change the answers

In descending order of value per unit effort:

1. **Replace `basis_excess_med` with un-annualised `fut_basis_pct`** in Stage 1.
   The latter is well-behaved at every days-to-expiry and is always populated,
   recovering the 448 lost dates. A one-feature change and a ~4-minute rerun.
   **This is the single highest-value experiment outstanding**, because it
   directly tests whether Stage 1's null is an artefact of §8.1.
2. **Replace Stage 2's HMM with a continuous latent-state model.** If the
   observables are a continuum, a linear-Gaussian state-space model (a Kalman
   filter over a scalar "positioning intensity") matches the data's shape where
   a discrete-state model cannot — and would give Stage 3 a smooth per-symbol
   score to consume.
3. **Replace Stage 3's earnings instrument with index inclusion/exclusion
   events.** These force large, externally dated, unambiguously institutional
   trades in specific named symbols — a genuine natural experiment rather than
   a period of general unusualness. A few dozen events over five years,
   assembled by hand.
4. **Aggregate Stage 2's observables to weekly bars.** Positioning may be
   persistent weekly and not daily. Direct test, at the cost of dividing the
   sample by five.

---

## 11. Reproduction

```bash
# Prerequisites: populated option_metrics and ml_dataset (see ../../CLAUDE.md)
uv run python scripts/verify_db_health.py        # gate; must report PASS

uv run mktmtm flow-features                      # Stage 1b   ~6m44s
uv run python scripts/measure_delta_adjustment.py 24
uv run mktmtm market-flow                        # Stage 1    ~3m57s
uv run mktmtm flow-regimes --states 2,3,4,5,6,8 --restarts 6   # Stage 2  ~10m54s
uv run mktmtm flow-attribution                   # Stage 3    ~25s
uv run mktmtm flow-momentum                      # Stage 4    ~20s
```

Artifacts land in `models/evaluation/*.json` (gitignored — they stay local;
this report and the stage documents are the committed record).

| Table | Rows |
|---|---|
| `ml_dataset` / `flow_features` / `option_metrics` | 212,441 |
| `flow_regimes` | 161,717 |
| `flow_attribution` | 518,613 |
| `participant_oi` | 6,290 |

**Superseded work.** The project's first approach — a supervised per-symbol
next-day price-direction classifier — is documented in
[`../models/xgboost-direction-model-card.md`](../models/xgboost-direction-model-card.md).
Its code lives at the git tag `archive/xgboost-direction` and must not be
merged; it predates several measured pipeline bug fixes.
