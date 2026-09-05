"""Stage 5 — detecting institutional accumulation from the F&O footprint.

The project's goal restated correctly: **nowcasting, not forecasting**. Given a
symbol's F&O activity over a window, decide whether institutions were
accumulating the underlying stock during it.

Stages 1 and 4 asked forecasting questions and returned nulls. Stage 3 had the
right shape — contemporaneous, per-symbol — and the wrong label, validating
against an earnings calendar its own placebo beat. This stage is that
experiment against a real label: SEBI quarterly shareholding, ~20 filings for
each of ~210 symbols.

## The target

For consecutive filings ``f₋₁ → f``::

    accumulation = (fii_shares(f) − fii_shares(f₋₁)) / total_shares(f₋₁)

Share *counts*, because a holding percentage moves when the company buys back
stock with nobody having traded. Divided by shares outstanding, so "FIIs bought
0.8% of the company" compares between a large cap and a mid cap — a raw share
delta would rank purely by size.

## Three windows, three different claims

===============  =====================  ==========================================
Window           Features from          Claim tested
===============  =====================  ==========================================
contemporaneous  all of ``f₋₁ → f``     the footprint exists at all
early            first 30 days after    it appears *early*, before the quarter ends
                 ``f₋₁``
lead             the prior period       it precedes the accumulation entirely
===============  =====================  ==========================================

``early`` is the one that matters: shareholding is published roughly six weeks
after the period closes, so a detector that fires inside the window knows the
position change well before the market is told.

## Why means are expected to lose

The window is ~90 days. If an institution accumulates over three weeks, a
quarter-long average dilutes the signal roughly fourfold. Mean-based and
tail-based feature sets are therefore built as an explicit ablation: if tails
win the accumulation is episodic, if means win it is diffuse, and either answer
is informative.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "MEAN_FEATURES",
    "TAIL_FEATURES",
    "WINDOWS",
    "aggregate_window",
    "build_accumulation_frame",
    "build_targets",
    "flag_corporate_actions",
    "run_accumulation",
]

Window = Literal["contemporaneous", "early", "lead"]
WINDOWS: tuple[Window, ...] = ("contemporaneous", "early", "lead")

#: Days after the prior filing that the "early" window covers. Chosen as one
#: month before any result was seen, not tuned afterwards.
EARLY_DAYS = 30

#: Share count must not move more than this between filings, or a corporate
#: action rather than trading explains it. A bonus issue doubles the count and
#: a split multiplies it; genuine drift (ESOPs, small placements) is under 5%.
SHARE_JUMP_TOLERANCE = 0.05

#: Raw/adjusted price divergence flagging a split or bonus inside a window.
#: Dividends move this by ~1%, splits by 50%+, so 10% separates them cleanly.
PRICE_ADJ_TOLERANCE = 0.10

MIN_DAYS_IN_WINDOW = 20
MIN_SYMBOLS_PER_PERIOD = 30
RANDOM_SEED = 42
EVALUATION_DIR = Path("models/evaluation")
DEFAULT_PARQUET = Path("data/processed/accumulation.parquet")

#: Diffuse-accumulation hypothesis: the average level over the window.
MEAN_FEATURES = (
    "q_fut_direction_mean",
    "opt_direction_mean",
    "ce_writer_frac_mean",
    "basis_excess_mean",
    "outlier_share_mean",
    "gex_mean",
    "pcr_oi_mean",
)

#: Episodic-accumulation hypothesis: how often, how extreme, how one-sided.
TAIL_FEATURES = (
    "outlier_day_rate",
    "outlier_share_max",
    "q_fut_direction_pos_rate",
    "opt_direction_pos_rate",
    "q_fut_direction_max",
    "basis_excess_max",
    "fut_oi_chg_turnover",
    "outlier_build_turnover",
)

_FLOW_SQL = """
SELECT f.symbol, f.date,
       f.q_fut_direction, f.opt_direction, f.ce_writer_frac,
       f.fut_basis_excess, f.oi_outlier_share, f.oi_outlier_count,
       f.fut_oi_chg_notional, f.fut_oi_notional, f.oi_outlier_build_notional,
       m.gex, m.put_call_oi_ratio, m.ret_1d
