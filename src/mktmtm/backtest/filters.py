"""Phase 1 — do this project's research features improve the strategy?

Gate 0 established that systematic strangle-selling earned a positive
net-of-cost return. This module asks the only question that decides whether five
modelling stages contributed anything tradeable:

    Does conditioning entry on our features beat entering unconditionally --
    AND beat the filter every retail vol seller already uses?

That second clause is the whole phase. "Sell options when implied volatility is
historically high" needs none of this research and is available in any free
screener, so **matching IV rank is failing**. It plays the role the expiry
calendar played in Stage 1 and price momentum played in Stage 5: the boring
explanation that must be ruled out before anything else earns credit.

## The look-ahead trap specific to this phase

Gate 0 enters on the first trading day of the month and prices at that day's
``settle``. As a *fill assumption* that is acceptable. It becomes look-ahead the
instant a filter reads the same day's data, because ``settle``, IV and every
IV-derived feature are knowable only after the close.

    WRONG                        RIGHT
    features(t) -> trade(t)      features(t-1) -> trade(t)

:func:`attach_features` therefore joins each trade to the **last trading day
strictly before entry**, and :func:`leakage_tripwire` plants a filter
conditioned on the trade's own outcome to prove the harness can detect the
failure it is guarding against.

## Why significance is measured over months

Trades within a calendar month share that month's market, so they are not
independent. A t-test over ~7,700 trades would treat them as though they were
and overstate significance badly. Everything here resamples **whole months**,
the same correction the research branch applied to daily series.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "CANDIDATE_FEATURES",
    "MOMENTS",
    "apply_filter",
    "attach_features",
    "compare_to_baselines",
    "leakage_tripwire",
    "monthly_block_bootstrap",
    "rank_filter",
    "regime_split",
    "walk_forward_filter",
]

#: Features tested as entry conditions, with the sign hypothesis for each.
#:
#: ``higher_is_better = True`` means a high value argues *for* selling the
#: strangle. A filter family that can only ever add trades is not a filter, so
#: both directions are represented.
CANDIDATE_FEATURES: dict[str, dict[str, Any]] = {
    # ── the boring explanation, and the bar ────────────────────────────────
    "ce_ivr": {
        "higher_is_better": True,
        "baseline": True,
        "why": "IV rank — sell when volatility is rich against its own history. "
        "Requires none of this project's research.",
    },
    # ── genuinely different information ────────────────────────────────────
    "iv_rv_spread": {
        "higher_is_better": True,
        "why": "The variance risk premium directly: implied minus realised. "
        "What a writer is actually paid.",
    },
    "fut_basis_excess": {
        "higher_is_better": False,
        "why": "Strongest constructed feature (-0.0516 vs next-day |return|). "
        "High leveraged-long demand went with LARGER moves, so a rich basis "
        "argues against being short vol.",
    },
    "dist_to_flip": {
        "higher_is_better": True,
        "why": "Best dealer feature (+0.0210) and invariant to the sign "
        "convention that turned out wrong. Far from the gamma-flip level means "
        "the hedging regime is stable.",
    },
    "gex_z": {
        "higher_is_better": True,
        "why": "Within-symbol gamma z-score. The magnitude effect that survived "
        "(+0.0436 after the IV control): unusually LITTLE dealer gamma went "
        "with larger next-day moves, so a high z argues for selling.",
    },
    "oi_outlier_share": {
        "higher_is_better": False,
        "why": "Is something unusual being positioned here? A reason not to be "
        "short vol into it.",
    },
}

#: The metric every comparison is made on. Not points, not win rate: a filter
#: that raises the win rate while lowering net return has made the strategy
#: worse and more comfortable.
METRIC = "return_on_margin"

MOMENTS = ("mean", "median", "count")

_FEATURE_SQL = """
SELECT m.symbol, m.date,
       m.ce_ivr, m.ce_ivp, m.iv_rv_spread, m.ce_iv_30, m.rv_yz,
       m.dist_to_flip, m.gex, m.days_to_earnings,
       f.fut_basis_excess, f.oi_outlier_share
