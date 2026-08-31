"""Unit tests for mktmtm.ml.baselines — trivial reference estimators."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.baselines import (
    RANDOM_SEED,
    constant_up_scores,
    random_uniform_scores,
    raw_ret_1d_scores,
    reversal_scores,
    score_baselines,
)


def _panel(
    n_dates: int = 10, n_symbols: int = 20, seed: int = 0
) -> pd.DataFrame:
    """Synthetic cross-sectional panel with ret_1d and the two targets."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-03-03", periods=n_dates)
    ret = rng.normal(0.0, 0.02, n_dates * n_symbols)
    ret_1d = rng.normal(0.0, 0.02, n_dates * n_symbols)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(dates.values, n_symbols)),
            "symbol": [f"S{i}" for i in np.tile(np.arange(n_symbols), n_dates)],
            "ret_1d": ret_1d,
            "next_day_return": ret,
            "next_day_direction": (ret > 0.0).astype(int),
        }
    )


def test_constant_up_scores_are_one_for_every_row() -> None:
    df = _panel()
    scores = constant_up_scores(df)
    assert len(scores) == len(df)
    assert scores.index.equals(df.index)
    assert (scores == 1.0).all()


def test_random_uniform_scores_in_unit_interval_and_reproducible() -> None:
    df = _panel()
    first = random_uniform_scores(df)
    second = random_uniform_scores(df)
    other = random_uniform_scores(df, seed=RANDOM_SEED + 1)
    assert ((first >= 0.0) & (first < 1.0)).all()
    assert first.equals(second)  # fixed seed => identical scores
    assert not first.equals(other)
    assert len(first) == len(df)


def test_reversal_scores_flip_ret_1d() -> None:
    df = _panel()
    scores = reversal_scores(df)
    assert np.allclose(scores, -df["ret_1d"])


def test_raw_ret_1d_scores_are_the_identity() -> None:
    df = _panel()
    scores = raw_ret_1d_scores(df)
    assert np.allclose(scores, df["ret_1d"])


def test_score_baselines_returns_one_row_per_baseline() -> None:
    df = _panel(n_dates=60, n_symbols=30, seed=3)
    table = score_baselines(df)
    assert list(table.index) == [
        "constant_up",
        "random_uniform",
        "reversal_minus_ret_1d",
        "leak_tripwire_ret_1d",
    ]
    assert set(table.columns) == {
        "auc",
        "accuracy",
        "mean_ic",
        "ic_std",
        "icir",
        "ic_t_stat",
        "mean_spread",
        "hit_rate",
    }
    # Constant "up": accuracy is the base rate; a constant rank carries no IC.
    assert table.loc["constant_up", "accuracy"] == pytest.approx(
        float(df["next_day_direction"].mean())
    )
    assert table.loc["constant_up", "mean_ic"] == pytest.approx(0.0)
    # Random uniform scores: no signal, so IC and ICIR hug zero.
    assert abs(table.loc["random_uniform", "mean_ic"]) < 0.02
    assert abs(table.loc["random_uniform", "icir"]) < 0.5
