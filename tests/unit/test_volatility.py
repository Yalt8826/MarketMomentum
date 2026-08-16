"""Unit tests for mktmtm.options.volatility (Yang-Zhang realised vol)."""

import numpy as np
import pandas as pd
import pytest

from mktmtm.options.volatility import compute_yz_rolling_vol, yang_zhang_volatility


def _synthetic_ohlc(seed: int, n: int) -> pd.DataFrame:
    """Deterministic synthetic daily OHLC frame (log-normal walk + noise)."""
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n)))
    open_ = close * np.exp(rng.normal(0.0, 0.002, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, 0.003, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, 0.003, n)))
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close})


def test_yang_zhang_on_synthetic_ohlc_matches_known_value() -> None:
    """YZ on a deterministic 30-day frame reproduces the reference value."""
    df = _synthetic_ohlc(seed=42, n=30)
    yz = yang_zhang_volatility(df)
    # Anchor computed from the original athena-verified implementation.
    assert yz == pytest.approx(0.06305067735540197, rel=1e-9)


def test_yang_zhang_returns_nan_for_too_few_rows() -> None:
    assert np.isnan(yang_zhang_volatility(pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0]}
    )))


def test_yz_rolling_vol_shape_and_known_tail() -> None:
    """Rolling YZ: business-day index, warm-up NaNs, known final value."""
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2025-01-02", periods=40)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, 40)))
    open_ = close * np.exp(rng.normal(0.0, 0.002, 40))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, 0.003, 40)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, 0.003, 40)))
    df = pd.DataFrame(
        {"Date": dates, "Open": open_, "High": high, "Low": low, "Close": close}
    )

    rv = compute_yz_rolling_vol(df, window=20, max_lookback=60, trading_periods=252)

    assert isinstance(rv, pd.Series)
    assert len(rv) == 40
    assert rv.index.name == "Date"
    assert np.isnan(rv.iloc[0])  # warm-up: not enough lookback yet
    assert rv.iloc[-1] == pytest.approx(0.08258510595762435, rel=1e-9)
    assert int(np.isfinite(rv.values).sum()) == 20
