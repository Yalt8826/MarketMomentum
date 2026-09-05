# Stage 2 — Per-symbol latent regime model (HMM)

**Read [model-00-overview.md](model-00-overview.md) first.**
**Requires [Stage 1b](model-02-buildup-quadrants.md) to be built** — without
direction features the states are not worth fitting (§1).

**Goal.** Assign every symbol-day a latent **positioning regime** without any
per-symbol label, then validate those regimes against the market-level label
that does exist.

**Build target:** `src/mktmtm/ml/regimes.py`,
`src/mktmtm/storage/sql/init_flow_regimes.sql`, CLI command `flow-regimes`,
`tests/unit/test_regimes.py`.

---

## 1. Why an HMM, and why not before Stage 1b

The per-symbol level has 212,441 rows and **no label**. Supervised learning is
impossible there. An HMM is the right unsupervised tool for three reasons
specific to this problem:

1. **Positioning is persistent.** An institution accumulating a position does
   it over days or weeks, not in one print. A model with a transition matrix
   encodes that; a clustering method (k-means, GMM) treats each day as
   independent and throws the persistence away.
2. **The states are latent by construction.** We never observe "accumulation";
   we observe basis, roll, OI concentration and direction, and infer the state
   behind them. That is exactly the HMM's generative story.
3. **It is falsifiable.** If the fitted transition matrix has a near-uniform
   diagonal, states are not persistent, "regimes" do not exist in these
   observables, and the model has produced a negative result worth reporting.
   Most unsupervised methods cannot fail this visibly. Keep that property.

**Do not substitute `sklearn.mixture.GaussianMixture`.** It has no transition
matrix, so it cannot represent persistence — which is the whole hypothesis.

**Dependency:** add `"hmmlearn>=0.3.3"` to `pyproject.toml` `dependencies`,
then `uv sync`.

### The ordering constraint

Without Stage 1b, every observable is a magnitude: "how much unusual activity",
"how large the basis". The best such a model can distinguish is quiet from
busy, which is a volatility proxy and not a positioning regime. The direction
features are what let a state mean *accumulation* rather than *activity*.

## 2. Observables

Six per symbol-day. Resist adding more: HMM parameter count grows as
`n_states × n_features²` under a full covariance matrix, and the fit becomes
unstable well before the row count suggests it should.

| Observable | Source | Carries |
|---|---|---|
| `fut_basis_excess` | `flow_features` | Financing pressure vs carry — strongest measured signal |
| `fut_roll_ratio` | `flow_features` | Position in the roll cycle |
| `oi_outlier_share` | `flow_features` | Concentration of unusual OI |
| `oi_outlier_direction` | **Stage 1b** | Direction of that unusual OI |
| `q_fut_direction` | **Stage 1b** | Direction of futures positioning |
| `gex` | `ml_dataset` | Dealer gamma exposure |

### Normalization — within symbol, causally

Raw values are not comparable across symbols: RELIANCE's GEX and a mid-cap's
GEX differ by orders of magnitude, and a pooled fit on raw values would
discover **market capitalisation**, not regimes. That exact confound already
invalidated the pooled GEX H1 result — see the memory note and
[gex-dealer-positioning.md](gex-dealer-positioning.md). Do not repeat it.

Use the established convention:

```python
g = frame.sort_values(["symbol", "date"]).groupby("symbol", observed=True)[col]
mu = g.expanding().mean().shift(1)
sd = g.expanding().std().shift(1)
z  = (frame[col] - mu) / sd          # NULL until 60 observations exist
```

`shift(1)` is what makes it causal: the statistics for date `t` come from
`t-1` and earlier only. The 60-observation minimum matches the rest of the
project. Winsorize the result at ±5 before fitting — a single 40-sigma print
will otherwise capture an entire Gaussian component.

### Missing values

`hmmlearn` **cannot accept NaN**. Require complete cases across all six
observables, and **report the retention rate** in §7. Expect a substantial
loss: the 60-day warm-up alone removes the first ~3 months per symbol, and
`oi_outlier_direction` is non-null on only ~16% of symbol-days by design.

That last point is a genuine design decision, not a detail:

- **Option A — complete cases only.** Fits on roughly 16% of rows. Clean, but
  the model only ever sees days with flagged outlier activity, so its states
  describe *unusual* days rather than the full cycle.
