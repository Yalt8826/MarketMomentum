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
    row: Any, window: Window
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Feature window for one (symbol, filing) row, per the three variants."""
    if window == "contemporaneous":
        return row.prev_date, row.date
    if window == "early":
        if pd.isna(row.prev_date):
            return None, None
        return row.prev_date, row.prev_date + pd.Timedelta(days=EARLY_DAYS)
    if window == "lead":
        return row.prev_prev_date, row.prev_date
    raise ValueError(f"unknown window {window!r}")


def build_accumulation_frame(
    conn: Any,
    *,
    holder: str = "fii",
    window: Window = "contemporaneous",
    drop_corporate_actions: bool = True,
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
        start, end = _window_bounds(row, window)
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
