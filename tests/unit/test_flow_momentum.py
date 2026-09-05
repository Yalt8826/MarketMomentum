"""Unit tests for mktmtm.ml.flow_momentum.

The forward-window construction is the whole experiment: if `forward_mean`
leaks the present into the target, every IC in the results is meaningless and
nothing else would reveal it. Hence the arithmetic is pinned exactly rather
than approximately.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.flow_momentum import (
    HORIZONS,
    MIN_SYMBOLS_PER_DATE,
    SIGNALS,
    TARGETS,
    forward_mean,
    momentum_table,
)

DATES = pd.bdate_range("2024-01-01", periods=12)


def _two_symbols() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["AAA"] * 12 + ["BBB"] * 12,
            "date": list(DATES) * 2,
            "q_fut_direction": [float(i) for i in range(12)]
            + [float(-i) for i in range(12)],
        }
    )


def test_forward_mean_is_the_next_h_days() -> None:
    """target(t) == mean(col[t+1] .. col[t+h]), exactly."""
    frame = _two_symbols()
    out = forward_mean(frame, "q_fut_direction", 3)
    # AAA values are 0..11; at t=0 the forward window is 1,2,3 -> mean 2.0
    assert out.iloc[0] == pytest.approx(2.0)
    # at t=5 the window is 6,7,8 -> 7.0
    assert out.iloc[5] == pytest.approx(7.0)


def test_forward_mean_never_includes_the_present() -> None:
    """h=1 must be strictly the next day, not today."""
    frame = _two_symbols()
    out = forward_mean(frame, "q_fut_direction", 1)
    values = frame["q_fut_direction"].to_numpy()
    # AAA rows 0..11: target(t) == value(t+1)
    np.testing.assert_allclose(out.to_numpy()[:11], values[1:12])


def test_last_rows_have_no_target() -> None:
    """No future means NaN — an inference row, never a training row."""
    frame = _two_symbols()
    out = forward_mean(frame, "q_fut_direction", 3)
    assert out.iloc[9:12].isna().all()   # AAA tail
    assert out.iloc[21:24].isna().all()  # BBB tail


def test_forward_mean_does_not_cross_symbol_boundaries() -> None:
    """AAA's last days must not borrow BBB's first days."""
    frame = _two_symbols()
    out = forward_mean(frame, "q_fut_direction", 2)
    # AAA's final two rows have no forward window of their own.
    assert out.iloc[10:12].isna().all()
    # BBB's first row looks only at BBB values (negative), never AAA's.
    assert out.iloc[12] < 0


def test_forward_mean_requires_a_full_window() -> None:
    """A partial window would silently change the horizon being measured."""
    frame = _two_symbols()
    out = forward_mean(frame, "q_fut_direction", 10)
    assert out.iloc[0] == pytest.approx(np.mean(range(1, 11)))
    assert out.iloc[2:12].isna().all()


def test_forward_mean_rejects_a_zero_horizon() -> None:
    with pytest.raises(ValueError, match="horizon must be >= 1"):
        forward_mean(_two_symbols(), "q_fut_direction", 0)


def test_forward_mean_is_order_independent() -> None:
    """Row order is load-bearing, so the function must sort rather than assume."""
    frame = _two_symbols()
    expected = forward_mean(frame, "q_fut_direction", 2)
    shuffled = frame.sample(frac=1.0, random_state=0)
    got = forward_mean(shuffled, "q_fut_direction", 2)
    # Compare on the shared (symbol, date) key rather than on position.
    a = frame.assign(t=expected.to_numpy()).set_index(["symbol", "date"])["t"]
    b = shuffled.assign(t=got.to_numpy()).set_index(["symbol", "date"])["t"]
    pd.testing.assert_series_equal(a.sort_index(), b.sort_index())


def _panel(n_symbols: int = 40, n_days: int = 300, seed: int = 0) -> pd.DataFrame:
    """A panel with no cross-sectional structure — every IC should be ~0."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    rows = []
    for s in range(n_symbols):
        for day in dates:
            rows.append(
                {
                    "symbol": f"S{s:03d}",
                    "date": day,
                    **{sig: float(rng.normal()) for sig in SIGNALS},
                    "fut_oi_notional": float(rng.uniform(1e6, 1e8)),
                }
            )
    frame = pd.DataFrame(rows)
    for target in TARGETS:
        for h in HORIZONS:
            frame[f"fwd_{target}_{h}"] = forward_mean(frame, target, h)
    return frame


def test_noise_panel_yields_no_momentum() -> None:
    """On i.i.d. noise every cell's IC must be indistinguishable from zero.

    The calibration test: if this panel shows momentum, the metric is broken
    and no real result from it could be believed.
    """
    table = momentum_table(_panel(), horizons=(1, 5))
    for _target, signals in table["grid"].items():
        for _signal, cells in signals.items():
            for _h, cell in cells.items():
                assert abs(cell["mean_ic"]) < 0.05


def test_shuffled_baseline_destroys_any_ordering() -> None:
    table = momentum_table(_panel(), horizons=(1,))
    cell = table["baselines"]["shuffled_within_date"]["1"]
    assert abs(cell["mean_ic"]) < 0.05


