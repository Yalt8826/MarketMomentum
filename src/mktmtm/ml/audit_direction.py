"""Phase 7 artifact audit for the XGBoost next-day direction model.

Phase 7 of the xgboost-direction plan (``docs/implementation/xgboost-direction-model.md``).
Establishes that whatever edge the Phase 6 model showed on VALID is real before TEST (2026)
is spent — the only run that may touch 2026 is Task 7.4.

Public API:

- ``ablation_variants`` — Task 7.1: score full / no-``_is_null`` / clean-rows-only on VALID.
- ``gain_importance_report`` — Task 7.2: per-feature gain as a share of total gain.
- ``shap_summary`` — Task 7.2: mean |TreeSHAP| per feature on a VALID sample (native
  ``pred_contribs``, no ``shap`` dependency).
- ``tradeability_report`` — Task 7.3: median raw ``chain_volume`` / ``chain_oi`` in the top
  and bottom prediction deciles on VALID.
- ``run_test`` — Task 7.4: the one TEST run. Fits TRAIN+VALID with the Phase 6
  ``best_iteration`` tree count, predicts 2026 exactly once.
- ``run`` — orchestrates 7.1–7.4 and writes every artifact under ``models/evaluation/``.
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
from mktmtm.ml.metrics import decile_spread, summarize
from mktmtm.ml.splits import train_valid_test
from mktmtm.ml.train_direction import (
    CANDIDATES_DIR,
    EVALUATION_DIR,
    PARAMS,
    _predict_proba,
    fit_direction_model,
)

logger = logging.getLogger(__name__)

# The raw parquet (pre-normalization) — the tradeability check reads the real
# chain_volume / chain_oi from here, because the training frame rank-normalizes
# them away. Mirrors ml/features.py's default resolution (parents[3] == repo root).
_RAW_PARQUET = Path(__file__).resolve().parents[3] / "data" / "processed" / "ml_dataset.parquet"

# A feature carrying more than this share of total gain warrants an explicit
# explanation before proceeding (plan Task 7.2).
MAX_GAIN_SHARE = 0.25

# Default artifact locations (repo root relative; models/* is gitignored).
VALID_REPORT_PATH = EVALUATION_DIR / "xgb_direction_valid.json"

# Number of VALID rows sampled (fixed seed) for the SHAP review.
SHAP_SAMPLE_ROWS = 5000


def _fit_and_score(
    train: pd.DataFrame,
    valid: pd.DataFrame,
    feature_names: list[str],
) -> tuple[xgb.XGBClassifier, int, dict[str, float]]:
    """Fit early-stopped on VALID (the Phase 6 protocol) and score VALID.

    Returns ``(model, best_iteration, metrics)``. Early stopping picks each
    variant's own tree count; scoring is the Phase 4 ``summarize`` harness.
    """
    model, best_iteration = fit_direction_model(
        train[feature_names],
        train["next_day_direction"],
        valid[feature_names],
        valid["next_day_direction"],
    )
    scored = valid.copy()
    scored["pred"] = _predict_proba(model, valid[feature_names])
    return model, best_iteration, summarize(scored)


def _contiguous_features(feature_names: list[str]) -> list[str]:
    """The continuous features in ``feature_names`` (everything but ``_is_null``)."""
    return [f for f in feature_names if not f.endswith("_is_null")]


def ablation_variants(
    frame: pd.DataFrame,
    feature_names: list[str],
) -> dict[str, dict[str, Any]]:
    """Task 7.1 — the missingness ablation, scored on VALID.

    Three variants, all fit on TRAIN with early stopping on VALID (the Phase 6
    protocol), all scored with ``summarize`` on VALID:

    - ``full``: the Phase 6 feature set, baseline performance.
    - ``no_is_null``: the ``_is_null`` indicators removed — how much of the edge
      is missingness-driven (the NULL rows stay, XGBoost learns its own split).
    - ``clean_rows``: rows with any gated NULL in the continuous features are
      dropped from TRAIN and VALID alike — does the edge survive on clean,
      liquid names only. The indicator columns become constant zero and carry
      no information; the column set is kept identical for comparability.

    If performance collapses in variants 2 and 3, the model is a
    liquidity-artifact detector, not an alpha model.
    """
    train, valid, test = train_valid_test(frame)
    if len(test) == 0:
        raise ValueError("ablation requires TEST-era rows to exist (2026 data present)")
    del test  # the ablation never touches 2026

    continuous = _contiguous_features(feature_names)
    clean_mask = frame[continuous].notna().all(axis=1)

    variants: dict[str, dict[str, Any]] = {}
    for name, feats, mask in (
        ("full", feature_names, pd.Series(True, index=frame.index)),
        ("no_is_null", continuous, pd.Series(True, index=frame.index)),
        ("clean_rows", feature_names, clean_mask),
    ):
        tr = train.loc[mask.loc[train.index]]
        va = valid.loc[mask.loc[valid.index]]
        if len(tr) < 100 or len(va) < 100:
            raise ValueError(f"ablation variant '{name}' left too few rows to fit")
        model, best_iteration, metrics = _fit_and_score(tr, va, feats)
        variants[name] = {
            "metrics": metrics,
            "best_iteration": best_iteration,
            "n_trees": best_iteration + 1,  # predict_proba uses best_iteration+1 trees
            "n_features": len(feats),
            "n_indicator_features": len([f for f in feats if f.endswith("_is_null")]),
            "train_rows": int(len(tr)),
            "valid_rows": int(len(va)),
            "dropped_rows": int(len(frame) - mask.sum()),
        }
    return variants


def _as_gain(value: float | list[float]) -> float:
    """Coerce a per-feature gain value to a scalar float.

    The xgboost stubs type ``get_score`` values as ``float | list[float]``; the
    gain importance path always yields scalars, but the coercion keeps the
    sum below total typed.
    """
    if isinstance(value, list):
        return float(sum(value))
    return float(value)


def _effective_booster(model: xgb.XGBClassifier) -> xgb.Booster:
    """The booster sliced to the trees that actually produce predictions.

    An early-stopped XGBClassifier keeps its full training buffer (it trains
    ``early_stopping_rounds`` rounds past the optimum), but predictions use
    ``best_iteration + 1`` trees. Importances and SHAP must describe the model
    that predicts, not the buffer, so both reviews slice to those trees.
    """
    booster = model.get_booster()
    best = model.best_iteration
    if best is not None and booster.num_boosted_rounds() > best + 1:
        return booster[0 : best + 1]
    return booster


def gain_importance_report(
    model: xgb.XGBClassifier,
    feature_names: list[str],
) -> dict[str, Any]:
    """Task 7.2 — per-feature ``gain`` importances as a share of total gain.

    Features above ``MAX_GAIN_SHARE`` (25%) of total gain are flagged for an
    explicit explanation. ``total_gain`` is the sum over all trees of the gain
    attributed to each feature. Computed on the ``best_iteration + 1`` trees
    that actually produce predictions (see ``_effective_booster``).
    """
    booster = _effective_booster(model)
    scores = booster.get_score(importance_type="gain")
    total = sum(_as_gain(v) for v in scores.values())
    rows: list[dict[str, Any]] = []
    for feat in feature_names:
        gain = _as_gain(scores.get(feat, 0.0))
        share = gain / total if total > 0 else 0.0
        rows.append(
            {
                "feature": feat,
                "gain": gain,
                "share_of_total_gain": share,
                "flagged_over_25pct": share > MAX_GAIN_SHARE,
            }
        )
    rows.sort(key=lambda r: r["share_of_total_gain"], reverse=True)
    return {
        "total_gain": total,
        "n_trees": booster.num_boosted_rounds(),
        "n_trees_in_buffer": model.get_booster().num_boosted_rounds(),
        "threshold": MAX_GAIN_SHARE,
        "features": rows,
        "flagged": [r["feature"] for r in rows if r["flagged_over_25pct"]],
    }


def shap_summary(
    model: xgb.XGBClassifier,
    X_sample: pd.DataFrame,
    feature_names: list[str],
) -> dict[str, Any]:
    """Task 7.2 — mean TreeSHAP per feature on a VALID sample.

    Uses xgboost's native ``pred_contribs`` (the same TreeSHAP algorithm the
    ``shap`` package wraps), so no new dependency is needed. The bias column
    (the model's expected-value term) is reported separately, never attributed
    to a feature. Computed on the ``best_iteration + 1`` trees that actually
    produce predictions (see ``_effective_booster``).
    """
    booster = _effective_booster(model)
    contribs = booster.predict(
        xgb.DMatrix(X_sample[feature_names]), pred_contribs=True
    )
    # contribs has one column per feature plus a final bias column.
    bias = float(contribs[:, -1].mean())
    per_feature = contribs[:, :-1]
    mean_abs = np.abs(per_feature).mean(axis=0)
    mean = per_feature.mean(axis=0)
    total_abs = float(mean_abs.sum())
    rows: list[dict[str, Any]] = []
    for i, feat in enumerate(feature_names):
        rows.append(
            {
                "feature": feat,
                "mean_shap": float(mean[i]),
                "mean_abs_shap": float(mean_abs[i]),
                "share_of_total_abs_shap": (
                    float(mean_abs[i] / total_abs) if total_abs > 0 else 0.0
                ),
            }
        )
    rows.sort(key=lambda r: r["mean_abs_shap"], reverse=True)
    return {
        "n_rows": int(contribs.shape[0]),
        "n_trees": booster.num_boosted_rounds(),
        "n_trees_in_buffer": model.get_booster().num_boosted_rounds(),
        "bias": bias,
        "features": rows,
    }


def _decile_membership(scored: pd.DataFrame, n_deciles: int = 10) -> pd.DataFrame:
    """Within-date prediction decile (1 = lowest pred, 10 = highest) for each row.

    The tails match ``metrics.decile_spread`` exactly — bottom decile is every
    row with within-date pred pct-rank <= 1/n, top decile is pct-rank >= 1 - 1/n
    — so the tradeability buckets are the same rows the spread strategy trades.
    The middle is partitioned into the remaining n-2 equal-width buckets.
    """
    out = scored.copy()
    pct = out.groupby("date")["pred"].rank(pct=True)
    bottom_edge = 1.0 / n_deciles
    top_edge = 1.0 - bottom_edge
    middle = np.floor(
        (pct - bottom_edge) / (1.0 - 2.0 * bottom_edge) * (n_deciles - 2)
    ).astype(int) + 2
    out["decile"] = np.where(
        pct <= bottom_edge,
        1,
        np.where(pct >= top_edge, n_deciles, middle),
    )
    return out


def tradeability_report(
    scored_valid: pd.DataFrame,
    raw_frame: pd.DataFrame,
) -> dict[str, Any]:
    """Task 7.3 — median raw ``chain_volume`` / ``chain_oi`` per prediction decile.

    The edge must survive on liquid, tradeable names: if the top decile's
    median chain volume sits far below the universe median, the signal is
    concentrated in names you cannot actually trade. Medians are computed on
    the *raw* parquet values (``chain_volume`` / ``chain_oi`` are rank-
    normalized inside the training frame, so the raw join is the only honest
    read). Top/bottom decile are assigned within each date by prediction rank.
    """
    raw = raw_frame[["symbol", "date", "chain_volume", "chain_oi"]]
    # Select only identity + prediction columns before the join: the scored
    # frame also carries the rank-normalized chain_volume/chain_oi as model
    # features, which would suffix the raw columns with _x/_y.
    merged = _decile_membership(scored_valid)[["symbol", "date", "pred", "decile"]].merge(
        raw, on=["symbol", "date"], how="left"
    )

    def _stats(rows: pd.DataFrame) -> dict[str, Any]:
        vol = rows["chain_volume"].dropna()
        oi = rows["chain_oi"].dropna()
        return {
            "rows": int(len(rows)),
            "median_chain_volume": float(vol.median()) if len(vol) else None,
            "median_chain_oi": float(oi.median()) if len(oi) else None,
            "pct_volume_missing": float(rows["chain_volume"].isna().mean()),
        }

    deciles = {
        f"decile_{d}": _stats(rows) for d, rows in merged.groupby("decile", sort=True)
    }
    top = merged[merged["decile"] == 10]
    bottom = merged[merged["decile"] == 1]
    return {
        "top_decile": _stats(top),
        "bottom_decile": _stats(bottom),
        "universe": _stats(merged),
        "deciles": deciles,
    }


def fit_final(
    X_fit: pd.DataFrame,
    y_fit: pd.Series,
    n_estimators: int,
) -> xgb.XGBClassifier:
    """Fit on TRAIN+VALID with a fixed tree count (no early stopping)."""
    params = {**PARAMS, "n_estimators": n_estimators}
    model = xgb.XGBClassifier(**params)
    model.fit(X_fit, y_fit, verbose=False)
    return model


def _decile_mean_returns(scored: pd.DataFrame) -> dict[str, float]:
    """Mean next-day return per prediction decile (1 = lowest pred)."""
    ranked = _decile_membership(scored)
    return {
        f"decile_{d}": float(rows["next_day_return"].mean())
        for d, rows in ranked.groupby("decile", sort=True)
    }


def run_test(
    frame: pd.DataFrame,
    feature_names: list[str],
    best_iteration: int,
    evaluation_dir: Path = EVALUATION_DIR,
    candidates_dir: Path = CANDIDATES_DIR,
) -> dict[str, Any]:
    """Task 7.4 — the one TEST run. Fit TRAIN+VALID, predict 2026 exactly once.

    ``best_iteration`` comes from the Phase 6 early-stopped fit. XGBoost's
    ``best_iteration`` is 0-based and prediction uses ``best_iteration + 1``
    trees, so the final fit fixes ``n_estimators = best_iteration + 1`` to
    reproduce the exact model whose VALID numbers Phase 6 audited. Whatever
    the resulting TEST numbers are, they are the numbers — no tuning after
    this point.

    Writes ``xgb_direction_test.json`` (the Phase 7 exit artifact) and the
    final model to ``xgb_direction_test_<date>.json`` under candidates.
    """
    train, valid, test = train_valid_test(frame)
    fit_frame = pd.concat([train, valid], ignore_index=True)
    X_fit = fit_frame[feature_names]
    y_fit = fit_frame["next_day_direction"]
    n_estimators = best_iteration + 1

    model = fit_final(X_fit, y_fit, n_estimators)

    test_scored = test.copy()
    test_scored["pred"] = _predict_proba(model, test[feature_names])
    metrics = summarize(test_scored)
    spread = decile_spread(test_scored)
    report: dict[str, Any] = {
        "model": "xgb_direction",
        "phase": 7,
        "trained_at": date.today().isoformat(),
        "params": {**PARAMS, "n_estimators": n_estimators},
        "phase6_best_iteration": best_iteration,
        "n_estimators": n_estimators,
        "fit": {
            "start": fit_frame["date"].min().isoformat(),
            "end": fit_frame["date"].max().isoformat(),
            "rows": int(len(fit_frame)),
        },
        "test": {
            "start": test["date"].min().isoformat(),
            "end": test["date"].max().isoformat(),
            "rows": int(len(test)),
            "dates": int(test["date"].nunique()),
            "metrics": metrics,
            "decile_spread": {
                "mean": float(spread.mean()) if len(spread) else 0.0,
                "std": float(spread.std(ddof=1)) if len(spread) > 1 else 0.0,
                "n_dates": int(len(spread)),
            },
            "decile_mean_returns": _decile_mean_returns(test_scored),
        },
        "feature_names": feature_names,
    }

    evaluation_dir.mkdir(parents=True, exist_ok=True)
    candidates_dir.mkdir(parents=True, exist_ok=True)
    eval_path = evaluation_dir / "xgb_direction_test.json"
    model_path = candidates_dir / f"xgb_direction_test_{date.today().isoformat()}.json"
    eval_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    model.save_model(str(model_path))
    logger.info("wrote %s and %s", eval_path, model_path)
    return report


def run(
    parquet_path: Path | None = None,
    evaluation_dir: Path = EVALUATION_DIR,
    candidates_dir: Path = CANDIDATES_DIR,
    valid_report_path: Path = VALID_REPORT_PATH,
    shap_sample_rows: int = SHAP_SAMPLE_ROWS,
    seed: int = 42,
) -> dict[str, Any]:
    """Run the Phase 7 exit test end to end (tasks 7.1–7.4).

    Loads the gated, rank-normalized frame once, runs the ablation (7.1), the
    gain + SHAP importance review (7.2), the tradeability check (7.3), then —
    and only then — the single TEST run (7.4) using the Phase 6
    ``best_iteration`` read from ``xgb_direction_valid.json``. Writes:

    - ``xgb_direction_ablation.json``
    - ``xgb_direction_importance.json``
    - ``xgb_direction_tradeability.json``
    - ``xgb_direction_test.json``  (the required exit artifact)

    Returns the combined report dict.
    """
    path = parquet_path if parquet_path is not None else _RAW_PARQUET
    frame, feature_names = load_training_frame(parquet_path=path)
    raw_frame = pd.read_parquet(path)[["symbol", "date", "chain_volume", "chain_oi"]]

    # ── Task 7.1 — missingness ablation ────────────────────────────────────────
    ablation = ablation_variants(frame, feature_names)

    # ── Task 7.2 — importance review (on the full-model fit) ──────────────────
    train, valid, test = train_valid_test(frame)
    del test  # 2026 stays sealed
    full_model, _, _ = _fit_and_score(train, valid, feature_names)
    gain = gain_importance_report(full_model, feature_names)
    rng = np.random.default_rng(seed)
    sample_idx = rng.choice(
        len(valid), size=min(shap_sample_rows, len(valid)), replace=False
    )
    shap = shap_summary(full_model, valid.iloc[sample_idx], feature_names)

    # ── Task 7.3 — tradeability check ──────────────────────────────────────────
    full_scored = valid.copy()
    full_scored["pred"] = _predict_proba(full_model, valid[feature_names])
    trade = tradeability_report(full_scored, raw_frame)

    # ── Task 7.4 — the one TEST run ────────────────────────────────────────────
    if valid_report_path.exists():
        with valid_report_path.open(encoding="utf-8") as fh:
            phase6 = json.load(fh)
        best_iteration = int(phase6["best_iteration"])
    else:
        raise FileNotFoundError(
            f"Phase 6 report {valid_report_path} not found; cannot seal TEST without "
            "the Phase 6 best_iteration (the sealed test depends on it)"
        )
    test_report = run_test(
        frame,
        feature_names,
        best_iteration,
        evaluation_dir=evaluation_dir,
        candidates_dir=candidates_dir,
    )

    report: dict[str, Any] = {
        "model": "xgb_direction",
        "phase": 7,
        "trained_at": date.today().isoformat(),
        "ablation": ablation,
        "importance": {
            "gain": gain,
            "shap": shap,
            "source": {
                "fit": "TRAIN, early-stopped on VALID (Phase 6 protocol)",
                "shap_sample_rows": int(len(valid.iloc[sample_idx])),
            },
        },
        "tradeability": trade,
        "test": test_report["test"],
        "phase6_best_iteration": best_iteration,
        "n_estimators_test": test_report["n_estimators"],
        "feature_names": feature_names,
    }

    evaluation_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("xgb_direction_ablation.json", ablation),
        ("xgb_direction_importance.json", report["importance"]),
        ("xgb_direction_tradeability.json", trade),
        ("xgb_direction_test.json", test_report),
    ):
        (evaluation_dir / name).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    logger.info("wrote %s", evaluation_dir)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
