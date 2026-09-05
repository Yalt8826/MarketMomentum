"""Stage 3 — per-symbol flow attribution from a market-wide label.

The project is stuck between two grains: a real institutional-flow label that
exists only market-wide (``participant_oi``, ~1,250 rows) and a per-symbol
panel with no label at all (212,441 rows). This module bridges them.

Formally this is **Learning from Label Proportions**: instances are grouped
into *bags*, and only a bag-level aggregate label is observed.

    bag       = one trading date            (~1,250 bags)
    instances = the ~170 symbols on it      (212,441 instances)
    bag label = that day's market-wide institutional flow
    wanted    = a per-instance value that is never observed

Naming the framing matters. It connects the work to an established literature
with known guarantees and known failure modes, and it is the difference
between "we split the market number across symbols with a heuristic" and a
method that can be argued for.

**Read this before trusting any number here.** The per-symbol allocation is
*not uniquely determined by the data*. With ``n`` symbols and one equation per
day the system is underdetermined by construction; many different allocations
reproduce the same market aggregate exactly. What picks one solution is the
functional form of ``f``, the regularisation, and the choice of weights —
assumptions, not discoveries. :func:`identifiability_check` measures how much
that matters here rather than asserting it does not, and a negative result
there is a legitimate finding rather than a failure.

The linear case has a closed form worth knowing: with ``s_i = x_i . beta``,

    y_hat_t = sum_i w_it (x_it . beta) = (sum_i w_it x_it) . beta

so fitting the bag loss *is* ridge regression on the weighted-mean feature
vector per day. It is exact, it is a few lines, and it is the floor any
iterative or non-linear variant has to beat.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mktmtm.ml.splits import walk_forward_splits
from mktmtm.progress import progress_stage

logger = logging.getLogger(__name__)

__all__ = [
    "ATTRIBUTION_FEATURES",
    "attribution_weights",
    "bag_aggregate",
    "fit_linear_attribution",
    "identifiability_check",
    "run_flow_attribution",
    "weighted_mean_features",
]

#: Per-symbol features the attribution is built from. Deliberately the direction
#: family plus the basis/roll block: Stage 1 measured these as the only feature
#: group that improved anything, and a smaller f is a less arbitrary choice in
#: an underdetermined problem.
ATTRIBUTION_FEATURES = (
    "fut_basis_excess",
    "fut_basis_pct",
    "fut_roll_ratio",
    "q_fut_direction",
    "opt_direction",
    "ce_writer_frac",
    "oi_outlier_share",
    "gex",
    "put_call_oi_ratio",
)

#: Pre-registered event window: institutional positioning should build in the
#: days *before* earnings, not after. Fixed here, before any result is seen —
#: choosing the window after looking is how a null becomes a finding.
EVENT_WINDOW_DAYS = 5

RIDGE_ALPHAS = (0.1, 1.0, 10.0, 100.0, 1000.0)
RANDOM_SEED = 42
MIN_SYMBOLS_PER_DATE = 30

DEFAULT_PARQUET = Path("data/processed/flow_attribution.parquet")
EVALUATION_DIR = Path("models/evaluation")

_PANEL_SQL = """
SELECT f.symbol, f.date,
       f.fut_basis_excess, f.fut_basis_pct, f.fut_roll_ratio,
       f.q_fut_direction, f.opt_direction, f.ce_writer_frac, f.oi_outlier_share,
       f.fut_oi_notional,
       m.gex, m.put_call_oi_ratio, m.chain_oi, m.underlying_price
