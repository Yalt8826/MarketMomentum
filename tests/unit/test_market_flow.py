"""Unit tests for mktmtm.ml.market_flow — all pure, no database.

The load-bearing test here is `test_expanding_normalize_has_no_lookahead`.
Everything else in this module is arithmetic that a wrong answer would make
obvious; a leaking normalizer produces numbers that look completely normal and
inflate every downstream metric.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.market_flow import (
    CALENDAR_FEATURES,
    CORE_FEATURES,
    DIRECTION_FEATURES,
    aggregate_symbol_day,
    build_targets,
    expanding_rank_normalize,
    score_static_baselines,
)

DATES = pd.date_range("2024-01-01", periods=5, freq="B")


def _panel(n_symbols: int = 4) -> pd.DataFrame:
    """A small per-symbol panel with every column aggregate_symbol_day reads."""
    rows = []
    for d_i, day in enumerate(DATES):
        for s_i in range(n_symbols):
            rows.append(
                {
                    "symbol": f"SYM{s_i}",
                    "date": day,
                    "fut_basis_excess": float(s_i - 1 + d_i),
                    "fut_basis_pct": 0.01 * (s_i + 1),
                    "fut_roll_ratio": 0.1 * (d_i + 1),
                    "fut_dte": 20 - d_i,
                    "fut_oi_chg_notional": 1000.0 * (s_i + 1),
                    "fut_oi_notional": 50_000.0,
                    "oi_outlier_count": 1 if s_i == 0 else 0,
                    "oi_outlier_call_frac": 1.0 if s_i == 0 else np.nan,
                    "oi_outlier_moneyness": 0.05 if s_i == 0 else np.nan,
                    "oi_outlier_dte": 15.0 if s_i == 0 else np.nan,
                    "oi_outlier_build_notional": 900.0 if s_i == 0 else 0.0,
                    "oi_outlier_unwind_notional": 100.0 if s_i == 0 else 0.0,
                    "q_fut_direction": 0.2,
                    "opt_direction": -0.1,
                    "oi_outlier_direction": -1.0 if s_i == 0 else np.nan,
                    "ce_writer_frac": 0.6,
                    "gex": 1e9,
                    "put_call_oi_ratio": 0.9,
                    "ce_iv_30": 25.0,
                    "chain_oi_chg": 5000.0,
                    "rv_yz": 18.0,
                    "chain_breadth": 120.0,
                    "put_call_iv_spread": 1.5,
                    "dist_to_flip": 0.03,
                }
            )
    return pd.DataFrame(rows)


# ── aggregation ─────────────────────────────────────────────────────────────


def test_aggregate_uses_median_for_ratios() -> None:
    """One extreme symbol must not move a ratio aggregate.

    The cross-section contains outliers by construction — the outlier detector
    exists to find them — so a mean here would let a single name set the
    market-level reading.
    """
    panel = _panel()
    clean = aggregate_symbol_day(panel)

    spiked = panel.copy()
    mask = (spiked["symbol"] == "SYM0")
    spiked.loc[mask, "fut_basis_excess"] = 10_000.0
    moved = aggregate_symbol_day(spiked)

    assert moved["basis_excess_med"].equals(clean["basis_excess_med"]) or (
        (moved["basis_excess_med"] - clean["basis_excess_med"]).abs().max() < 2.0
    )


def test_notional_columns_are_summed() -> None:
    """Notionals are additive across symbols; sums, not medians."""
    out = aggregate_symbol_day(_panel(n_symbols=4))
    # 1000 + 2000 + 3000 + 4000 per date
    assert out["fut_oi_chg_notional_sum"].iloc[0] == pytest.approx(10_000.0)


def test_outlier_rate_is_the_share_of_symbols() -> None:
    out = aggregate_symbol_day(_panel(n_symbols=4))
    assert out["outlier_rate"].iloc[0] == pytest.approx(0.25)


def test_weighted_mean_ignores_symbols_with_no_flagged_notional() -> None:
    """Only flagged symbols carry weight; the rest must not dilute toward zero."""
    out = aggregate_symbol_day(_panel())
    assert out["outlier_call_frac"].iloc[0] == pytest.approx(1.0)


def test_d_roll_is_differenced_after_aggregation() -> None:
    """The market roll ratio is differenced at market level, not per symbol.

    Differencing per symbol and averaging answers a different question and
    silently drops every symbol that entered the panel that day.
    """
    out = aggregate_symbol_day(_panel())
    assert pd.isna(out["d_roll"].iloc[0])
    assert out["d_roll"].iloc[1] == pytest.approx(0.1)


def test_day_of_week_dummies_drop_monday() -> None:
    out = aggregate_symbol_day(_panel())
    assert {"dow_1", "dow_2", "dow_3", "dow_4"} <= set(out.columns)
    assert "dow_0" not in out.columns


# ── targets ─────────────────────────────────────────────────────────────────


def _participants() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": list(DATES) * 2,
            "client_type": ["Pro"] * 5 + ["Client"] * 5,
            "d_net_opt_stock_call": [10.0, -5.0, 20.0, -1.0, 7.0,
                                     -10.0, 5.0, -20.0, 1.0, -7.0],
        }
    )


def test_label_is_next_day_not_same_day() -> None:
    """y(t) is built from the flow observed at t+1, not at t."""
    out = build_targets(_participants(), participant="Pro")
    # Pro flows are [10, -5, 20, -1, 7]; y(t) = sign(flow(t+1)).
    assert list(out["y"].iloc[:4]) == [0.0, 1.0, 0.0, 1.0]


def test_last_date_has_null_label() -> None:
    """No tomorrow means no label — an inference row, never a training row."""
    out = build_targets(_participants(), participant="Pro")
    assert pd.isna(out["y"].iloc[-1])
    assert pd.isna(out["y_magnitude"].iloc[-1])


def test_persistence_is_todays_flow() -> None:
    """The persistence baseline gets what is legitimately known at t."""
    out = build_targets(_participants(), participant="Pro")
    assert list(out["persistence"]) == [10.0, -5.0, 20.0, -1.0, 7.0]


def test_unknown_participant_raises() -> None:
    with pytest.raises(ValueError, match="no rows for client_type"):
        build_targets(_participants(), participant="NOBODY")


# ── normalization ───────────────────────────────────────────────────────────


def test_expanding_normalize_has_no_lookahead() -> None:
    """Appending future rows must leave every earlier value bit-identical.

    This is what "no look-ahead" means operationally. A global scaler, or a
    whole-sample rank, fails it immediately — and produces numbers that look
    entirely reasonable while inflating every downstream metric.
    """
    rng = np.random.default_rng(0)
    frame = pd.DataFrame({"x": rng.normal(size=400)})
    first = expanding_rank_normalize(frame, ["x"], min_obs=50)

    extended = pd.concat(
        [frame, pd.DataFrame({"x": rng.normal(size=200) + 50.0})], ignore_index=True
    )
    second = expanding_rank_normalize(extended, ["x"], min_obs=50)

    np.testing.assert_array_equal(
        first["x"].to_numpy(), second["x"].to_numpy()[: len(frame)]
    )


def test_normalize_is_null_before_min_obs() -> None:
    """Warm-up rows are NaN, not a neutral 0.5."""
    frame = pd.DataFrame({"x": np.arange(100, dtype="float64")})
    out = expanding_rank_normalize(frame, ["x"], min_obs=30)
    assert out["x"].iloc[:30].isna().all()
    assert out["x"].iloc[30:].notna().all()


def test_normalize_output_is_a_percentile() -> None:
    """A monotonically rising series ranks at the top of its own history."""
    frame = pd.DataFrame({"x": np.arange(100, dtype="float64")})
    out = expanding_rank_normalize(frame, ["x"], min_obs=10)
    assert out["x"].iloc[10:].between(0.0, 1.0).all()
    assert out["x"].iloc[50] == pytest.approx(1.0)


def test_normalize_leaves_other_columns_untouched() -> None:
    frame = pd.DataFrame(
        {"x": np.arange(60, dtype="float64"), "keep": np.arange(60, dtype="float64")}
    )
    out = expanding_rank_normalize(frame, ["x"], min_obs=10)
    np.testing.assert_array_equal(out["keep"].to_numpy(), frame["keep"].to_numpy())


# ── contracts and baselines ─────────────────────────────────────────────────


def test_calendar_features_are_a_subset_of_core() -> None:
    """The calendar baseline must be strictly nested inside the model it is
    compared against, or the margin is not attributable to the flow features."""
    assert set(CALENDAR_FEATURES) <= set(CORE_FEATURES)


def test_feature_contract_has_no_duplicates() -> None:
    combined = (*CORE_FEATURES, *DIRECTION_FEATURES)
    assert len(combined) == len(set(combined))


def test_calendar_baseline_touches_no_flow_column() -> None:
    """The control must not smuggle in a flow feature."""
    flowish = ("basis", "outlier", "gex", "roll", "pcr", "iv_30", "chain", "direction")
    assert not any(any(f in c for f in flowish) for c in CALENDAR_FEATURES)


def test_leak_tripwire_reproduces_the_label() -> None:
    """The tripwire must be near-perfect; if it is not, the harness is broken."""
    scored = pd.DataFrame(
        {
            "y": [1.0, 0.0, 1.0, 0.0],
            "y_magnitude": [5.0, -5.0, 3.0, -3.0],
            "persistence": [1.0, 1.0, -1.0, -1.0],
        }
    )
    baselines = score_static_baselines(scored)
    assert list(baselines["leak_tripwire"]) == [1.0, 0.0, 1.0, 0.0]


def test_persistence_baseline_reads_today_not_tomorrow() -> None:
    scored = pd.DataFrame(
        {
            "y": [1.0, 0.0, 1.0, 0.0],
            "y_magnitude": [5.0, -5.0, 3.0, -3.0],
            "persistence": [1.0, 1.0, -1.0, -1.0],
        }
    )
    baselines = score_static_baselines(scored)
    assert list(baselines["persistence"]) == [1.0, 1.0, 0.0, 0.0]
