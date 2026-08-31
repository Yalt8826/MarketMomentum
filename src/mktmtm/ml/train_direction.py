"""Training entry point for the XGBoost next-day direction model.

Phase 6 of the xgboost-direction plan (``docs/implementation/xgboost-direction-model.md``).
Fits the binary classifier on TRAIN with early stopping on VALID (AUC), then refits across
the Phase 3 walk-forward folds to establish robustness. TEST (2026) stays sealed — it is
touched exactly once, in Phase 7.

Public API:

- ``PARAMS`` / ``EARLY_STOPPING_ROUNDS`` — the fixed model configuration (Task 6.1).
- ``fit_direction_model`` — fit with early stopping on VALID (Task 6.2).
- ``walk_forward_ics`` — per-fold IC across the Phase 3 folds (Task 6.3).
- ``run`` — orchestrates the whole exit test and writes the artifacts.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb

from mktmtm.ml.features import load_training_frame
from mktmtm.ml.metrics import daily_rank_ic, summarize
from mktmtm.ml.splits import train_valid_test, walk_forward_splits

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Task 6.1 — the config. Every value is justified in the plan appendix; do not
# tune here. Early stopping picks the real tree count; n_estimators is a cap.
# ─────────────────────────────────────────────────────────────────────────────

# TEST stays sealed until Phase 7: no walk-forward fold may validate on 2026.
_TEST_START = pd.Timestamp("2026-01-01")

PARAMS: dict[str, Any] = dict(
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
    n_estimators=5000,  # early stopping picks the real count
    random_state=42,
    n_jobs=-1,
)

EARLY_STOPPING_ROUNDS = 200

# Default artifact locations (repo root relative; models/* is gitignored).
EVALUATION_DIR = Path("models/evaluation")
CANDIDATES_DIR = Path("models/candidates")


def fit_direction_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_valid: pd.DataFrame,
    y_valid: pd.Series,
) -> tuple[xgb.XGBClassifier, int]:
    """Fit on TRAIN, early-stopped on VALID AUC (Task 6.2).

    Early stopping chooses the tree count, which is a hyperparameter — so it must
    be chosen on VALID, never on TEST. Returns ``(model, best_iteration)``; the
    model's predictions automatically use ``best_iteration`` after fitting.
    """
    model = xgb.XGBClassifier(
        **PARAMS,
        early_stopping_rounds=EARLY_STOPPING_ROUNDS,
    )
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_valid, y_valid)],
        verbose=False,
    )
    assert model.best_iteration is not None, "early stopping must set best_iteration"
    return model, int(model.best_iteration)


def _predict_proba(model: xgb.XGBClassifier, X: pd.DataFrame) -> np.ndarray:
    """Positive-class probabilities; model must have been early-stopped."""
    return model.predict_proba(X)[:, 1]


def walk_forward_ics(
    frame: pd.DataFrame,
    feature_names: list[str],
    n_splits: int = 5,
) -> pd.DataFrame:
    """Refit across the Phase 3 walk-forward folds; per-fold daily rank IC (Task 6.3).

    Each fold trains on the expanding window before its validation window and
    reports the mean daily rank IC on that fold's validation rows. A model whose
    IC swings sign between folds has not found anything stable, whatever the
    average says.
    """
    rows: list[dict[str, Any]] = []
    for fold, (train_idx, valid_idx) in enumerate(
        walk_forward_splits(frame["date"], n_splits=n_splits)
    ):
        train = frame.iloc[train_idx]
        valid = frame.iloc[valid_idx]
        model = xgb.XGBClassifier(
            **PARAMS,
            early_stopping_rounds=EARLY_STOPPING_ROUNDS,
        )
        model.fit(
            train[feature_names],
            train["next_day_direction"],
            eval_set=[(valid[feature_names], valid["next_day_direction"])],
            verbose=False,
        )
        valid = valid.copy()
        valid["pred"] = _predict_proba(model, valid[feature_names])
        ic = daily_rank_ic(valid)
        rows.append(
            {
                "fold": fold,
                "train_start": train["date"].min().isoformat(),
                "train_end": train["date"].max().isoformat(),
                "valid_start": valid["date"].min().isoformat(),
                "valid_end": valid["date"].max().isoformat(),
                "n_train_rows": int(len(train)),
                "n_valid_rows": int(len(valid)),
                "n_valid_dates": int(ic.shape[0]),
                "mean_ic": float(ic.mean()),
                "ic_std": float(ic.std(ddof=1)) if len(ic) > 1 else 0.0,
                "icir": float(ic.mean() / ic.std(ddof=1)) if len(ic) > 1 and ic.std(ddof=1) > 0 else 0.0,
                "best_iteration": int(model.best_iteration or 0),
            }
        )
    return pd.DataFrame(rows)


def run(
    parquet_path: Path | None = None,
    evaluation_dir: Path = EVALUATION_DIR,
    candidates_dir: Path = CANDIDATES_DIR,
) -> dict[str, Any]:
    """Run the Phase 6 exit test end to end.

    Loads the gated, per-date rank-normalized frame; splits TRAIN/VALID/TEST
    (TEST unused); fits early-stopped on VALID; scores VALID with ``summarize``;
    refits across walk-forward folds; writes ``xgb_direction_valid.json`` and the
    model to ``xgb_direction_<date>.json``. Returns the report dict.
    """
    frame, feature_names = load_training_frame(parquet_path=parquet_path)
    train, valid, test = train_valid_test(frame)
    del test  # 2026 stays sealed until Phase 7

    X_train = train[feature_names]
    y_train = train["next_day_direction"]
    X_valid = valid[feature_names]
    y_valid = valid["next_day_direction"]

    model, best_iteration = fit_direction_model(X_train, y_train, X_valid, y_valid)

    valid_scored = valid.copy()
    valid_scored["pred"] = _predict_proba(model, X_valid)
    valid_metrics = summarize(valid_scored)

    # Walk-forward robustness runs over TRAIN+VALID era only: 2026 (TEST) stays
    # sealed until Phase 7, so no fold may validate on it.
    pre_test = frame[frame["date"] < _TEST_START]
    folds = walk_forward_ics(pre_test, feature_names, n_splits=5)
    fold_ics = folds["mean_ic"]
    wf_report = {
        "mean_ic": float(fold_ics.mean()),
        "ic_std": float(fold_ics.std(ddof=1)) if len(fold_ics) > 1 else 0.0,
        "icir": float(fold_ics.mean() / fold_ics.std(ddof=1))
        if len(fold_ics) > 1 and fold_ics.std(ddof=1) > 0
        else 0.0,
        "min_ic": float(fold_ics.min()),
        "max_ic": float(fold_ics.max()),
        "n_folds": int(len(folds)),
        "folds": folds.to_dict(orient="records"),
    }

    report: dict[str, Any] = {
        "model": "xgb_direction",
        "trained_at": date.today().isoformat(),
        "params": PARAMS,
        "early_stopping_rounds": EARLY_STOPPING_ROUNDS,
        "best_iteration": best_iteration,
        "n_estimators_fit": int(model.best_iteration or 0),
        "n_features": len(feature_names),
        "train": {
            "start": train["date"].min().isoformat(),
            "end": train["date"].max().isoformat(),
            "rows": int(len(train)),
        },
        "valid": {
            "start": valid["date"].min().isoformat(),
            "end": valid["date"].max().isoformat(),
            "rows": int(len(valid)),
            "metrics": valid_metrics,
        },
        "walk_forward": wf_report,
        "feature_names": feature_names,
    }

    evaluation_dir.mkdir(parents=True, exist_ok=True)
    candidates_dir.mkdir(parents=True, exist_ok=True)
    eval_path = evaluation_dir / "xgb_direction_valid.json"
    model_path = candidates_dir / f"xgb_direction_{date.today().isoformat()}.json"
    eval_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    model.save_model(str(model_path))

    logger.info("wrote %s and %s", eval_path, model_path)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
