"""Unit tests for mktmtm.ml.gex_hypothesis — Phase 6 H1/H2 tests + robustness.

Pure pandas/sklearn/xgboost, no DB: the module's analysis functions are tested
on synthetic panels with a planted signal. The SQL liquidity aggregate and the
ml_dataset loader are covered by the integration path (they need a live DB).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.gex_hypothesis import (
    FEATURE_COLUMNS,
    MissingDataError,
    _has_signal,
    fit_gex_model,
    positioning_change,
    robustness_checks,
    run_model,
    sign_test,
)

RANDOM_SEED = 7


def _panel(
    n_dates: int = 120,
    n_symbols: int = 30,
    seed: int = RANDOM_SEED,
    h1_strength: float = 0.0,
    start: str = "2021-01-04",
) -> pd.DataFrame:
    """Synthetic cross-sectional panel with gex and a vol target.

    When ``h1_strength > 0``, negative gex is followed by *higher* abs returns
    (the H1 short-gamma regime): the mean target is shifted by
    ``-h1_strength * sign(gex)``.

    The default date span (2021-01-04 onward) covers the sealed TRAIN/VALID
    windows so ``run_model``'s fixed split has rows on every side.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_dates)
    gex = rng.normal(0.0, 1e8, n_dates * n_symbols)
    base_vol = rng.lognormal(mean=-4.2, sigma=0.3, size=n_dates * n_symbols)
    vol = base_vol + h1_strength * np.where(gex < 0, 1.0, 0.0)
    vol = np.maximum(vol, 1e-6)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(dates.values, n_symbols)),
            "symbol": [f"S{i:02d}" for i in np.tile(np.arange(n_symbols), n_dates)],
            "gex": gex,
            "gex_chg": rng.normal(0.0, 2e7, n_dates * n_symbols),
            "otm_put_oi_build": rng.normal(0.0, 5e4, n_dates * n_symbols),
            "dex": rng.normal(0.0, 1e8, n_dates * n_symbols),
            "dex_chg": rng.normal(0.0, 2e7, n_dates * n_symbols),
            "vanna_exp": rng.normal(0.0, 1e4, n_dates * n_symbols),
            "charm_exp": rng.normal(0.0, 1e4, n_dates * n_symbols),
            "oi_hhi": rng.uniform(0.0, 1.0, n_dates * n_symbols),
            "oi_hhi_chg": rng.normal(0.0, 0.05, n_dates * n_symbols),
            "dist_to_call_wall": rng.normal(0.0, 0.05, n_dates * n_symbols),
            "dist_to_flip": rng.normal(0.0, 0.1, n_dates * n_symbols),
            "ret_1d": rng.normal(0.0, 0.02, n_dates * n_symbols),
            "rv_yz": rng.lognormal(mean=-4.2, sigma=0.3, size=n_dates * n_symbols),
            "ce_iv_30": rng.lognormal(mean=-4.2, sigma=0.3, size=n_dates * n_symbols) * 100,
            "pe_iv_30": rng.lognormal(mean=-4.2, sigma=0.3, size=n_dates * n_symbols) * 100,
            "iv_rv_spread": rng.normal(0.0, 0.02, n_dates * n_symbols),
            "term_slope": rng.normal(0.0, 0.01, n_dates * n_symbols),
            "put_call_iv_spread": rng.normal(0.0, 0.01, n_dates * n_symbols),
            "atm_straddle": rng.lognormal(mean=-3.8, sigma=0.3, size=n_dates * n_symbols),
            "skew_25d": rng.normal(0.0, 0.01, n_dates * n_symbols),
            "put_call_vol_ratio": rng.lognormal(mean=0.0, sigma=0.3, size=n_dates * n_symbols),
            "put_call_oi_ratio": rng.lognormal(mean=0.0, sigma=0.3, size=n_dates * n_symbols),
            "chain_volume": rng.lognormal(mean=12.0, sigma=0.5, size=n_dates * n_symbols),
            "chain_oi": rng.lognormal(mean=14.0, sigma=0.5, size=n_dates * n_symbols),
            "chain_oi_chg": rng.normal(0.0, 1e5, n_dates * n_symbols),
            "chain_breadth": rng.uniform(0.0, 1.0, n_dates * n_symbols),
            "days_to_expiry_30d": rng.integers(1, 40, n_dates * n_symbols),
            "next_day_abs_return": vol,
        }
    )
    # make the expiry-week split meaningful: every 5th day is expiry week
    frame["days_to_expiry_30d"] = np.where(
        frame.groupby("date").cumcount() == 0, 3, frame["days_to_expiry_30d"]
    )
    return frame


