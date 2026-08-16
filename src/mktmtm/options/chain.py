"""Option-chain helpers: strike picking, time-to-expiry (pure math, no I/O).

Ported verbatim from the athena-verified `process_data.py` pipeline
(2026-08-16), with type hints added.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import pytz

__all__ = [
    "pick_strike_nearest_underlying",
    "get_time_to_expiry_in_years",
    "get_option_expiry",
]


def pick_strike_nearest_underlying(
    underlying: float | None,
    options_data: pd.DataFrame,
) -> tuple[
    pd.Series[Any],
    pd.Series[Any],
    pd.Series[Any],
    pd.Series[Any],
    pd.Series[Any],
    pd.Series[Any],
    float,
] | None:
    """Return rows for the strike closest to `underlying` that exists (with both CE & PE)
    in the *monthly* contracts of the next three months.

    Returns
    -------
    (ce30, ce60, ce90, pe30, pe60, pe90, chosen_strike)  or  None
    """
    if underlying is None or not np.isfinite(underlying):
        return None

    df = options_data.copy()
    df["EXPIRY_DT"] = pd.to_datetime(df["EXPIRY_DT"], errors="coerce")
    df = df.dropna(subset=["EXPIRY_DT"])
    if df.empty:
        return None

    # pick the *latest* expiry in each month (monthly contract)
    monthly_expiries = (
        df.groupby(df["EXPIRY_DT"].dt.to_period("M"))["EXPIRY_DT"]
        .max()
        .sort_values()
    )
    if len(monthly_expiries) < 3:
        return None

    exp30, exp60, exp90 = monthly_expiries.iloc[:3].values
    buckets = {exp: df[df["EXPIRY_DT"] == exp] for exp in (exp30, exp60, exp90)}

    # strikes present in *all* three buckets with both CE & PE
    common_strikes = (
        set(buckets[exp30]["STRIKE_PR"])
        & set(buckets[exp60]["STRIKE_PR"])
        & set(buckets[exp90]["STRIKE_PR"])
    )
    if not common_strikes:
        return None

    # choose the strike nearest to the underlying price
    chosen_strike = min(common_strikes, key=lambda k: abs(float(k) - underlying))

    def _rows(
        bucket: pd.DataFrame,
    ) -> tuple[pd.Series[Any] | None, pd.Series[Any] | None]:
        ce = bucket[(bucket["STRIKE_PR"] == chosen_strike) & (bucket["OPTION_TYP"] == "CE")]
        pe = bucket[(bucket["STRIKE_PR"] == chosen_strike) & (bucket["OPTION_TYP"] == "PE")]
        return (ce.iloc[0] if not ce.empty else None,
                pe.iloc[0] if not pe.empty else None)

    ce30, pe30 = _rows(buckets[exp30])
    ce60, pe60 = _rows(buckets[exp60])
    ce90, pe90 = _rows(buckets[exp90])

    # ensure all six rows are present
    if any(x is None for x in (ce30, pe30, ce60, pe60, ce90, pe90)):
        return None

    # mypy can't narrow tuple elements through `any`; the guard above
    # already guarantees every row is a Series.
    assert ce30 is not None and pe30 is not None
    assert ce60 is not None and pe60 is not None
    assert ce90 is not None and pe90 is not None

    return ce30, ce60, ce90, pe30, pe60, pe90, float(chosen_strike)


def get_time_to_expiry_in_years(date_key: str, expiry_str: str) -> float:
    """Converts an expiry date string like '25-Apr-2025' into the fractional
    number of years until expiry from the given as-of date, in India time.
    """
    india_tz = pytz.timezone("Asia/Kolkata")
    expiry_naive = datetime.strptime(expiry_str, "%d-%b-%Y")
    expiry_date = india_tz.localize(expiry_naive)

    # Convert date_key (string) to datetime
    as_of_naive = datetime.strptime(date_key, "%d-%b-%Y")
    as_of_date = india_tz.localize(as_of_naive)

    # time_diff = expiry_date - as_of_date
    time_diff = expiry_date - as_of_date

    days_to_expiry = time_diff.days + time_diff.seconds / 86400.0
    days_to_expiry = max(0, days_to_expiry)  # clamp negative to zero

    # Approx with days in a year:
    return days_to_expiry / 365.0


def get_option_expiry(option_row: pd.Series[Any]) -> str | None:
    """Safely extract expiry date from option row."""
    try:
        expiry = option_row["EXPIRY_DT"]
        if not isinstance(expiry, datetime):
            return None
        return expiry.strftime("%d-%b-%Y")
    except (KeyError, AttributeError):
        return None
