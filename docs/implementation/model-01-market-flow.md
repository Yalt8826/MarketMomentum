# Stage 1 — Market-level institutional flow model

**Read [model-00-overview.md](model-00-overview.md) first** for the shared
conventions; this file assumes them.

**Goal.** Predict tomorrow's market-wide institutional positioning change from
today's derived option/futures features. This is the only place in the project
where a real institutional-flow label exists, so it is where the core
hypothesis — *our features carry information about institutional activity* —
gets its first honest test.

**Build target:** `src/mktmtm/ml/market_flow.py`,
`src/mktmtm/ml/market_metrics.py`, CLI command `market-flow`,
`tests/unit/test_market_flow.py`, `tests/unit/test_market_metrics.py`.

---

## 1. The label

### Source

`participant_oi`, 1,258 rows per participant type over 2021-08-02 → 2026-09-03.
Counts are **contracts**. Categories: `Client`, `DII`, `FII`, `Pro`, `TOTAL`.

`data/participant_oi.add_net_positions(df)` already derives what is needed:

```
net_fut_index        net_opt_index_call     net_opt_stock_call
net_fut_stock        net_opt_index_put      net_opt_stock_put     net_total
d_<each of the above>   -- groupby(client_type).diff(), the day-over-day flow
```

The `d_*` columns are the flow: a large positive `d_net_opt_stock_call` for FII
is institutions putting on upside exposure that day, **measured rather than
inferred**. That is the quantity worth predicting.

### Target definition

Feature date is `t`. The label is the value observed at `t+1`:

```python
label(t) = sign(d_net_opt_stock_call at date t+1)   # {0, 1}
```

Model **two participants separately**: `Pro` and `Client`.

- `Pro` is the proprietary-desk book — the closest available proxy for the
  dealer whose gamma position the GEX features describe.
- `Client` is the retail/other side. It is mechanically close to the opposite
  of the institutional side.

Modelling both is not duplicated work: **requiring the two predictions to come
out anti-correlated is a free structural validity check.** If the model
predicts Pro and Client will both increase net call exposure on the same day,
it has learned noise, because the market is a closed book.

Also fit `FII` as a third target for the report. Measured next-day partial
correlation was weaker for FII (+0.116) than Pro (+0.174) or Client (−0.192),
so expect it to score worse; report it anyway.

### Why classification, not regression

The measured relationships were **rank** correlations. `d_net_*` is heavy-tailed
— expiry days and index rebalances produce outliers several standard deviations
out, and a squared-error regression will spend its capacity on those. Predict
the sign, and report Spearman correlation of the score against the signed
magnitude as a secondary metric.

Implement the target builder so a continuous variant is one argument away:

```python
def build_targets(part: pd.DataFrame, *, kind: Literal["sign", "rank"]) -> pd.DataFrame
```

### The look-ahead question, settled

NSE publishes participant-wise OI **after market close** on day `t`. Our
features for day `t` are also end-of-day quantities. Predicting `t+1`'s flow
from `t`'s features is therefore legitimate — the information was genuinely
available before the outcome.

Two traps regardless:

- `participant_oi` runs to 2026-09-03 but `flow_features` stops at 2026-08-28.
  The join must be an **inner join on date**, and the last feature date has no
  `t+1` label and must be dropped from training. Expect **~1,252 usable rows**.
- `add_net_positions` sorts by `(client_type, date)` to diff, then re-sorts by
  `(date, client_type)`. Do not re-sort it again before pivoting.

---

## 2. The features

All are **market aggregates over symbols on date `t`**. Roughly 170 symbols
have complete rows on a typical date.

### Aggregation rules

- **Median**, not mean, for any per-symbol ratio or rate. The cross-section
  contains outliers by construction — the outlier detector exists to find them.
- **Sum** for notionals, which are additive and where the tails are the signal.
- Compute the cross-sectional **dispersion** (p75 − p25) alongside the median
  for the basis family: a day when every symbol's basis moves together is a
  different market from one where they scatter, and the median hides that.
- Aggregate on the raw columns, then difference. Never difference per symbol
  and then aggregate the differences unless a column is explicitly a flow.

### Core feature set — start here (15 features)

