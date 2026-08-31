"""Unit tests for the Phase 7 artifact audit (ml/audit_direction.py).

Pins the ablation protocol (7.1), the gain/SHAP importance review (7.2), the
tradeability check (7.3) and the sealed one-time TEST run (7.4) on small
synthetic panels — the real exit test is the run against the actual parquet
that writes ``models/evaluation/*.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.audit_direction import (
    MAX_GAIN_SHARE,
    ablation_variants,
    gain_importance_report,
    run,
    run_test,
    shap_summary,
    tradeability_report,
)
from mktmtm.ml.dataset import DATASET_COLUMNS

_METRIC_KEYS = {
    "auc",
    "accuracy",
    "mean_ic",
    "ic_std",
    "icir",
    "ic_t_stat",
    "mean_spread",
    "hit_rate",
}


def _panel(
    n_dates: int = 1200,
    n_symbols: int = 12,
    seed: int = 13,
) -> pd.DataFrame:
    """Synthetic panel crossing into 2026, with NULLs in two features.

    Dates run 2021-08-02 → deep into 2026 so every sealed split exists and the
    audit functions cannot silently fit on the TEST era. ``skew_25d`` gets a
    ~6% NULL rate and ``ce_ivp`` ~12% so the ``_is_null`` indicators are
    created and ``clean_rows`` actually drops rows. At least 10 symbols per
    date so the decile tails (``metrics.decile_spread``) are non-empty.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-08-02", periods=n_dates)
    n = n_dates * n_symbols
    ret = rng.normal(0.0, 0.02, n)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(dates.values, n_symbols)),
            "symbol": [f"S{i}" for i in np.tile(np.arange(n_symbols), n_dates)],
            "next_day_return": ret,
            "next_day_direction": (ret > 0.0).astype(int),
        }
    )
    rng = np.random.default_rng(seed + 1)
    for col in DATASET_COLUMNS:
        if col in frame.columns:
            continue
        if col in ("ce_iv_30", "pe_iv_30"):
            frame[col] = rng.uniform(10.0, 60.0, n)
        elif col == "skew_25d":
            frame[col] = np.where(rng.random(n) < 0.06, np.nan, rng.normal(size=n))
        elif col == "ce_ivp":
            frame[col] = np.where(rng.random(n) < 0.12, np.nan, rng.normal(size=n))
        else:
            frame[col] = rng.normal(size=n)
    return frame


def _write(frame: pd.DataFrame, tmp_path: Path) -> Path:
    path = tmp_path / "ml_dataset.parquet"
    frame.to_parquet(path, index=False)
    return path


def _loaded(tmp_path: Path) -> tuple[pd.DataFrame, list[str]]:
    from mktmtm.ml.features import load_training_frame

    return load_training_frame(parquet_path=_write(_panel(), tmp_path))


# ───────────────────────────── Task 7.1 — ablation ─────────────────────────────


def test_ablation_three_variants_with_finite_metrics(tmp_path: Path) -> None:
    frame, feature_names = _loaded(tmp_path)
    result = ablation_variants(frame, feature_names)

    assert set(result) == {"full", "no_is_null", "clean_rows"}
    for _, variant in result.items():
        assert set(variant["metrics"]) == _METRIC_KEYS
        assert all(np.isfinite(v) for v in variant["metrics"].values())
        assert variant["best_iteration"] >= 0
        assert variant["n_trees"] == variant["best_iteration"] + 1
        assert variant["train_rows"] > 0 and variant["valid_rows"] > 0

    # no_is_null drops exactly the indicator columns, keeps every row.
    assert result["no_is_null"]["n_indicator_features"] == 0
    assert result["no_is_null"]["dropped_rows"] == 0
    assert result["no_is_null"]["n_features"] < result["full"]["n_features"]

    # clean_rows drops exactly the rows with a NULL in any continuous feature.
    assert result["full"]["n_indicator_features"] > 0
    assert result["clean_rows"]["dropped_rows"] > 0
    assert result["clean_rows"]["train_rows"] < result["full"]["train_rows"]
    assert result["clean_rows"]["valid_rows"] < result["full"]["valid_rows"]


