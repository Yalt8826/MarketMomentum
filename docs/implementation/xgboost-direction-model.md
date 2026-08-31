# XGBoost — Next-Day Direction Model — Executable Implementation Plan

> **Goal:** Train and honestly evaluate an XGBoost classifier that predicts `next_day_direction`
> from the 47 option-derived features in `ml_dataset`, used as a **cross-sectional ranker**
> (long top decile / short bottom decile per day), not as an absolute up/down call.
> **Branch:** `model/xgboost-direction`, cut from `main` at `e13e4b9`.
> **Data:** `data/processed/ml_dataset.parquet` — 212,441 rows · 217 symbols · 1,253 dates ·
> 2021-08-02 → 2026-08-28. Rebuilt 2026-08-31 after the F8/F9 fixes; see
> `docs/data/etl-integrity-change-report.md` §10 for the verification.
> **New dependencies:** `xgboost`, `scikit-learn` (neither is currently in `pyproject.toml`).
> **Status:** NOT STARTED.

---

## 0. Execution prerequisites (read first)

**Repo:** `/mnt/storage/Study/Major Project/Code/MktMtm` · branch `model/xgboost-direction`.

**Shell:** the interactive default is fish; run everything through bash.

```bash
cd "/mnt/storage/Study/Major Project/Code/MktMtm"
uv sync --group dev
uv run pytest -m "not integration" -q     # must be green before you start
```

**Hard rules for the executor:**

1. **Build the evaluation harness before the model.** Phases 1–4 ship no model at all. This is
   deliberate: every failure mode in this problem hides in the splitter and the metrics, not in
   the estimator. A model built first will look excellent and be wrong.
2. Do not touch `src/mktmtm/storage/`, `src/mktmtm/pipeline/`, `src/mktmtm/options/`, any
   `sql/*.sql`, `.env`, or anything under `data/`. This branch is additive: new modules under
   `src/mktmtm/ml/`, new tests, new artifacts under `models/`.
3. One commit per task, `feat|fix|test|docs: <scope>: <description>`.
4. Full unit suite green before every commit.
5. At each phase end: run the **Exit test**, commit, then **stop and report**.
6. **2026 is sealed.** It is touched exactly once, in Phase 7. Not for early stopping, not for
   feature selection, not for "just checking". Any peek invalidates the result.

### The three splits (measured, post-gate)

| Split | Period | Rows | Dates | Symbols | Up-rate |
|---|---|---|---|---|---|
| TRAIN | 2021-08-02 → 2024-12-31 | 125,410 | 814 | 206 | 0.5073 |
| VALID | 2025-01-01 → 2025-12-31 | 46,814 | 236 | 217 | 0.4999 |
| TEST | 2026-01-01 → 2026-08-28 | 27,705 | 153 | 186 | 0.4903 |

Note the base-rate drift, **0.5073 → 0.4999 → 0.4903**. This is why the model is used as a
ranker: a probability threshold calibrated on TRAIN is systematically wrong on TEST, and would
manufacture a long bias that has nothing to do with skill.

### What success looks like

| Metric | Realistic | Treat as a bug until proven otherwise |
|---|---|---|
| AUC | 0.51 – 0.54 | > 0.60 |
| Mean daily rank IC | 0.02 – 0.05 | > 0.10 |
| ICIR | > 0.3 usable | > 1.0 |
| Directional accuracy | 51 – 53% | > 55% |

This project has already produced a 100%-accuracy model once, from a row-ordering leak
(`docs/data/ml-dataset-report.md` §2). Optimism is the symptom to investigate, not celebrate.

---

## Phase 1 — Dependencies and package scaffold

**Objective:** `xgboost` and `scikit-learn` installed and importable; empty modules in place.

### Task 1.1
Add to `pyproject.toml` `[project] dependencies`: `xgboost>=2.1`, `scikit-learn>=1.5`.
Run `uv sync`, commit the updated `uv.lock`.