# ───────────────────────────── Task 6.1 — H1 ───────────────────────────────

def test_sign_test_null_panel_no_false_positive() -> None:
    """No planted signal -> the t-test must NOT reject H1."""
    res = sign_test(_panel(h1_strength=0.0))
    assert res["gex_positive"]["n"] > 0
    assert res["gex_negative"]["n"] > 0
    assert res["welch_p_value"] > 0.05
    assert res["h1_consistent"] is False
    assert abs(res["ic_neg_gex"]["mean_ic"]) < 0.1


def test_sign_test_detects_planted_h1() -> None:
    """Planted short-gamma regime -> lower vol for gex>0, t-test significant."""
    res = sign_test(_panel(h1_strength=0.01))
    assert res["gex_positive"]["mean_abs_return"] < res["gex_negative"]["mean_abs_return"]
    assert res["welch_p_value"] < 0.05
    assert res["h1_consistent"] is True
    assert res["ic_neg_gex"]["mean_ic"] > 0
    assert res["ic_neg_gex"]["n_days"] > 30


def test_sign_test_missing_columns_raises() -> None:
    with pytest.raises(MissingDataError):
        sign_test(pd.DataFrame({"date": [], "symbol": []}))


# ───────────────────────────── Task 6.2 — H2 ───────────────────────────────

def test_positioning_change_returns_both_features() -> None:
    res = positioning_change(_panel())
    assert set(res) == {"gex_chg", "otm_put_oi_build"}
    for entry in res.values():
        assert "partial_ic_controlling_gex" in entry
        assert entry["partial_ic_controlling_gex"]["n_days"] > 0


def test_positioning_change_raises_when_features_missing() -> None:
    frame = _panel().drop(columns=["gex_chg", "otm_put_oi_build"])
    with pytest.raises(MissingDataError):
        positioning_change(frame)


# ───────────────────────────── Task 6.3 gate + model ───────────────────────

def test_signal_gate_negative_without_signal() -> None:
    h1 = sign_test(_panel(h1_strength=0.0))
    h2 = positioning_change(_panel())
    ok, reason = _has_signal(h1, h2)
    assert ok is False
    assert "no signal" in reason


def test_signal_gate_positive_with_planted_h1() -> None:
    h1 = sign_test(_panel(h1_strength=0.02))
    h2 = positioning_change(_panel(h1_strength=0.02))
    ok, _reason = _has_signal(h1, h2)
    assert ok is True


