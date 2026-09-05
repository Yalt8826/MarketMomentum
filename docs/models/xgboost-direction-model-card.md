# Model Card — XGBoost Next-Day Direction Classifier

> **Archived 2026-09-05. This approach was superseded and its code is no longer
> in the working tree.**
>
> This card documents the project's *first* modelling approach: supervised
> per-symbol next-day price direction. It was completed through a sealed test
> and is retained as the record of that phase — including why the project
> pivoted away from it.
>
> **Why it was superseded.** The project's goal is detecting institutional
> positioning, not predicting price. This model predicts price, and the
> Phase 7 audit below found the edge to be thin and substantially tied to
> *missingness* (rows where the IV solver failed on illiquid or violently
> moving names) rather than to per-name signal. The successor work targets
> institutional flow directly; see
> [`docs/implementation/model-00-overview.md`](../implementation/model-00-overview.md).
>
> **Where the code lives.** The branch `model/xgboost-direction` was deleted
> after being tagged. Everything referenced below —
> `src/mktmtm/ml/train_direction.py`, `audit_direction.py`, `features.py`,
> `promote.py`, `docs/implementation/xgboost-direction-model.md`, and the
> `train-direction` / `promote-direction` CLI commands — exists only at that
> tag:
>
> ```bash
> git show archive/xgboost-direction:src/mktmtm/ml/train_direction.py
> git checkout archive/xgboost-direction -- <path>   # to restore a file
> ```
>
> That branch **must never be merged**: it predates the OPEN_INT-in-shares fix,
> the gex parquet re-export and all of `flow_features`, so merging would revert
> measured bug fixes.
>
> **The numbers below are still valid** — they were produced by a sealed
> protocol against `ml_dataset` as rebuilt on 2026-08-31, and that table is
> unchanged. The `models/` artifacts it lists are gitignored and remain on the
> machine that produced them.


| | |
|---|---|
| **Model** | `xgb_direction` — binary classifier used as a cross-sectional ranker (long top decile / short bottom decile per day) |
| **Branch** | `model/xgboost-direction` (cut from `main` at `e13e4b9`) |
| **Trained** | 2026-08-31 · Phase 7 (artifact audit + sealed test) |
| **Source** | `src/mktmtm/ml/train_direction.py` (Phase 6 fit), `src/mktmtm/ml/audit_direction.py` (Phase 7 audit) |
| **Plan** | `docs/implementation/xgboost-direction-model.md` |

## What it does

Predicts `next_day_direction` (P(close_t+1 > close_t)) from 42 option-derived
continuous features plus 9 explicit missingness indicators (51 features total).
It is **not** used as an absolute up/down call: the score is ranked within each
date and only the decile tails are traded. Base rates drift across the splits
(TRAIN 0.5073 → VALID 0.4999 → TEST 0.4903), so any threshold-based
interpretation of the score is systematically wrong out of sample — ranking is
the only honest use.

## Training configuration (sealed in Phase 6, Task 6.1 — not tuned since)

```python
PARAMS = dict(
    objective="binary:logistic", eval_metric="auc", tree_method="hist",
    max_depth=4, min_child_weight=100, gamma=0.0,
    reg_lambda=10.0, reg_alpha=0.0, subsample=0.8, colsample_bytree=0.7,
    learning_rate=0.02, n_estimators=5000, random_state=42, n_jobs=-1,
)
EARLY_STOPPING_ROUNDS = 200
```

- Early stopping on **VALID AUC** chose `best_iteration = 2` (0-based). XGBoost
  predictions use `best_iteration + 1 = 3` trees; the fitted booster's buffer
  holds 203 trees (training ran 200 rounds past the optimum) but only the
  3-tree prefix ever produces predictions. All VALID/TEST scores in this card
  come from the 3-tree model.
- No imputation: missing values stay NaN and XGBoost learns a default split
  direction; the `_is_null` indicators (Phase 2, Task 2.2) make that behaviour
  visible as explicit features.
- No `scale_pos_weight`: classes are ~50/50.

## Data and splits

Source: `data/processed/ml_dataset.parquet` — 212,441 rows · 217 symbols ·
1,253 dates · 2021-08-02 → 2026-08-28, rebuilt 2026-08-31
(`docs/data/etl-integrity-change-report.md` §10). After the F4 gate (both IV
legs, `docs/data/iv-integrity-audit.md`), per-date rank normalization of all
continuous features, and the 1-day embargo purge on TRAIN/VALID tails:

| Split | Period | Rows (gated, post-embargo) | Dates |
|---|---|---|---|
| TRAIN | 2021-08-02 → 2024-12-30 | 125,208 | 813 |
| VALID | 2025-01-01 → 2025-12-29 | 46,629 | 235 |
| TEST | 2026-01-01 → 2026-08-27 | 27,705 | 153 |