### Task 1.2
Create empty modules with docstrings: `src/mktmtm/ml/features.py`, `splits.py`, `metrics.py`,
`baselines.py`, `train_direction.py`.

**Exit test:** `uv run python -c "import xgboost, sklearn; print(xgboost.__version__, sklearn.__version__)"`
**Checkpoint:** report versions; wait for go.

---

## Phase 2 — Feature contract and loader (`ml/features.py`)

**Objective:** One function that turns the parquet into a model-ready frame, with every
transformation explicit and tested. No model, no training.

### Task 2.1 — the F4 gate, both legs

```python
GATE = "(0 < ce_iv_30 <= 200) & (0 < pe_iv_30 <= 200) & next_day_direction.notna()"
```

Applying it leaves **199,929 rows (94.2%)**. The existing gate in
`docs/data/iv-integrity-audit.md` F4 covers only the CE leg; extending it to PE is nearly
free — it removes just **91 additional rows**, because the two legs go NULL together
(11,878 of ~12,000 NULL rows have both legs missing, since a chain that fails to price one
side usually fails on the other). Gate both anyway: the cost is nil and it removes a class
of row that would otherwise need imputation.

### Task 2.2 — missingness indicators (do this *before* anything else touches the NaNs)

For every feature with a NULL rate above 1%, add a companion `<feature>_is_null` int8 column.

**This is the single most important step in the plan.** The missingness is strongly associated
with the label:

| Feature | NULL rate | up-rate when NULL | up-rate when present |
|---|---|---|---|
| `skew_25d` | 4.9% | **0.628** | 0.503 |
| `ce_iv_30` | 5.7% | **0.605** | 0.503 |
| `ce_ivp` | 11.8% | 0.549 | 0.504 |

Those rows also have higher realized volatility (mean \|next-day return\| 0.0152–0.0158 vs
0.0135). This is **not look-ahead** — NULL-ness is known at time *t* — but it is almost
certainly a liquidity/selection artifact rather than tradeable alpha: IV goes NULL when the
solver cannot converge, i.e. no time value, illiquid, or a violent move, and those are exactly
the names you cannot trade well.

Making it an explicit feature means it shows up in importances instead of being smuggled in
through XGBoost's learned NaN split direction. Phase 6 then measures how much of the model's
performance depends on it.

### Task 2.3 — drop collinear duplicates

33 feature pairs exceed \|r\| > 0.8. Drop these outright:

| Drop | Keep | \|r\| | Why |
|---|---|---|---|
| `pe_vega` | `ce_vega` | **1.000** | ATM call and put vega are identical in Black-Scholes at the same strike/expiry/vol — pure duplication |
| `pe_rho` | `ce_rho` | 0.993 | near-duplicate |
| `ce_volume`, `pe_volume` | `chain_volume`, `put_call_vol_ratio` | 0.997–0.999 | the ratio already carries the CE/PE split |

### Task 2.4 — drop `interest_rate`

It is constant within **100% of dates** (verified). In a cross-sectional model it carries
exactly zero information and only adds a split dimension for the trees to waste capacity on.

### Task 2.5 — per-date cross-sectional normalization

For each date, rank-normalize every remaining continuous feature to [0, 1]
(`groupby("date")[col].rank(pct=True)`), leaving the `_is_null` indicators as raw 0/1.

Rationale: feature magnitudes span ten orders of magnitude (`pe_delta` ∈ [−0.95, −0.01] vs
`chain_oi` up to 4.97e9), and the levels are non-stationary across the five years. Ranking
makes each feature answer "where does this name sit *today*", which is the question a
cross-sectional ranker needs, and neutralizes market-wide regime drift for free.

> Trees do not need this for scale reasons — they are invariant to monotone transforms. They
> need it because a raw level like `atm_straddle` means something different in 2021 than in
> 2026, so a threshold learned on TRAIN does not transfer to TEST.