def test_ablation_no_is_null_removes_all_indicators(tmp_path: Path) -> None:
    frame, feature_names = _loaded(tmp_path)
    result = ablation_variants(frame, feature_names)
    indicators = [f for f in feature_names if f.endswith("_is_null")]
    assert len(indicators) > 0
    # feature-name parity: full keeps them, no_is_null drops all of them.
    assert result["full"]["n_features"] == len(feature_names)
    assert result["no_is_null"]["n_features"] == len(feature_names) - len(indicators)


# ───────────────────────────── Task 7.2 — importance ──────────────────────────


def _fit_full(tmp_path: Path) -> tuple[object, list[str], pd.DataFrame]:
    from mktmtm.ml.audit_direction import _fit_and_score
    from mktmtm.ml.splits import train_valid_test

    frame, feature_names = _loaded(tmp_path)
    train, valid, _ = train_valid_test(frame)
    model, _, _ = _fit_and_score(train, valid, feature_names)
    return model, feature_names, valid


def test_gain_importance_shares_sum_to_one_and_flag_threshold(tmp_path: Path) -> None:
    model, feature_names, _ = _fit_full(tmp_path)
    report = gain_importance_report(model, feature_names)

    assert report["n_trees"] >= 1
    total_share = sum(r["share_of_total_gain"] for r in report["features"])
    assert total_share == pytest.approx(1.0, abs=1e-9)
    assert set(report["flagged"]) == {
        r["feature"] for r in report["features"] if r["share_of_total_gain"] > MAX_GAIN_SHARE
    }
    assert all(
        r["flagged_over_25pct"] == (r["share_of_total_gain"] > MAX_GAIN_SHARE)
        for r in report["features"]
    )
    # The report must cover exactly the model's feature list, in some order.
    assert {r["feature"] for r in report["features"]} == set(feature_names)


def test_shap_summary_returns_per_feature_rows(tmp_path: Path) -> None:
    model, feature_names, valid = _fit_full(tmp_path)
    sample = valid.sample(n=200, random_state=42)
    report = shap_summary(model, sample, feature_names)

    assert report["n_rows"] == 200
    assert len(report["features"]) == len(feature_names)
    assert np.isfinite(report["bias"])
    assert all(np.isfinite(r["mean_shap"]) for r in report["features"])
    assert all(np.isfinite(r["mean_abs_shap"]) for r in report["features"])
    total = sum(r["share_of_total_abs_shap"] for r in report["features"])
    assert total == pytest.approx(1.0, abs=1e-6)
    # Rows sorted by mean |SHAP| descending.
    shares = [r["mean_abs_shap"] for r in report["features"]]
    assert shares == sorted(shares, reverse=True)


# ───────────────────────────── Task 7.3 — tradeability ────────────────────────


def test_tradeability_report_joins_raw_chain_metrics() -> None:
    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"])
    rows = []
    for d in dates:
        for i in range(10):
            rows.append(
                {
                    "symbol": f"S{i}",
                    "date": d,
                    "pred": i / 10 + 0.001,
                    "next_day_return": 0.01 * (i - 5),
                }
            )
    scored = pd.DataFrame(rows)
    # Raw chain metrics present on every date (the real parquet has one row per
    # symbol-date); volume = 1000 * (i+1), OI = 5000 * (i+1).
    raw = pd.DataFrame(
        {
            "symbol": [f"S{i}" for i in range(10)] * len(dates),
            "date": np.repeat(dates.values, 10),
            "chain_volume": [1000.0 * (i + 1) for i in range(10)] * len(dates),
            "chain_oi": [5000.0 * (i + 1) for i in range(10)] * len(dates),
        }
    )
    report = tradeability_report(scored, raw)

    # pred = i/10 → tails match metrics.decile_spread: bottom = pct <= 0.1 (S0),
    # top = pct >= 0.9 (S8 and S9), per date.
    assert report["bottom_decile"]["median_chain_volume"] == pytest.approx(1000.0)
    assert report["bottom_decile"]["median_chain_oi"] == pytest.approx(5000.0)
    assert report["top_decile"]["median_chain_volume"] == pytest.approx(9500.0)
    assert report["top_decile"]["median_chain_oi"] == pytest.approx(47500.0)
    assert report["universe"]["rows"] == len(scored)
    assert report["top_decile"]["rows"] == 8  # S8+S9 on 4 dates
    assert report["bottom_decile"]["rows"] == 4  # S0 on 4 dates