FROM ml_dataset m
JOIN flow_features f USING (symbol, date)
ORDER BY m.symbol, m.date
"""


def _causal_symbol_z(frame: pd.DataFrame, column: str, min_periods: int = 60) -> pd.Series:
    """Z-score ``column`` against each symbol's own *prior* history.

    Expanding mean and standard deviation over strictly earlier rows
    (``shift(1)``), so the statistic is causal — a value is never scaled using
    information from its own day or later.

    This is the construction the GEX magnitude result used. Note the research
    branch's standing warning: causal within-symbol z-scoring must **not** be
    used to measure persistence, because consecutive z-scores share a slowly
    updating baseline and look autocorrelated when the raw series is not. Here
    it is used for its original and correct purpose — comparability across
    symbols of different size — not to measure a series against itself.
    """
    grouped = frame.groupby("symbol", observed=True, sort=False)[column]
    shifted = grouped.shift(1)
    by_symbol = shifted.groupby(frame["symbol"], observed=True, sort=False)
    mean = by_symbol.expanding(min_periods=min_periods).mean().reset_index(level=0, drop=True)
    std = by_symbol.expanding(min_periods=min_periods).std().reset_index(level=0, drop=True)
    return (frame[column] - mean) / std.where(std > 0)


def attach_features(trades: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    """Join each trade to the feature row of the last trading day BEFORE entry.

    The whole phase turns on this being strictly prior. ``merge_asof`` with
    ``allow_exact_matches=False`` enforces it: a feature row dated on the entry
    day itself is never matched, so a filter cannot read a close that had not
    happened when the position was opened.
    """
    if trades.empty:
        return trades

    feat = features.copy()
    feat["date"] = pd.to_datetime(feat["date"])
    feat = feat.sort_values(["symbol", "date"], kind="stable")
    feat["gex_z"] = _causal_symbol_z(feat, "gex")

    work = trades.loc[trades[METRIC].notna()].copy()
    work["entry_date"] = pd.to_datetime(work["entry_date"])
    work = work.sort_values(["entry_date", "symbol"], kind="stable")

    # Renamed rather than relying on merge suffixes, which only fire on a
    # collision: the feature date must always be inspectable, so that the
    # strictly-prior property can be asserted rather than assumed.
    feat = feat.rename(columns={"date": "feature_date"})

    merged = pd.merge_asof(
        work,
        feat.sort_values("feature_date", kind="stable"),
        left_on="entry_date",
        right_on="feature_date",
        by="symbol",
        direction="backward",
        allow_exact_matches=False,   # ← strictly prior. The load-bearing argument.
    )
    merged["feature_lag_days"] = (
        merged["entry_date"] - merged["feature_date"]
    ).dt.days
    return merged


# ═════════════════════════════ filters ═════════════════════════════════════


def rank_filter(
    frame: pd.DataFrame, feature: str, keep: float, higher_is_better: bool = True
) -> pd.Series:
    """Keep the best ``keep`` fraction of trades by ``feature``, ranked per month.

    Ranking **within each month** rather than pooling is deliberate: a pooled
    threshold would silently become a market-timing rule, taking every trade in
    high-IV months and none in calm ones. That is a different strategy from
    choosing between symbols, and it would not be tested by what follows.
    """
    if not 0.0 < keep <= 1.0:
        raise ValueError(f"keep must be in (0, 1], got {keep}")
    values = frame[feature]
    if not higher_is_better:
        values = -values
    pct = values.groupby([frame["year"], frame["month"]]).rank(pct=True, method="average")
    return (pct >= 1.0 - keep) & values.notna()


def apply_filter(frame: pd.DataFrame, mask: pd.Series) -> dict[str, float]:
    """Summarise the trades a filter keeps."""
    kept = frame.loc[mask.fillna(False)]
    if kept.empty:
        return {"trades": 0.0, "mean": 0.0, "median": 0.0, "months": 0.0}
    return {
        "trades": float(len(kept)),
        "mean": float(kept[METRIC].mean()),
        "median": float(kept[METRIC].median()),
        "months": float(kept.groupby(["year", "month"]).ngroups),
    }


# ═════════════════════════════ baselines ═══════════════════════════════════


def random_baseline(
    frame: pd.DataFrame, keep: float, draws: int = 500, seed: int = 42
) -> dict[str, float]:
    """Distribution of a meaningless filter keeping the same fraction of trades.

    Selecting 30% of 7,700 trades is enormous freedom to find a subset that did
    well. This says how good pure chance looks at that trade count, and any real
    filter has to clear it.
    """
    rng = np.random.default_rng(seed)
    values = frame[METRIC].to_numpy()
    n_keep = max(1, int(round(len(values) * keep)))
    means = np.empty(draws, dtype="float64")
    for i in range(draws):
        means[i] = values[rng.choice(len(values), n_keep, replace=False)].mean()
    return {
        "mean": float(means.mean()),
        "p95": float(np.quantile(means, 0.95)),
        "p99": float(np.quantile(means, 0.99)),
        "std": float(means.std(ddof=1)),
    }


def compare_to_baselines(
    frame: pd.DataFrame,
    feature: str,
    keep: float,
    higher_is_better: bool = True,
    seed: int = 42,
) -> dict[str, Any]:
    """One feature against all three baselines at one selectivity."""
    unfiltered = float(frame[METRIC].mean())
    iv_mask = rank_filter(frame, "ce_ivr", keep, higher_is_better=True)
    iv_rank = apply_filter(frame, iv_mask)
    mask = rank_filter(frame, feature, keep, higher_is_better=higher_is_better)
    result = apply_filter(frame, mask)
    chance = random_baseline(frame, keep, seed=seed)

    return {
        "feature": feature,
        "keep": keep,
        "mean": result["mean"],
        "trades": result["trades"],
        "vs_unfiltered": result["mean"] - unfiltered,
        "vs_iv_rank": result["mean"] - iv_rank["mean"],
        "vs_chance_p95": result["mean"] - chance["p95"],
        "beats_unfiltered": result["mean"] > unfiltered,
        "beats_iv_rank": result["mean"] > iv_rank["mean"],
        "beats_chance": result["mean"] > chance["p95"],
    }


# ═════════════════════════════ significance ════════════════════════════════


def monthly_block_bootstrap(
    frame: pd.DataFrame,
    mask: pd.Series,
    draws: int = 2_000,
    seed: int = 42,
) -> dict[str, float]:
    """Confidence interval on a filter's edge, resampling whole months.

    Months are the independent unit: symbols within one share that month's
    market. Resampling trades would pretend ~7,700 observations were independent
    when there are really 61.

    The statistic is the *difference* between filtered and unfiltered mean
    return, computed inside each resample so the two are always drawn from the
    same months — a paired comparison.
    """
    work = frame.assign(_keep=mask.fillna(False))
    months = [g for _, g in work.groupby(["year", "month"], sort=True)]
    if len(months) < 2:
        return {}

    rng = np.random.default_rng(seed)
    diffs = np.empty(draws, dtype="float64")
    for i in range(draws):
        picked = [months[j] for j in rng.integers(0, len(months), len(months))]
        sample = pd.concat(picked, ignore_index=True)
        kept = sample.loc[sample["_keep"], METRIC]
        diffs[i] = kept.mean() - sample[METRIC].mean() if len(kept) else np.nan

    diffs = diffs[np.isfinite(diffs)]
    if diffs.size == 0:
        return {}
    return {
        "mean_edge": float(diffs.mean()),
        "ci_low": float(np.quantile(diffs, 0.025)),
        "ci_high": float(np.quantile(diffs, 0.975)),
        "excludes_zero": bool(np.quantile(diffs, 0.025) > 0.0),
    }


# ═════════════════════════════ validity checks ═════════════════════════════


def leakage_tripwire(frame: pd.DataFrame, keep: float = 0.3) -> dict[str, float]:
    """A filter given the trade's own outcome. It MUST look spectacular.

    The same device as the research branch's Stage 1 tripwire, which scored a
    perfect AUC of 1.0000 and thereby proved the harness could detect leakage at
    all. If this does not dominate every honest filter, the comparison machinery
    is broken and no result from it means anything.
    """
    mask = rank_filter(frame, METRIC, keep, higher_is_better=True)
    leaked = apply_filter(frame, mask)
    return {
        "leaked_mean": leaked["mean"],
        "unfiltered_mean": float(frame[METRIC].mean()),
        "lift": leaked["mean"] - float(frame[METRIC].mean()),
    }


def regime_split(frame: pd.DataFrame, feature: str, keep: float, higher_is_better: bool) -> pd.DataFrame:
    """The same filter measured in each half of the sample, and per year.

    Gate 0 found the strategy's edge decaying monotonically — 8.30% in 2021 to
    1.09% in 2025. A filter fitted across that decay can look good by
    concentrating its trades in the early, easy years while being useless
    forward. Reporting per-year performance is the cheapest guard against
    mistaking a time trend for a signal.
    """
    rows = []
    for year, group in frame.groupby("year", sort=True):
        if len(group) < 50:
            continue
        mask = rank_filter(group, feature, keep, higher_is_better=higher_is_better)
        kept = apply_filter(group, mask)
        rows.append(
            {
                "year": int(year),
                "trades": kept["trades"],
                "filtered": kept["mean"],
                "unfiltered": float(group[METRIC].mean()),
                "edge": kept["mean"] - float(group[METRIC].mean()),
            }
        )
    return pd.DataFrame(rows).set_index("year") if rows else pd.DataFrame()


def walk_forward_filter(
    frame: pd.DataFrame,
    feature: str,
    higher_is_better: bool,
    keeps: tuple[float, ...] = (0.2, 0.3, 0.5),
    min_train_months: int = 24,
) -> pd.DataFrame:
    """Choose the selectivity on past months, score on the next — never both.

    Each fold picks the ``keep`` fraction that performed best over every month
    before the test window, then applies it unchanged to that window. Selecting
    and reporting on the same rows is what makes a backtest optimistic, and at
    three candidate thresholds over 61 months it is a real risk.
    """
    work = frame.copy()
    work["_period"] = pd.PeriodIndex(
        [f"{int(y)}-{int(m):02d}" for y, m in zip(work["year"], work["month"], strict=False)],
        freq="M",
    )
    periods = sorted(work["_period"].unique())
    if len(periods) <= min_train_months:
        return pd.DataFrame()

    rows = []
    for i in range(min_train_months, len(periods)):
        train = work.loc[work["_period"] < periods[i]]
        test = work.loc[work["_period"] == periods[i]]
        if test.empty or len(train) < 100:
            continue
        best_keep, best_mean = None, -np.inf
        for keep in keeps:
            stats = apply_filter(train, rank_filter(train, feature, keep, higher_is_better))
            if stats["trades"] >= 30 and stats["mean"] > best_mean:
                best_keep, best_mean = keep, stats["mean"]
        if best_keep is None:
            continue
        scored = apply_filter(test, rank_filter(test, feature, best_keep, higher_is_better))
        rows.append(
            {
                "period": str(periods[i]),
                "chosen_keep": best_keep,
                "trades": scored["trades"],
                "filtered": scored["mean"],
                "unfiltered": float(test[METRIC].mean()),
                "edge": scored["mean"] - float(test[METRIC].mean()),
            }
        )
    return pd.DataFrame(rows).set_index("period") if rows else pd.DataFrame()


def load_features(conn: Any) -> pd.DataFrame:
    """Feature panel for the whole universe."""
    return pd.read_sql(_FEATURE_SQL, conn)
