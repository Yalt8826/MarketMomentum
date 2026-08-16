"""Unit tests for mktmtm.options.chain (strike picking, TTE, expiry)."""

import numpy as np
import pandas as pd
import pytest

from mktmtm.options.chain import (
    get_option_expiry,
    get_time_to_expiry_in_years,
    pick_strike_nearest_underlying,
)


def _three_expiry_chain() -> pd.DataFrame:
    """Synthetic chain: 3 monthly expiries, 3 strikes, CE+PE rows each."""
    expiries = ["2025-04-24", "2025-05-29", "2025-06-26"]
    strikes = [24000, 24500, 25000]
    rows = []
    for exp in expiries:
        for strike in strikes:
            for typ in ("CE", "PE"):
                rows.append(
                    {
                        "EXPIRY_DT": exp,
                        "STRIKE_PR": strike,
                        "OPTION_TYP": typ,
                        "CLOSE": 100.0,
                    }
                )
    return pd.DataFrame(rows)


def test_pick_strike_nearest_underlying_returns_nearest_common_strike() -> None:
    """3-expiry synthetic chain: nearest common strike to the underlying wins."""
    df = _three_expiry_chain()

    result = pick_strike_nearest_underlying(underlying=25100.0, options_data=df)

    assert result is not None
    ce30, ce60, ce90, pe30, pe60, pe90, chosen_strike = result
    assert chosen_strike == pytest.approx(25000.0)
    for row in (ce30, ce60, ce90):
        assert row["OPTION_TYP"] == "CE"
        assert row["STRIKE_PR"] == 25000
    for row in (pe30, pe60, pe90):
        assert row["OPTION_TYP"] == "PE"
        assert row["STRIKE_PR"] == 25000


def test_pick_strike_returns_none_for_nan_underlying() -> None:
    df = _three_expiry_chain()
    assert pick_strike_nearest_underlying(float("nan"), df) is None
    assert pick_strike_nearest_underlying(None, df) is None


def test_pick_strike_returns_none_when_fewer_than_three_expiries() -> None:
    df = _three_expiry_chain()
    df = df[df["EXPIRY_DT"] != "2025-06-26"]
    assert pick_strike_nearest_underlying(25100.0, df) is None


def test_time_to_expiry_12_aug_to_28_oct() -> None:
    """12-Aug -> 28-Oct is 77 days => 77/365 = 0.2110 years."""
    tte = get_time_to_expiry_in_years("12-Aug-2025", "28-Oct-2025")
    assert tte == pytest.approx(0.211, abs=0.001)


def test_time_to_expiry_clamps_negative_to_zero() -> None:
    assert get_time_to_expiry_in_years("28-Oct-2025", "12-Aug-2025") == 0.0


def test_get_option_expiry_formats_timestamp() -> None:
    row = pd.Series({"EXPIRY_DT": pd.Timestamp("2025-10-28")})
    assert get_option_expiry(row) == "28-Oct-2025"


def test_get_option_expiry_returns_none_when_missing() -> None:
    assert get_option_expiry(pd.Series({"SYMBOL": "NIFTY"})) is None
    assert get_option_expiry(pd.Series({"EXPIRY_DT": np.nan})) is None
