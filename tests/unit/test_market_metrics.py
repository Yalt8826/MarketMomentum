"""Unit tests for mktmtm.ml.market_metrics.

These metrics exist because ml/metrics.py is cross-sectional and undefined on a
one-observation-per-date series. The block bootstrap is the part worth testing
carefully: it is what stops an autocorrelated daily series from being reported
as far more significant than it is.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.market_metrics import (
    accuracy_and_base_rate,
    block_bootstrap_ci,
    block_bootstrap_margin_ci,
    directional_auc,
    flow_spearman,
    summarize_market,
)


def test_auc_of_a_perfect_score_is_one() -> None:
    y = pd.Series([0.0, 0.0, 1.0, 1.0])
    assert directional_auc(y, pd.Series([0.1, 0.2, 0.8, 0.9])) == 1.0


def test_auc_of_an_inverted_score_is_zero() -> None:
    y = pd.Series([0.0, 0.0, 1.0, 1.0])
    assert directional_auc(y, pd.Series([0.9, 0.8, 0.2, 0.1])) == 0.0


def test_auc_of_a_single_class_label_is_the_no_information_value() -> None:
    """Degenerate input degrades to 0.5 rather than raising or returning NaN."""
    y = pd.Series([1.0, 1.0, 1.0])
    assert directional_auc(y, pd.Series([0.1, 0.5, 0.9])) == 0.5


def test_auc_ignores_missing_pairs() -> None:
    y = pd.Series([0.0, 1.0, np.nan, 1.0])
    score = pd.Series([0.1, 0.9, 0.5, np.nan])
    assert directional_auc(y, score) == 1.0


def test_accuracy_is_reported_beside_its_base_rate() -> None:
    """Accuracy alone invites reading a skewed label's base rate as skill."""
    y = pd.Series([1.0] * 9 + [0.0])
    score = pd.Series([0.9] * 10)
    accuracy, base = accuracy_and_base_rate(y, score)
    assert accuracy == pytest.approx(0.9)
    assert base == pytest.approx(0.9)  # a constant predictor scores the same


def test_summarize_reports_edge_over_the_base_rate() -> None:
    frame = pd.DataFrame(
        {"y": [1.0] * 9 + [0.0], "pred": [0.9] * 10, "mag": list(range(10))}
    )
    out = summarize_market(frame, magnitude_col="mag")
    assert out["edge"] == pytest.approx(0.0)
    assert set(out) == {"auc", "accuracy", "base_rate", "edge", "spearman", "n"}
    assert all(np.isfinite(v) for v in out.values())


def test_flow_spearman_uses_the_signed_magnitude() -> None:
    magnitude = pd.Series([-10.0, -1.0, 1.0, 10.0])
    assert flow_spearman(magnitude, pd.Series([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)
    assert flow_spearman(magnitude, pd.Series([0.9, 0.8, 0.2, 0.1])) == pytest.approx(-1.0)


def test_flow_spearman_degrades_on_constant_input() -> None:
    assert flow_spearman(pd.Series([1.0, 1.0, 1.0]), pd.Series([0.1, 0.2, 0.3])) == 0.0


def test_block_bootstrap_is_reproducible_under_a_fixed_seed() -> None:
    rng = np.random.default_rng(0)
    y = pd.Series((rng.random(300) > 0.5).astype(float))
    score = pd.Series(rng.random(300))
    first = block_bootstrap_ci(y, score, n_boot=200, seed=7)
    second = block_bootstrap_ci(y, score, n_boot=200, seed=7)
    assert first == second


def test_block_bootstrap_brackets_the_point_estimate() -> None:
    rng = np.random.default_rng(1)
    y = pd.Series((rng.random(400) > 0.5).astype(float))
    # Informative but not separable: the noise must dominate the signal, or
    # AUC pins at exactly 1.0 and the interval degenerates to a point.
    score = y * 0.3 + pd.Series(rng.random(400))
    lo, hi = block_bootstrap_ci(y, score, n_boot=300, seed=3)
    point = directional_auc(y, score)
    assert 0.5 < point < 1.0, "test needs a genuinely noisy score"
    assert lo < point < hi


def test_block_bootstrap_returns_nan_on_too_few_rows() -> None:
    """Not enough rows for two blocks means no interval, not a fake one."""
    y = pd.Series([0.0, 1.0] * 10)
    lo, hi = block_bootstrap_ci(y, pd.Series(np.arange(20.0)), block=21)
    assert np.isnan(lo) and np.isnan(hi)


def test_margin_ci_is_zero_centred_for_identical_scores() -> None:
    """A model identical to its baseline has a margin of exactly zero."""
    rng = np.random.default_rng(5)
    y = pd.Series((rng.random(300) > 0.5).astype(float))
    score = pd.Series(rng.random(300))
    margin, lo, hi = block_bootstrap_margin_ci(y, score, score, n_boot=200, seed=2)
    assert margin == pytest.approx(0.0)
    assert lo == pytest.approx(0.0) and hi == pytest.approx(0.0)


def test_margin_ci_detects_a_real_improvement() -> None:
    rng = np.random.default_rng(11)
    y = pd.Series((rng.random(500) > 0.5).astype(float))
    informative = y * 0.5 + pd.Series(rng.random(500))
    noise = pd.Series(rng.random(500))
    margin, lo, hi = block_bootstrap_margin_ci(
        y, informative, noise, n_boot=300, seed=4
    )
    assert margin > 0.1
    assert lo > 0.0  # the improvement is distinguishable from zero