- **Option B (recommended) — impute `oi_outlier_direction` to 0 with a
  companion `has_outlier` indicator observable.** Retains ~85% of rows. The
  indicator prevents the model from reading "no unusual activity" as "neutral
  direction", which is the confusion that a bare zero would create.

Take Option B, and record the choice and the retention rate. Note this is the
one sanctioned exception to the project's "`NULL` never `0`" rule; it is
sanctioned only because the paired indicator column preserves the distinction
that the rule exists to protect. Do not generalise it.

## 3. Fitting

```python
from hmmlearn.hmm import GaussianHMM

model = GaussianHMM(
    n_components=n_states,
    covariance_type="diag",   # "full" overfits at 7 observables; start diagonal
    n_iter=200,
    tol=1e-3,
    random_state=seed,
)
model.fit(X_train, lengths=lengths_train)
```

### One pooled model, not 217

Fit a single model on symbol-normalized observables from all symbols. Per-symbol
models would have ~1,000 rows each — too few for a stable multivariate fit —
and 217 separate state spaces would not be comparable, making any aggregation
meaningless. Pooling gives one shared state vocabulary, which is what Stage 3
needs.

### `lengths` — the trap that silently corrupts the fit

`hmmlearn` takes one concatenated array plus a `lengths` array giving each
sequence's length. Rows **must** be grouped by symbol and ordered by date
within each symbol, and `lengths` must match exactly:

```python
frame = frame.sort_values(["symbol", "date"]).reset_index(drop=True)
lengths = frame.groupby("symbol", observed=True).size().to_numpy()
assert lengths.sum() == len(frame)
```

Get this wrong and `hmmlearn` raises no error — it silently learns transitions
*across symbol boundaries*, treating the end of ADANIPORTS as flowing into the
start of ASIANPAINT. The result looks plausible and is meaningless. Assert the
sum, and unit-test the `lengths` construction.

Symbols with gaps (suspended trading, late listing) still form one sequence.
The HMM has no notion of elapsed time, so a gap is treated as a single step —
acceptable here, but note it as a limitation.

### The no-look-ahead requirement

**Fit on training dates only, then decode forward.** Fitting on all history and
then "discovering" states that explain it is look-ahead, and it is the easiest
mistake in this stage because nothing in the code will complain.

```python
train = frame[frame["date"] <= TRAIN_END]
model.fit(train_X, lengths=train_lengths)          # training window only
states = model.predict(all_X, lengths=all_lengths) # decode everything
```

The Viterbi decode of a *fitted* model is a legitimate forward operation. The
fit is what must be sealed. Use `TRAIN_END` from `ml/splits.py` (2024-12-31).

### Choosing `n_states`

Fit `n_states ∈ {2, 3, 4, 5, 6}` on the training window and select by **BIC**,
computed on the training window only. Report the full BIC curve, not just the
winner — a flat curve means the state count is not identified, and that belongs
in the write-up.

Expect 3–4. If BIC picks 6, suspect the observables are carrying a continuous
factor (size, liquidity) that the model is slicing arbitrarily.

### Local optima

EM converges to a local optimum. Run **10 restarts** with different
`random_state` values, keep the highest training log-likelihood, and record the
spread across restarts. A wide spread means the likelihood surface is rough and
the states are not robust — report it rather than hiding it behind the best run.

## 4. Interpreting states

`hmmlearn` returns arbitrarily-numbered states. Label them from
`model.means_` — the mean observable vector of each state:

```
State with high oi_outlier_direction + high q_fut_direction  → "accumulation"
State with low  oi_outlier_direction + low  q_fut_direction  → "distribution"
State with low  oi_outlier_share, basis near carry           → "quiet"
State with high basis_excess and high roll_ratio             → "roll/expiry"
```

**Label states from their observable means, never from their outcomes.**
Naming a state "accumulation" because returns after it were positive is fitting
the story to the data, and it converts an unsupervised result into a
circular one. Write the labels down before looking at any outcome.

State numbering is not stable across restarts or refits. Persist the fitted
model to `models/candidates/flow_hmm.pkl` (with `joblib`) and canonicalise the
ordering — sort states by mean `q_fut_direction` — so the integers in
`flow_regimes` mean the same thing across runs.

## 5. Output table

`src/mktmtm/storage/sql/init_flow_regimes.sql`, added to `init_db.sql` in
dependency order:

