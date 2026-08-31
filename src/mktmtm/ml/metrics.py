"""Evaluation metrics for the cross-sectional ranker.

AUC, mean daily rank IC, ICIR, and directional accuracy — the honest
metrics for a long-top-decile / short-bottom-decile ranker, computed
per day and then aggregated.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# sklearn 1.9 ships no py.typed marker; mypy treats it as untyped, the same
# pattern as the psycopg2/pyarrow overrides in pyproject.toml.
from sklearn.metrics import roc_auc_score  # type: ignore[import-untyped]

__all__ = ["daily_rank_ic", "decile_spread", "icir", "summarize"]


def daily_rank_ic(
    df: pd.DataFrame,
    pred_col: str = "pred",
    ret_col: str = "next_day_return",
    date_col: str = "date",
) -> pd.Series:
    """Spearman corr between prediction and realized return, one value per date.

    Rank IC is computed against ``ret_col`` (the realized return), not the
    direction label: ordering names by conviction is what the decile strategy
    monetizes, and a correct-sign-but-wrong-ordering model deserves no credit.
    Dates with fewer than two complete (pred, ret) pairs, or with constant
    ranks on either side, yield an undefined correlation and are dropped.
    """
    tmp = df[[date_col, pred_col, ret_col]].dropna(subset=[pred_col, ret_col])
    if tmp.empty:
        return pd.Series(dtype="float64", name="ic")

    # Spearman rho is the Pearson correlation of within-date average ranks.
    ranks = tmp.groupby(date_col, sort=True)[[pred_col, ret_col]].rank(method="average")
    x = ranks[pred_col]
    y = ranks[ret_col]
    g = tmp[date_col]

    n = x.groupby(g).size()
    sx = x.groupby(g).sum()
    sy = y.groupby(g).sum()
    sxx = x.pow(2).groupby(g).sum()
    syy = y.pow(2).groupby(g).sum()
    sxy = x.mul(y).groupby(g).sum()

    numerator = n * sxy - sx * sy
    denominator = ((n * sxx - sx.pow(2)) * (n * syy - sy.pow(2))).pow(0.5)
    ic = (numerator / denominator).dropna()
    ic.name = "ic"
    return ic


def icir(ic: pd.Series) -> float:
    """mean(IC) / std(IC) — the signal-to-noise of the signal itself."""
    ic = ic.dropna()
    if len(ic) < 2:
        return 0.0
    std = float(ic.std(ddof=1))
    if std == 0.0 or not np.isfinite(std):
        return 0.0
    return float(ic.mean()) / std


def decile_spread(
    df: pd.DataFrame,
    n_deciles: int = 10,
    pred_col: str = "pred",
    ret_col: str = "next_day_return",
    date_col: str = "date",
) -> pd.Series:
    """Daily mean return of top decile minus bottom decile, by prediction rank.

    Within each date, symbols are ranked by ``pred_col``; the spread is the
    equal-weighted mean ``ret_col`` of the top decile minus that of the bottom
    decile. Dates whose tails are empty (fewer than ``n_deciles`` names) are
    dropped.
    """
    tmp = df[[date_col, pred_col, ret_col]].dropna(subset=[pred_col, ret_col])
    if tmp.empty:
        return pd.Series(dtype="float64", name="spread")

    pct = tmp.groupby(date_col, sort=True)[pred_col].rank(pct=True)
    top = tmp[pct >= 1.0 - 1.0 / n_deciles]
    bottom = tmp[pct <= 1.0 / n_deciles]
    spread = (
        top.groupby(date_col)[ret_col].mean() - bottom.groupby(date_col)[ret_col].mean()
    ).dropna()
    spread.name = "spread"
    return spread


def summarize(
    df: pd.DataFrame,
    pred_col: str = "pred",
    ret_col: str = "next_day_return",
    label_col: str = "next_day_direction",
    date_col: str = "date",
) -> dict[str, float]:
    """auc, accuracy, mean_ic, ic_std, icir, ic_t_stat, mean_spread, hit_rate.

    Every key is a finite float for any input: degenerate inputs (no valid
    dates, single-class labels, zero-variance IC) degrade to conservative
    zeros / 0.5 rather than NaN, so the dict is always JSON-friendly.
    """
    ic = daily_rank_ic(df, pred_col=pred_col, ret_col=ret_col, date_col=date_col)
    spread = decile_spread(df, pred_col=pred_col, ret_col=ret_col, date_col=date_col)
    n_days = int(len(ic))

    mean_ic = float(ic.mean()) if n_days > 0 else 0.0
    ic_std = float(ic.std(ddof=1)) if n_days > 1 else 0.0
    mean_spread = float(spread.mean()) if len(spread) > 0 else 0.0
    hit_rate = float((ic > 0.0).mean()) if n_days > 0 else 0.0

    labeled = df[[label_col, pred_col]].dropna()
    if labeled.empty:
        auc, accuracy = 0.5, 0.0
    else:
        y = labeled[label_col]
        p = labeled[pred_col]
        auc = 0.5 if y.nunique() < 2 else float(roc_auc_score(y, p))
        accuracy = float((p > 0.5).astype(int).eq(y).mean())

    return {
        "auc": auc,
        "accuracy": accuracy,
        "mean_ic": mean_ic,
        "ic_std": ic_std,
        "icir": icir(ic),
        "ic_t_stat": _ic_t_stat(ic),
        "mean_spread": mean_spread,
        "hit_rate": hit_rate,
    }


def _ic_t_stat(ic: pd.Series) -> float:
    """mean(IC) / (std(IC) / sqrt(n_days)) — the t-stat of the mean IC."""
    ic = ic.dropna()
    n = len(ic)
    if n < 2:
        return 0.0
    mean_ic = float(ic.mean())
    std = float(ic.std(ddof=1))
    if std == 0.0 or not np.isfinite(std):
        return 0.0
    return mean_ic / (std / math.sqrt(n))