FROM flow_features f
JOIN ml_dataset m USING (symbol, date)
ORDER BY f.date, f.symbol
"""


# ═════════════════════════ weights and bags (pure) ═════════════════════════


def attribution_weights(panel: pd.DataFrame) -> pd.Series:
    """Each symbol's share of its date's total F&O open-interest notional.

    Pure — no I/O. Institutional flow concentrates in liquid names, so an
    equal-weighted aggregate would let 200 illiquid symbols outvote the handful
    that actually move the market number.

    Weights are **observed data, never learned**. Fitting them alongside the
    scores leaves the model badly unidentified: weight and score trade off
    directly against each other with nothing in the data to separate them.
    """
    exposure = panel["fut_oi_notional"].fillna(0.0) + (
        panel["chain_oi"].fillna(0.0) * panel["underlying_price"].fillna(0.0)
    )
    exposure = exposure.clip(lower=0.0)
    total = exposure.groupby(panel["date"]).transform("sum")
    return (exposure / total.where(total > 0)).rename("weight")


def weighted_mean_features(
    panel: pd.DataFrame,
    features: tuple[str, ...] | list[str],
    weights: pd.Series,
) -> pd.DataFrame:
    """The bag's weighted-mean feature vector, one row per date. Pure — no I/O.

    This is the closed form referred to in the module docstring: for a linear
    ``f``, the bag prediction is this vector dotted with ``beta``, so the whole
    LLP fit reduces to a ridge regression on these rows.
    """
    work = panel.copy()
    work["_w"] = weights.to_numpy()
    out: dict[str, pd.Series] = {}
    for col in features:
        usable = work[col].notna() & work["_w"].notna() & (work["_w"] > 0)
        num = (work[col] * work["_w"]).where(usable).groupby(work["date"]).sum(
            min_count=1
        )
        den = work["_w"].where(usable).groupby(work["date"]).sum(min_count=1)
        out[col] = num / den.where(den > 0)
    frame = pd.DataFrame(out)
    frame.index.name = "date"
    counts = work.groupby("date").size()
    return frame.loc[counts[counts >= MIN_SYMBOLS_PER_DATE].index].sort_index()


def bag_aggregate(scores: pd.Series, weights: pd.Series, dates: pd.Series) -> pd.Series:
    """``sum_i w_it s_it`` per date — the model's reconstruction of the bag label."""
    frame = pd.DataFrame({"s": scores, "w": weights, "date": dates}).dropna()
    return (frame["s"] * frame["w"]).groupby(frame["date"]).sum()


# ═════════════════════════ fitting ══════════════════════════════════════════


