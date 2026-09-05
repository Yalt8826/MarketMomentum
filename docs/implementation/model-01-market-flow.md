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

**Run 2026-09-05.** `uv run mktmtm market-flow`, 3m57s.
Artifacts: `models/evaluation/market_flow_{pro,client,fii}.json`,
`market_flow_structural.json`, `data/processed/market_flow.parquet`.

### 9.1 Headline: the flow features do not beat the calendar

Every feature set's AUC margin over `calendar_only`, with a paired
moving-block bootstrap CI (21-day blocks, 2,000 draws). **Negative is worse
than the calendar.**

| Feature set / model | Pro | Client | FII |
|---|---|---|---|
| `core:ridge` | **−0.036** [−0.079, −0.001] | **−0.035** [−0.079, −0.002] | −0.018 [−0.055, +0.011] |
| `core:xgboost` | −0.034 [−0.082, +0.012] | −0.033 [−0.073, +0.000] | +0.001 [−0.032, +0.035] |
| `core+direction:ridge` | −0.018 [−0.059, +0.020] | −0.008 [−0.033, +0.018] | **−0.033** [−0.070, −0.002] |
| `core+direction:xgboost` | −0.014 [−0.064, +0.042] | **−0.043** [−0.089, −0.003] | −0.010 [−0.043, +0.023] |
| `core+direction+extended:ridge` | +0.002 [−0.044, +0.041] | −0.002 [−0.042, +0.037] | −0.021 [−0.067, +0.018] |
| `core+direction+extended:xgboost` | −0.006 [−0.071, +0.053] | −0.035 [−0.086, +0.007] | −0.017 [−0.053, +0.016] |

**Not one margin is significantly positive. Four are significantly negative.**
The best result across all 18 combinations is +0.002, comfortably inside its
own confidence interval.

The honest conclusion: **on this task, at this horizon, the derived
option-and-futures features add nothing to what the expiry calendar already
tells you about tomorrow's market-wide institutional flow — and adding them to
the calendar model actively hurts, because they behave as noise that dilutes a
genuine signal.**

### 9.2 The calendar is a strong predictor on its own

| Participant | `calendar_only` AUC | Base rate | Best flow-model AUC |
|---|---|---|---|
| Client | **0.720** | 0.642 | 0.718 (`core+direction+extended:ridge`) |
| FII | **0.654** | 0.383 | 0.654 (`core:xgboost`) |
| Pro | **0.612** | 0.434 | 0.614 (`core+direction+extended:ridge`) |

Five calendar features — days to near-month expiry plus four day-of-week
dummies — predict the sign of tomorrow's market-wide flow at AUC 0.61–0.72.
That is a much larger effect than anything measured in the per-symbol work, and
it is entirely mechanical: positions roll on a schedule published years in
advance. It is the reason this baseline was specified as the headline
comparison before any model was fitted.

### 9.3 The predictions in advance held up

Both calls recorded in §3 before running anything:

- **Ridge matched or beat XGBoost**, as predicted, in 2 of 3 participants
  (Pro +0.008, Client +0.033 in AUC; XGBoost wins FII by +0.018). At n=556
  scored rows the extra flexibility buys nothing.
- **No AUC anywhere near 0.85.** The measured range, 0.58–0.72, sits close to
  the 0.55–0.60 expectation once the calendar's contribution is included.

### 9.4 What did work

**The leakage tripwire scores AUC 1.0000 for all three participants.** The
harness is wired correctly — a stronger statement than any passing unit test.

**The structural check passes**, though it is weaker evidence than it first
appears. Pro and Client predictions correlate at **−0.897**: the two models
were fitted independently and never told about each other.

Measured afterwards, while writing up [Stage 2](model-03-regime-hmm.md#83-the-fiiclient-check-is-near-tautological--do-not-count-it),
the *labels* themselves are already −0.802 correlated (Pro vs Client) — the book
is closed, so Client is mechanically the other side. Two models that track
their own labels at all will therefore produce negatively correlated
predictions. The check confirms the models learned something related to their
labels; it does **not** independently demonstrate that they recovered market
structure, and an earlier version of this section claimed it did.

**Stage 1b's direction features carry information.** This answers the question
left open in [model-02 §7.4](model-02-buildup-quadrants.md). Adding them to the
core set improves the margin consistently:

| Participant | `core` | `core+direction` | `core+direction+extended` |
|---|---|---|---|
| Pro | −0.036 | −0.018 | **+0.002** |
| Client | −0.035 | −0.008 | −0.002 |

The buildup quadrants roughly **halve** the damage the directionless features
do, and for Pro they close the gap entirely. So the delta-adjusted direction
work was not wasted — it is the only feature block that measurably improves
this task. It just does not get past the calendar.

**Persistence is weak but not zero.** AUC 0.526 (Pro), 0.555 (Client), 0.604
(FII). This partly corrects an earlier expectation: linear autocorrelation of
`d_net_*` is ≈0 (FII lag-1 = −0.019), but *sign* persistence is not — a
rank-based metric picks up dependence that a Pearson correlation misses. FII
flow in particular has real day-to-day sign persistence.

### 9.5 A structural problem this run exposed

`fut_basis_excess` is `NULL` whenever `fut_dte < 5` (`ANNUALISE_MIN_DTE`,
introduced because annualising a 1-day basis multiplies it by 365). At market
level *every* symbol is inside that window simultaneously, so the median
vanishes for the whole of every expiry week.

**448 of 1,254 dates (36%) are lost this way**, and they are not lost at
random — they are exactly the days when the calendar baseline is most
informative. The strongest per-symbol flow feature is systematically absent
precisely when institutional positioning is most active.

The first version of this run did not equalise for it, and scored
`calendar_only` on 1,004 dates against the flow models' 556 — inflating the
baseline's apparent absolute AUC to 0.749 for Client on an easier subset. Every
number above is now computed on the **same 806-row common set** (556 validation
rows after the walk-forward warm-up).

Worth trying before concluding the features are useless: an un-annualised
`fut_basis_pct` aggregate, which is well-behaved at every dte and always
populated, as a replacement for `basis_excess_med` rather than a companion to
it.

### 9.6 What this means for Stages 2 and 3

It does **not** invalidate them, and the reason is worth being precise about.

Stage 1 tested one specific claim: *can these features predict tomorrow's
market-wide flow?* No. But Stages 2 and 3 do not depend on next-day
predictability:

- **Stage 2** asks whether symbol-days fall into persistent latent states. A
  state can be real and stable without predicting a market aggregate one day
  ahead — and the aggregate is a sum over 217 symbols, where per-symbol
  structure is exactly what cancels.
- **Stage 3** asks how to *attribute* an observed aggregate across symbols. It
  conditions on the aggregate rather than predicting it.

What the null does change is the **claim available at the end**. There is no
basis for saying these features forecast institutional flow. The defensible
claims are about description and attribution, not prediction — and §9.4's
structural check plus the direction features' measurable contribution are what
those rest on.
