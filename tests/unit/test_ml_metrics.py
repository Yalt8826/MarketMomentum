"""Unit tests for mktmtm.ml.metrics — rank IC, ICIR, decile spread, summarize."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.metrics import daily_rank_ic, decile_spread, icir, summarize


def _panel(n_dates: int = 60, n_symbols: int = 50, seed: int = 0) -> pd.DataFrame:
    """Synthetic cross-sectional panel: (date, symbol) rows with a return."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-01-01", periods=n_dates)
    ret = rng.normal(0.0, 0.02, n_dates * n_symbols)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(dates.values, n_symbols)),
            "symbol": [f"S{i}" for i in np.tile(np.arange(n_symbols), n_dates)],
            "next_day_return": ret,
            "next_day_direction": (ret > 0.0).astype(int),
        }
    )


def _perfect_signal(df: pd.DataFrame) -> pd.DataFrame:
    """Prediction that ranks names exactly as their return does, per date."""
    out = df.copy()
    out["pred"] = out.groupby("date")["next_day_return"].rank(pct=True)
    return out


def test_perfect_predictions_give_ic_one() -> None:
    df = _perfect_signal(_panel())
    ic = daily_rank_ic(df)
    assert len(ic) == df["date"].nunique()
    assert np.allclose(ic, 1.0, atol=1e-12)


def test_reversed_predictions_give_ic_minus_one() -> None:
    df = _perfect_signal(_panel())
    df["pred"] = -df["pred"]
    ic = daily_rank_ic(df)
    assert len(ic) == df["date"].nunique()
    assert np.allclose(ic, -1.0, atol=1e-12)


def test_random_predictions_ic_close_to_zero() -> None:
    df = _panel(n_dates=500, n_symbols=30, seed=42)
    rng = np.random.default_rng(1)
    df["pred"] = rng.random(len(df))
    ic = daily_rank_ic(df)
    assert len(ic) == 500
    assert abs(float(ic.mean())) < 0.02


def test_icir_is_mean_over_std() -> None:
    ic = pd.Series([0.05, 0.06, 0.04, 0.05, 0.07])
    assert icir(ic) == pytest.approx(float(ic.mean() / ic.std(ddof=1)))


def test_icir_degenerate_series_is_zero() -> None:
    assert icir(pd.Series([0.03])) == 0.0  # single day: std undefined
    assert icir(pd.Series([0.03, 0.03, 0.03])) == 0.0  # zero variance
    assert icir(pd.Series([np.nan, np.nan])) == 0.0  # all missing


def test_decile_spread_positive_on_monotone_signal() -> None:
    df = _perfect_signal(_panel(n_dates=40, n_symbols=100))
    spread = decile_spread(df)
    assert len(spread) == df["date"].nunique()
    assert (spread > 0.0).all()


def test_decile_spread_reversed_signal_is_negative() -> None:
    df = _perfect_signal(_panel(n_dates=40, n_symbols=100))
    df["pred"] = -df["pred"]
    spread = decile_spread(df)
    assert (spread < 0.0).all()


def test_summarize_returns_all_keys_without_nan() -> None:
    df = _perfect_signal(_panel(n_dates=120, n_symbols=40, seed=7))
    out = summarize(df)
    assert set(out) == {
        "auc",
        "accuracy",
        "mean_ic",
        "ic_std",
        "icir",
        "ic_t_stat",
        "mean_spread",
        "hit_rate",
    }
    assert all(np.isfinite(v) for v in out.values())


def test_summarize_sane_on_perfect_signal() -> None:
    df = _panel(n_dates=120, n_symbols=40, seed=7)
    df["pred"] = df["next_day_return"]  # the realized return itself: perfect score
    out = summarize(df)
    assert out["mean_ic"] == pytest.approx(1.0)
    assert out["mean_spread"] > 0.0
    assert out["hit_rate"] == pytest.approx(1.0)
    assert out["auc"] == pytest.approx(1.0)


def test_summarize_no_nan_on_degenerate_input() -> None:
    # A single date with one symbol: no pairwise correlation is computable.
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-05"]),
            "symbol": ["S0"],
            "next_day_return": [0.01],
            "next_day_direction": [1],
            "pred": [0.6],
        }
    )
    out = summarize(df)
    assert set(out) == {
        "auc",
        "accuracy",
        "mean_ic",
        "ic_std",
        "icir",
        "ic_t_stat",
        "mean_spread",
        "hit_rate",
    }
    assert all(np.isfinite(v) for v in out.values())