TEST (2026) was sealed until Phase 7: it never informed early stopping, feature
selection, or any hyperparameter. It is touched exactly once, in Task 7.4.

## Baselines (Phase 5, VALID)

| Baseline | AUC | Mean IC | ICIR | IC t-stat | Mean spread |
|---|---|---|---|---|---|
| Constant "up" | 0.5000 | 0.0000 | 0.00 | 0.00 | 0.00000 |
| Random uniform | 0.5019 | 0.0035 | 0.05 | 0.77 | +0.00026 |
| Reversal (−ret_1d) | 0.5056 | 0.0162 | 0.11 | 1.71 | −0.00002 |
| Leak tripwire (raw ret_1d) | 0.4944 | −0.0162 | −0.11 | −1.71 | +0.00013 |

The leak tripwire is clean: |IC| = 0.016, nowhere near the ±0.5 signature of
the historical row-ordering bug (`docs/data/ml-dataset-report.md` §2).

## VALID performance (Phase 6)

Fit on TRAIN, early-stopped on VALID AUC; scored on VALID with the Phase 4
`summarize` harness (`models/evaluation/xgb_direction_valid.json`):

| Metric | VALID | Plan's realistic range |
|---|---|---|
| AUC | 0.5165 | 0.51 – 0.54 |
| Accuracy | 0.4994 | 51 – 53% (threshold meaningless for a ranker) |
| Mean daily rank IC | 0.0284 | 0.02 – 0.05 |
| ICIR | 0.2709 | > 0.3 usable |
| IC t-stat | 4.15 | > 2 minimal bar |
| Mean decile spread | +0.000861 (≈ +0.09%/day) | — |
| Positive-IC day rate | 61.3% | — |

### Walk-forward robustness (Phase 6, 5 folds over TRAIN+VALID era)

Corrected 2026-08-31. The original run early-stopped each fold on that fold's
*own* validation rows and then scored them, so the tree count was chosen on the
data being measured and every fold IC was optimistically biased. Folds now
early-stop on an `inner_holdout` carved from the tail of the fold's training
window (`ml/splits.py`), leaving the validation rows untouched by any fitting
decision.

| Fold | Valid window | Mean IC | Trees | Superseded (biased) IC |
|---|---|---|---|---|
| 0 | 2022-08 → 2023-04 | +0.0180 | 106 | +0.0102 |
| 1 | 2023-04 → 2023-12 | +0.0189 | 137 | +0.0285 |
| 2 | 2024-01 → 2024-08 | +0.0027 | 584 | −0.0164 |
| 3 | 2024-08 → 2025-04 | +0.0030 | 1072 | +0.0292 |
| 4 | 2025-05 → 2025-12 | +0.0034 | 23 | +0.0402 |

Mean fold IC **+0.0092** (min +0.0027, max +0.0189) — **half** the +0.0183 the
biased protocol reported. No fold is negative any more, so the fold-2 regime
warning is withdrawn: it was an artifact of the biased tree selection, not a
regime effect. Sign stability across all five folds is the strongest evidence
here that the signal is real, even though it is thin.

The fold ICIR (1.09) is a mean/std across only five fold means, not a daily
ICIR; it is not comparable to the daily ICIR figures elsewhere in this card and
should not be read against the plan's ">1.0 is suspicious" daily band.

## Phase 7 audit — is the edge real?

### 7.1 Missingness ablation (mandatory) — `models/evaluation/xgb_direction_ablation.json`

Three variants, all fit on TRAIN with early stopping on VALID, scored on VALID:

| Variant | AUC | Mean IC | ICIR | IC t-stat | Mean spread | Trees |
|---|---|---|---|---|---|---|
| **Full model** (baseline) | 0.5165 | 0.0284 | 0.271 | 4.15 | +0.000861 | 3 |
| **`_is_null` removed** (42 features, NULL rows kept) | 0.5135 | **0.0310** | **0.318** | 4.88 | +0.001244 | 3 |
| **Clean rows only** (19,662 NULL rows dropped; 112,979 train / 43,181 valid) | 0.5066 | 0.0127 | 0.140 | 2.15 | **−0.002957** | 1 |

**Interpretation (reported plainly):** the explicit missingness *indicators*
are not the driver — removing them leaves VALID IC *unchanged or slightly
better* (0.0310 vs 0.0284). But restricting to rows with **no NULL in any
feature** halves the rank IC (0.0284 → 0.0127), cuts ICIR from 0.27 to 0.14,
and flips the decile spread **negative** (−0.30%/day). A material part of the
edge lives in the NULL rows themselves — the names where the IV solver failed
(no time value / illiquid / violent move, `docs/data/iv-integrity-audit.md`
F4) — which is exactly the liquidity-artifact pattern the plan warned about.