def test_fit_gex_model_early_stops_on_inner_holdout() -> None:
    frame = _panel(n_dates=80, n_symbols=30)
    feats = [f for f in FEATURE_COLUMNS if f in frame.columns]
    train, inner = frame.iloc[: len(frame) // 2], frame.iloc[len(frame) // 2:]
    model, best_iter = fit_gex_model(
        train[feats], train["next_day_abs_return"],
        inner[feats], inner["next_day_abs_return"],
        params={"n_estimators": 50, "n_jobs": 1},
        early_stopping_rounds=10,
    )
    assert best_iter >= 0
    pred = model.predict(inner[feats])
    assert np.isfinite(pred).all()


def test_run_model_smoke() -> None:
    # 1400 bdays from 2021-01-04 reaches well into 2026, so the sealed
    # TRAIN/VALID/TEST windows all have rows on the fixed split.
    frame = _panel(n_dates=1400, n_symbols=8)
    res = run_model(
        frame,
        n_walk_forward_folds=2,
        params={"n_estimators": 100, "n_jobs": 1},
    )
    assert res["n_rows"] > 0
    assert "fixed_split" in res
    assert "valid" in res["fixed_split"]
    assert res["walk_forward"]["n_folds"] == 2
    assert len(res["walk_forward"]["folds"]) == 2
    assert "mean_ic" in res["walk_forward"]["folds"][0]["ic"]


def test_run_model_raises_on_too_few_features() -> None:
    frame = _panel().drop(columns=[c for c in FEATURE_COLUMNS if c not in ("gex", "ret_1d")])
    with pytest.raises(MissingDataError):
        run_model(frame)


def test_run_model_passes_embargo_through() -> None:
    """The 5-day-target embargo must not be weakenable by accident.

    The plan's hard rule: next_day_rv_5d needs embargo_days=5. The walk-forward
    fold boundaries must reflect the requested embargo — with a large enough
    embargo the first fold simply cannot form, proving the parameter is wired
    through the splitter rather than hardcoded at 1.
    """
    frame = _panel(n_dates=1400, n_symbols=8)
    with pytest.raises(ValueError, match="embargo_days"):
        run_model(
            frame,
            n_walk_forward_folds=2,
            params={"n_estimators": 50, "n_jobs": 1},
            embargo_days=1300,  # larger than the train window -> ValueError
        )


# ───────────────────────────── Task 6.4 — robustness ───────────────────────

def test_robustness_flip_sign_is_exact_mirror() -> None:
    """Flipping DEALER_SIGN (negating gex) must mirror the IC exactly."""
    frame = _panel(h1_strength=0.01)
    base = sign_test(frame)
    flipped = sign_test(frame.assign(gex=-frame["gex"]))
    assert flipped["ic_neg_gex"]["mean_ic"] == pytest.approx(
        -base["ic_neg_gex"]["mean_ic"]
    )
    # group means swap
    assert flipped["gex_positive"]["mean_abs_return"] == pytest.approx(
        base["gex_negative"]["mean_abs_return"]
    )


def test_robustness_liquidity_filter_blocked_without_data() -> None:
    res = robustness_checks(_panel(), gex_liq=None)
    assert res["liquidity_filter"]["status"] == "blocked"
    assert "gamma" in res["liquidity_filter"]["reason"]


def test_robustness_liquidity_filter_runs_with_gex_liq() -> None:
    frame = _panel(h1_strength=0.01)
    gex_liq = frame[["symbol", "date"]].assign(gex_liq=frame["gex"] * 0.9)
    res = robustness_checks(frame, gex_liq=gex_liq)
    assert res["liquidity_filter"]["gex_positive"]["n"] > 0


def test_robustness_index_vs_stock_split() -> None:
    frame = _panel()
    frame["symbol"] = np.where(
        np.arange(len(frame)) % 10 == 0, "NIFTY", frame["symbol"]
    )
    res = robustness_checks(frame)
    assert res["index_vs_stock"]["indices"] is not None
    assert res["index_vs_stock"]["stocks"] is not None
    assert res["index_vs_stock"]["indices"]["gex_positive"]["n"] > 0


def test_robustness_expiry_week_split() -> None:
    res = robustness_checks(_panel())
    ew = res["expiry_week"]
    assert ew["in_expiry_week"] is not None
    assert ew["outside_expiry_week"] is not None
    # every 5th symbol-day was forced to expiry week, so both sides are non-empty
    assert ew["in_expiry_week"]["gex_positive"]["n"] > 0
    assert ew["outside_expiry_week"]["gex_positive"]["n"] > 0


def test_robustness_expiry_week_blocked_without_column() -> None:
    frame = _panel().drop(columns=["days_to_expiry_30d"])
    res = robustness_checks(frame)
    assert res["expiry_week"]["status"] == "blocked"