Do **not** start with the full list. n=1,252; every added feature costs
degrees of freedom. Begin with these, chosen because each was individually
measured as informative or is a required control:

| Feature | From | Aggregation | Why |
|---|---|---|---|
| `basis_excess_med` | `flow_features.fut_basis_excess` | median | Strongest per-symbol signal measured (t=−20.7) |
| `basis_excess_iqr` | same | p75 − p25 | Cross-sectional agreement vs scatter |
| `basis_pct_med` | `fut_basis_pct` | median | Raw basis level |
| `roll_ratio_med` | `fut_roll_ratio` | median | Position in the roll cycle |
| `d_roll` | `roll_ratio_med` | first difference | Measured: strongest market-level link to Pro/Client |
| `fut_oi_chg_notional_sum` | `fut_oi_chg_notional` | sum | Net futures money in/out |
| `outlier_rate` | `oi_outlier_count > 0` | mean (share of symbols) | Breadth of unusual strike activity |
| `outlier_build_ratio` | build/(build+unwind) notional | ratio of sums | Opening vs closing bias |
| `outlier_call_frac` | `oi_outlier_call_frac` | notional-weighted mean | Call vs put tilt of unusual activity |
| `gex_sum` | `ml_dataset.gex` | sum | Market dealer gamma |
| `pcr_oi_med` | `put_call_oi_ratio` | median | Standard positioning gauge |
| `iv_30_med` | `ce_iv_30` | median | Vol level — the control that killed most raw correlations |
| `chain_oi_chg_sum` | `chain_oi_chg` | sum | Whole-chain OI flow |
| `fut_dte_med` | `flow_features.fut_dte` | median | **The calendar control.** Mandatory. |
| `dow` | `date` | day of week, 0–4 | Weekly seasonality |

### Persistence features — add as a second block

Today's participant flow, known at `t`, for each of Pro/Client/FII:
`d_net_opt_stock_call`, `d_net_fut_stock`, `d_net_opt_stock_put`, plus their
5-day rolling means. That is 18 more columns.

**Measured expectation: these will contribute almost nothing.** Participant
flow autocorrelation is ≈ 0 (FII lag-1 = −0.019). Include them anyway, because
"the model beats persistence" is only a meaningful claim if persistence was
actually available to it.

### Extended set — only after the core set is scored

`rv_yz` median, `chain_breadth` median, `put_call_iv_spread` median,
`oi_outlier_moneyness` weighted mean, `oi_outlier_dte` weighted mean,
`fut_oi_notional` sum, `dist_to_flip` median.

Add these as an **ablation**, reporting core vs core+extended. If the extended
set does not improve out-of-sample AUC, say so and keep the core set. At this
sample size that is the likely outcome and it is a legitimate result.

### Normalization

Rank-transform each feature to `[0, 1]` using an **expanding window ending at
`t-1`**. Do not use a global `StandardScaler` fitted on the full frame — that
leaks the future distribution into every training row, and it is the exact bug
class `validate_no_lookahead` exists to catch elsewhere in this pipeline.

Minimum 250 observations before a feature is scored; `NULL` before that. This
consumes the first trading year, leaving roughly 1,000 usable rows. That is
intended: it is the price of an honest normalization.

---

## 3. Models

Run both. **The comparison is itself a reportable result.**

### Model A — Ridge / ElasticNet (the interpretable control)

```python
from sklearn.linear_model import LogisticRegression   # penalty="l2", solver="liblinear"
```

Sweep `C` over `[0.01, 0.03, 0.1, 0.3, 1.0, 3.0]` inside each walk-forward
fold, selecting on `inner_holdout`. Report the coefficient vector from the
final fold — with rank-normalized inputs the coefficients are directly
comparable, and **the sign of each coefficient is a finding**. If
`basis_excess_med` enters with the sign opposite to the measured per-symbol
correlation, stop and investigate before reporting anything.

For the continuous target variant use `ElasticNet` with `l1_ratio` in
`[0.1, 0.5, 0.9]`.

### Model B — XGBoost, aggressively regularized