# ───────────────────────────── Task 7.4 — the sealed TEST run ─────────────────


def test_run_test_fits_pre_2026_only_and_writes_json(tmp_path: Path) -> None:
    parquet = _write(_panel(), tmp_path)
    from mktmtm.ml.features import load_training_frame

    frame, feature_names = load_training_frame(parquet_path=parquet)
    eval_dir = tmp_path / "eval"
    cand_dir = tmp_path / "cand"

    report = run_test(frame, feature_names, best_iteration=2, evaluation_dir=eval_dir, candidates_dir=cand_dir)

    # The one TEST run: fit ends in 2025, prediction starts in 2026.
    assert pd.Timestamp(report["fit"]["end"]) < pd.Timestamp("2026-01-01")
    assert pd.Timestamp(report["test"]["start"]) >= pd.Timestamp("2026-01-01")
    # best_iteration is 0-based; prediction uses best_iteration + 1 trees.
    assert report["n_estimators"] == 3
    assert report["phase6_best_iteration"] == 2
    assert set(report["test"]["metrics"]) == _METRIC_KEYS
    assert set(report["test"]["decile_spread"]) == {"mean", "std", "n_dates"}
    assert report["test"]["decile_spread"]["n_dates"] == report["test"]["dates"]
    assert len(report["test"]["decile_mean_returns"]) == 10

    eval_path = eval_dir / "xgb_direction_test.json"
    assert eval_path.exists()
    assert list(cand_dir.glob("xgb_direction_test_*.json"))
    with eval_path.open() as fh:
        saved = json.load(fh)
    assert saved["test"]["metrics"] == report["test"]["metrics"]


def test_run_writes_all_phase7_artifacts(tmp_path: Path) -> None:
    parquet = _write(_panel(), tmp_path)
    eval_dir = tmp_path / "eval"
    cand_dir = tmp_path / "cand"
    valid_report = eval_dir / "xgb_direction_valid.json"
    eval_dir.mkdir(parents=True)
    valid_report.write_text(json.dumps({"best_iteration": 2}), encoding="utf-8")

    report = run(
        parquet_path=parquet,
        evaluation_dir=eval_dir,
        candidates_dir=cand_dir,
        valid_report_path=valid_report,
        shap_sample_rows=100,
    )

    assert set(report["ablation"]) == {"full", "no_is_null", "clean_rows"}
    assert report["importance"]["gain"]["features"]
    assert report["importance"]["shap"]["features"]
    assert report["tradeability"]["top_decile"]["rows"] > 0
    assert report["test"]["metrics"]["auc"] >= 0.0
    assert report["phase6_best_iteration"] == 2
    assert report["n_estimators_test"] == 3

    for name in (
        "xgb_direction_ablation.json",
        "xgb_direction_importance.json",
        "xgb_direction_tradeability.json",
        "xgb_direction_test.json",
    ):
        assert (eval_dir / name).exists()


def test_run_requires_phase6_report(tmp_path: Path) -> None:
    parquet = _write(_panel(), tmp_path)
    eval_dir = tmp_path / "eval"
    with pytest.raises(FileNotFoundError):
        run(parquet_path=parquet, evaluation_dir=eval_dir, valid_report_path=eval_dir / "missing.json")