The edge is **not purely** an artifact: on clean, liquid names only, the rank
IC is still positive (0.0127, t-stat 2.15) and the positive-IC day rate is
59.6%. But the model is best understood as a **hybrid**: a weak genuine
ranking signal plus a strong missingness/selection component, and the
tradeable value of the missingness component is exactly what the
liquidity-artifact hypothesis predicts it would be — hard to capture.

### 7.2 Importance review — `models/evaluation/xgb_direction_importance.json`

Gain and SHAP (native TreeSHAP via `pred_contribs`) are computed on the
**3 trees that actually produce predictions** (the booster's other 200 trees
are an early-stopping buffer, never used for prediction). VALID sample: 5,000
rows, seed 42.

Top gain shares (share of total gain, 3 trees):

| Feature | Share |
|---|---|
| `days_to_expiry_30d` | 16.6% |
| `ce_vega` | 14.8% |
| `ret_1d` | 9.0% |
| `put_call_vol_ratio` | 8.9% |
| `ce_gamma` | 6.6% |
| `days_to_earnings` | 4.7% |

Top mean-|SHAP| shares: `ret_1d` 21.9%, `put_call_vol_ratio` 20.5%,
`days_to_expiry_30d` 13.2%, `put_call_oi_ratio` 8.1%, `ret_10d` 6.4%.

**Verdict: no single feature exceeds the 25% threshold** (gain or SHAP) — no
feature requires a blocking explanation. The two largest contributors are
unsurprising: `ret_1d` is the documented 1-day reversal (the Phase 5 baseline
the model must beat, and it does), and `put_call_vol_ratio` is market-wide
hedging/sentiment flow. `days_to_expiry_30d` is near-constant cross-sectionally
(a calendar feature) — its prominence in a 3-tree model is a sign the trees are
using date-ish structure rather than deep per-name signal, consistent with the
thin edge. Caveat: with 3 trees, importances are inherently coarse.

### 7.3 Tradeability — `models/evaluation/xgb_direction_tradeability.json`

Median **raw** chain volume / OI (from the parquet, pre-normalization) of the
names the model actually picks on VALID:

| Bucket | Rows | Median chain_volume (contracts) | Median chain_oi (contracts) |
|---|---|---|---|
| Top decile (long) | 4,350 | 16,751 | 6,849,750 |
| Bottom decile (short) | 4,448 | 16,974 | 13,018,800 |
| Universe (all VALID) | 46,629 | 16,519 | 10,567,150 |

**Verdict: the edge is not concentrated in untradeable names.** Both deciles
trade at roughly the universe-median chain volume (~16.8k contracts/day); no
decile rows are missing volume data. The top decile skews to slightly lower OI
than the universe, but nothing close to an illiquidity red flag.

### 7.4 The sealed TEST run (2026, exactly once) — `models/evaluation/xgb_direction_test.json`

Fit on **TRAIN+VALID** (171,837 rows, 2021-08-02 → 2025-12-29) with
`n_estimators = 3` (Phase 6 `best_iteration = 2` + 1, reproducing the exact
model whose VALID numbers were audited — no early stopping on TEST), predicted
the 27,705 TEST rows (153 dates) once. **Whatever this number is, it is the
number; no tuning followed.**

| Metric | TEST 2026 | VALID 2025 |
|---|---|---|
| AUC | **0.5152** | 0.5165 |
| Accuracy | 0.4903 (= TEST base rate, as expected for a ranker) | 0.4994 |
| Mean daily rank IC | **0.0359** | 0.0284 |
| ICIR | **0.326** | 0.271 |
| IC t-stat | **4.04** | 4.15 |
| Mean decile spread | **+0.001295 (≈ +0.13%/day)** | +0.000861 |
| Positive-IC day rate | 64.7% | 61.3% |

Decile mean returns on TEST: D1 (short leg) −0.053%/day, D10 (long leg)
+0.091%/day; spread +0.14%/day at the tails (equal-weighted, gross, no
costs/liquidity haircut). The TEST number is consistent with VALID — the edge
transfers out of sample and sits inside the plan's realistic envelope (AUC
0.51–0.54, IC 0.02–0.05, ICIR > 0.3 usable). It is a thin but statistically
non-zero ranking signal: IC t-stat 4.04 over 153 dates.

## Known limitations

- **A material part of the edge is missingness/selection driven** (7.1): on
  clean, fully-populated rows the decile spread goes negative and IC halves.
  The deployable signal is weaker than the headline VALID/TEST number.