**Public API:**
```python
def load_training_frame(
    parquet_path: Path | None = None,
    gate: bool = True,
    normalize: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    """Return (frame, feature_names). Frame keeps symbol, date, both targets."""
```

**Exit test:** `tests/unit/test_ml_features.py` — built on a small synthetic frame, asserting:
gate arithmetic; `_is_null` columns created for exactly the >1% features and matching
`.isna()`; dropped columns absent; per-date ranks in [0,1] with each date independent;
row count and column set stable across two calls.
**Checkpoint:** report gated row count per split (expect the table in §0); wait for go.

---

## Phase 3 — Purged walk-forward splitter (`ml/splits.py`)

**Objective:** A splitter that cannot leak, with the leak-detection tests written first.

### Task 3.1

```python
def walk_forward_splits(
    dates: pd.Series,
    n_splits: int = 5,
    embargo_days: int = 1,
    min_train_dates: int = 250,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window splits over unique dates, with an embargo gap."""
```

Rules the implementation must enforce:

- Split on **unique dates**, never on rows. All ~157 names on a date go to the same side; a
  random row split would put a symbol's neighbours across the boundary and leak the market
  factor.
- **Embargo ≥ 1 day** between train and validation. The label spans *t → t+1*, so the last
  training date's outcome is realized on the first validation date.
- **Expanding**, not sliding: each fold trains on everything before its validation window.
  Five years is not enough history to discard the early part.

### Task 3.2 — the final split helper

```python
def train_valid_test(frame: pd.DataFrame) -> tuple[pd.DataFrame, ...]:
    """The fixed 2021-24 / 2025 / 2026 split from §0, with embargo applied."""
```

**Exit test:** `tests/unit/test_ml_splits.py` — assert for every fold that
`max(train_dates) + embargo < min(valid_dates)`; that no date appears on both sides; that no
`(symbol, date)` appears twice; that train sets grow monotonically; and a deliberate
**negative test** — a splitter without the embargo must fail the boundary assertion, proving
the test can actually detect the bug.
**Checkpoint:** report fold boundaries and sizes; wait for go.

---

## Phase 4 — Metrics harness (`ml/metrics.py`)

**Objective:** Metrics that measure the thing you care about. Accuracy is nearly useless at a
50.7% base rate.

### Task 4.1

```python
def daily_rank_ic(df, pred_col="pred", ret_col="next_day_return") -> pd.Series:
    """Spearman corr between prediction and realized return, one value per date."""

def icir(ic: pd.Series) -> float:
    """mean(IC) / std(IC) — the signal-to-noise of the signal itself."""

def decile_spread(df, n_deciles=10) -> pd.Series:
    """Daily mean return of top decile minus bottom decile, by prediction rank."""

def summarize(df) -> dict[str, float]:
    """auc, accuracy, mean_ic, ic_std, icir, ic_t_stat, mean_spread, hit_rate."""
```

`ic_t_stat = mean(IC) / (std(IC) / sqrt(n_days))` — with 153 test dates, a t-stat above ~2 is
the minimum bar for claiming the IC is distinguishable from zero.

### Task 4.2 — evaluate against the *return*, not the label

Rank IC is computed against `next_day_return`, even though the model is trained on
`next_day_direction`. Ordering names by conviction is worth more than being right about a sign,
and it is what the decile strategy actually monetizes.

**Exit test:** `tests/unit/test_ml_metrics.py` — perfect predictions give IC = 1.0; reversed
give −1.0; random predictions give |mean IC| < 0.02 over 500 synthetic dates; `decile_spread`
on a constructed monotone signal is positive; `summarize` returns all keys with no NaN.
**Checkpoint:** report metric values on synthetic fixtures; wait for go.

---

## Phase 5 — Baselines (`ml/baselines.py`)

**Objective:** Numbers the model must beat. Skipping this is how people ship a model that is
worse than a coin flip and never find out.

### Task 5.1 — three baselines, scored with the Phase 4 harness on VALID

