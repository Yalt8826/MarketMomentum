"""Unit tests for mktmtm.ml.accumulation.

All pure, no database. The two that matter are the corporate-action detector
(left unfiltered, splits put the 99th percentile at FIIs acquiring 31% of a
company in one quarter) and the partial-IC control, which is what separates a
genuine signal from a price-momentum proxy.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.accumulation import (
    EARLY_DAYS,
    MEAN_FEATURES,
    MIN_DAYS_IN_WINDOW,
    TAIL_FEATURES,
    _period_partial_ic,
    _window_bounds,
    aggregate_window,
    build_targets,
    flag_corporate_actions,
)

FILING_DATES = pd.to_datetime(
    ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
)


def _filings(fii=(1000, 1200, 1100, 1500), total=(10_000,) * 4) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["ACME"] * 4,
            "date": FILING_DATES,
            "fii_shares": list(fii),
            "dii_shares": [2000, 2100, 2200, 2300],
            "total_shares": list(total),
        }
    )


# ── targets ─────────────────────────────────────────────────────────────────


def test_accumulation_is_the_share_delta_over_shares_outstanding() -> None:
    out = build_targets(_filings())
    # 1200 - 1000 = 200 new shares, over 10,000 outstanding at the start.
    assert out["accumulation"].iloc[1] == pytest.approx(0.02)


def test_first_filing_has_no_target() -> None:
    """No previous filing means no delta — an inference row, never a training row."""
    assert pd.isna(build_targets(_filings())["accumulation"].iloc[0])


def test_accumulation_uses_share_counts_not_percentages() -> None:
    """A percentage moves on a buyback with nobody having traded.

    Shares outstanding halve while the FII holding is untouched: the percentage
    would double, but no accumulation occurred.
    """
    out = build_targets(_filings(fii=(1000, 1000, 1000, 1000), total=(10_000, 5_000, 5_000, 5_000)))
    assert out["accumulation"].iloc[1] == pytest.approx(0.0)


def test_targets_difference_consecutive_filings_not_calendar_quarters() -> None:
    """Filing dates are usually quarter ends but not always."""
    odd = _filings()
    odd.loc[2, "date"] = pd.Timestamp("2024-10-29")  # an off-cycle revision
    out = build_targets(odd)
    assert out["prev_date"].iloc[3] == pd.Timestamp("2024-10-29")
    assert out["elapsed_days"].iloc[3] == 63


def test_unknown_holder_raises() -> None:
    with pytest.raises(ValueError, match="no column 'nope_shares'"):
        build_targets(_filings(), holder="nope")


# ── corporate actions ───────────────────────────────────────────────────────


def test_a_share_count_jump_is_flagged() -> None:
    """A bonus issue doubles every holder's count without anybody trading."""
    split = _filings(total=(10_000, 20_000, 20_000, 20_000))
    flagged = flag_corporate_actions(build_targets(split))
    assert bool(flagged.iloc[1])


def test_organic_share_drift_is_not_flagged() -> None:
    """ESOPs and small placements move the count by a percent or two."""
    drift = _filings(total=(10_000, 10_200, 10_300, 10_400))
    flagged = flag_corporate_actions(build_targets(drift))
    assert not flagged.iloc[1:].any()


def test_price_adjustment_catches_what_the_share_count_misses() -> None:
    """total_shares is derived and NULL on ~11% of filings; the price covers those.

    Yahoo adjusts retroactively for splits, so raw/adjusted jumps on the
    ex-date even when the share count is unavailable.
    """
    missing = _filings()
    missing["total_shares"] = [10_000, None, 10_000, 10_000]
    targets = build_targets(missing)
    dates = pd.date_range("2024-04-01", "2024-06-30", freq="D")
    ratio = np.where(dates < pd.Timestamp("2024-05-15"), 1.0, 2.0)
    prices = pd.DataFrame(
        {
            "symbol": "ACME",
            "date": dates,
            "underlying_price": 100.0 * ratio,
            "underlying_price_adj": 100.0,
        }
    )
    assert bool(flag_corporate_actions(targets, prices).iloc[1])


def test_a_dividend_sized_adjustment_is_not_flagged() -> None:
    """Dividends move raw/adjusted ~1%; splits move it 50%+."""
    targets = build_targets(_filings())
    dates = pd.date_range("2024-04-01", "2024-06-30", freq="D")
    ratio = np.where(dates < pd.Timestamp("2024-05-15"), 1.0, 1.01)
    prices = pd.DataFrame(
        {
            "symbol": "ACME",
            "date": dates,
            "underlying_price": 100.0 * ratio,
            "underlying_price_adj": 100.0,
        }
    )
    assert not flag_corporate_actions(targets, prices).iloc[1:].any()