```python
PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": "auc",
    "max_depth": 2,              # 3 at the absolute most
    "learning_rate": 0.03,
    "subsample": 0.8,
    "colsample_bytree": 0.6,
    "min_child_weight": 20,      # ~2% of the smallest training fold
    "reg_lambda": 5.0,
    "reg_alpha": 1.0,
    "n_estimators": 2000,        # cut by early stopping, never reached
    "random_state": 42,
}
EARLY_STOPPING_ROUNDS = 100
```

Early-stop on `inner_holdout(train, frac=0.2, embargo_days=1)`, **never** on
the fold's own validation rows. `ml/train_direction.py` on the
`model/xgboost-direction` branch already does this correctly — read it.

### The honest prediction, recorded in advance

**At n≈1,000 with 15 features, Ridge is expected to match or beat XGBoost.**
Writing that here before running it is deliberate: if XGBoost wins by a wide
margin, the first hypothesis is a leak, not a discovery. Trees find spurious
interactions easily at this sample size.

---

## 4. Baselines it must beat

Mirror the structure of `ml/baselines.py`.

| Baseline | Definition | Expectation |
|---|---|---|
| `constant` | Always predict the majority class | AUC 0.50, accuracy = base rate |
| `random_uniform` | Seeded uniform scores | AUC ≈ 0.50 |
| `persistence` | Sign of today's `d_net_*` for the same participant | Near 0.50 — autocorrelation ≈ 0 |
| **`calendar_only`** | **Logistic regression on `fut_dte_med` + `dow` only** | **The real competitor** |
| `leak_tripwire` | Includes `t+1`'s flow as a feature | AUC > 0.95 or the harness is broken |

### The calendar baseline is the whole point

The measured raw next-day correlations dropped by roughly half once the expiry
calendar was controlled:

| Relationship | Raw | Partial (dte controlled) |
|---|---|---|
| `d_roll` → Pro calls | +0.389 | **+0.174** |
| `d_roll` → Client calls | −0.492 | **−0.192** |
| `fut_oi_chg` → Pro | −0.376 | **−0.173** |
| `fut_oi_chg` → Client | +0.459 | **+0.174** |
| `outlier_rate` → flow | +0.130 | **−0.092 (sign flips)** |

The expiry calendar is known years in advance. A model that only rediscovers
"positions roll near expiry" has found nothing. **The headline result of Stage 1
is the margin over `calendar_only`, not the absolute AUC.** Report it that way.

Note the last row: `outlier_rate` **flips sign** under the control. Do not build
a narrative on it. If it appears with a large coefficient, treat that as a
warning, not a discovery.

---

## 5. Validation and metrics

### Splits

```python
from mktmtm.ml.splits import walk_forward_splits
for train_idx, valid_idx in walk_forward_splits(
    frame["date"], n_splits=5, embargo_days=1, min_train_dates=250
):
    ...
```

`min_train_dates=250` matches the normalization warm-up. Report per-fold and
pooled metrics; a signal present in only one fold is not a signal.

### `ml/market_metrics.py` — new module, required

`ml/metrics.py` is cross-sectional (`daily_rank_ic` needs many symbols per
date). Stage 1 has one observation per date, so **none of it applies**. Do not
try to force it; write a new module:

```python
def directional_auc(y_true: pd.Series, score: pd.Series) -> float
def accuracy_and_base_rate(y_true, y_pred) -> tuple[float, float]
def flow_spearman(actual_magnitude: pd.Series, score: pd.Series) -> float
def block_bootstrap_ci(y_true, score, *, metric, block: int = 21,
                       n_boot: int = 2000, seed: int = 42) -> tuple[float, float]
def summarize_market(frame, *, score_col, label_col) -> dict[str, float]
```

`summarize_market` returns finite floats for degenerate input (single-class
labels → AUC 0.5), matching the contract `ml/metrics.summarize` already honours.

### Significance: use the block bootstrap, not a t-test

Daily market series are autocorrelated. A naive t-test on 1,000 daily
observations treats them as independent and **will overstate significance**.
Use a moving-block bootstrap with a block length of ~21 trading days (one
month) to get a confidence interval on the AUC margin over `calendar_only`.

Report the margin's CI. If it straddles zero, the honest conclusion is that
the features add nothing beyond the calendar — write that down.

---

## 6. Module layout