| Baseline | Expectation |
|---|---|
| Constant "up" | accuracy ≈ 0.4999 on VALID, IC = 0 |
| Random uniform scores | IC ≈ 0, ICIR ≈ 0 |
| Single feature: `-ret_1d` (1-day reversal) | The one to actually beat — short-horizon reversal is a real effect and a plausible chunk of whatever the model finds |

### Task 5.2 — leakage tripwire

Score `ret_1d` raw as a predictor. If it produces a large IC of either sign, stop — that
signature is what the historical look-ahead bug looked like. Expect |IC| < 0.05.

**Exit test:** a table of all baselines under `summarize`, written to
`models/evaluation/baselines.json`.
**Checkpoint:** report the table; wait for go.

---

## Phase 6 — XGBoost training (`ml/train_direction.py`)

**Objective:** Fit the classifier, early-stopped on VALID. TEST stays sealed.

### Task 6.1 — the config

```python
PARAMS = dict(
    objective="binary:logistic",
    eval_metric="auc",
    tree_method="hist",

    max_depth=4,
    min_child_weight=100,
    gamma=0.0,
    reg_lambda=10.0,
    reg_alpha=0.0,

    subsample=0.8,
    colsample_bytree=0.7,

    learning_rate=0.02,
    n_estimators=5000,       # early stopping picks the real count

    random_state=42,
    n_jobs=-1,
)
EARLY_STOPPING_ROUNDS = 200
```

| Parameter | Why this value, for this dataset |
|---|---|
| `binary:logistic` | Produces the continuous score the cross-sectional ranker needs. |
| `eval_metric="auc"` | Threshold-free, and measures ranking quality directly — the actual objective. Accuracy at a 50.7% base rate is noise. |
| `tree_method="hist"` | Bins once; 125k×~45 fits in seconds, which matters across 5 walk-forward folds × a hyperparameter sweep. |
| `max_depth=4` | 16 leaves, ≤4-way interactions. At a target IC of 0.02–0.05 there is very little signal; depth 8+ memorizes 125k rows instead of generalizing. |
| `min_child_weight=100` | The main noise guard. For logloss the hessian is `p(1-p) ≈ 0.25` near p=0.5, so this is a floor of **~400 rows per leaf** (0.32% of TRAIN) — *not* 100 rows. Scale it with that conversion in mind. |
| `reg_lambda=10.0` | L2 on leaf weights `w* = -G/(H+λ)`; pulls thin leaves toward zero rather than letting them assert confident edges. |
| `subsample=0.8` | Row sampling decorrelates trees. |
| `colsample_bytree=0.7` | Earns its keep: even after Phase 2's pruning, correlated clusters remain. Column sampling forces different trees onto different cluster members. |
| `learning_rate=0.02` + 5000 trees | Small steps, many of them, beats few large steps on noisy data. |
| **no** `scale_pos_weight` | Classes are 50.7/49.3. Nothing to rebalance; forcing it only miscalibrates. |
| **no** imputation | Keep `missing=np.nan` (default). XGBoost learns a default direction per node; the `_is_null` indicators from Phase 2 make that behaviour visible rather than hidden. |

### Task 6.2 — fit

Train on TRAIN, `eval_set=[(X_valid, y_valid)]`, early stopping 200 rounds on AUC.

> **The trap:** early stopping chooses the tree count, which is a hyperparameter. Stopping on
> TEST means TEST informed the model, and the reported score is optimistic. Stop on VALID only.

### Task 6.3 — walk-forward robustness

Refit across the Phase 3 folds. Report mean ± std of IC across folds. A model whose IC swings
sign between folds has not found anything stable, whatever its average says.

**Exit test:** VALID metrics from `summarize`, the chosen `best_iteration`, and the per-fold IC
table, written to `models/evaluation/xgb_direction_valid.json`. Model to
`models/candidates/xgb_direction_<date>.json`.
**Checkpoint:** report VALID AUC/IC/ICIR against §0's expectations; wait for go.