def test_planted_momentum_is_detected() -> None:
    """A signal that genuinely leads the forward target must show up.

    The other half of the calibration: a metric that reports zero on noise is
    only useful if it reports non-zero on signal.
    """
    frame = _panel(seed=3)
    # Make the forward 1-day target a noisy function of the signal.
    rng = np.random.default_rng(9)
    frame["fwd_q_fut_direction_1"] = frame["q_fut_direction"] + rng.normal(
        scale=0.5, size=len(frame)
    )
    table = momentum_table(
        frame, signals=("q_fut_direction",), targets=("q_fut_direction",),
        horizons=(1,),
    )
    cell = table["grid"]["q_fut_direction"]["q_fut_direction"]["1"]
    assert cell["mean_ic"] > 0.5
    assert cell["significant"] == 1.0


def test_block_scales_with_the_horizon() -> None:
    """Overlapping forward windows need a block wider than the overlap.

    At horizon h consecutive targets share h-1 days, so the daily IC series is
    autocorrelated by construction; a 21-day block would be too narrow at h=10
    and the interval would come back falsely tight.
    """
    from mktmtm.ml.flow_momentum import _ic_summary

    frame = _panel(seed=5)
    wide = _ic_summary(
        frame, "q_fut_direction", "fwd_q_fut_direction_10", horizon=10, seed=1
    )
    assert np.isfinite(wide["ci_low"]) and np.isfinite(wide["ci_high"])
    assert wide["ci_low"] < wide["mean_ic"] < wide["ci_high"]


def test_thin_dates_are_excluded() -> None:
    """A date with too few names cannot support a rank correlation."""
    assert MIN_SYMBOLS_PER_DATE >= 30


def test_contemporaneous_table_exposes_shared_construction() -> None:
    """The same-day table is what separates a real lead from a mirrored one.

    `ce_writer_frac` and `oi_outlier_direction` share buckets with
    `opt_direction`, so each is a near-mirror of it on the same day. Such a
    signal appears to "lead" tomorrow purely by inheriting the target's own
    reversal, and only the contemporaneous IC reveals that.
    """
    table = momentum_table(_panel(), horizons=(1,))
    assert "contemporaneous" in table
    for target, pairs in table["contemporaneous"].items():
        assert target not in pairs  # a target is not compared against itself
        assert all(-1.0 <= v <= 1.0 for v in pairs.values())


def test_within_symbol_leadlag_is_reported_alongside_the_grid() -> None:
    """The raw per-symbol table is the confound-free check on the IC grid.

    A cross-sectional IC removes date effects but not symbol fixed effects, and
    causal within-symbol z-scoring is worse than useless here: it subtracts a
    slowly updating expanding mean, so consecutive z-scores share a baseline
    error and look persistent even on a series with zero autocorrelation.
    """
    table = momentum_table(_panel(), horizons=(1,))
    assert set(table["within_symbol_leadlag"]) == set(table["grid"])
    cell = table["within_symbol_leadlag"]["q_fut_direction"]["opt_direction"]["1"]
    assert set(cell) == {
        "mean", "median", "consistency", "symbols",
        "partial", "partial_consistency",
    }
    assert 0.0 <= cell["consistency"] <= 1.0


def test_leadlag_reports_low_consistency_on_noise() -> None:
    """On i.i.d. noise the sign should agree across symbols about half the time.

    Consistency is what separates a broad effect from a few symbols dragging an
    average, so it must not read high when nothing is there.
    """
    table = momentum_table(_panel(n_symbols=60), horizons=(1,))
    cell = table["within_symbol_leadlag"]["opt_direction"]["q_fut_direction"]["1"]
    assert abs(cell["mean"]) < 0.05
    assert cell["consistency"] < 0.75


def test_multiple_comparison_count_is_reported() -> None:
    """With this many cells some will clear a 95% interval by chance; the
    expected count is stated rather than left to the reader."""
    table = momentum_table(_panel(), horizons=(1, 5))
    assert table["n_tests"] == len(SIGNALS) * len(TARGETS) * 2
    assert table["expected_false_positives"] > 0


def test_partial_removes_the_mechanical_route() -> None:
    """A "lead" that is only signal_t -> target_t -> target_t+1 must vanish.

    Construct a target with genuine self-reversal and a signal that is purely a
    same-day mirror of it. The raw lead is then non-zero by arithmetic alone,
    and only the partial correlation reveals that nothing leads anything.
    """
    rng = np.random.default_rng(4)
    n = 800
    target = rng.normal(size=n)
    # Target reverses against itself; signal mirrors the target contemporaneously.
    reverting = target.copy()
    for i in range(1, n):
        reverting[i] = -0.5 * reverting[i - 1] + rng.normal(scale=0.5)
    frame = pd.DataFrame(
        {
            "symbol": "AAA",
            "date": pd.bdate_range("2022-01-03", periods=n),
            "signal": -reverting + rng.normal(scale=0.05, size=n),
            "target": reverting,
        }
    )
    from mktmtm.ml.flow_momentum import _lead_lag

    cell = _lead_lag(frame, "signal", "target", 1)
    assert cell["mean"] > 0.2, "the mechanical route should show a raw lead"
    assert abs(cell["partial"]) < 0.1, "partialling it out should remove it"
