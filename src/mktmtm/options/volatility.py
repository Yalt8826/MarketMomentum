"""Yang-Zhang realized volatility (pure math, no I/O).

Ported verbatim from the athena-verified `process_data.py` pipeline
(2026-08-16), with type hints added.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "yang_zhang_volatility",
    "compute_yz_rolling_vol",
]


def yang_zhang_volatility(
    ohlc_df: pd.DataFrame,
    trading_periods: int = 252,
) -> float:
    """Computes the Yang-Zhang volatility for a given set of daily OHLC data.

    Typically this is done over a window (e.g. last 20 or 30 days).
    ohlc_df columns required: 'Open', 'High', 'Low', 'Close'.
    trading_periods is the annualization factor (252 for trading days).
    """
    n = len(ohlc_df)
    if n < 2:
        # Not enough data for the YZ formula
        return float("nan")

    # Convert to logs (this YZ variant consumes Open/Close; High/Low are
    # accepted for schema compatibility but not part of the estimator).
    open_prices = np.log(ohlc_df["Open"].values)
    close_prices = np.log(ohlc_df["Close"].values)

    # Close-to-close variance
    cc_returns = np.diff(close_prices)
    if len(cc_returns) < 1:
        return float("nan")
    sigma_cc_sq = np.var(cc_returns, ddof=1)

    # Open-to-open variance
    oo_returns = np.diff(open_prices)
    sigma_oo_sq = np.var(oo_returns, ddof=1) if len(oo_returns) > 1 else 0.0

    # Overnight returns
    overnight = open_prices[1:] - close_prices[:-1]
    if len(overnight) < 1:
        return float("nan")
    mean_overnight = np.mean(overnight)
    sigma_on_sq = np.sum((overnight - mean_overnight) ** 2) / (n - 1)

    # Weighted factor k
    k = 0.34 / (1.34 + (n + 1) / (n - 1))

    # YZ variance
    yz_variance = sigma_cc_sq + k * sigma_on_sq - (1 - k) * sigma_oo_sq

    # If negative or NaN (e.g. large overnight gaps make the YZ
    # combination unstable), fall back to close-to-close realised vol.
    if not np.isfinite(yz_variance) or yz_variance <= 0:
        if sigma_cc_sq > 0 and np.isfinite(sigma_cc_sq):
            return math.sqrt(sigma_cc_sq * trading_periods)
        return float("nan")

    # Otherwise, annualize
    yz_vol = math.sqrt(yz_variance * trading_periods)
    return yz_vol


def compute_yz_rolling_vol(
    df: pd.DataFrame,
    window: int = 30,
    max_lookback: int = 120,
    trading_periods: int = 252,
) -> pd.Series[Any]:
    """Computes Yang-Zhang volatility with special handling for gaps in the data.

    Looks for 'window' valid trading days within 'max_lookback' calendar days.
    """
    # Sort by date
    df = df.sort_values("Date")

    # Create a lookup dictionary for quick access
    data_by_date: dict[Any, pd.Series[Any]] = {
        row["Date"]: row for _, row in df.iterrows()
    }

    # Calculate volatility for each date
    results: list[dict[str, Any]] = []

    for date in df["Date"].unique():
        # Look back from current date to find window valid trading days
        valid_data: list[pd.Series[Any]] = []
        lookback_days = 0
        current_date = date

        while len(valid_data) < window and lookback_days < max_lookback:
            # Go back one business day
            business_days = pd.date_range(end=current_date, periods=2, freq="B")
            if len(business_days) < 2:
                break

            prev_business_day = business_days[0]

            # Check if we have data for this business day
            if prev_business_day in data_by_date:
                row = data_by_date[prev_business_day]

                # Validate OHLC data
                if (row["Open"] > 0 and row["High"] > 0 and
                        row["Low"] > 0 and row["Close"] > 0):
                    valid_data.append(row)

            current_date = prev_business_day
            lookback_days += 1

        # Calculate YZ volatility if we have enough data
        if len(valid_data) == window:
            valid_df = pd.DataFrame(valid_data).sort_values("Date")
            rv = yang_zhang_volatility(valid_df, trading_periods)
        else:
            rv = float("nan")

        results.append({"Date": date, "RV": rv})

    return pd.DataFrame(results).set_index("Date")["RV"]