FROM flow_features f
JOIN ml_dataset m USING (symbol, date)
ORDER BY f.symbol, f.date
"""

_SHAREHOLDING_SQL = "SELECT * FROM shareholding ORDER BY symbol, date"

_PRICE_SQL = """
SELECT symbol, date, underlying_price, underlying_price_adj
FROM option_metrics
WHERE underlying_price IS NOT NULL AND underlying_price_adj IS NOT NULL
ORDER BY symbol, date
"""


# ═════════════════════════ target construction (pure) ══════════════════════


def build_targets(
    filings: pd.DataFrame, *, holder: str = "fii"
) -> pd.DataFrame:
    """Accumulation between consecutive filings, per symbol. Pure — no I/O.

    Differences *consecutive filings* rather than calendar quarters: filing
    dates are usually quarter ends but not always (RELIANCE filed on
    08-DEC-2021 and 29-OCT-2024), and differencing on quarters would silently
    misalign those.

    Returns one row per (symbol, filing) with the window bounds the features
    must be aggregated over, so the two sides cannot drift apart.
    """
    shares = f"{holder}_shares"
    if shares not in filings.columns:
        raise ValueError(f"shareholding has no column {shares!r}")

    work = filings.copy()
    work["date"] = pd.to_datetime(work["date"]).astype("datetime64[us]")
    work = work.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)
    grouped = work.groupby("symbol", observed=True, sort=False)

    work["prev_date"] = grouped["date"].shift(1)
    work["prev_prev_date"] = grouped["date"].shift(2)
    work["prev_shares"] = grouped[shares].shift(1)
    work["prev_total"] = grouped["total_shares"].shift(1)
    work["elapsed_days"] = (work["date"] - work["prev_date"]).dt.days

    delta = work[shares].astype("float64") - work["prev_shares"].astype("float64")
    total = work["prev_total"].astype("float64")
    work["accumulation"] = delta / total.where(total > 0)
    # Share-count ratio between filings: the primary corporate-action signal.
    work["share_ratio"] = work["total_shares"].astype("float64") / total.where(
        total > 0
    )
    return work


def flag_corporate_actions(
    targets: pd.DataFrame,
    prices: pd.DataFrame | None = None,
    *,
    share_tolerance: float = SHARE_JUMP_TOLERANCE,
    price_tolerance: float = PRICE_ADJ_TOLERANCE,
) -> pd.Series:
    """Boolean mask: periods whose share count moved for reasons other than trading.

    A bonus issue or split changes every holder's share count without anybody
    buying or selling, so the "accumulation" measured across it is arithmetic
    rather than behaviour. Left in, these dominate: the raw distribution's 99th
    percentile is FIIs apparently acquiring **31% of a company in one quarter**,
    which no institution does organically.

    Two independent detectors, because neither covers everything:

    - **Share count ratio** between consecutive filings. Direct, but
      ``total_shares`` is derived and is NULL on ~11% of filings.
    - **Raw-versus-adjusted price divergence** inside the window. Yahoo adjusts
      retroactively for splits and bonuses, so the ratio jumps on the ex-date.
      This covers the rows where the share count is missing. Dividends move it
      by ~1% and splits by 50%+, so the threshold separates them cleanly.
    """
    ratio = targets["share_ratio"].astype("float64")
    flagged = (ratio - 1.0).abs() > share_tolerance
    flagged = flagged.fillna(False)

    if prices is None or prices.empty:
        return flagged

    price = prices.copy()
    price["date"] = pd.to_datetime(price["date"]).astype("datetime64[us]")
    price["_adj_ratio"] = price["underlying_price"].astype("float64") / price[
        "underlying_price_adj"
    ].astype("float64").where(lambda s: s > 0)
    price = price.dropna(subset=["_adj_ratio"])

    # Largest within-window swing in the adjustment factor, per (symbol, window).
    lookup: dict[str, pd.Series] = {
        str(symbol): group.set_index("date")["_adj_ratio"].sort_index()
        for symbol, group in price.groupby("symbol", observed=True)
    }
    swings = np.zeros(len(targets), dtype="float64")
    for i, row in enumerate(targets.itertuples(index=False)):
        series = lookup.get(str(row.symbol))
        start, end = row.prev_date, row.date
        if series is None or pd.isna(start):
            continue
        window = series.loc[(series.index > start) & (series.index <= end)]
        lo, hi = float(window.min()), float(window.max())
        if len(window) < 2 or lo <= 0:
            continue
        swings[i] = hi / lo - 1.0

    return flagged | (pd.Series(swings, index=targets.index) > price_tolerance)


# ═════════════════════════ window aggregation (pure) ═══════════════════════


def _pos_rate(series: pd.Series) -> float:
    clean = series.dropna()
    return float((clean > 0).mean()) if len(clean) else float("nan")


def aggregate_window(daily: pd.DataFrame) -> dict[str, float]:
    """Collapse one symbol's daily rows in one window to a feature vector. Pure.

    Notionals are divided by the window's mean futures open-interest notional
    rather than left raw. A raw rupee flow ranks by company size, and size is
    exactly the confound that made the pooled GEX H1 result an artefact; a
    turnover ratio is scale-free and always computable.
    """
    if len(daily) < MIN_DAYS_IN_WINDOW:
        return {}

    oi_base = daily["fut_oi_notional"].mean()
    oi_base = oi_base if oi_base and np.isfinite(oi_base) and oi_base > 0 else np.nan

    out: dict[str, float] = {
        # Diffuse hypothesis.
        "q_fut_direction_mean": float(daily["q_fut_direction"].mean()),
        "opt_direction_mean": float(daily["opt_direction"].mean()),
        "ce_writer_frac_mean": float(daily["ce_writer_frac"].mean()),
        "basis_excess_mean": float(daily["fut_basis_excess"].mean()),
        "outlier_share_mean": float(daily["oi_outlier_share"].mean()),
        "gex_mean": float(daily["gex"].mean()),
        "pcr_oi_mean": float(daily["put_call_oi_ratio"].mean()),
        # Episodic hypothesis.
        "outlier_day_rate": float((daily["oi_outlier_count"].fillna(0) > 0).mean()),
        "outlier_share_max": float(daily["oi_outlier_share"].max()),
        "q_fut_direction_pos_rate": _pos_rate(daily["q_fut_direction"]),
        "opt_direction_pos_rate": _pos_rate(daily["opt_direction"]),
        "q_fut_direction_max": float(daily["q_fut_direction"].max()),
        "basis_excess_max": float(daily["fut_basis_excess"].max()),
        "fut_oi_chg_turnover": float(daily["fut_oi_chg_notional"].sum()) / oi_base,
        "outlier_build_turnover": float(
            daily["oi_outlier_build_notional"].sum()
        ) / oi_base,
        # Baseline inputs, not model features.
        # Compounded window return, the price-momentum baseline's input.
        "_window_return": float(
            (1.0 + daily["ret_1d"].fillna(0.0)).prod()  # type: ignore[arg-type]
        ) - 1.0,
        "_oi_notional": float(oi_base) if np.isfinite(oi_base) else float("nan"),
        "_days": float(len(daily)),
    }
    return out


# ═════════════════════════ assembly ════════════════════════════════════════


def _window_bounds(
    row: Any, window: Window, *, early_days: int = EARLY_DAYS
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Feature window for one (symbol, filing) row, per the three variants.

    ``early_days`` parameterises the "early" variant. It exists so a live
    nowcast can be calibrated against a *matched* historical window rather than
    interpolated between the 30-day and full-quarter results — the open window
    at any given moment is whatever has elapsed since the last filing, and that
    is rarely either of them.
    """
    if window == "contemporaneous":
        return row.prev_date, row.date
    if window == "early":
        if pd.isna(row.prev_date):
            return None, None
        return row.prev_date, row.prev_date + pd.Timedelta(days=early_days)
    if window == "lead":
        return row.prev_prev_date, row.prev_date
    raise ValueError(f"unknown window {window!r}")