def fit_linear_attribution(
    bag_features: pd.DataFrame,
    bag_labels: pd.Series,
    *,
    alphas: tuple[float, ...] = RIDGE_ALPHAS,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Ridge on the weighted-mean features — the exact linear LLP solution.

    Alpha is selected by walk-forward out-of-sample R2, never in-sample: with
    the bag loss being exactly what is optimised, an in-sample criterion would
    pick the least regularised model every time.

    Regularisation is load-bearing here, not a tuning detail. In an
    underdetermined problem it is part of what *picks* the solution, so the
    chosen alpha and the sensitivity to it are reported rather than buried.
    """
    from sklearn.linear_model import Ridge  # type: ignore[import-untyped]

    joined = bag_features.join(bag_labels.rename("y"), how="inner").dropna()
    if len(joined) < 300:
        raise ValueError(f"only {len(joined)} usable bags — too few to fit")

    x = joined[list(bag_features.columns)]
    y = joined["y"]
    dates = pd.Series(joined.index, index=joined.index, name="date")

    scored: dict[float, list[float]] = {a: [] for a in alphas}
    for train_idx, valid_idx in walk_forward_splits(
        dates, n_splits=5, embargo_days=1, min_train_dates=250
    ):
        x_tr, y_tr = x.iloc[train_idx], y.iloc[train_idx]
        x_va, y_va = x.iloc[valid_idx], y.iloc[valid_idx]
        for alpha in alphas:
            model = Ridge(alpha=alpha, random_state=seed)
            model.fit(x_tr.to_numpy(), y_tr.to_numpy())
            pred = model.predict(x_va.to_numpy())
            ss_res = float(np.sum((y_va.to_numpy() - pred) ** 2))
            ss_tot = float(np.sum((y_va.to_numpy() - y_tr.mean()) ** 2))
            scored[alpha].append(1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0)

    mean_r2 = {a: float(np.mean(v)) for a, v in scored.items() if v}
    best_alpha = max(mean_r2, key=lambda a: mean_r2[a])

    final = Ridge(alpha=best_alpha, random_state=seed)
    final.fit(x.to_numpy(), y.to_numpy())
    return {
        "model": final,
        "alpha": best_alpha,
        "oos_r2_by_alpha": {str(a): r for a, r in mean_r2.items()},
        "oos_r2": mean_r2[best_alpha],
        "coefficients": dict(
            zip(bag_features.columns, final.coef_.tolist(), strict=True)
        ),
        "bags": int(len(joined)),
    }


# ═════════════════════════ validation ═══════════════════════════════════════


def _scores_from(model: Any, panel: pd.DataFrame, features: list[str]) -> pd.Series:
    """Per-symbol score ``f(x_i)``; NaN where a feature is missing, never imputed."""
    out = pd.Series(np.nan, index=panel.index, dtype="float64")
    ok = panel[features].notna().all(axis=1)
    if ok.any():
        out.loc[ok] = model.predict(panel.loc[ok, features].to_numpy())
    return out


def earnings_corroboration(
    panel: pd.DataFrame,
    scores: pd.Series,
    *,
    window: int = EVENT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Do large |scores| cluster in the days *before* earnings?

    The real test of the attribution, and necessarily indirect: there is no
    per-symbol ground truth, so this asks whether the allocation lands where an
    independent source says institutions were active. NSE board-meeting dates
    are externally dated and were never seen by the model.

    The window is pre-registered at module level. Positioning should build
    *before* the announcement; an "after" effect would be reaction, not
    positioning, so the post-event window is reported as a control rather than
    counted as success.
    """
    from mktmtm.data.earnings import load_earnings_dates

    try:
        calendar = load_earnings_dates()
    except (FileNotFoundError, ValueError) as exc:
        return {"error": f"earnings calendar unavailable: {exc}"}

    work = panel[["symbol", "date"]].copy()
    work["score"] = scores.to_numpy()
    work = work.dropna(subset=["score"])
    work["abs_score"] = work["score"].abs()
    work["date"] = pd.to_datetime(work["date"])

    events = pd.DataFrame(
        [
            {"symbol": sym, "event": pd.Timestamp(day)}
            for sym, days in calendar.items()
            for day in days
        ]
    )
    if events.empty:
        return {"error": "earnings calendar is empty"}

    merged = work.merge(events, on="symbol", how="left")
    offset = (merged["date"] - merged["event"]).dt.days
    merged["_pre"] = offset.between(-window, -1)
    merged["_post"] = offset.between(1, window)

    pre = merged.loc[merged["_pre"]].groupby(["symbol", "date"])["abs_score"].first()
    post = merged.loc[merged["_post"]].groupby(["symbol", "date"])["abs_score"].first()
    keys = set(pre.index) | set(post.index)
    baseline = work.set_index(["symbol", "date"])["abs_score"]
    baseline = baseline.loc[~baseline.index.isin(keys)]

    def _lift(sample: pd.Series) -> float:
        if sample.empty or baseline.empty or baseline.mean() == 0:
            return float("nan")
        return float(sample.mean() / baseline.mean() - 1.0)

    return {
        "pre_event_rows": int(len(pre)),
        "post_event_rows": int(len(post)),
        "baseline_rows": int(len(baseline)),
        "pre_event_lift": _lift(pre),
        "post_event_lift": _lift(post),
        "window_days": window,
    }


def score_stability(panel: pd.DataFrame, scores: pd.Series) -> float:
    """Lag-1 autocorrelation of the score within a symbol.

    Accumulation takes days; a score that reverses sign daily is noise. Row
    order is load-bearing — the shift is a groupby operation.
    """
    work = panel[["symbol", "date"]].copy()
    work["score"] = scores.to_numpy()
    work = work.dropna(subset=["score"]).sort_values(
        ["symbol", "date"], kind="stable"
    )
    lagged = work.groupby("symbol", observed=True)["score"].shift(1)
    pair = pd.DataFrame({"a": work["score"], "b": lagged}).dropna()
    if len(pair) < 100:
        return float("nan")
    return float(pair["a"].corr(pair["b"]))


def identifiability_check(
    bag_features: pd.DataFrame,
    bag_labels: pd.Series,
    panel: pd.DataFrame,
    features: list[str],
    *,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Does a different ``f`` reproduce the aggregate but allocate differently?

    This is the most honest measurement in the stage. If a linear and a
    tree-based ``f`` reconstruct the market aggregate about equally well but
    produce *uncorrelated* per-symbol scores, then the per-symbol allocation is
    not identified by the data — the choice of functional form is doing the
    work, and no ranking of symbols from this method is a finding.

    Note the tree variant does not satisfy the bag identity: ``sum_i w_i f(x_i)
    != f(sum_i w_i x_i)`` unless ``f`` is linear, which is exactly why the
    linear closed form is the floor. Its aggregate R2 is reported on the
    aggregate-feature fit, and the comparison of interest is the per-symbol
    rank correlation, not which R2 is higher.
    """
    from scipy.stats import spearmanr  # type: ignore[import-untyped]
    from sklearn.ensemble import (  # type: ignore[import-untyped]
        GradientBoostingRegressor,
    )
    from sklearn.linear_model import Ridge

    joined = bag_features.join(bag_labels.rename("y"), how="inner").dropna()
    x, y = joined[list(bag_features.columns)].to_numpy(), joined["y"].to_numpy()

    linear = Ridge(alpha=1.0, random_state=seed).fit(x, y)
    tree = GradientBoostingRegressor(
        max_depth=2, n_estimators=200, learning_rate=0.05, subsample=0.8,
        random_state=seed,
    ).fit(x, y)

    s_linear = _scores_from(linear, panel, features)
    s_tree = _scores_from(tree, panel, features)
    both = pd.DataFrame({"a": s_linear, "b": s_tree}).dropna()
    rho = (
        float(spearmanr(both["a"], both["b"]).statistic) if len(both) > 100 else float("nan")
    )
    return {
        "per_symbol_rank_correlation": rho,
        "linear_in_sample_r2": float(linear.score(x, y)),
        "tree_in_sample_r2": float(tree.score(x, y)),
        "compared_rows": int(len(both)),
        # Below this the two forms rank symbols differently enough that the
        # allocation cannot be attributed to the data.
        "identified": bool(np.isfinite(rho) and abs(rho) > 0.7),
    }


def placebo_test(
    bag_features: pd.DataFrame,
    bag_labels: pd.Series,
    panel: pd.DataFrame,
    features: list[str],
    *,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Refit on labels shuffled across dates; the event corroboration must collapse.

    Cheap and the most informative single thing in the stage, so it is run
    early rather than last. If shuffled labels still cluster scores around
    earnings, the clustering comes from the features' own structure and not
    from the flow labels — and the whole stage is measuring nothing.
    """
    rng = np.random.default_rng(seed)
    shuffled = pd.Series(
        rng.permutation(bag_labels.to_numpy()), index=bag_labels.index
    )
    try:
        fit = fit_linear_attribution(bag_features, shuffled, seed=seed)
    except ValueError as exc:
        return {"error": str(exc)}
    scores = _scores_from(fit["model"], panel, features)
    return {
        "oos_r2": fit["oos_r2"],
        "earnings": earnings_corroboration(panel, scores),
        "stability": score_stability(panel, scores),
    }


# ═════════════════════════ orchestration ════════════════════════════════════

_ATTRIBUTION_COLUMNS = ("symbol", "date", "participant", "flow_score", "weight")


def write_flow_attribution(conn: Any, frame: pd.DataFrame) -> int:
    """UPSERT into ``flow_attribution`` on (symbol, date, participant)."""
    from psycopg2.extras import execute_values

    from mktmtm.ml.regimes import _clean

    if frame.empty:
        return 0
    cols = list(_ATTRIBUTION_COLUMNS)
    rows = [
        tuple(_clean(rec[i], False) for i in range(len(cols)))
        for rec in frame[cols].itertuples(index=False, name=None)
    ]
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols
        if c not in ("symbol", "date", "participant")
    )
    sql = (
        f"INSERT INTO flow_attribution ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (symbol, date, participant) DO UPDATE SET {updates}"
    )
    written = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), 1000):
            page = rows[i : i + 1000]
            execute_values(cur, sql, page, page_size=len(page))
            written += len(page)
    conn.commit()
    return written


