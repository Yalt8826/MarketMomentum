"""Unit tests for mktmtm.ml.train_direction — config, early-stopped fit, 2026 seal.

Phase 6 of the xgboost-direction plan. The real exit test is the training run
itself (``run()`` against the actual parquet → ``models/evaluation/*.json``);
these tests pin the contract on small synthetic data so the module stays
honest without a 125k-row training job.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from mktmtm.ml.dataset import DATASET_COLUMNS
from mktmtm.ml.train_direction import (
    EARLY_STOPPING_ROUNDS,
    PARAMS,
    fit_direction_model,
    run,
    walk_forward_ics,
)


def _panel(
    n_dates: int = 300,
    n_symbols: int = 8,
    seed: int = 7,
    start: str = "2021-08-02",
) -> pd.DataFrame:
    """Synthetic cross-sectional panel with a couple of weak features."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_dates)
    ret = rng.normal(0.0, 0.02, n_dates * n_symbols)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(dates.values, n_symbols)),
            "symbol": [f"S{i}" for i in np.tile(np.arange(n_symbols), n_dates)],
            "next_day_return": ret,
            "next_day_direction": (ret > 0.0).astype(int),
        }
    )
    frame["f1"] = rng.normal(size=len(frame))
    # f2 carries a weak signal so early stopping has something to find.
    frame["f2"] = frame["next_day_return"] + rng.normal(0.0, 0.05, len(frame))
    return frame


FEATURES = ["f1", "f2"]


def test_params_match_plan_config() -> None:
    assert PARAMS["objective"] == "binary:logistic"
    assert PARAMS["eval_metric"] == "auc"
    assert PARAMS["tree_method"] == "hist"
    assert PARAMS["max_depth"] == 4
    assert PARAMS["min_child_weight"] == 100
    assert PARAMS["gamma"] == 0.0
    assert PARAMS["reg_lambda"] == 10.0
    assert PARAMS["reg_alpha"] == 0.0
    assert PARAMS["subsample"] == 0.8
    assert PARAMS["colsample_bytree"] == 0.7
    assert PARAMS["learning_rate"] == 0.02
    assert PARAMS["n_estimators"] == 5000
    assert PARAMS["random_state"] == 42
    assert EARLY_STOPPING_ROUNDS == 200


def test_fit_direction_model_early_stops_and_reports_best_iteration() -> None:
    frame = _panel()
    train, valid = frame.iloc[: len(frame) // 2], frame.iloc[len(frame) // 2 :]
    model, best = fit_direction_model(
        train[FEATURES],
        train["next_day_direction"],
        valid[FEATURES],
        valid["next_day_direction"],
    )
    assert best >= 0
    assert model.best_iteration == best
    proba = model.predict_proba(valid[FEATURES])[:, 1]
    assert proba.shape == (len(valid),)
    assert np.isfinite(proba).all()
    assert float(proba.min()) >= 0.0 and float(proba.max()) <= 1.0


def test_walk_forward_folds_are_embargoed_and_monotone() -> None:
    frame = _panel(n_dates=320)
    folds = walk_forward_ics(frame, FEATURES, n_splits=5)
    assert len(folds) == 5

    train_ends = pd.to_datetime(folds["train_end"])
    valid_starts = pd.to_datetime(folds["valid_start"])
    # Embargo: at least one calendar day between train tail and valid head.
    assert (valid_starts > train_ends).all()
    # Expanding windows: train windows start at the same date and grow.
    assert (pd.to_datetime(folds["train_start"]) == pd.to_datetime(folds["train_start"]).iloc[0]).all()
    assert pd.to_datetime(folds["train_end"]).is_monotonic_increasing
    # Validation chunks are disjoint and time-ordered.
    assert valid_starts.is_monotonic_increasing
    assert (valid_starts.diff().dropna() > pd.Timedelta(days=0)).all()
    # Every fold produced a finite mean IC.
    assert np.isfinite(folds["mean_ic"]).all()


def test_run_writes_artifacts_and_never_validates_on_2026(tmp_path: Path) -> None:
    """End-to-end run() on a synthetic parquet: seal holds, JSONs written."""
    rng = np.random.default_rng(11)
    # ~1,200 business days from 2021-08-02 → crosses well into 2026 (TEST era),
    # so the seal assertion is meaningful: folds must stop before 2026-01-01.
    dates = pd.bdate_range("2021-08-02", periods=1200)
    n = len(dates) * 4
    ret = rng.normal(0.0, 0.02, n)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(dates.values, 4)),
            "symbol": [f"S{i}" for i in np.tile(np.arange(4), len(dates))],
            "next_day_return": ret,
            "next_day_direction": (ret > 0.0).astype(int),
        }
    )
    for col in DATASET_COLUMNS:
        if col not in frame.columns:
            # Continuous feature: mildly noisy; gate columns kept in (0, 200].
            if col in ("ce_iv_30", "pe_iv_30"):
                frame[col] = rng.uniform(10.0, 60.0, len(frame))
            else:
                frame[col] = rng.normal(size=len(frame))
    parquet = tmp_path / "ml_dataset.parquet"
    frame.to_parquet(parquet)

    report = run(
        parquet_path=parquet,
        evaluation_dir=tmp_path / "eval",
        candidates_dir=tmp_path / "cand",
    )

    assert set(report["valid"]["metrics"]) == {
        "auc",
        "accuracy",
        "mean_ic",
        "ic_std",
        "icir",
        "ic_t_stat",
        "mean_spread",
        "hit_rate",
    }
    # The seal: no walk-forward fold may validate on 2026 (TEST era).
    assert report["walk_forward"]["n_folds"] == 5
    valid_ends = pd.to_datetime([f["valid_end"] for f in report["walk_forward"]["folds"]])
    assert (valid_ends < pd.Timestamp("2026-01-01")).all()
    # TEST rows must not be used for early stopping either — the trained model's
    # reported VALID window ends in 2025.
    assert pd.Timestamp(report["valid"]["end"]) < pd.Timestamp("2026-01-01")

    eval_path = tmp_path / "eval" / "xgb_direction_valid.json"
    cand_dir = tmp_path / "cand"
    assert eval_path.exists()
    assert list(cand_dir.glob("xgb_direction_*.json"))
    with eval_path.open() as fh:
        saved = json.load(fh)
    assert saved["best_iteration"] == report["best_iteration"]