def build_accumulation_frame(
    conn: Any,
    *,
    holder: str = "fii",
    window: Window = "contemporaneous",
    drop_corporate_actions: bool = True,
    early_days: int = EARLY_DAYS,
) -> pd.DataFrame:
    """One row per (symbol, filing): window features plus the accumulation target."""
    flow = pd.read_sql(_FLOW_SQL, conn)
    flow["date"] = pd.to_datetime(flow["date"]).astype("datetime64[us]")
    filings = pd.read_sql(_SHAREHOLDING_SQL, conn)
    if filings.empty:
        raise ValueError("shareholding is empty — run `mktmtm fetch-shareholding`")

    targets = build_targets(filings, holder=holder)
    targets = targets.loc[targets["accumulation"].notna()].reset_index(drop=True)

    if drop_corporate_actions:
        prices = pd.read_sql(_PRICE_SQL, conn)
        flagged = flag_corporate_actions(targets, prices)
        logger.info(
            "dropping %d of %d periods with a corporate action (%.1f%%)",
            int(flagged.sum()), len(targets), 100 * flagged.mean(),
        )
        targets = targets.loc[~flagged].reset_index(drop=True)

    by_symbol: dict[str, pd.DataFrame] = {
        str(symbol): group.set_index("date").sort_index()
        for symbol, group in flow.groupby("symbol", observed=True)
    }

    rows: list[dict[str, Any]] = []
    for row in targets.itertuples(index=False):
        start, end = _window_bounds(row, window, early_days=early_days)
        if start is None or pd.isna(start) or pd.isna(end):
            continue
        daily = by_symbol.get(str(row.symbol))
        if daily is None:
            continue
        chunk = daily.loc[(daily.index > start) & (daily.index <= end)]
        features = aggregate_window(chunk)
        if not features:
            continue
        rows.append(
            {
                "symbol": row.symbol,
                "date": row.date,
                "period": pd.Timestamp(row.date).to_period("Q").to_timestamp(),  # type: ignore[arg-type]
                "accumulation": row.accumulation,
                "elapsed_days": row.elapsed_days,
                **features,
            }
        )

    if not rows:
        raise ValueError(f"no usable (symbol, filing) rows for window {window!r}")
    frame = pd.DataFrame(rows)

    # Periods with too thin a cross-section cannot support a rank correlation.
    counts = frame.groupby("period")["symbol"].transform("size")
    frame = frame.loc[counts >= MIN_SYMBOLS_PER_PERIOD].reset_index(drop=True)

    # Institutions accumulate over consecutive quarters, so the previous
    # period's accumulation is the persistence baseline and must be lagged
    # within symbol, never taken from the same row.
    frame = frame.sort_values(["symbol", "date"], kind="stable")
    frame["_prev_accumulation"] = frame.groupby(
        "symbol", observed=True, sort=False
    )["accumulation"].shift(1)
    return frame.reset_index(drop=True)


