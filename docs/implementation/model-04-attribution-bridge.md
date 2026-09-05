# Stage 3 — Per-symbol flow attribution (the bridge)

**Read [model-00-overview.md](model-00-overview.md) first.**
**Requires Stages [1](model-01-market-flow.md), [1b](model-02-buildup-quadrants.md)
and [2](model-03-regime-hmm.md).**

**Goal.** Produce a per-symbol-day estimate of institutional flow whose
market-level aggregate reconstructs the observed `participant_oi` series.

**This is the research contribution, and the stage most likely to fail.**
It is specified last and built last for that reason. Read §5 before §2 — the
identifiability problem there is not a caveat, it is the central difficulty,
and a version of this stage that ignores it produces confident numbers that
mean nothing.

**Build target:** `src/mktmtm/ml/attribution.py`,
`src/mktmtm/storage/sql/init_flow_attribution.sql`, CLI command
`flow-attribution`, `tests/unit/test_attribution.py`.

---

## 1. The framing

Formally this is **Learning from Label Proportions (LLP)**: instances are
grouped into *bags*, and only a bag-level aggregate label is observed.

```
Bag        = one trading date          (1,252 bags)
Instances  = the ~170 symbols on it    (212,441 instances)
Bag label  = that day's market-wide institutional flow, from participant_oi
Wanted     = a per-instance value that is never observed
```

Naming the framing matters. It connects the project to an established
literature with known guarantees and known failure modes, and it is the
difference between "we split the market number across symbols using a heuristic"
and a defensible method. Cite the framing explicitly in the write-up.

## 2. The model

Let `x_{i,t}` be symbol `i`'s feature vector on date `t` (flow features, Stage 1b
directions, Stage 2 state posteriors), and `w_{i,t}` its share of that day's
market open-interest notional.

```
s_{i,t} = f(x_{i,t}; θ)                      per-symbol flow score (unobserved)
ŷ_t     = Σ_i w_{i,t} · s_{i,t}              bag prediction
y_t     = observed market flow at t          (participant_oi, the bag label)

θ* = argmin_θ  Σ_t (ŷ_t − y_t)²  +  λ · R(θ)
```

Only the aggregate enters the loss. The per-symbol scores are never compared to
a label, because no per-symbol label exists.

### Weights `w_{i,t}`

Use each symbol's share of total F&O open-interest notional on date `t`:
`fut_oi_notional + chain_oi × spot`, normalized to sum to 1 across the day.
Institutional flow concentrates in liquid names, so an equal-weighted aggregate
would let 200 illiquid symbols outvote the handful that actually move the
market number.

Weights are **observed data, not learned**. Learning them alongside `θ` makes
the model badly unidentified — `w` and `s` trade off against each other with no
information to separate them.

### Choosing `f`

Start with `f` **linear** in the features. Not because non-linearity is
implausible, but because a linear `f` makes the attribution auditable: the
contribution of each feature to each symbol's score can be read off directly,
and a reviewer can check it by hand.

```python
s = X @ beta                # X: (n_symbols_t, n_features), beta: (n_features,)
```

Fit by gradient descent on the bag loss, or in closed form: with linear `f` the
bag prediction is `ŷ_t = (Σ_i w_{i,t} x_{i,t}) · β`, which is **ordinary ridge
regression on the weighted-mean feature vector per day**.

That closed form is worth pausing on. It means the linear version of Stage 3
**is** Stage 1 with weighted-mean aggregation, and its per-symbol scores are a
*disaggregation* of a market-level fit. Implement the closed form first: it is a
few lines, it is exact, and it establishes the floor that any iterative version
must beat. Only move to a non-linear `f` (small gradient-boosted model or a
shallow constrained network) once the linear version is measured — and only if
Stage 1 showed a real margin over the calendar baseline.

## 3. What this buys, stated precisely

The per-symbol scores are **not** a measurement of institutional flow in that
symbol. They are the allocation, consistent with the observed market aggregate,
that a model with this feature set and this regularization considers most
likely.

Write it that way in the report. The temptation is to present `s_{i,t}` as
"estimated FII flow in RELIANCE"; it is not, and claiming so is the kind of
overreach a viva examiner will find immediately. The defensible claim is:

> Given a market-wide flow measurement and per-symbol option-market features,
> this is the per-symbol allocation most consistent with both.

## 4. Validation

There is no per-symbol ground truth, so validation is necessarily **indirect**.
All four checks; the first is mandatory and the rest are corroborating.

### 4.1 Aggregate reconstruction (mandatory, but weak)

Out-of-sample R² of `ŷ_t` against `y_t`, using `walk_forward_splits`.

This is necessary but **far from sufficient** — it is exactly what the model
optimized, and a model can reconstruct the aggregate perfectly while allocating
it across symbols arbitrarily. Never present this as evidence the per-symbol
scores are right. It is a sanity floor.