---

## Phase 7 — Artifact audit, then the sealed test

**Objective:** Establish that whatever edge exists is real before spending TEST on it.

### Task 7.1 — the missingness ablation (mandatory)

Score three variants on VALID:

| Variant | Question it answers |
|---|---|
| Full model | baseline performance |
| `_is_null` features removed | how much of the edge is missingness-driven |
| Rows with any gated NULL dropped | does the edge survive on clean, liquid names only |

If performance collapses in variants 2 and 3, the model is a liquidity-artifact detector, not
an alpha model. That is a legitimate finding — report it plainly rather than shipping the
number from variant 1.

### Task 7.2 — importance review

`gain` importances plus SHAP on a VALID sample. Any single feature above ~25% of total gain
deserves an explicit explanation before proceeding.

### Task 7.3 — tradeability check

For the top and bottom prediction deciles, report median `chain_volume` and `chain_oi`. An edge
concentrated in untradeable names is not an edge.

### Task 7.4 — the one TEST run

Only after 7.1–7.3 pass. Fit on TRAIN+VALID with the `best_iteration` from Phase 6, predict
2026 once, report `summarize` plus the decile spread. **Whatever this number is, it is the
number.** No tuning afterwards.

**Exit test:** `models/evaluation/xgb_direction_test.json` + a model card at
`docs/models/xgboost-direction-model-card.md` recording config, splits, all metrics, the
ablation results, and known limitations.
**Checkpoint:** report and stop.

---

## Phase 8 — Wiring (optional, only if Phase 7 is worth keeping)

### Task 8.1
`mktmtm train-direction` CLI command: `--train-end`, `--valid-end`, `--output-dir`,
`--no-normalize`.

### Task 8.2
Promotion: copy the accepted model from `models/candidates/` to `models/promoted/` with its
evaluation JSON. Note that `models/{candidates,evaluation,promoted}/` are gitignored — artifacts
stay local, only the model card is committed.

---

## Appendix — measured dataset facts

Everything the config above is justified against, verified 2026-08-31:

| Fact | Value |
|---|---|
| Rows / symbols / dates | 212,441 · 217 · 1,253 |
| After F4 gate (both legs) | 199,929 (94.2%) |
| Median names per date | 157 |
| Features available | 47 (→ ~45 after Phase 2 pruning, + null indicators) |
| Target up-rate | 0.509 overall; 0.5073 / 0.4999 / 0.4903 by split |
| `next_day_return` | mean +0.00067, std 0.01933, kurtosis 5.7, 420 rows beyond 5σ |
| Feature magnitude span | 3.9e-01 … 4.97e+09 |
| Collinear pairs \|r\| > 0.8 | 33 of 1,081 |
| NULL rates | `ce_ivp`/`ce_ivr` 11.8%, `ce_iv_30` 5.7%, `skew_25d` 4.9%, `days_to_earnings` 3.3% |
| `interest_rate` | constant within 100% of dates |
| Look-ahead check | PASS; `corr(ret_1d, next_day_return)` = +0.0002 |

## Appendix — known limitations to carry into the model card

- **Index rows** (NIFTY, BANKNIFTY, NIFTYNXT50) are 1.45% of rows and a different volatility
  regime. Consider excluding; at minimum, report metrics with and without.
- **`rv_yz` is computed from near-month futures OHLC**, which rolls monthly and is unadjusted —
  both inject discontinuities. Suspect this feature first if realized-vol signals misbehave.
- **`days_to_earnings` never reaches 0** and has a 1,282-day maximum, so the earnings reference
  file has gaps (`docs/data/iv-integrity-audit.md` §3).
- **56 symbols have fewer than 500 trading days** (newer F&O listings). Chronological splits
  handle this correctly; random splits would not.
- **The five symbols with no Yahoo spot source** (FINNIFTY, MIDCPNIFTY, LTIM, PEL, TATAMOTORS)
  are absent from the dataset entirely.