```sql
CREATE TABLE IF NOT EXISTS flow_regimes (
    symbol      VARCHAR(32) NOT NULL,
    date        DATE        NOT NULL,
    state       SMALLINT,            -- canonicalised Viterbi state, NULL if unscored
    state_prob  DOUBLE PRECISION,    -- posterior of the decoded state
    p_state_0   DOUBLE PRECISION,
    p_state_1   DOUBLE PRECISION,
    p_state_2   DOUBLE PRECISION,
    p_state_3   DOUBLE PRECISION,    -- extend to the selected n_states
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_flow_regimes_date ON flow_regimes (date);
```

Symbol-days dropped for missing observables get **no row** (preferred) or a row
with `NULL` state. Do not invent a state for them.

Export `data/processed/flow_regimes.parquet` from the same frame that is
written to PostgreSQL, in the same call — this is why `flow_features` and its
parquet cannot drift, and the reason `ml_dataset.parquet` once went stale.

## 6. Validation

An unsupervised model with no validation is a rearrangement of the data. All
four checks are required.

### 6.1 Persistence (the falsification test)

Inspect `model.transmat_`. Diagonal entries should exceed ~0.5 — a state should
usually persist to the next day. **If the diagonal is near uniform, regimes do
not exist in these observables. Report that and stop.** Do not tune the feature
set until persistence appears; that is fitting the method to the desired
conclusion.

### 6.2 Aggregate against the market label

Compute daily state occupancy — the share of symbols in each state on date `t` —
and correlate the differenced series against `participant_oi` `d_net_*` flows,
with the expiry-calendar control applied, as in Stage 1.

The structural check, as everywhere in this project: **`Client` should move
opposite to `FII` and `Pro`.** If an "accumulation" occupancy share rises with
institutional buying and falls with client buying, the states carry real
information. If they track `Client` in the same direction as `FII`, something is
inverted.

### 6.3 Against the known event calendar

`data/earnings.py` supplies NSE board-meeting dates. Positioning states should
be non-uniformly distributed in the days before earnings. This is a weak test —
an association is expected — but a *flat* result would be strong evidence the
states are noise.

### 6.4 Stability

Refit on a different training window (e.g. ending 2023-12-31) and compare state
means. Genuine regimes should reappear with similar observable signatures.
Report the mean absolute difference in state means across the two fits.

## 7. Tests and acceptance

`tests/unit/test_regimes.py` — all pure, no `hmmlearn` fit in the unit suite
(fitting is slow and stochastic; test the data plumbing around it):

| Test | Asserts |
|---|---|
| `test_lengths_match_row_count` | `lengths.sum() == len(frame)` and no cross-symbol runs |
| `test_lengths_follow_symbol_order` | Reordering the frame reorders `lengths` consistently |
| `test_causal_z_has_no_lookahead` | Appending future rows never changes an earlier row's z |
| `test_z_is_null_before_min_obs` | First 60 rows per symbol are `NULL` |
| `test_winsorize_bounds_observables` | Post-winsorize range is within ±5 |
| `test_state_canonicalisation_is_deterministic` | Same means → same integer labels |
| `test_missing_observable_yields_no_row` | Incomplete symbol-days are excluded, not defaulted |

Acceptance:

- [ ] `hmmlearn` added to `pyproject.toml`, `uv sync` clean
- [ ] BIC curve reported for `n_states` 2–6
- [ ] Transition-matrix diagonal reported; persistence test explicitly passed or failed
- [ ] Retention rate after complete-case filtering reported
- [ ] Restart log-likelihood spread reported
- [ ] `flow_regimes` populated, parquet exported from the same frame
- [ ] State labels assigned from observable means, recorded **before** outcome analysis
- [ ] `mypy --strict`, `ruff`, tests pass
- [ ] Results appended to §8, including a negative result if persistence fails

## 8. Results

**Run 2026-09-05.** `uv run mktmtm flow-regimes --states 2,3,4,5,6,8 --restarts 6`,
10m54s. `models/evaluation/flow_regimes.json`, `flow_regimes` populated,
`data/processed/flow_regimes.parquet`.

### 8.1 The persistence test fails at every state count

This is §6.1, the falsification test, and it is the result.

| k | BIC | **min transition diagonal** | restart log-lik spread |
|---|---|---|---|
| 2 | −297,668 | **0.367** | 0 |
| 3 | −544,977 | **0.255** | 0 |
| 4 | −605,915 | **0.255** | 29,501 |
| 5 | −643,110 | **0.253** | 18,934 |
| 6 | −645,699 | **0.169** | 6,859 |
| 8 | −892,883 | **0.072** | 112,865 |