# ═════════════════════════ evaluation ══════════════════════════════════════


def _period_ic(frame: pd.DataFrame, signal: str) -> pd.Series:
    """Spearman rank IC per filing period, ranking symbols within each."""
    from mktmtm.ml.metrics import daily_rank_ic

    return daily_rank_ic(
        frame, pred_col=signal, ret_col="accumulation", date_col="period"
    )


def _period_partial_ic(
    frame: pd.DataFrame, signal: str, control: str
) -> pd.Series:
    """Rank IC of ``signal`` against accumulation, controlling ``control``.

    Comparing two |IC| magnitudes says which is larger, **not** whether either
    adds anything to the other. Two signals measuring the same underlying thing
    both score well and neither is informative given the other. This is the
    test that separates those cases, and it is the one the stage turns on:
    price momentum reaches IC +0.33, so an F&O feature only earns credit if it
    survives with momentum partialled out.

    Spearman throughout — ranks are taken within each period before the partial
    correlation, so the result is a partial *rank* IC.
    """
    values: dict[Any, float] = {}
    for period, group in frame.groupby("period", observed=True, sort=True):
        chunk = group[[signal, control, "accumulation"]].dropna()
        if len(chunk) < 10:
            continue
        ranked = chunk.rank(method="average")
        x, z, y = ranked[signal], ranked[control], ranked["accumulation"]
        if x.nunique() < 2 or z.nunique() < 2 or y.nunique() < 2:
            continue
        rxy, rxz, ryz = x.corr(y), x.corr(z), y.corr(z)
        if not all(np.isfinite(v) for v in (rxy, rxz, ryz)):
            continue
        denominator = np.sqrt((1.0 - rxz**2) * (1.0 - ryz**2))
        if denominator > 0:
            values[period] = float((rxy - rxz * ryz) / denominator)
    return pd.Series(values, dtype="float64")


def _summarize(ic: pd.Series) -> dict[str, float]:
    """Per-period ICs summarised honestly for a ~20-observation sample.

    No t-statistic: with roughly twenty periods, one quarter moves the mean and
    a t-stat on 19 degrees of freedom invites more confidence than the data
    supports. The share of positive periods is the number to read.
    """
    clean = ic.dropna()
    n = len(clean)
    if n == 0:
        return {"mean_ic": 0.0, "median_ic": 0.0, "positive_rate": 0.0, "periods": 0.0}
    return {
        "mean_ic": float(clean.mean()),
        "median_ic": float(clean.median()),
        "positive_rate": float((clean > 0).mean()),
        "std_ic": float(clean.std(ddof=1)) if n > 1 else 0.0,
        "periods": float(n),
    }