- **The VALID metrics are inflated by early stopping; TEST is the honest
  number.** The tree count is a hyperparameter chosen on VALID AUC, so VALID
  cannot also be a clean estimate of it. Measured directly (2026-08-31,
  TRAIN+VALID only): re-fitting with early stopping moved to an `inner_holdout`
  — VALID never seen during fitting — drops VALID AUC 0.5165 → **0.5074**, mean
  IC 0.0284 → **0.0141**, spread 0.00086 → **0.00022**. So roughly **half to
  two-thirds of the VALID edge is the tree count fitting VALID.** This does not
  touch TEST: selecting a hyperparameter on VALID and evaluating on TEST is
  exactly what the split is for, so TEST (IC 0.0359) remains uncontaminated.
  But read the honest through-time expectation off the walk-forward
  (**IC ≈ 0.009**), not off VALID — TEST at 0.0359 is likely the lucky side of
  a much thinner true edge.
- **3 trees, and it is not a tuning artifact.** Early stopping chose
  `best_iteration = 2`. Probed across `learning_rate` ∈ {0.02, 0.05, 0.10} ×
  `early_stopping_rounds` ∈ {200, 500}: **every** combination stopped at
  `best_iteration = 2` when stopping on VALID. VALID AUC genuinely peaks at
  tree 3 and never recovers over 500 further rounds at any learning rate, so
  this is not impatience. With an honest inner holdout the optimum instead
  lands at 80–717 trees — i.e. 3 trees is the count that best fits *VALID*,
  which is the same finding as the bullet above. Importances are coarse at this
  size, and the model is correspondingly fragile.
- **Index rows** (NIFTY, BANKNIFTY, NIFTYNXT50) are 1.45% of rows and a
  different volatility regime; metrics are not reported separately with/without
  them.
- **`rv_yz` is computed from near-month futures OHLC** (rolls monthly,
  unadjusted) — suspect it first if realized-vol signals misbehave.
- **`days_to_earnings` never reaches 0** and has a 1,282-day maximum
  (`docs/data/iv-integrity-audit.md` §3).
- **56 symbols have fewer than 500 trading days** (newer F&O listings);
  chronological splits handle this, random splits would not.
- **The final fit uses the sealed splitter's TRAIN+VALID windows**, which
  exclude one embargo-purged tail date from each side (2024-12-31 from TRAIN,
  2025-12-31 from VALID) — a deliberate, tiny (≈2 dates) conservative choice
  for procedural identity.
- **No costs.** The decile spread is gross equal-weighted return; bid-ask and
  impact on ~16.8k-contract names are not modelled.
- **Scoring this model requires pinning its feature list.** The `_is_null`
  indicator set is otherwise derived from the data (NULL rate > 1%), and
  `ret_10d` sits at 1.20% against that 1% threshold — a refreshed dataset could
  silently add or drop a column and misalign the saved model. Always pass the
  `feature_names` recorded in this model's evaluation JSON to
  `load_training_frame(..., feature_names=...)`; it rebuilds exactly that
  contract regardless of observed rates. Unpinned loading is for training only.
- **TEST was predicted twice by the audit harness during development** (once
  before a fix to the *importance* code path, once after); both runs were the
  identical sealed protocol (same fit, same seed) and produced identical
  numbers. No TEST-informed tuning occurred.

## Artifacts

- `models/evaluation/xgb_direction_valid.json` — Phase 6 VALID + walk-forward
- `models/evaluation/xgb_direction_ablation.json` — 7.1
- `models/evaluation/xgb_direction_importance.json` — 7.2
- `models/evaluation/xgb_direction_tradeability.json` — 7.3
- `models/evaluation/xgb_direction_test.json` — 7.4 (the exit artifact)
- `models/candidates/xgb_direction_2026-08-31.json` (Phase 6) and
  `models/candidates/xgb_direction_test_2026-08-31.json` (Phase 7 final fit)
- `models/promoted/xgb_direction_test_2026-08-31.json` +
  `models/promoted/xgb_direction_test.json` — Phase 8 promotion of the
  accepted final fit (byte-verified copies of the candidate + its evaluation)

Phase 8 wiring (`mktmtm train-direction` with `--train-end` / `--valid-end` /
`--output-dir` / `--no-normalize`, and `mktmtm promote-direction`) is in
`src/mktmtm/cli.py` (`src/mktmtm/ml/splits.py`, `ml/promote.py`).

`models/{evaluation,candidates,promoted}/` are gitignored — artifacts stay
local; this card is the committed record.

## Verdict

The Phase 7 audit **does not disqualify** the model, but it changes what it
should be believed to be: a **thin, partially liquidity-linked ranking signal**
(AUC 0.515 / IC 0.036 / ICIR 0.33 on sealed 2026), not a robust alpha model.
Phase 8 wiring is optional and only worth it if the 7.1 finding is accepted as
the operating envelope — i.e. treat the clean-rows IC (≈0.013, t≈2) as the
conservative floor and the headline number as the ceiling that assumes the
missingness component can actually be traded.