**No state count produces uniformly persistent states.** The requirement is
that every state's self-transition exceeds 0.5 — that a regime survives to the
next day. Even the two-state model has a state with a 0.367 diagonal, implying
a mean dwell time of 1/(1−0.367) ≈ **1.6 days**. That is not a positioning
regime; it is a day-to-day flicker.

It also gets monotonically *worse* with k. A model finding real regimes and
being given too many states typically splits them into sub-states that remain
persistent. This one instead produces ever more transient states, which is what
happens when a model is partitioning a **continuum** rather than discovering
discrete modes.

Per §6.1 the correct response is to report this and stop, not to search for a
feature set that makes persistence appear. The table above is the finding.

### 8.2 BIC never turns around, so it cannot select k

BIC decreases monotonically across the entire grid, including out to k=8
(−892,883 against −645,699 at k=6). It is still improving at the largest state
count tried and would keep improving.

A model-selection criterion that does not turn around inside the search grid
has not selected anything. This is the second, independent signature of the
same problem in §8.1: slicing a continuous factor into finer bands always
raises the likelihood faster than the parameter penalty grows.

`_select_states` therefore refuses to trust BIC here. Its fallback — the lowest
BIC among state counts whose states are all persistent — finds **no eligible
k**, and the result records `"criterion": "BIC, unidentified — no k had all
states persistent"`. The run still decodes and persists a k=8 solution so the
numbers are inspectable, but **those state labels should not be consumed
downstream.** Stage 3 does not read them.

The restart spread reinforces it: at k=8 the best and worst of six EM runs
differ by **112,865** log-likelihood units. The likelihood surface is rough and
the fitted states are not reproducible across seeds.

### 8.3 The FII/Client check is near-tautological — do not count it

The run reports "states with opposed FII/Client signs: 8/8", which looks like a
clean structural pass. **It is not evidence, and this is worth recording so it
is not cited later.**

Correlation of `d_net_opt_stock_call` between participants:

| | FII | Pro | Client | DII |
|---|---|---|---|---|
| **FII** | 1.000 | 0.373 | **−0.757** | 0.696 |
| **Pro** | 0.373 | 1.000 | **−0.802** | 0.376 |
| **Client** | −0.757 | −0.802 | 1.000 | −0.835 |

Client is already −0.76 to −0.84 correlated with every institutional category,
because the book is closed and they are the other side of it. Any variable
correlating with FII must therefore correlate oppositely with Client, whether
or not it carries information. Getting 8/8 is close to automatic.

The check as specified in §6.2 is only informative for a variable whose
relationship to the institutional side is itself in question. It cannot
validate an unsupervised state assignment.

### 8.4 What did work

Retention was better than feared: **161,717 of 212,441 symbol-days (76.1%)
across 211 symbols**, well above the ~16% that complete-case handling of
`oi_outlier_direction` would have given. The Option B imputation with a paired
`has_outlier` indicator (§2) was the right call.

The mechanical parts are sound — `lengths` construction, causal within-symbol
z-scoring, canonical state ordering — and are covered by 14 unit tests
including a no-look-ahead assertion.

### 8.5 Interpretation

The honest conclusion: **these six observables do not contain persistent
discrete positioning regimes at the symbol-day level.** Two independent
diagnostics agree — unbounded BIC and universal persistence failure — and both
point at the same explanation: the observables vary continuously, and an HMM
asked to find discrete modes in a continuum will always return something while
meaning nothing.

That is a real result for the write-up, and it is more informative than a
tuned three-state model with a story attached would have been. It also
retrospectively justifies §4's insistence on labelling states from observable
means before looking at outcomes: had that discipline been dropped, a k=3 fit
with plausible-sounding "accumulation / distribution / quiet" labels would have
been very easy to believe.

### 8.6 What would change the answer

Two things worth trying before concluding regimes cannot exist at all, in
order of promise:

1. **A continuous latent-state model instead of a discrete one.** If the
   observables are a continuum, a linear-Gaussian state-space model (a Kalman
   filter over a scalar "positioning intensity") matches the data's shape where
   an HMM does not. It would also produce a smoothly varying score that Stage 3
   could consume directly.
2. **Coarser time.** Positioning may be persistent weekly and not daily.
   Aggregating observables to weekly bars before fitting would test that
   directly, at the cost of dividing the sample by five.