# ── windows ─────────────────────────────────────────────────────────────────


def _row(**kw):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def test_early_window_starts_at_the_previous_filing() -> None:
    """The point of 'early' is firing before the period closes."""
    row = _row(
        date=pd.Timestamp("2024-06-30"),
        prev_date=pd.Timestamp("2024-03-31"),
        prev_prev_date=pd.Timestamp("2023-12-31"),
    )
    start, end = _window_bounds(row, "early")
    assert start == pd.Timestamp("2024-03-31")
    assert (end - start).days == EARLY_DAYS


def test_lead_window_ends_before_the_period_it_predicts() -> None:
    """The lead variant must not see any of the period it is scored against."""
    row = _row(
        date=pd.Timestamp("2024-06-30"),
        prev_date=pd.Timestamp("2024-03-31"),
        prev_prev_date=pd.Timestamp("2023-12-31"),
    )
    start, end = _window_bounds(row, "lead")
    assert end == pd.Timestamp("2024-03-31") <= row.prev_date


# ── aggregation ─────────────────────────────────────────────────────────────


def _daily(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    return pd.DataFrame(
        {
            "q_fut_direction": rng.uniform(-1, 1, n),
            "opt_direction": rng.uniform(-1, 1, n),
            "ce_writer_frac": rng.uniform(0, 1, n),
            "fut_basis_excess": rng.normal(0, 2, n),
            "oi_outlier_share": rng.uniform(0, 0.2, n),
            "oi_outlier_count": rng.integers(0, 2, n),
            "fut_oi_chg_notional": rng.normal(0, 1e7, n),
            "fut_oi_notional": np.full(n, 1e9),
            "oi_outlier_build_notional": rng.uniform(0, 1e7, n),
            "gex": rng.normal(0, 1e9, n),
            "put_call_oi_ratio": rng.uniform(0.5, 1.5, n),
            "ret_1d": rng.normal(0, 0.01, n),
        }
    )


def test_a_short_window_yields_no_features() -> None:
    assert aggregate_window(_daily(MIN_DAYS_IN_WINDOW - 1)) == {}


def test_aggregation_emits_both_feature_families() -> None:
    out = aggregate_window(_daily())
    assert set(MEAN_FEATURES) <= set(out)
    assert set(TAIL_FEATURES) <= set(out)


def test_notionals_are_scaled_to_turnover() -> None:
    """A raw rupee flow ranks by company size — the GEX H1 confound."""
    small, large = _daily(), _daily()
    large["fut_oi_chg_notional"] *= 1000.0
    large["fut_oi_notional"] *= 1000.0
    assert aggregate_window(small)["fut_oi_chg_turnover"] == pytest.approx(
        aggregate_window(large)["fut_oi_chg_turnover"]
    )


# ── the partial control ─────────────────────────────────────────────────────


def test_partial_ic_annihilates_a_pure_momentum_proxy() -> None:
    """A feature that is only a restatement of momentum must not survive.

    This is the test the whole stage turns on: FII accumulation tracks price
    momentum at IC +0.33, so an F&O feature that merely restates price
    direction would post a large raw IC while adding nothing.
    """
    rng = np.random.default_rng(1)
    n = 600
    momentum = rng.normal(size=n)
    frame = pd.DataFrame(
        {
            "period": np.repeat(pd.date_range("2022-03-31", periods=6, freq="QE"), 100),
            "_momentum": momentum,
            "proxy": momentum + rng.normal(scale=0.05, size=n),
            "independent": rng.normal(size=n),
            "accumulation": momentum + rng.normal(scale=0.5, size=n),
        }
    )
    proxy = _period_partial_ic(frame, "proxy", "_momentum").mean()
    assert abs(proxy) < 0.15


def test_partial_ic_preserves_an_independent_signal() -> None:
    """The other half of the calibration: a real signal must survive the control."""
    rng = np.random.default_rng(2)
    n = 600
    momentum = rng.normal(size=n)
    extra = rng.normal(size=n)
    frame = pd.DataFrame(
        {
            "period": np.repeat(pd.date_range("2022-03-31", periods=6, freq="QE"), 100),
            "_momentum": momentum,
            "extra": extra,
            "accumulation": momentum + extra + rng.normal(scale=0.3, size=n),
        }
    )
    assert _period_partial_ic(frame, "extra", "_momentum").mean() > 0.5


def test_partial_ic_skips_periods_with_too_few_names() -> None:
    frame = pd.DataFrame(
        {
            "period": [pd.Timestamp("2024-03-31")] * 5,
            "_momentum": range(5),
            "sig": range(5),
            "accumulation": range(5),
        }
    )
    assert len(_period_partial_ic(frame, "sig", "_momentum")) == 0


def _robustness_frame(effect: float, n_periods: int = 12, n_symbols: int = 80):
    """A panel where `basis_excess_mean` genuinely drives accumulation."""
    rng = np.random.default_rng(7)
    rows = []
    for period in pd.date_range("2022-03-31", periods=n_periods, freq="QE"):
        for s_i in range(n_symbols):
            basis = rng.normal()
            rows.append(
                {
                    "symbol": f"S{s_i:03d}",
                    "date": period,
                    "period": period,
                    "basis_excess_mean": basis,
                    "_window_return": rng.normal(),
                    "_oi_notional": float(10 ** (7 + (s_i % 3))),
                    "accumulation": effect * basis + rng.normal(scale=1.0),
                }
            )
    return pd.DataFrame(rows)


def test_robustness_splits_report_both_halves() -> None:
    from mktmtm.ml.accumulation import robustness_checks

    out = robustness_checks(_robustness_frame(effect=1.0), n_boot=200)
    for key in ("full_sample", "first_half", "second_half", "large_caps", "small_caps"):
        assert out[key]["mean_ic"] > 0.2, key
    assert out["first_half"]["period_range"] < out["second_half"]["period_range"]


def test_bootstrap_ci_excludes_zero_only_for_a_real_effect() -> None:
    """The calibration pair: a real effect clears zero, pure noise does not."""
    from mktmtm.ml.accumulation import robustness_checks

    real = robustness_checks(_robustness_frame(effect=1.0), n_boot=500)
    noise = robustness_checks(_robustness_frame(effect=0.0), n_boot=500)
    assert real["bootstrap"]["excludes_zero"]
    assert not noise["bootstrap"]["excludes_zero"]


def test_bootstrap_resamples_periods_not_rows() -> None:
    """Rows within a quarter share that quarter's market entirely.

    Resampling them as independent draws would treat ~150 symbols as 150
    observations and produce an interval far too tight.
    """
    from mktmtm.ml.accumulation import robustness_checks

    out = robustness_checks(_robustness_frame(effect=1.0, n_periods=12), n_boot=300)
    assert out["bootstrap"]["periods"] == 12


def test_size_split_is_within_period() -> None:
    """Splitting on a global median would confound size with time.

    Open interest grows across the sample, so a global threshold would put
    early quarters in 'small' and late ones in 'large'.
    """
    from mktmtm.ml.accumulation import robustness_checks

    frame = _robustness_frame(effect=1.0)
    # Inflate later periods so a global split would be a time split.
    late = frame["period"] > frame["period"].median()
    frame.loc[late, "_oi_notional"] *= 100.0
    out = robustness_checks(frame, n_boot=200)
    # Both halves of the universe must still see most periods.
    assert out["large_caps"]["periods"] >= 10
    assert out["small_caps"]["periods"] >= 10


def test_time_gradient_separates_flow_from_calendar_time() -> None:
    """Every period-level covariate trends with time; the partial is what sorts them.

    A raw correlation cannot tell "more flow to detect" from "later date". A
    covariate that drives the IC must survive controlling the period index; one
    that was only a proxy for the date must collapse.
    """
    from mktmtm.ml.accumulation import time_gradient

    rng = np.random.default_rng(11)
    rows = []
    periods = pd.date_range("2022-03-31", periods=16, freq="QE")
    for p_i, period in enumerate(periods):
        # Flow strength rises with time, but noisily, so the two are separable.
        strength = 0.3 + 0.1 * p_i + rng.normal(scale=0.6)
        for s_i in range(80):
            basis = rng.normal()
            rows.append(
                {
                    "symbol": f"S{s_i:03d}",
                    "date": period,
                    "period": period,
                    "basis_excess_mean": basis,
                    "_window_return": rng.normal(),
                    "_oi_notional": 1e8,
                    # Detection is easy when `strength` is high.
                    "accumulation": strength * basis + rng.normal(scale=1.0),
                }
            )
    out = time_gradient(pd.DataFrame(rows))
    assert out["periods"] == 16
    # Gross flow drives the IC by construction and must survive the control;
    # a constant covariate (market depth) must not appear as a driver.
    assert out["partial_vs_time"]["gross_accumulation"] > 0.2
    assert abs(out["partial_vs_time"].get("median_oi_notional", 0.0)) < 0.4


def test_time_gradient_needs_enough_periods() -> None:
    from mktmtm.ml.accumulation import time_gradient

    tiny = _robustness_frame(effect=1.0, n_periods=4, n_symbols=40)
    assert "error" in time_gradient(tiny)


# ── the live nowcast ────────────────────────────────────────────────────────


def test_verification_date_follows_the_open_quarter() -> None:
    """A forward prediction needs a date on which it becomes checkable."""
    from mktmtm.ml.accumulation import _verification_note

    note = _verification_note(pd.Timestamp("2026-08-28"))
    assert note["open_quarter_ends"] == "2026-09-30"
    # SEBI allows 21 days; NSE publication has run a few weeks behind that.
    assert pd.Timestamp(note["filing_expected"]) > pd.Timestamp("2026-10-21")


def test_early_window_is_parameterised() -> None:
    """The open window is whatever has elapsed since the last filing.

    Calibrating a live nowcast means measuring the historical IC at *that*
    length rather than interpolating between the published 30-day and
    full-quarter numbers.
    """
    row = _row(
        date=pd.Timestamp("2024-06-30"),
        prev_date=pd.Timestamp("2024-03-31"),
        prev_prev_date=pd.Timestamp("2023-12-31"),
    )
    _start, end = _window_bounds(row, "early", early_days=59)
    assert (end - pd.Timestamp("2024-03-31")).days == 59


def test_nowcast_score_is_momentum_neutral() -> None:
    """The validated result is a partial IC, so the shipped score must be too.

    A raw ranking would largely be a momentum ranking, and momentum explains
    foreign flow rather than domestic fund flow.
    """
    rng = np.random.default_rng(3)
    n = 200
    momentum = rng.normal(size=n)
    # A signal deliberately contaminated with momentum.
    raw = 0.8 * momentum + 0.4 * rng.normal(size=n)
    frame = pd.DataFrame({"signal_raw": raw, "window_return": momentum})

    ranked = frame[["signal_raw", "window_return"]].rank(method="average")
    x, z = ranked["signal_raw"], ranked["window_return"]
    beta = x.cov(z) / z.var()
    score = x - beta * (z - z.mean()) - x.mean()

    assert abs(np.corrcoef(raw, momentum)[0, 1]) > 0.7, "premise: raw is contaminated"
    assert abs(np.corrcoef(score, momentum)[0, 1]) < 0.15


def test_stale_filings_are_excluded_from_the_nowcast() -> None:
    """A symbol that missed a filing has a window spanning two quarters.

    Its accumulation is not comparable to the rest of the cross-section, so it
    must not be ranked alongside them.
    """
    from mktmtm.ml.accumulation import NOWCAST_WINDOW_TOLERANCE

    windows = pd.Series([59, 59, 59, 59, 697])
    median = float(windows.median())
    stale = windows > median * NOWCAST_WINDOW_TOLERANCE
    assert stale.tolist() == [False, False, False, False, True]


def test_a_prediction_record_is_not_silently_overwritten(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A record whose immutability relies on nobody re-running the command is not frozen.

    Overwriting would replace a forecast made before the outcome with one made
    after it — under the original filename, and the original claim.
    """
    from mktmtm.ml.accumulation import NowcastRecordExists, write_nowcast_record

    result = {
        "as_of": "2026-08-28", "holder": "mf", "feature": "basis_excess_mean",
        "symbols": 2, "dropped_stale_filing": 0, "median_window_days": 59,
        "calibration": {"mean_ic": 0.11, "positive_rate": 0.89, "periods": 19},
        "verification": {
            "open_quarter_ends": "2026-09-30", "filing_expected": "2026-11-14",
            "how": "re-run",
        },
        "ranking": pd.DataFrame(
            {
                "rank": [1, 2], "symbol": ["AAA", "BBB"], "score": [1.0, -1.0],
                "signal_raw": [2.0, -2.0], "window_return": [0.1, -0.1],
            }
        ),
    }
    path = tmp_path / "nowcast-2026-08-28.md"
    write_nowcast_record(result, path)
    assert path.exists()

    with pytest.raises(NowcastRecordExists, match="frozen"):
        write_nowcast_record(result, path)

    # Correcting a record made the same day stays possible, explicitly.
    write_nowcast_record(result, path, overwrite=True)
