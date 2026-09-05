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

*To be filled in by the implementer. Lead with the persistence test — it
determines whether anything else in this stage is worth reporting.*