def evaluate_accumulation(
    frame: pd.DataFrame,
    *,
    features: tuple[str, ...] = MEAN_FEATURES + TAIL_FEATURES,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Rank IC for every feature, plus the baselines that calibrate them."""
    work = frame.copy()
    results: dict[str, Any] = {"features": {}, "baselines": {}}

    for name in features:
        if name in work.columns:
            results["features"][name] = _summarize(_period_ic(work, name))

    rng = np.random.default_rng(seed)
    # Price momentum is this stage's calendar baseline: institutions buy what
    # is going up, so a signal that only rediscovers that has found nothing.
    work["_momentum"] = work["_window_return"]
    work["_size"] = work["_oi_notional"]
    work["_persistence"] = work["_prev_accumulation"]
    work["_shuffled"] = work.groupby("period", observed=True)[
        "accumulation"
    ].transform(lambda s: rng.permutation(s.to_numpy()))
    # Must score ~1.0; anything less means the harness is broken.
    work["_leak_tripwire"] = work["accumulation"]

    for label, column in (
        ("price_momentum", "_momentum"),
        ("size_oi_notional", "_size"),
        ("persistence", "_persistence"),
        ("shuffled", "_shuffled"),
        ("leak_tripwire", "_leak_tripwire"),
    ):
        results["baselines"][label] = _summarize(_period_ic(work, column))

    # The decisive table: does anything survive with momentum partialled out?
    results["partial_vs_momentum"] = {
        name: _summarize(_period_partial_ic(work, name, "_momentum"))
        for name in features
        if name in work.columns
    }
    # Momentum controlled by the best raw feature, for symmetry: if each
    # annihilates the other they are the same signal, if momentum survives and
    # the feature does not then the feature was only ever a momentum proxy.
    results["momentum_partial_vs_best"] = {}

    results["best_feature"] = max(
        results["features"], key=lambda k: abs(results["features"][k]["mean_ic"]),
        default=None,
    )
    results["best_partial_feature"] = max(
        results["partial_vs_momentum"],
        key=lambda k: abs(results["partial_vs_momentum"][k]["mean_ic"]),
        default=None,
    )
    if results["best_partial_feature"]:
        best = results["best_partial_feature"]
        results["momentum_partial_vs_best"] = _summarize(
            _period_partial_ic(work, "_momentum", best)
        )
        results["best_partial_ic"] = results["partial_vs_momentum"][best]["mean_ic"]
    momentum_ic = results["baselines"]["price_momentum"]["mean_ic"]
    if results["best_feature"]:
        raw_best = results["features"][results["best_feature"]]["mean_ic"]
        results["margin_over_momentum"] = abs(raw_best) - abs(momentum_ic)
    return results


def run_accumulation(
    conn: Any,
    *,
    holders: tuple[str, ...] = ("fii", "dii"),
    windows: tuple[Window, ...] = WINDOWS,
    drop_corporate_actions: bool = True,
    out_dir: Path = EVALUATION_DIR,
    parquet: bool = True,
) -> dict[str, Any]:
    """Build, evaluate and persist the accumulation detection study."""
    summary: dict[str, Any] = {}
    exported = False

    for holder in holders:
        for window in windows:
            key = f"{holder}:{window}"
            try:
                frame = build_accumulation_frame(
                    conn, holder=holder, window=window,
                    drop_corporate_actions=drop_corporate_actions,
                )
            except ValueError as exc:
                summary[key] = {"error": str(exc)}
                continue
            summary[key] = evaluate_accumulation(frame)
            summary[key]["_meta"] = {
                "rows": int(len(frame)),
                "symbols": int(frame["symbol"].nunique()),
                "periods": int(frame["period"].nunique()),
                "median_window_days": float(frame["_days"].median()),
            }
            if parquet and not exported:
                DEFAULT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
                frame.to_parquet(DEFAULT_PARQUET, index=False)
                exported = True

    # The robustness battery runs on the pre-registered feature only, against
    # the holder the effect was localised to. Nothing here searches.
    if "mf:contemporaneous" in summary and "error" not in summary["mf:contemporaneous"]:
        try:
            frame = build_accumulation_frame(
                conn, holder="mf", window="contemporaneous",
                drop_corporate_actions=drop_corporate_actions,
            )
            summary["_robustness"] = robustness_checks(frame)
            summary["_counterparty"] = counterparty_check(conn)
        except ValueError as exc:
            summary["_robustness"] = {"error": str(exc)}

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "accumulation.json").write_text(
        json.dumps(summary, indent=2, default=float) + "\n"
    )
    return summary


# ═════════════════════════ robustness ══════════════════════════════════════

#: Pre-registered for the robustness battery: the single feature §9.2 found,
#: fixed before any subset was examined. Nothing here searches over features.
PREREGISTERED_FEATURE = "basis_excess_mean"


def robustness_checks(
    frame: pd.DataFrame,
    *,
    feature: str = PREREGISTERED_FEATURE,
    n_boot: int = 2000,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Does the one surviving signal hold up under splitting and resampling?

    Out-of-sample confirmation on monthly mutual-fund portfolios is the test
    this stage really wants, but SEBI requires each AMC to publish on its own
    site and AMFI does not aggregate them — roughly forty heterogeneous scrapes
    over sixty months. This battery attacks the same question with data already
    on disk.

    The result is a **single feature with no fitted parameters**, so there is
    nothing to overfit except the *choice* of feature. That makes subset
    stability the right substitute for a holdout: if the effect survives in
    both halves of the sample and in both halves of the universe, the choice
    was not luck in one corner of the data.

    ``feature`` is pinned at module level and is not searched over here.
    """
    work = frame.copy()
    work["_momentum"] = work["_window_return"]
    out: dict[str, Any] = {"feature": feature}

    full = _period_partial_ic(work, feature, "_momentum")
    out["full_sample"] = _summarize(full)

    # ── Time split. A regime-specific artefact shows up here. ──────────────
    periods = sorted(work["period"].unique())
    midpoint = periods[len(periods) // 2]
    for label, mask in (
        ("first_half", work["period"] < midpoint),
        ("second_half", work["period"] >= midpoint),
    ):
        subset = work.loc[mask]
        out[label] = _summarize(_period_partial_ic(subset, feature, "_momentum"))
        out[label]["period_range"] = (
            f"{subset['period'].min():%Y-%m} → {subset['period'].max():%Y-%m}"
        )

    # ── Universe split, within period so it is a size split and not a time
    # one. Institutions concentrate in large caps, so an effect present only
    # there would be a liquidity artefact wearing a signal's clothes. ───────
    median_size = work.groupby("period", observed=True)["_oi_notional"].transform(
        "median"
    )
    for label, mask in (
        ("large_caps", work["_oi_notional"] >= median_size),
        ("small_caps", work["_oi_notional"] < median_size),
    ):
        out[label] = _summarize(
            _period_partial_ic(work.loc[mask], feature, "_momentum")
        )

    # ── Bootstrap over PERIODS, the unit of independence. Resampling rows
    # would treat ~150 symbols in one quarter as 150 independent draws when
    # they share that quarter's market entirely. ───────────────────────────
    clean = full.dropna()
    if len(clean) >= 5:
        rng = np.random.default_rng(seed)
        values = clean.to_numpy()
        draws = np.array(
            [rng.choice(values, size=len(values), replace=True).mean()
             for _ in range(n_boot)]
        )
        lo, hi = np.quantile(draws, [0.025, 0.975])
        out["bootstrap"] = {
            "mean": float(values.mean()),
            "ci_low": float(lo),
            "ci_high": float(hi),
            "excludes_zero": bool(lo > 0.0),
            "periods": int(len(values)),
        }
    return out


def time_gradient(
    frame: pd.DataFrame, *, feature: str = PREREGISTERED_FEATURE
) -> dict[str, Any]:
    """Why does the effect nearly triple between the first and second halves?

    Two readings, with very different consequences. If detection strength
    tracks **how much flow there is to detect** — more accumulation, more
    cross-sectional dispersion to rank — the gradient is a property of the
    mechanism and the signal is stable. If it tracks calendar time and nothing
    else, the effect is specific to the 2024-26 market and does not generalise.

    Correlates the per-period IC against period-level covariates. With 19
    periods this is descriptive, not inferential: it can rule a story out or
    make it plausible, and cannot establish one.
    """
    work = frame.copy()
    work["_momentum"] = work["_window_return"]
    ic = _period_partial_ic(work, feature, "_momentum")
    if len(ic) < 8:
        return {"error": f"only {len(ic)} periods with a computable IC"}

    grouped = work.groupby("period", observed=True)
    covariates = pd.DataFrame(
        {
            # How much is there to detect: the spread of accumulation across
            # symbols in that period.
            "accumulation_dispersion": grouped["accumulation"].std(),
            # How much flow in aggregate, direction ignored.
            "gross_accumulation": grouped["accumulation"].apply(
                lambda s: float(s.abs().sum())
            ),
            # Net direction: were funds buying the market or selling it?
            "net_accumulation": grouped["accumulation"].sum(),
            # Market depth, to separate "more flow" from "bigger F&O market".
            "median_oi_notional": grouped["_oi_notional"].median(),
            "symbols": grouped.size(),
        }
    )
    # Elapsed periods, to test the bare calendar-time story on its own terms.
    covariates["period_index"] = np.arange(len(covariates), dtype="float64")

    aligned = covariates.loc[covariates.index.isin(ic.index)].copy()
    aligned["ic"] = ic.reindex(aligned.index)
    aligned = aligned.dropna(subset=["ic"])

    out: dict[str, Any] = {"periods": int(len(aligned)), "correlations": {}}
    for column in covariates.columns:
        pair = aligned[[column, "ic"]].dropna()
        if len(pair) < 8 or pair[column].nunique() < 3:
            continue
        ranked = pair.rank(method="average")
        out["correlations"][column] = float(ranked[column].corr(ranked["ic"]))
    # Every covariate here trends with time, so a raw correlation cannot tell
    # "more flow to detect" from "later date". Partialling out the period index
    # is what separates them: a covariate that survives it carries information
    # beyond the trend, one that collapses was only ever a proxy for the date.
    out["partial_vs_time"] = {}
    for column in covariates.columns:
        if column == "period_index":
            continue
        pair = aligned[[column, "period_index", "ic"]].dropna()
        if len(pair) < 8 or pair[column].nunique() < 3:
            continue
        ranked = pair.rank(method="average")
        x, z, y = ranked[column], ranked["period_index"], ranked["ic"]
        rxy, rxz, ryz = x.corr(y), x.corr(z), y.corr(z)
        if not all(np.isfinite(v) for v in (rxy, rxz, ryz)):
            continue
        denominator = np.sqrt((1.0 - rxz**2) * (1.0 - ryz**2))
        if denominator > 0:
            out["partial_vs_time"][column] = float((rxy - rxz * ryz) / denominator)

    out["per_period"] = {
        str(pd.Timestamp(str(idx)).date()): {
            "ic": float(row["ic"]),
            "dispersion": float(row["accumulation_dispersion"]),
            "gross": float(row["gross_accumulation"]),
        }
        for idx, row in aligned.iterrows()
    }
    return out


def counterparty_check(
    conn: Any, *, seed: int = RANDOM_SEED
) -> dict[str, Any]:
    """Are foreign sellers and domestic funds two sides of the same trade?

    ``basis_excess_mean`` carries opposite signs against FII and mutual-fund
    accumulation. If that reflects one trade rather than two coincidences, then
    **FII and MF accumulation should themselves be negatively correlated across
    symbols**, and the basis signal should be sharpest where the two disagree
    most.

    This is a structural check, not a metric: it can corroborate the mechanism
    or contradict it, and neither outcome changes the headline IC.
    """
    fii = build_accumulation_frame(conn, holder="fii", window="contemporaneous")
    mf = build_accumulation_frame(conn, holder="mf", window="contemporaneous")

    joined = fii.merge(
        mf[["symbol", "date", "accumulation"]],
        on=["symbol", "date"],
        suffixes=("_fii", "_mf"),
    )
    if joined.empty:
        return {"error": "no overlapping (symbol, filing) rows"}

    # Within-period rank correlation of the two accumulation series.
    per_period: list[float] = []
    for _period, group in joined.groupby("period", observed=True):
        chunk = group[["accumulation_fii", "accumulation_mf"]].dropna()
        if len(chunk) < 10:
            continue
        ranked = chunk.rank(method="average")
        corr = ranked["accumulation_fii"].corr(ranked["accumulation_mf"])
        if np.isfinite(corr):
            per_period.append(float(corr))

    values = np.asarray(per_period)
    return {
        "fii_vs_mf_accumulation_corr": float(values.mean()) if values.size else float("nan"),
        "negative_periods": float((values < 0).mean()) if values.size else float("nan"),
        "periods": int(values.size),
        "rows": int(len(joined)),
    }


# ═════════════════════════ live nowcast ════════════════════════════════════

#: A symbol whose open window is this much longer than the median has a stale
#: filing (a missed quarter, or a delisting in progress) and is not comparable
#: to the rest of the cross-section.
NOWCAST_WINDOW_TOLERANCE = 1.5


def nowcast(
    conn: Any,
    *,
    holder: str = "mf",
    feature: str = PREREGISTERED_FEATURE,
    drop_corporate_actions: bool = True,
) -> dict[str, Any]:
    """Rank symbols by accumulation in the quarter SEBI has not disclosed yet.

    Every filing period in `shareholding` is closed and scored. The *open*
    period — from each symbol's last filing to the most recent flow date — has
    no label and will not until SEBI publishes, roughly six weeks after the
    quarter ends. That gap is what makes this a **falsifiable forward
    prediction with a known verification date** rather than a restatement of
    the fit.

    Two things make the output honest rather than a stock tip:

    - **The score is momentum-residualised.** The validated result is a
      *partial* IC controlling price momentum; handing back a raw
      `basis_excess` ranking would largely hand back a momentum ranking, which
      §9.1 of the stage report shows explains FII flow and not fund flow.
    - **It is calibrated at the matched window length.** The open window is
      whatever has elapsed since the last filing — usually neither 30 days nor
      a full quarter — so the caller gets the historical IC measured at
      *that* length rather than an interpolation between the two published
      numbers.
    """
    flow = pd.read_sql(_FLOW_SQL, conn)
    flow["date"] = pd.to_datetime(flow["date"]).astype("datetime64[us]")
    as_of = flow["date"].max()

    filings = pd.read_sql(_SHAREHOLDING_SQL, conn)
    filings["date"] = pd.to_datetime(filings["date"]).astype("datetime64[us]")
    last = (
        filings.groupby("symbol", observed=True)["date"].max().rename("last_filing")
    )

    by_symbol: dict[str, pd.DataFrame] = {
        str(symbol): group.set_index("date").sort_index()
        for symbol, group in flow.groupby("symbol", observed=True)
    }

    rows: list[dict[str, Any]] = []
    for symbol, filed in last.items():
        daily = by_symbol.get(str(symbol))
        if daily is None:
            continue
        chunk = daily.loc[(daily.index > filed) & (daily.index <= as_of)]
        features = aggregate_window(chunk)
        if not features or feature not in features:
            continue
        rows.append(
            {
                "symbol": str(symbol),
                "last_filing": filed,
                "window_days": int((as_of - filed).days),
                "signal_raw": features[feature],
                "window_return": features["_window_return"],
                "trading_days": features["_days"],
            }
        )
    if not rows:
        raise ValueError("no symbols have an open window with usable flow data")

    frame = pd.DataFrame(rows)
    # A stale filing makes a window that is not comparable to the rest.
    median_days = float(frame["window_days"].median())
    stale = frame["window_days"] > median_days * NOWCAST_WINDOW_TOLERANCE
    frame = frame.loc[~stale].reset_index(drop=True)

    # Momentum-residualised score, cross-sectionally: rank both, regress the
    # signal rank on the momentum rank, keep the residual.
    ranked = frame[["signal_raw", "window_return"]].rank(method="average")
    x, z = ranked["signal_raw"], ranked["window_return"]
    beta = x.cov(z) / z.var() if z.var() > 0 else 0.0
    frame["score"] = x - beta * (z - z.mean()) - x.mean()
    frame = frame.sort_values("score", ascending=False).reset_index(drop=True)
    frame["rank"] = np.arange(1, len(frame) + 1)

    window_days = int(median_days)
    calibration = _calibrate_window(
        conn, holder=holder, feature=feature, window_days=window_days,
        drop_corporate_actions=drop_corporate_actions,
    )
    return {
        "as_of": str(pd.Timestamp(as_of).date()),
        "holder": holder,
        "feature": feature,
        "symbols": int(len(frame)),
        "dropped_stale_filing": int(stale.sum()),
        "median_window_days": window_days,
        "verification": _verification_note(as_of),
        "calibration": calibration,
        "ranking": frame,
    }


def _calibrate_window(
    conn: Any,
    *,
    holder: str,
    feature: str,
    window_days: int,
    drop_corporate_actions: bool,
) -> dict[str, Any]:
    """Historical IC of ``feature`` measured at the nowcast's own window length."""
    try:
        frame = build_accumulation_frame(
            conn, holder=holder, window="early", early_days=window_days,
            drop_corporate_actions=drop_corporate_actions,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    frame["_momentum"] = frame["_window_return"]
    summary = _summarize(_period_partial_ic(frame, feature, "_momentum"))
    summary["window_days"] = float(window_days)
    return summary


def _verification_note(as_of: pd.Timestamp) -> dict[str, str]:
    """When the open quarter's filing lands, and so when this becomes checkable."""
    quarter_end = pd.Timestamp(as_of).to_period("Q").end_time.normalize()
    # SEBI allows 21 days after the quarter for the filing; NSE publication has
    # run a few weeks behind that across this dataset.
    return {
        "open_quarter_ends": str(quarter_end.date()),
        "filing_expected": str((quarter_end + pd.Timedelta(days=45)).date()),
        "how": (
            "re-run `mktmtm fetch-shareholding`, then score this ranking against "
            "the realised mf_shares change for the quarter"
        ),
    }


def write_nowcast_record(result: dict[str, Any], path: Path) -> Path:
    """Commit the prediction to a markdown record, git-timestamped.

    The point of a forward prediction is that it is fixed *before* the outcome
    is known. `models/` is gitignored, so an artifact there proves nothing
    about when it was written; a committed file does.
    """
    frame: pd.DataFrame = result["ranking"]
    cal = result["calibration"]
    v = result["verification"]

    def _table(rows: pd.DataFrame) -> str:
        lines = [
            "| Rank | Symbol | Score | `basis_excess_mean` | Window return |",
            "|---:|---|---:|---:|---:|",
        ]
        lines += [
            f"| {r.rank} | `{r.symbol}` | {r.score:+.1f} | {r.signal_raw:+.2f} "
            f"| {r.window_return:+.2%} |"
            for r in rows.itertuples(index=False)
        ]
        return "\n".join(lines)

    body = f"""# Nowcast — {result['holder'].upper()} accumulation, open quarter

**Made {result['as_of']}, before the outcome was observable.** This file is
committed so its timestamp is verifiable; `models/` is gitignored and proves
nothing about when a prediction was written.

| | |
|---|---|
| Signal | `{result['feature']}`, momentum-residualised |
| Window | {result['median_window_days']} days since each symbol's last filing |
| Universe | {result['symbols']} symbols ({result['dropped_stale_filing']} dropped for a stale filing) |
| **Calibrated IC at this window** | **{cal.get('mean_ic', float('nan')):+.4f}** |
| Historical hit rate | positive in {cal.get('positive_rate', 0):.0%} of {int(cal.get('periods', 0))} quarters |
| Open quarter ends | {v['open_quarter_ends']} |
| **Checkable from** | **~{v['filing_expected']}** |

## What this is, and is not

A **weak cross-sectional ranking**. An IC of {cal.get('mean_ic', float('nan')):+.3f}
means the ordering is right rather more often than chance and nothing
stronger — individual symbols will be wrong. The claim is about the *ranking*
across the universe, not about any name in it.

The score is residualised on the window's price return, because the raw signal
correlates with momentum and momentum explains *foreign* flow rather than
domestic fund flow. A raw ranking would largely be a momentum ranking.

## How to score it

```bash
uv run mktmtm fetch-shareholding      # once the open quarter's filing publishes
uv run mktmtm accumulation --holder mf --window contemporaneous
```

Then rank-correlate the `score` column below against the realised
`mf_shares` change for the quarter. Under the null the correlation is 0; the
calibration above says to expect ≈ {cal.get('mean_ic', float('nan')):+.3f}.

## Most likely under accumulation

{_table(frame.head(25))}

## Least likely

{_table(frame.tail(25))}

---

*Method: [`model-06-accumulation-detection.md`](../implementation/model-06-accumulation-detection.md).
Full ranking of all {result['symbols']} symbols is in
`models/evaluation/nowcast.json`.*
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)

    EVALUATION_DIR.mkdir(parents=True, exist_ok=True)
    (EVALUATION_DIR / "nowcast.json").write_text(
        json.dumps(
            {**{k: v for k, v in result.items() if k != "ranking"},
             "ranking": frame.to_dict(orient="records")},
            indent=2, default=str,
        ) + "\n"
    )
    return path
