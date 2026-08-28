"""Unit tests for mktmtm.ml.dataset — targets + derived features (pure pandas, no DB)."""

from __future__ import annotations

import pandas as pd
import pytest

from mktmtm.ml.dataset import _add_derived, _add_targets, _lift_ce_pe, _ratio


def _frame() -> pd.DataFrame:
    """Two symbols with interleaved dates; A: 100->101->99, B: 50->51."""
    return pd.DataFrame(
        {
            "symbol": ["A", "B", "A", "B", "A"],
            "date": pd.to_datetime(
                ["2026-08-11", "2026-08-12", "2026-08-12", "2026-08-13", "2026-08-13"]
            ),
            "underlying_price": [100.0, 50.0, 101.0, 51.0, 99.0],
        }
    )


def test_targets_shift_within_symbol() -> None:
    df = _add_targets(_frame())
    a = df[df["symbol"] == "A"].sort_values("date").reset_index(drop=True)
    assert a.loc[0, "next_day_return"] == pytest.approx(101.0 / 100.0 - 1.0)
    assert a.loc[1, "next_day_return"] == pytest.approx(99.0 / 101.0 - 1.0)
    assert pd.isna(a.loc[2, "next_day_return"])  # most recent day: no target
    assert a.loc[0, "next_day_direction"] == 1
    assert a.loc[1, "next_day_direction"] == 0
    assert pd.isna(a.loc[2, "next_day_direction"])


def test_targets_do_not_leak_across_symbols() -> None:
    """Interleaved dates must not borrow the *other* symbol's close."""
    df = _add_targets(_frame())
    a = df[df["symbol"] == "A"].sort_values("date").reset_index(drop=True)
    assert pd.isna(a.loc[2, "next_day_return"])  # 13 Aug A -> NULL, never B's 51.0
    b = df[df["symbol"] == "B"].sort_values("date").reset_index(drop=True)
    assert b.loc[0, "next_day_return"] == pytest.approx(51.0 / 50.0 - 1.0)
    assert pd.isna(b.loc[1, "next_day_return"])


def test_lift_ce_pe_flattens_jsonb() -> None:
    df = pd.DataFrame(
        {
            "symbol": ["A"],
            "ce": [
                {
                    "iv_30": 10.5,
                    "ivp": 60.0,
                    "ivr": 57.7,
                    "close": 218.1,
                    "volume": 100,
                    "oi": 12345,
                    "greeks": {
                        "delta": 0.568,
                        "gamma": 0.0009,
                        "theta": -10.9,
                        "vega": 16.6,
                        "rho": 4.1,
                    },
                }
            ],
            "pe": [{"iv_30": 11.0, "greeks": {"delta": -0.44}}],
        }
    )
    out = _lift_ce_pe(df)
    assert out["ce_iv_30"].iloc[0] == 10.5
    assert out["ce_delta"].iloc[0] == pytest.approx(0.568)
    assert out["ce_gamma"].iloc[0] == pytest.approx(0.0009)
    assert out["ce_oi"].iloc[0] == 12345.0
    assert out["pe_iv_30"].iloc[0] == 11.0
    assert out["pe_delta"].iloc[0] == pytest.approx(-0.44)
    assert pd.isna(out["pe_close"].iloc[0])  # missing key -> None


def test_lift_ce_pe_handles_empty_blocks() -> None:
    out = _lift_ce_pe(pd.DataFrame({"symbol": ["A"], "ce": [{}], "pe": [None]}))
    assert pd.isna(out["ce_iv_30"].iloc[0])
    assert pd.isna(out["pe_iv_30"].iloc[0])


def test_ratio_guards_zero_denominator() -> None:
    r = _ratio(pd.Series([2.0, 0.0]), pd.Series([1.0, 0.0]))
    assert r.iloc[0] == pytest.approx(2.0)
    assert pd.isna(r.iloc[1])


def test_derived_features() -> None:
    df = pd.DataFrame(
        {
            "symbol": ["A", "A"],
            "date": pd.to_datetime(["2026-08-11", "2026-08-12"]),
            "underlying_price": [100.0, 101.0],
            "rv_yz": [0.10, 0.12],
            "ce_iv_30": [10.0, 11.0],
            "ce_iv_90": [12.0, 13.0],
            "pe_iv_30": [10.5, 11.5],
            "expiry_30d": pd.to_datetime(["2026-09-10", "2026-09-10"]),
            "upcoming_earning_date": pd.to_datetime(["2026-08-20", pd.NaT]),
            "chain_pe_vol": [50.0, 60.0],
            "chain_ce_vol": [100.0, 120.0],
            "chain_pe_oi": [500.0, 600.0],
            "chain_ce_oi": [1000.0, 1200.0],
            "otm_put_iv": [12.0, 13.0],
        }
    )
    out = _add_derived(df)
    assert pd.isna(out["ret_1d"].iloc[0])
    assert out["ret_1d"].iloc[1] == pytest.approx(0.01)
    assert out["iv_rv_spread"].iloc[0] == pytest.approx(0.0)  # 10.0 - 0.10*100
    assert out["term_slope"].iloc[0] == pytest.approx(2.0)
    assert out["put_call_iv_spread"].iloc[0] == pytest.approx(0.5)
    assert out["days_to_expiry_30d"].iloc[0] == 30
    assert out["days_to_earnings"].iloc[0] == 9
    assert pd.isna(out["days_to_earnings"].iloc[1])
    assert out["put_call_vol_ratio"].iloc[0] == pytest.approx(0.5)
    assert out["put_call_oi_ratio"].iloc[0] == pytest.approx(0.5)
    assert out["otm_put_iv_premium"].iloc[0] == pytest.approx(1.5)  # 12.0 - 10.5
