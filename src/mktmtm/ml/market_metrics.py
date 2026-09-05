"""Evaluation metrics for a market-level time series, one observation per date.

``ml/metrics.py`` is cross-sectional: ``daily_rank_ic`` correlates predictions
against outcomes *within* a date across ~170 symbols, and ``decile_spread``
sorts a date's names into deciles. Neither is defined when a date carries a
single observation, which is the shape of the market-level flow task — one
row per trading day, ~1,250 rows total. Hence a separate module rather than
new arguments on the old one.

The other difference that matters is **significance**. A daily market series
is autocorrelated: positioning, basis and the expiry cycle all persist for
days. A t-test over 1,000 such observations treats them as independent and
overstates significance, sometimes badly. Everything here that reports a
confidence interval uses a moving-block bootstrap with a block long enough to
carry that dependence (one month of trading days by default).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

# sklearn ships no py.typed marker; same pattern as ml/metrics.py.
from sklearn.metrics import roc_auc_score  # type: ignore[import-untyped]

__all__ = [
    "accuracy_and_base_rate",
    "block_bootstrap_ci",
    "block_bootstrap_margin_ci",
    "directional_auc",
    "flow_spearman",
    "summarize_market",
]

#: Moving-block length in trading days. One month: long enough to carry the
#: expiry cycle and the persistence in participant positioning, short enough
#: that ~1,250 observations still yield many distinct blocks.
BLOCK_DAYS = 21

N_BOOT = 2000
RANDOM_SEED = 42


def directional_auc(y_true: pd.Series, score: pd.Series) -> float:
    """ROC AUC of ``score`` against a binary ``y_true``.

    Returns 0.5 — the no-information value — when the label is single-class or
    nothing survives the NaN drop, rather than raising or returning NaN. Every
    metric here is JSON-friendly for any input, the contract
    ``ml/metrics.summarize`` already honours.
    """
    frame = pd.DataFrame({"y": y_true, "s": score}).dropna()
    if frame.empty or frame["y"].nunique() < 2:
        return 0.5
    return float(roc_auc_score(frame["y"].to_numpy(), frame["s"].to_numpy()))


def accuracy_and_base_rate(
    y_true: pd.Series, score: pd.Series, threshold: float = 0.5
) -> tuple[float, float]:
    """``(accuracy, base_rate)`` — accuracy is only meaningful against its base rate.

    The base rate is the majority-class share, which is what a constant
    predictor scores. Reporting accuracy alone on a 55/45 label invites reading
    0.55 as skill.
    """
    frame = pd.DataFrame({"y": y_true, "s": score}).dropna()
    if frame.empty:
        return 0.0, 0.0
    y = frame["y"].astype(int)
    predicted = (frame["s"] > threshold).astype(int)
    base = float(max(y.mean(), 1.0 - y.mean()))
    return float((predicted == y).mean()), base


def flow_spearman(magnitude: pd.Series, score: pd.Series) -> float:
    """Spearman correlation of the score against the *signed magnitude* of the flow.

    The binary label throws away how large tomorrow's positioning change was.
    This keeps it: a model that ranks big institutional days above small ones
    is more useful than one that only gets the sign right, and the two come
    apart when the label is near-balanced.
    """
    frame = pd.DataFrame({"m": magnitude, "s": score}).dropna()
    if len(frame) < 3 or frame["m"].nunique() < 2 or frame["s"].nunique() < 2:
        return 0.0
    ranks = frame.rank(method="average")
    corr = float(ranks["m"].corr(ranks["s"]))
    return 0.0 if not np.isfinite(corr) else corr


def _moving_blocks(
    n: int, block: int, rng: np.random.Generator
) -> np.ndarray:
    """Positional indices for one moving-block resample of length ``n``."""
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, max(1, n - block + 1), size=n_blocks)
    idx = (starts[:, None] + np.arange(block)[None, :]).ravel()
    return idx[:n] % n


def block_bootstrap_ci(
    y_true: pd.Series,
    score: pd.Series,
    metric: Callable[[pd.Series, pd.Series], float] = directional_auc,
    *,
    block: int = BLOCK_DAYS,
    n_boot: int = N_BOOT,
    seed: int = RANDOM_SEED,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile confidence interval for ``metric``, resampling contiguous blocks.

    Rows must arrive in date order — the block structure is what carries the
    autocorrelation, so a shuffled input silently degrades this to an ordinary
    bootstrap and re-inflates significance.
    """
    frame = pd.DataFrame({"y": y_true, "s": score}).dropna().reset_index(drop=True)
    n = len(frame)
    if n < block * 2:
        return float("nan"), float("nan")

    rng = np.random.default_rng(seed)
    draws = np.empty(n_boot, dtype="float64")
    y_all = frame["y"]
    s_all = frame["s"]
    for i in range(n_boot):
        idx = _moving_blocks(n, block, rng)
        draws[i] = metric(y_all.iloc[idx].reset_index(drop=True),
                          s_all.iloc[idx].reset_index(drop=True))
    lo, hi = np.quantile(draws, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)


def block_bootstrap_margin_ci(
    y_true: pd.Series,
    score: pd.Series,
    baseline: pd.Series,
    *,
    metric: Callable[[pd.Series, pd.Series], float] = directional_auc,
    block: int = BLOCK_DAYS,
    n_boot: int = N_BOOT,
    seed: int = RANDOM_SEED,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """``(margin, lo, hi)`` for ``metric(score) - metric(baseline)``.

    The margin over the calendar-only baseline is the headline number for the
    market-level task, not the absolute AUC: the expiry cycle is known years in
    advance and mechanically explains roughly half the raw next-day
    correlation, so beating chance is not the bar — beating the calendar is.

    Both series are resampled with the **same** blocks in each draw, which
    keeps the comparison paired and gives a far tighter interval than
    differencing two independent bootstraps.
    """
    frame = (
        pd.DataFrame({"y": y_true, "s": score, "b": baseline})
        .dropna()
        .reset_index(drop=True)
    )
    n = len(frame)
    margin = metric(frame["y"], frame["s"]) - metric(frame["y"], frame["b"])
    if n < block * 2:
        return margin, float("nan"), float("nan")

    rng = np.random.default_rng(seed)
    draws = np.empty(n_boot, dtype="float64")
    for i in range(n_boot):
        idx = _moving_blocks(n, block, rng)
        y = frame["y"].iloc[idx].reset_index(drop=True)
        draws[i] = (
            metric(y, frame["s"].iloc[idx].reset_index(drop=True))
            - metric(y, frame["b"].iloc[idx].reset_index(drop=True))
        )
    lo, hi = np.quantile(draws, [alpha / 2.0, 1.0 - alpha / 2.0])
    return margin, float(lo), float(hi)


def summarize_market(
    frame: pd.DataFrame,
    *,
    score_col: str = "pred",
    label_col: str = "y",
    magnitude_col: str | None = None,
) -> dict[str, float]:
    """auc, accuracy, base_rate, edge, spearman, n — finite floats for any input."""
    auc = directional_auc(frame[label_col], frame[score_col])
    accuracy, base = accuracy_and_base_rate(frame[label_col], frame[score_col])
    spearman = (
        flow_spearman(frame[magnitude_col], frame[score_col])
        if magnitude_col and magnitude_col in frame.columns
        else 0.0
    )
    return {
        "auc": auc,
        "accuracy": accuracy,
        "base_rate": base,
        # Accuracy above what a constant predictor gets. Negative means the
        # model is worse than always guessing the majority class.
        "edge": accuracy - base,
        "spearman": spearman,
        "n": float(len(frame[[label_col, score_col]].dropna())),
    }
