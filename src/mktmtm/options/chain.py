"""Option-chain helpers: strike picking, time-to-expiry (pure math, no I/O).

Ported verbatim from the athena-verified `process_data.py` pipeline
(2026-08-16), with type hints added.
"""

from __future__ import annotations

import math
from datetime import datetime
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
import pytz

__all__ = [
    "pick_strike_nearest_underlying",
    "get_time_to_expiry_in_years",
    "get_option_expiry",
    "unadjusted_spot_from_bhavcopy",
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


@lru_cache(maxsize=200_000)
def get_time_to_expiry_in_years(date_key: str, expiry_str: str) -> float:
    """Converts an expiry date string like '25-Apr-2025' into the fractional
    number of years until expiry from the given as-of date, in India time.

    Memoized: this is a pure function of two date strings, but the callers hit
    it once per *chain element* while a chain holds only a handful of distinct
    expiries (measured: 3 unique expiries across 832 rows). Both enrichment
    engines and ``dealer_exposure.gamma_flip_spot`` walk the chain, so an
    uncached call costs ~32 µs × ~99M calls ≈ 50 min of redundant
    ``strptime``/``localize`` work on a five-year ETL run. The cache is bounded
    at 200k entries — a full run has roughly 1,254 dates × ~40 expiries of
    distinct pairs, so it never evicts in practice, and each entry is two short
    strings plus a float. ``lru_cache`` is thread-safe in CPython, which the
    threaded per-symbol ETL relies on.
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


def unadjusted_spot_from_bhavcopy(
    df_sym: pd.DataFrame,
    fut: pd.DataFrame,
    date_key: str,
    r_dec: float,
) -> tuple[float, str] | None:
    """Spot on the same corporate-action basis as the bhavcopy strikes.

    NSE bhavcopy strikes and settles are *traded* (unadjusted) values, so the
    spot driving ATM-strike selection, IV and Greeks has to be unadjusted too.
    Yahoo cannot supply that: ``quote.close`` is already restated for splits and
    bonuses — only the *dividend* adjustment separates it from ``adjclose`` — so
    before a corporate action it is off by the split factor. Measured against
    the NSE futures close on 2021-08-02: KOTAKBANK 5.05x, NESTLEIND 20.42x,
    RELIANCE 2.19x. That mismatch picks the ATM strike from the wrong region of
    the chain and collapses IV (docs/data/iv-integrity-audit.md).

    Sources, in precedence order:

    1. ``UNDERLYING_PRICE`` — NSE's own underlying value, present in UDiFF files
       (post 2024-07-08) as ``UndrlygPric``.
    2. Near-month futures close discounted by carry, ``S = F * exp(-r*T)`` — the
       legacy era has no underlying column, and the futures close is the only
       unadjusted underlying proxy in the file.

    ``fut`` must hold futures rows only (``FUTSTK``/``FUTIDX``), sorted by
    expiry, with ``EXPIRY_DT`` already parsed to datetimes.

    Returns ``(spot, source)``, or None when neither source yields a usable
    price — callers skip the day rather than fall back to an adjusted price.
    """
    if "UNDERLYING_PRICE" in df_sym.columns:
        vals = pd.to_numeric(df_sym["UNDERLYING_PRICE"], errors="coerce").to_numpy(dtype=float)
        vals = vals[np.isfinite(vals) & (vals > 0)]
        if len(vals):
            return float(vals[0]), "undrlyg"

    if not fut.empty:
        fr = fut.iloc[0]
        try:
            f_close = float(fr["CLOSE"])
        except (TypeError, ValueError):
            return None
        expiry = fr["EXPIRY_DT"]
        if np.isfinite(f_close) and f_close > 0 and pd.notna(expiry):
            t_fut = get_time_to_expiry_in_years(
                date_key, pd.Timestamp(expiry).strftime("%d-%b-%Y")
            )
            return float(f_close * math.exp(-r_dec * max(t_fut, 0.0))), "futures"
    return None


def get_option_expiry(option_row: pd.Series[Any]) -> str | None:
    """Safely extract expiry date from option row."""
    try:
        expiry = option_row["EXPIRY_DT"]
        if not isinstance(expiry, datetime):
            return None
        return expiry.strftime("%d-%b-%Y")
    except (KeyError, AttributeError):
        return None