def run_flow_attribution(
    conn: Any,
    *,
    participants: tuple[str, ...] = ("FII", "Pro", "Client"),
    column: str = "d_net_opt_stock_call",
    parquet: bool = True,
    out_dir: Path = EVALUATION_DIR,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Fit, score, validate and persist the attribution. CLI entry point."""
    from mktmtm.data.participant_oi import add_net_positions

    panel = pd.read_sql(_PANEL_SQL, conn)
    if panel.empty:
        raise ValueError("flow_features JOIN ml_dataset returned no rows")
    panel["date"] = pd.to_datetime(panel["date"]).astype("datetime64[us]")

    weights = attribution_weights(panel)
    features = list(ATTRIBUTION_FEATURES)
    bag_features = weighted_mean_features(panel, features, weights)

    nets = add_net_positions(pd.read_sql("SELECT * FROM participant_oi", conn))
    nets["date"] = pd.to_datetime(nets["date"]).astype("datetime64[us]")

    summary: dict[str, Any] = {}
    attributions: list[pd.DataFrame] = []

    bar_ctx = progress_stage(
        "Attributing flow", total=len(participants), unit="participants"
    )
    with bar_ctx as bar:
        for participant in participants:
            bar.set_label(participant)
            labels = (
                nets.loc[nets["client_type"] == participant]
                .set_index("date")[column]
                .astype("float64")
            )
            try:
                fit = fit_linear_attribution(bag_features, labels, seed=seed)
            except ValueError as exc:
                summary[participant] = {"error": str(exc)}
                bar.advance()
                continue

            scores = _scores_from(fit["model"], panel, features)
            reconstructed = bag_aggregate(scores, weights, panel["date"])
            aligned = pd.DataFrame({"pred": reconstructed}).join(
                labels.rename("y"), how="inner"
            ).dropna()
            ss_res = float(((aligned["y"] - aligned["pred"]) ** 2).sum())
            ss_tot = float(((aligned["y"] - aligned["y"].mean()) ** 2).sum())

            summary[participant] = {
                "alpha": fit["alpha"],
                "bags": fit["bags"],
                "oos_r2_walk_forward": fit["oos_r2"],
                "oos_r2_by_alpha": fit["oos_r2_by_alpha"],
                "in_sample_reconstruction_r2": (
                    1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
                ),
                "coefficients": fit["coefficients"],
                "earnings": earnings_corroboration(panel, scores),
                "stability_lag1_autocorr": score_stability(panel, scores),
                "placebo": placebo_test(bag_features, labels, panel, features, seed=seed),
                "identifiability": identifiability_check(
                    bag_features, labels, panel, features, seed=seed
                ),
            }
            attributions.append(
                panel[["symbol", "date"]]
                .assign(participant=participant, flow_score=scores.to_numpy(),
                        weight=weights.to_numpy())
                .dropna(subset=["flow_score"])
            )
            bar.advance()

    if attributions:
        combined = pd.concat(attributions, ignore_index=True)
        summary["_rows_written"] = write_flow_attribution(conn, combined)
        if parquet:
            DEFAULT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(DEFAULT_PARQUET, index=False)
            summary["_parquet"] = str(DEFAULT_PARQUET)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "flow_attribution.json").write_text(
        json.dumps(summary, indent=2, default=float) + "\n"
    )
    return summary