```
src/mktmtm/ml/
  market_flow.py      # frame assembly, targets, training, orchestration
  market_metrics.py   # AUC / accuracy / Spearman / block bootstrap
```

`market_flow.py` public surface:

```python
MARKET_FEATURES: tuple[str, ...]        # the pinned core contract, order fixed
EXTENDED_FEATURES: tuple[str, ...]
TARGET_PARTICIPANTS = ("Pro", "Client", "FII")
DEFAULT_PARQUET = Path("data/processed/market_flow.parquet")

def aggregate_symbol_day(frame: pd.DataFrame) -> pd.DataFrame:      # PURE
def build_targets(part: pd.DataFrame, *, kind: str) -> pd.DataFrame # PURE
def build_market_frame(conn, *, limit_dates: int | None = None) -> pd.DataFrame
def expanding_rank_normalize(frame, features, *, min_obs: int = 250) -> pd.DataFrame  # PURE
def fit_fold(train, valid, features, label, *, model: str) -> tuple[Any, np.ndarray]
def walk_forward_market(frame, features, label, *, model: str) -> pd.DataFrame
def score_market_baselines(frame, label) -> pd.DataFrame
def run_market_flow(conn, *, participants, model, out_dir) -> dict[str, Any]
```

Keep `aggregate_symbol_day`, `build_targets` and `expanding_rank_normalize`
**pure** (no I/O) so they are unit-testable without a database — the same
separation `features/flow.py` uses for `detect_outlier_strikes` and
`futures_basis`.

### CLI

Follow the `flow-features` command in `cli.py`:

```bash
uv run mktmtm market-flow \
    --participant Pro --participant Client --participant FII \
    --model ridge --model xgboost \
    --extended/--no-extended \
    --limit-dates N
```

Import heavy modules **inside** the command body, as the existing commands do.

---

## 7. Tests

`tests/unit/test_market_flow.py` — all pure-function tests, no database:

| Test | Asserts |
|---|---|
| `test_aggregate_uses_median_for_ratios` | One extreme symbol does not move `basis_excess_med` |
| `test_notional_columns_are_summed` | Sums are additive across symbols |
| `test_label_is_next_day_not_same_day` | `y(t)` equals `d_net_*(t+1)`, built from a hand-made 5-day frame |
| `test_last_date_has_null_label` | No "tomorrow" → `NULL`, never dropped silently |
| `test_expanding_normalize_has_no_lookahead` | Appending future rows never changes an earlier row's value — **the critical test** |
| `test_normalize_is_null_before_min_obs` | First 250 rows are `NULL`, not 0.5 |
| `test_feature_contract_is_pinned` | `MARKET_FEATURES` matches the built frame's columns exactly, in order |
| `test_calendar_baseline_uses_only_calendar` | Baseline touches no flow column |

`test_expanding_normalize_has_no_lookahead` is the one that matters. Build a
frame, normalize it, append rows, re-normalize, and assert the original rows'
values are **bit-identical**. That property is what "no look-ahead" means
operationally, and a global scaler fails it immediately.

`tests/unit/test_market_metrics.py`: AUC against a known case, degenerate
single-class input returning 0.5, block bootstrap reproducible under a fixed
seed, and a bootstrap CI that widens as the block length grows.

---

## 8. Acceptance criteria

- [ ] `market_flow.parquet` has ~1,252 rows, one per trading date, no duplicates
- [ ] `mypy --strict` and `ruff` pass
- [ ] All tests pass, including the no-look-ahead test
- [ ] The leakage tripwire scores AUC > 0.95
- [ ] `persistence` scores near 0.50 (confirms the measured autocorrelation)
- [ ] Pro and Client predictions are negatively correlated (structural check)
- [ ] Results in `models/evaluation/market_flow_<participant>.json` with
      provenance and per-fold metrics
- [ ] **Results section appended to this file**, reporting the margin over
      `calendar_only` with its bootstrap CI — including if that margin is zero

## 9. Results

*To be filled in by the implementer. Report the calendar-margin CI first,
absolute AUC second, and the Ridge-vs-XGBoost comparison third. If the margin
is not distinguishable from zero, say so plainly and state what that implies
for Stages 2 and 3 — a null here does not invalidate them, because they do not
depend on next-day predictability, but it does change what can be claimed.*
