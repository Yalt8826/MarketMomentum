"""Unit tests for mktmtm.ml.attribution.

The bag identity (`sum_i w_i s_i` reconstructs the label) and the weights
summing to one per date are the arithmetic this whole stage rests on. The
placebo and identifiability machinery is tested on synthetic data, because
those are the checks that decide whether any real result may be believed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.attribution import (
    ATTRIBUTION_FEATURES,
    EVENT_WINDOW_DAYS,
    MIN_SYMBOLS_PER_DATE,
    attribution_weights,
    bag_aggregate,
    fit_linear_attribution,
    score_stability,
    weighted_mean_features,
)

DATES = pd.bdate_range("2022-01-03", periods=400)


def _panel(n_symbols: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    rows = []
    for day in DATES:
        for s in range(n_symbols):
            rows.append(
                {
                    "symbol": f"S{s:03d}",
                    "date": day,
                    "fut_oi_notional": float(rng.uniform(1e6, 1e8)),
                    "chain_oi": float(rng.uniform(1e4, 1e6)),
                    "underlying_price": float(rng.uniform(100, 3000)),
                    **{f: float(rng.normal()) for f in ATTRIBUTION_FEATURES},
                }
            )
    return pd.DataFrame(rows)


def test_weights_sum_to_one_per_date() -> None:
    panel = _panel()
    weights = attribution_weights(panel)
    totals = weights.groupby(panel["date"]).sum()
    np.testing.assert_allclose(totals.to_numpy(), 1.0, rtol=1e-9)


def test_weights_are_observed_not_learned() -> None:
    """A symbol with more open interest gets more weight, deterministically."""
    panel = _panel(n_symbols=2).head(2).copy()
    panel["fut_oi_notional"] = [1e8, 1e6]
    panel["chain_oi"] = 0.0
    weights = attribution_weights(panel)
    assert weights.iloc[0] > weights.iloc[1]
    assert weights.sum() == pytest.approx(1.0)


def test_no_lookahead_in_weights() -> None:
    """w_it uses only date-t open interest; appending later dates changes nothing."""
    panel = _panel()
    first = attribution_weights(panel)
    later = panel.copy()
    later["date"] = later["date"] + pd.Timedelta(days=1000)
    later["fut_oi_notional"] *= 1000.0
    combined = attribution_weights(pd.concat([panel, later], ignore_index=True))
    np.testing.assert_allclose(
        first.to_numpy(), combined.to_numpy()[: len(panel)], rtol=1e-12
    )


def test_bag_prediction_matches_the_weighted_sum() -> None:
    dates = pd.Series([DATES[0]] * 3)
    scores = pd.Series([1.0, 2.0, 3.0])
    weights = pd.Series([0.5, 0.3, 0.2])
    out = bag_aggregate(scores, weights, dates)
    assert out.iloc[0] == pytest.approx(0.5 * 1 + 0.3 * 2 + 0.2 * 3)


def test_linear_closed_form_reconstructs_the_bag_identity() -> None:
    """For a linear f, sum_i w_i (x_i . beta) == (sum_i w_i x_i) . beta exactly.

    This identity is what makes the linear case an exact solution rather than
    an approximation, and it is the floor any other functional form must beat.
    """
    panel = _panel(n_symbols=50)
    features = list(ATTRIBUTION_FEATURES)
    weights = attribution_weights(panel)
    bag = weighted_mean_features(panel, features, weights)

    beta = np.arange(1, len(features) + 1, dtype="float64")
    per_symbol = pd.Series(panel[features].to_numpy() @ beta, index=panel.index)
    from_instances = bag_aggregate(per_symbol, weights, panel["date"])
    from_bag = pd.Series(bag[features].to_numpy() @ beta, index=bag.index)

    aligned = pd.DataFrame({"a": from_instances, "b": from_bag}).dropna()
    np.testing.assert_allclose(aligned["a"], aligned["b"], rtol=1e-9)


def test_thin_dates_are_excluded_from_the_bags() -> None:
    """A date with too few names is not a usable bag."""
    panel = _panel(n_symbols=MIN_SYMBOLS_PER_DATE - 5)
    weights = attribution_weights(panel)
    bag = weighted_mean_features(panel, list(ATTRIBUTION_FEATURES), weights)
    assert bag.empty


def test_fit_recovers_a_planted_linear_signal() -> None:
    """A label built as a known weighted combination must be recovered."""
    panel = _panel(n_symbols=50)
    features = list(ATTRIBUTION_FEATURES)
    weights = attribution_weights(panel)
    bag = weighted_mean_features(panel, features, weights)

    beta = np.zeros(len(features))
    beta[0] = 10.0
    labels = pd.Series(bag[features].to_numpy() @ beta, index=bag.index)

    fit = fit_linear_attribution(bag, labels)
    assert fit["oos_r2"] > 0.9
    top = max(fit["coefficients"], key=lambda k: abs(fit["coefficients"][k]))
    assert top == features[0]


def test_fit_finds_nothing_in_pure_noise() -> None:
    """Shuffled labels must not produce a positive out-of-sample R2.

    This is the placebo in miniature: if noise fits, nothing else here can be
    believed.
    """
    panel = _panel(n_symbols=50)
    weights = attribution_weights(panel)
    bag = weighted_mean_features(panel, list(ATTRIBUTION_FEATURES), weights)
    rng = np.random.default_rng(3)
    labels = pd.Series(rng.normal(size=len(bag)), index=bag.index)
    fit = fit_linear_attribution(bag, labels)
    assert fit["oos_r2"] < 0.1


def test_scores_are_null_for_unscored_symbols() -> None:
    """A symbol-day missing a feature gets NaN, never an imputed 0."""
    from mktmtm.ml.attribution import _scores_from

    panel = _panel(n_symbols=40)
    panel.loc[0, ATTRIBUTION_FEATURES[0]] = np.nan

    class _Model:
        @staticmethod
        def predict(x: np.ndarray) -> np.ndarray:
            return np.ones(len(x))

    scores = _scores_from(_Model(), panel, list(ATTRIBUTION_FEATURES))
    assert pd.isna(scores.iloc[0])
    assert scores.iloc[1] == 1.0


def test_stability_detects_a_persistent_score() -> None:
    """Accumulation takes days; a score reversing daily is noise."""
    panel = pd.DataFrame(
        {"symbol": ["A"] * 200, "date": DATES[:200]}
    )
    smooth = pd.Series(np.sin(np.arange(200) / 20.0))
    alternating = pd.Series([1.0, -1.0] * 100)
    assert score_stability(panel, smooth) > 0.9
    assert score_stability(panel, alternating) < -0.9


def test_event_window_is_pre_registered() -> None:
    """The window is fixed in code before any result is seen — choosing it
    after looking is how a null becomes a finding."""
    assert EVENT_WINDOW_DAYS == 5
