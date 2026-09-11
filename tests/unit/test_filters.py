"""Unit tests for mktmtm.backtest.filters (Phase 1 evaluation harness).

The load-bearing test is `test_features_are_never_read_from_the_entry_day`.
Everything Phase 1 concludes rests on features being strictly prior to the trade
they condition, and that property is invisible in any output — a leaked filter
simply looks brilliant.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.backtest.filters import (
    CANDIDATE_FEATURES,
    apply_filter,
    attach_features,
    leakage_tripwire,
    monthly_block_bootstrap,
    random_baseline,
    rank_filter,
    regime_split,
    walk_forward_filter,
)


def _trades(n_months: int = 12, per_month: int = 40, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for m in range(n_months):
        year, month = 2024 + m // 12, (m % 12) + 1
        entry = pd.Timestamp(year=year, month=month, day=1)
        for s in range(per_month):
            rows.append(
                {
                    "symbol": f"SYM{s:03d}",
                    "year": year,
                    "month": month,
                    "entry_date": entry,
                    "return_on_margin": float(rng.normal(0.04, 0.2)),
                    "ce_ivr": float(rng.uniform(0, 100)),
                    "fut_basis_excess": float(rng.normal(0, 5)),
                }
            )
    return pd.DataFrame(rows)


# ─────────────────────── the look-ahead guarantee ───────────────────────────


def test_features_are_never_read_from_the_entry_day() -> None:
    """A feature row dated ON the entry day must not be matched.

    Gate 0 enters at the first trading day's settle. Every IV-derived feature
    for that day is knowable only after it closes, so a filter reading it would
    be trading on information it could not have had.
    """
    trades = pd.DataFrame(
        [
            {
                "symbol": "ACME",
                "year": 2025,
                "month": 3,
                "entry_date": pd.Timestamp("2025-03-03"),
                "return_on_margin": 0.05,
            }
        ]
    )
    features = pd.DataFrame(
        [
            {"symbol": "ACME", "date": pd.Timestamp("2025-02-28"), "ce_ivr": 10.0, "gex": 1.0,
             "ce_ivp": 0.0, "iv_rv_spread": 0.0, "ce_iv_30": 0.0, "rv_yz": 0.0,
             "dist_to_flip": 0.0, "days_to_earnings": 0.0, "fut_basis_excess": 0.0,
             "oi_outlier_share": 0.0},
            # Same day as entry — must be invisible.
            {"symbol": "ACME", "date": pd.Timestamp("2025-03-03"), "ce_ivr": 99.0, "gex": 1.0,
             "ce_ivp": 0.0, "iv_rv_spread": 0.0, "ce_iv_30": 0.0, "rv_yz": 0.0,
             "dist_to_flip": 0.0, "days_to_earnings": 0.0, "fut_basis_excess": 0.0,
             "oi_outlier_share": 0.0},
        ]
    )

    merged = attach_features(trades, features)

    assert len(merged) == 1
    assert merged["ce_ivr"].iloc[0] == 10.0, "matched the entry day's own close"
    assert merged["feature_date"].iloc[0] == pd.Timestamp("2025-02-28")


def test_a_symbol_with_no_prior_feature_row_gets_nothing() -> None:
    """Better an unusable NaN than a value borrowed from another symbol."""
    trades = pd.DataFrame(
        [{"symbol": "NEW", "year": 2025, "month": 3,
          "entry_date": pd.Timestamp("2025-03-03"), "return_on_margin": 0.05}]
    )
    features = pd.DataFrame(
        [{"symbol": "OTHER", "date": pd.Timestamp("2025-02-28"), "ce_ivr": 10.0, "gex": 1.0,
          "ce_ivp": 0.0, "iv_rv_spread": 0.0, "ce_iv_30": 0.0, "rv_yz": 0.0,
          "dist_to_flip": 0.0, "days_to_earnings": 0.0, "fut_basis_excess": 0.0,
          "oi_outlier_share": 0.0}]
    )

    merged = attach_features(trades, features)

    assert pd.isna(merged["ce_ivr"].iloc[0])


def test_the_leakage_tripwire_fires() -> None:
    """A filter given the outcome must dominate. If it does not, the harness is
    broken and nothing measured with it means anything."""
    frame = _trades()

    result = leakage_tripwire(frame, keep=0.3)

    assert result["lift"] > 0.15  # enormous, by construction


# ─────────────────────────── filter mechanics ───────────────────────────────


def test_rank_filter_keeps_the_requested_fraction() -> None:
    frame = _trades(n_months=6, per_month=100)

    mask = rank_filter(frame, "ce_ivr", 0.25)

    assert 0.20 <= mask.mean() <= 0.30


def test_rank_filter_ranks_within_month_not_across() -> None:
    """A pooled threshold would become a market-timing rule — taking every trade
    in high-IV months and none in calm ones — which is a different strategy."""
    frame = _trades(n_months=4, per_month=50)
    # Shift one month's whole IV-rank distribution above every other month's,
    # while keeping variation inside each month.
    hot = frame["month"] == 2
    frame.loc[hot, "ce_ivr"] = frame.loc[hot, "ce_ivr"] % 5 + 95.0
    frame.loc[~hot, "ce_ivr"] = frame.loc[~hot, "ce_ivr"] % 5

    mask = rank_filter(frame, "ce_ivr", 0.5)
    per_month = mask.groupby(frame["month"]).mean()

    assert per_month.min() > 0.3, "a month was skipped entirely"
    assert per_month.max() < 0.7


def test_direction_is_respected() -> None:
    frame = _trades(n_months=6, per_month=60)

    high = apply_filter(frame, rank_filter(frame, "fut_basis_excess", 0.2, True))
    low = apply_filter(frame, rank_filter(frame, "fut_basis_excess", 0.2, False))

    kept_high = frame.loc[rank_filter(frame, "fut_basis_excess", 0.2, True), "fut_basis_excess"]
    kept_low = frame.loc[rank_filter(frame, "fut_basis_excess", 0.2, False), "fut_basis_excess"]
    assert kept_high.mean() > kept_low.mean()
    assert high["trades"] == pytest.approx(low["trades"], rel=0.2)


def test_keeping_everything_reproduces_the_unfiltered_result() -> None:
    frame = _trades()

    stats = apply_filter(frame, rank_filter(frame, "ce_ivr", 1.0))

    assert stats["mean"] == pytest.approx(frame["return_on_margin"].mean())
    assert stats["trades"] == len(frame)


def test_an_invalid_keep_is_rejected() -> None:
    frame = _trades(n_months=2)
    for bad in (0.0, 1.5, -0.1):
        with pytest.raises(ValueError, match="keep"):
            rank_filter(frame, "ce_ivr", bad)


# ─────────────────────────── baselines ──────────────────────────────────────


def test_the_random_baseline_centres_on_the_unfiltered_mean() -> None:
    """A meaningless filter earns the unfiltered return on average — and its p95
    is the bar a real filter must clear."""
    frame = _trades(n_months=12, per_month=60)

    chance = random_baseline(frame, keep=0.3, draws=200)

    assert chance["mean"] == pytest.approx(frame["return_on_margin"].mean(), abs=0.01)
    assert chance["p95"] > chance["mean"]


def test_the_random_baseline_is_reproducible_from_its_seed() -> None:
    frame = _trades(n_months=6)

    assert random_baseline(frame, 0.3, draws=50, seed=7) == random_baseline(
        frame, 0.3, draws=50, seed=7
    )


# ─────────────────────────── significance ───────────────────────────────────


def test_the_bootstrap_finds_no_edge_in_noise() -> None:
    """The filter and the returns are independent here, so the interval must
    straddle zero."""
    frame = _trades(n_months=24, per_month=40, seed=3)

    result = monthly_block_bootstrap(frame, rank_filter(frame, "ce_ivr", 0.3), draws=300)

    assert result["ci_low"] < 0.0 < result["ci_high"]
    assert not result["excludes_zero"]


def test_the_bootstrap_finds_a_planted_edge() -> None:
    frame = _trades(n_months=24, per_month=40, seed=4)
    boost = frame["ce_ivr"] > 70.0
    frame.loc[boost, "return_on_margin"] += 0.30

    result = monthly_block_bootstrap(frame, rank_filter(frame, "ce_ivr", 0.3), draws=300)

    assert result["mean_edge"] > 0.0
    assert result["excludes_zero"]


# ─────────────────────────── regime and walk-forward ────────────────────────


def test_regime_split_reports_every_year_separately() -> None:
    """Gate 0 found the edge decaying 8.30% -> 1.09%. A filter can look good by
    concentrating trades in the easy years, so per-year reporting is required."""
    frame = _trades(n_months=36, per_month=40)

    table = regime_split(frame, "ce_ivr", 0.3, True)

    assert len(table) == 3
    assert {"trades", "filtered", "unfiltered", "edge"} <= set(table.columns)


def test_walk_forward_never_scores_a_fold_on_its_own_training_months() -> None:
    frame = _trades(n_months=36, per_month=40)

    table = walk_forward_filter(frame, "ce_ivr", True, min_train_months=12)

    assert not table.empty
    periods = pd.PeriodIndex(table.index, freq="M")
    assert periods.min() > pd.Period("2024-12", freq="M")


def test_walk_forward_returns_empty_without_enough_history() -> None:
    frame = _trades(n_months=6, per_month=40)

    assert walk_forward_filter(frame, "ce_ivr", True, min_train_months=24).empty


# ─────────────────────────── the candidate set ──────────────────────────────


def test_every_candidate_states_a_direction_and_a_rationale() -> None:
    for name, spec in CANDIDATE_FEATURES.items():
        assert isinstance(spec["higher_is_better"], bool), name
        assert spec["why"].strip(), name


def test_iv_rank_is_marked_as_the_baseline() -> None:
    """It is the boring explanation Phase 1 exists to beat, not a candidate."""
    assert CANDIDATE_FEATURES["ce_ivr"]["baseline"] is True
    assert sum(s.get("baseline", False) for s in CANDIDATE_FEATURES.values()) == 1