### 4.2 Event-based corroboration (the real test)

Do high `|s_{i,t}|` scores land where independent evidence says institutions
were active?

- **Earnings.** `data/earnings.py` has NSE board-meeting dates. Positioning
  should build in the days *before* earnings, not after.
- **Index events.** Nifty/Bank Nifty inclusion and exclusion force large,
  externally-dated institutional trades. These are the cleanest natural
  experiments available in this dataset — the flow is known to have happened,
  known to be institutional, and known to be dated. Assemble the event list by
  hand if necessary; it is a few dozen events over five years and worth the
  effort.
- **Expiry weeks.** Should show elevated scores, but this is the weakest check
  because it is exactly what the calendar baseline already predicts.

**Specify the event windows and the test statistic before running any of
this.** Choosing the window after seeing the results is how a null becomes a
finding, and it is the failure mode this stage is most exposed to.

### 4.3 A placebo test

Re-fit with the bag labels **shuffled across dates**. The event-based
corroboration in §4.2 must collapse. If shuffled labels still produce scores
that cluster around earnings and index events, then the clustering comes from
the features' own structure and not from the flow labels, and the whole stage
is measuring nothing. This test is cheap and it is the most informative single
thing in the stage — run it early, not at the end.

### 4.4 Stability

Scores should be persistent within a symbol across adjacent days.
Accumulation takes days; a score that reverses sign daily is noise. Report the
lag-1 autocorrelation of `s_{i,t}` within symbol.

## 5. Identifiability — read before building

**The per-symbol allocation is not uniquely determined by the data.** Many
different allocations produce the same market aggregate. With `n` symbols and
1 equation per day, the system is underdetermined by construction.

What picks one solution is entirely: (a) the functional form of `f`, (b) the
regularization `R(θ)`, and (c) the choice of weights `w`. These are
**assumptions, not discoveries**, and they must be stated as such in the report.

Practical consequences:

- **Regularization is load-bearing, not a tuning detail.** Report the chosen
  `λ` and how sensitive the scores are to it. If halving `λ` reorders which
  symbols score highest, the ranking is not a finding.
- **Test sensitivity to `f`.** If a linear and a tree-based `f` produce
  uncorrelated per-symbol scores while reconstructing the aggregate equally
  well, that is a direct demonstration of non-identifiability and it belongs in
  the report as a limitation — arguably as the most honest result in the whole
  project.
- **A negative result here is publishable.** "Market-level flow labels are
  insufficient to identify per-symbol institutional positioning, and here is
  the sensitivity analysis showing why" is a genuine contribution for a
  final-year project. It is a better outcome than a confident per-symbol number
  that does not survive a change of prior.

## 6. Output table

```sql
CREATE TABLE IF NOT EXISTS flow_attribution (
    symbol        VARCHAR(32) NOT NULL,
    date          DATE        NOT NULL,
    participant   VARCHAR(16) NOT NULL,   -- 'FII' / 'Pro' / 'Client'
    flow_score    DOUBLE PRECISION,       -- s_{i,t}
    weight        DOUBLE PRECISION,       -- w_{i,t}, the OI-notional share
    PRIMARY KEY (symbol, date, participant)
);
CREATE INDEX IF NOT EXISTS idx_flow_attribution_date ON flow_attribution (date);
```

`participant` is in the key because the model is fitted once per participant
category, against that category's bag label.

## 7. Tests and acceptance

| Test | Asserts |
|---|---|
| `test_weights_sum_to_one_per_date` | Within each date, `w` sums to 1.0 |
| `test_bag_prediction_matches_weighted_sum` | `ŷ_t` equals `Σ w·s` on a hand-built frame |
| `test_linear_closed_form_matches_iterative` | The two fits agree to tolerance |
| `test_shuffled_labels_destroy_the_fit` | The placebo, as a unit test on synthetic data |
| `test_scores_are_null_for_unscored_symbols` | Missing features → `NULL`, not 0 |
| `test_no_lookahead_in_weights` | `w_{i,t}` uses only date-`t` open interest |

Acceptance:

- [ ] Linear closed form implemented and measured **before** any non-linear `f`
- [ ] Out-of-sample aggregate R² reported via `walk_forward_splits`
- [ ] Event-based corroboration run with **pre-registered** windows
- [ ] Placebo test run and reported
- [ ] Regularization sensitivity reported
- [ ] Linear-vs-non-linear score correlation reported (the identifiability check)
- [ ] `mypy --strict`, `ruff`, tests pass
- [ ] Results in §8, negative results included

## 8. Results

*To be filled in by the implementer. Lead with the placebo test and the
identifiability sensitivity analysis, then the event corroboration, and report
aggregate R² last — it is the least informative number in this stage despite
being the one the model optimized.*
