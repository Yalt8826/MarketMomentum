"""Unit tests for mktmtm.ml.dataset — targets + derived features (pure pandas, no DB)."""

from __future__ import annotations

import pandas as pd
import pytest

from mktmtm.ml.dataset import (
    _add_derived,
    _add_targets,
    _lift_ce_pe,
    _ratio,
    screen_corporate_actions,
    validate_no_lookahead,
)


def _frame() -> pd.DataFrame:
    """Two symbols with interleaved dates; A: 100->101->99, B: 50->51."""
    return pd.DataFrame(
        {
            "symbol": ["A", "B", "A", "B", "A"],
            "date": pd.to_datetime(
                ["2026-08-11", "2026-08-12", "2026-08-12", "2026-08-13", "2026-08-13"]
            ),
            "underlying_price": [100.0, 50.0, 101.0, 51.0, 99.0],
            "ce_iv_30": [10.0, 11.0, 11.0, 12.0, 12.0],
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


# ───────────────── regression: look-ahead + corporate actions ─────────────────
# Guards for the defects found in docs/data/ml-dataset-report.md §2/§3.


def _with_derived_inputs(df: pd.DataFrame) -> pd.DataFrame:
    """Pad a price-only frame with the columns _add_derived expects."""
    n = len(df)
    out = df.copy()
    for col, val in (
        ("rv_yz", 0.10), ("ce_iv_30", 10.0), ("ce_iv_90", 12.0), ("pe_iv_30", 10.5),
        ("chain_pe_vol", 50.0), ("chain_ce_vol", 100.0),
        ("chain_pe_oi", 500.0), ("chain_ce_oi", 1000.0), ("otm_put_iv", 12.0),
    ):
        out[col] = [val] * n
    out["expiry_30d"] = pd.to_datetime(["2026-09-10"] * n)
    out["upcoming_earning_date"] = pd.to_datetime([pd.NaT] * n)
    return out


def _unsorted_frame() -> pd.DataFrame:
    """One symbol, rows deliberately in reverse-chronological order.

    This is the shape Postgres returned without an ORDER BY, and it is what made
    ret_1d come out as the negated *forward* return.
    """
    return pd.DataFrame(
        {
            "symbol": ["A"] * 4,
            "date": pd.to_datetime(
                ["2026-08-14", "2026-08-13", "2026-08-12", "2026-08-11"]
            ),
            "underlying_price": [103.0, 99.0, 101.0, 100.0],
        }
    )


def test_derived_returns_are_backward_even_if_rows_arrive_unsorted() -> None:
    """_add_derived must sort first; otherwise ret_1d encodes tomorrow."""
    derived = _add_derived(_with_derived_inputs(_unsorted_frame()))
    out = derived.sort_values("date").reset_index(drop=True)
    assert pd.isna(out.loc[0, "ret_1d"])  # first day has no yesterday
    assert out.loc[1, "ret_1d"] == pytest.approx(101.0 / 100.0 - 1.0)
    assert out.loc[2, "ret_1d"] == pytest.approx(99.0 / 101.0 - 1.0)
    assert out.loc[3, "ret_1d"] == pytest.approx(103.0 / 99.0 - 1.0)


def test_validate_no_lookahead_accepts_a_clean_frame() -> None:
    df = _add_targets(_add_derived(_with_derived_inputs(_unsorted_frame())))
    validate_no_lookahead(df)  # must not raise


def test_validate_no_lookahead_catches_reversed_returns() -> None:
    """The exact corruption seen in production: ret_1d = P(t)/P(t+1) - 1."""
    df = _add_targets(_add_derived(_with_derived_inputs(_unsorted_frame())))
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["ret_1d"] = df["underlying_price"] / df["underlying_price"].shift(-1) - 1.0
    with pytest.raises(ValueError, match="look-ahead check FAILED"):
        validate_no_lookahead(df)


def _split_frame() -> pd.DataFrame:
    """A clean run, then a 2:1 split between 08-13 and 08-14."""
    return pd.DataFrame(
        {
            "symbol": ["A"] * 5,
            "date": pd.to_datetime(
                ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14", "2026-08-17"]
            ),
            "underlying_price": [100.0, 101.0, 102.0, 51.0, 52.0],
        }
    )


def test_corporate_action_screen_blanks_the_jump_and_its_label() -> None:
    df = _add_targets(_add_derived(_with_derived_inputs(_split_frame())))
    out = screen_corporate_actions(df).sort_values("date").reset_index(drop=True)

    # the -50% "return" itself is gone
    assert pd.isna(out.loc[3, "ret_1d"])
    # so is the label on the row before it, which would otherwise "predict" the split
    assert pd.isna(out.loc[2, "next_day_return"])
    assert pd.isna(out.loc[2, "next_day_direction"])
    # and every Phase-5 volatility target on that row (same label semantics)
    assert pd.isna(out.loc[2, "next_day_abs_return"])
    assert pd.isna(out.loc[2, "next_day_iv_chg"])
    assert pd.isna(out.loc[2, "next_day_vrp"])
    # untouched rows survive
    assert out.loc[1, "ret_1d"] == pytest.approx(101.0 / 100.0 - 1.0)
    assert out.loc[1, "next_day_direction"] == 1


def test_corporate_action_screen_clears_the_whole_lookback_window() -> None:
    """A split contaminates ret_5d for the following 5 rows, not just the jump."""
    df = _add_targets(_add_derived(_with_derived_inputs(_split_frame())))
    out = screen_corporate_actions(df).sort_values("date").reset_index(drop=True)
    assert out.loc[3:, "ret_5d"].isna().all()


def test_corporate_action_screen_is_a_noop_on_clean_data() -> None:
    df = _add_targets(_add_derived(_with_derived_inputs(_frame())))
    out = screen_corporate_actions(df)
    assert out["ret_1d"].notna().sum() == df["ret_1d"].notna().sum()
    assert out["next_day_return"].notna().sum() == df["next_day_return"].notna().sum()


# ───────────────────────── Phase 5: volatility targets ─────────────────────────
# next_day_abs_return / next_day_rv_5d / next_day_iv_chg / next_day_vrp:
# forward per-symbol windows, adjusted basis, NULL on each symbol's last day(s).


def _long_frame() -> pd.DataFrame:
    """One symbol, 8 rising days at +1% — enough for a full 5-day RV window."""
    return pd.DataFrame(
        {
            "symbol": ["A"] * 8,
            "date": pd.to_datetime([f"2026-08-{11 + i:02d}" for i in range(8)]),
            "underlying_price": [100.0 * (1.01**i) for i in range(8)],
            "ce_iv_30": [10.0] * 8,
        }
    )


def test_volatility_targets_shift_within_symbol() -> None:
    df = _add_targets(_frame())
    a = df[df["symbol"] == "A"].sort_values("date").reset_index(drop=True)
    daily_iv = 10.0 / (100.0 * (252.0**0.5))
    assert a.loc[0, "next_day_abs_return"] == pytest.approx(101.0 / 100.0 - 1.0)
    assert a.loc[1, "next_day_abs_return"] == pytest.approx(abs(99.0 / 101.0 - 1.0))
    assert pd.isna(a.loc[2, "next_day_abs_return"])  # most recent day: no target
    assert a.loc[0, "next_day_iv_chg"] == pytest.approx(1.0)  # ce_iv_30 10 -> 11
    assert a.loc[1, "next_day_iv_chg"] == pytest.approx(1.0)  # 11 -> 12
    assert pd.isna(a.loc[2, "next_day_iv_chg"])
    assert a.loc[0, "next_day_vrp"] == pytest.approx(0.01 - daily_iv)
    assert pd.isna(a.loc[2, "next_day_vrp"])
    b = df[df["symbol"] == "B"].sort_values("date").reset_index(drop=True)
    assert b.loc[0, "next_day_abs_return"] == pytest.approx(51.0 / 50.0 - 1.0)
    assert pd.isna(b.loc[1, "next_day_abs_return"])


def test_rv_5d_forward_window_and_five_trailing_nulls() -> None:
    out = _add_targets(_long_frame()).sort_values("date").reset_index(drop=True)
    # 8 rows of +1%: rows 0..2 see 5 forward returns, rows 3..7 do not — so the
    # symbol's last FIVE rows are NULL, not one (hard rule 5 for a 5-day window).
    assert out.loc[0:2, "next_day_rv_5d"].notna().all()
    assert out.loc[3:, "next_day_rv_5d"].isna().all()
    # constant returns -> zero realized vol
    assert out.loc[0, "next_day_rv_5d"] == pytest.approx(0.0, abs=1e-12)


def test_rv_5d_value_matches_close_to_close_std() -> None:
    prices = [100.0, 110.0, 99.0, 108.0, 101.0, 107.0, 102.0, 106.0]
    df = pd.DataFrame(
        {
            "symbol": ["A"] * 8,
            "date": pd.to_datetime([f"2026-08-{11 + i:02d}" for i in range(8)]),
            "underlying_price": prices,
            "ce_iv_30": [10.0] * 8,
        }
    )
    out = _add_targets(df).sort_values("date").reset_index(drop=True)
    ret = pd.Series(prices).pct_change()
    expected = ret.iloc[1:6].std(ddof=1) * (252.0**0.5)  # r_1..r_5, annualized
    assert out.loc[0, "next_day_rv_5d"] == pytest.approx(expected)
    assert out.loc[3:, "next_day_rv_5d"].isna().all()


def test_rv_5d_does_not_leak_across_symbols() -> None:
    """Interleaving another symbol must not pad or shorten the 5-day window."""
    both = pd.concat(
        [_long_frame(), _long_frame().assign(symbol="B")], ignore_index=True
    ).sample(frac=1.0, random_state=0)
    out = _add_targets(both).sort_values(["symbol", "date"]).reset_index(drop=True)
    for sym in ("A", "B"):
        sub = out[out["symbol"] == sym]
        assert sub["next_day_rv_5d"].iloc[0:3].notna().all()
        assert sub["next_day_rv_5d"].iloc[3:].isna().all()


def test_validate_no_lookahead_accepts_volatility_targets() -> None:
    df = _add_targets(_add_derived(_with_derived_inputs(_long_frame())))
    validate_no_lookahead(df)  # must not raise


def test_validate_catches_shifted_rv_5d() -> None:
    """The exit test: a window walked one row too far must fail the build."""
    df = _add_targets(_add_derived(_with_derived_inputs(_long_frame())))
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["next_day_rv_5d"] = df["next_day_rv_5d"].shift(1)
    with pytest.raises(ValueError, match="look-ahead check FAILED"):
        validate_no_lookahead(df)


def test_validate_catches_shifted_abs_return() -> None:
    df = _add_targets(_add_derived(_with_derived_inputs(_frame())))
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["next_day_abs_return"] = df["next_day_abs_return"].shift(-1)
    with pytest.raises(ValueError, match="look-ahead check FAILED"):
        validate_no_lookahead(df)


def test_validate_catches_aliased_vrp() -> None:
    df = _add_targets(_add_derived(_with_derived_inputs(_frame())))
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["next_day_vrp"] = df["next_day_vrp"] * 0.5  # not realized-minus-implied
    with pytest.raises(ValueError, match="look-ahead check FAILED"):
        validate_no_lookahead(df)


def test_corporate_action_screen_blanks_forward_rv_window() -> None:
    """A jump at row i blanks next_day_rv_5d for every row whose window contains it."""
    prices = [100.0] * 4 + [50.0] + [51.0, 52.0, 53.0, 54.0]  # jump at index 4
    df = pd.DataFrame(
        {
            "symbol": ["A"] * 9,
            "date": pd.to_datetime([f"2026-08-{11 + i:02d}" for i in range(9)]),
            "underlying_price": prices,
            "ce_iv_30": [10.0] * 9,
        }
    )
    out = screen_corporate_actions(
        _add_targets(_add_derived(_with_derived_inputs(df)))
    ).sort_values("date").reset_index(drop=True)
    # rows 0..3 have forward windows containing the 50% jump (rows 4..8 are the
    # last five — already NULL by construction). Without the forward-window
    # blanking, rows 0..2 would carry a ~40-sigma vol observation.
    assert out.loc[0:3, "next_day_rv_5d"].isna().all()
