"""Does whale positioning have momentum? — the project's title question, stated literally.

Every stage before this asked a *prediction* or *attribution* question: will
institutions act tomorrow (Stage 1), where did today's flow go (Stage 3). This
one asks whether a directional build in a symbol **continues** — if somebody is
accumulating in RELIANCE today, is there more accumulation in RELIANCE over the
next week than in a comparable symbol without today's signal?

That is a different question from Stage 2's, which asked whether symbol-days
fall into discrete persistent *states* and answered no. Continuation of a
continuous signal is exactly what a discrete-state model could not represent,
so Stage 2's negative result does not settle this one.

**Self-supervised: no external label.** Signal and target are both built from
`flow_features`, so this needs no data the project does not already have — which
matters, because the absence of a per-symbol institutional label is what shaped
every previous stage.

## Why the metric is a cross-sectional rank IC

The panel carries ~170 symbols per date, so this is a cross-sectional task and
`ml.metrics.daily_rank_ic` applies directly. Using a *within-date* rank
correlation is not a convenience — it neutralises the two confounds that would
otherwise dominate:

- **The market-wide common factor.** If every symbol drifts together, a pooled
  correlation would report that drift as per-symbol momentum. Ranking within a
  date removes anything common to the whole cross-section.
- **The expiry calendar**, which explained roughly half of everything in
  Stage 1. All NSE stock futures share one monthly expiry, so `fut_dte` is
  near-constant within a date — measured at a single value on 83.6% of dates,
  median cross-sectional spread 0. A within-date correlation therefore cannot
  be driven by the roll cycle.

## The overlapping-window trap

For horizon ``h`` the target at ``t`` and at ``t+1`` share ``h-1`` forward days,
so the daily IC series is autocorrelated *by construction* and a naive t-stat
overstates significance by roughly ``sqrt(h)``. Every interval here comes from a
moving-block bootstrap with a block of at least ``2h``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mktmtm.ml.market_metrics import block_bootstrap_mean_ci
from mktmtm.ml.metrics import daily_rank_ic, icir

logger = logging.getLogger(__name__)

__all__ = [
    "HORIZONS",
    "SIGNALS",
    "TARGETS",
    "build_momentum_frame",
    "forward_mean",
    "momentum_table",
    "run_flow_momentum",
]

#: Signals tested. The first three are Stage 1b's direction features — the only
#: feature block that measurably improved anything in Stage 1. `oi_outlier_share`
#: is included as a *magnitude* contrast: if activity alone shows the same
#: continuation as direction, then nothing here is about direction.
SIGNALS = (
    "q_fut_direction",
    "opt_direction",
    "oi_outlier_direction",
    "ce_writer_frac",
    "oi_outlier_share",
)

#: Forward horizons in trading days. A real effect decays smoothly across these;
#: an erratic profile is the signature of noise.
HORIZONS = (1, 2, 3, 5, 10)

#: What continuation is measured *in*. Both are dense and directional, so both
#: can carry a sign — and testing each signal against **both** is what separates
#: a behavioural lead from a mechanical artefact.
#:
#: Open interest is a bounded stock, so its flow is negatively autocorrelated
#: *by construction*: a position opened today must eventually close. That alone
#: can produce apparent short-horizon "reversal" in any direction score
#: measured against itself. It cannot, however, produce a **positive**
#: cross-instrument lead — futures and options are different OI pools — so the
#: off-diagonal cells of the signal x target grid are the ones that carry
#: information the diagonal cannot.
TARGETS = ("q_fut_direction", "opt_direction")

#: Kept for callers that want the historical single-target behaviour.
TARGET_COLUMN = TARGETS[0]

RANDOM_SEED = 42
MIN_SYMBOLS_PER_DATE = 30
EVALUATION_DIR = Path("models/evaluation")
DEFAULT_PARQUET = Path("data/processed/flow_momentum.parquet")

_PANEL_SQL = """
SELECT symbol, date,
       q_fut_direction, opt_direction, oi_outlier_direction,
       ce_writer_frac, oi_outlier_share, fut_dte, fut_oi_notional
FROM flow_features
ORDER BY symbol, date
"""


def forward_mean(frame: pd.DataFrame, column: str, horizon: int) -> pd.Series:
    """Mean of ``column`` over ``t+1 .. t+horizon``, within each symbol. Pure.

    ``shift(-1)`` drops today, ``rolling(h)`` accumulates the next ``h`` days,
    and the final ``shift(-(h-1))`` moves the window's right edge back onto
    ``t``. The trailing shift is not cosmetic: without it the first ``h-1``
    rows of every symbol come back ``NaN`` even though their forward window is
    fully populated, silently discarding the earliest observations.

    The last ``h`` rows of every symbol are ``NaN`` — there is no future to
    average. Those are inference rows, never training rows, the same rule
    ``ml/dataset.py`` applies to its targets.

    Row order is load-bearing: the rolling window counts *rows*, so the frame
    is sorted internally and the result is reindexed back onto the caller's
    order. Returning results in sorted order would be a trap, since callers
    assign the series positionally. Rows are consecutive trading days for that
    symbol; a symbol that did not trade on some date has that day absent rather
    than filled, so a gap compresses the window slightly.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be >= 1, got {horizon}")
    ordered = frame.sort_values(["symbol", "date"], kind="stable")
    scored = ordered.groupby("symbol", observed=True, sort=False)[column].transform(
        lambda s: s.shift(-1)
        .rolling(horizon, min_periods=horizon)
        .mean()
        .shift(-(horizon - 1))
    )
    return scored.reindex(frame.index)


def build_momentum_frame(conn: Any) -> pd.DataFrame:
    """Per (symbol, date): the signals, plus a forward target for every horizon."""
    panel = pd.read_sql(_PANEL_SQL, conn)
    if panel.empty:
        raise ValueError("flow_features returned no rows")
    panel["date"] = pd.to_datetime(panel["date"]).astype("datetime64[us]")
    panel = panel.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)

    for target in TARGETS:
        for h in HORIZONS:
            panel[f"fwd_{target}_{h}"] = forward_mean(panel, target, h)

    # A date with too thin a cross-section cannot support a rank correlation.
    counts = panel.groupby("date")["symbol"].transform("size")
    return panel.loc[counts >= MIN_SYMBOLS_PER_DATE].reset_index(drop=True)


def _ic_summary(
    frame: pd.DataFrame, signal: str, target: str, *, horizon: int, seed: int
) -> dict[str, float]:
    """Mean cross-sectional rank IC with a horizon-aware confidence interval."""
    ic = daily_rank_ic(frame, pred_col=signal, ret_col=target)
    # Block at least twice the horizon, so each resampled block carries the
    # overlap the forward window creates.
    block = max(21, 2 * horizon)
    mean, lo, hi = block_bootstrap_mean_ci(ic, block=block, seed=seed)
    n = int(len(ic))
    return {
        "mean_ic": mean,
        "ci_low": lo,
        "ci_high": hi,
        "icir": icir(ic),
        # Reported for comparison only — it assumes independent days, which
        # overlapping forward windows violate. The CI is the honest number.
        "naive_t_stat": (
            icir(ic) * float(np.sqrt(n)) if n > 1 else 0.0
        ),
        "days": float(n),
        "significant": float(bool(np.isfinite(lo) and (lo > 0.0 or hi < 0.0))),
    }


def _lead_lag(
    frame: pd.DataFrame, signal: str, target: str, lag: int
) -> dict[str, float]:
    """Per-symbol correlation of ``signal(t)`` with ``target(t+lag)``, aggregated.

    Raw Pearson within each symbol, then averaged — no ranking, no
    normalisation, no cross-section, so none of the confounds that affect the
    other tables apply here. ``consistency`` is the fraction of symbols whose
    correlation shares the mean's sign; a genuine effect is broad, an artefact
    is usually a few symbols dragging an average.
    """
    rows: list[float] = []
    partials: list[float] = []
    for _symbol, group in frame.groupby("symbol", observed=True, sort=False):
        x = group[signal]
        y = group[target].shift(-lag) if lag else group[target]
        z = group[target]  # the same-day value of the thing being predicted
        mask = x.notna() & y.notna()
        if int(mask.sum()) <= 60:
            continue
        corr = x[mask].corr(y[mask])
        if not np.isfinite(corr):
            continue
        rows.append(float(corr))
        if lag:
            both = mask & z.notna()
            if int(both.sum()) > 60:
                partials.append(_partial(x[both], y[both], z[both]))

    if not rows:
        return {"mean": float("nan"), "median": float("nan"),
                "consistency": float("nan"), "symbols": 0.0,
                "partial": float("nan"), "partial_consistency": float("nan")}
    values = np.asarray(rows)
    mean = float(values.mean())
    clean = np.asarray([v for v in partials if np.isfinite(v)])
    partial_mean = float(clean.mean()) if clean.size else float("nan")
    return {
        "mean": mean,
        "median": float(np.median(values)),
        "consistency": float((np.sign(values) == np.sign(mean)).mean()),
        "symbols": float(len(values)),
        # The raw lead double-counts a mechanical route: the signal is
        # correlated with the target *today*, and the target predicts its own
        # future. Partialling out today's target removes exactly that path, and
        # is what separates a genuine lead from an inherited reversal.
        "partial": partial_mean,
        "partial_consistency": (
            float((np.sign(clean) == np.sign(partial_mean)).mean())
            if clean.size else float("nan")
        ),
    }


def _partial(x: pd.Series, y: pd.Series, z: pd.Series) -> float:
    """First-order partial correlation of ``x`` and ``y`` controlling ``z``."""
    rxy, rxz, ryz = x.corr(y), x.corr(z), y.corr(z)
    if not all(np.isfinite(v) for v in (rxy, rxz, ryz)):
        return float("nan")
    denominator = np.sqrt((1.0 - rxz**2) * (1.0 - ryz**2))
    return float((rxy - rxz * ryz) / denominator) if denominator > 0 else float("nan")


def momentum_table(
    frame: pd.DataFrame,
    *,
    signals: tuple[str, ...] = SIGNALS,
    targets: tuple[str, ...] = TARGETS,
    horizons: tuple[int, ...] = HORIZONS,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Every (signal x target x horizon) cell, plus the baselines calibrating them.

    The grid's **diagonal** (signal measured against its own forward values) is
    contaminated by the bounded-OI artefact described at :data:`TARGETS`. The
    **off-diagonal** cells are not, and are where a genuine lead would show.
    """
    out: dict[str, Any] = {"grid": {}, "baselines": {}, "n_tests": 0}
    rng = np.random.default_rng(seed)
    work = frame.copy()

    for target in targets:
        cells: dict[str, Any] = {}
        for signal in signals:
            if signal not in work.columns:
                continue
            cells[signal] = {
                str(h): _ic_summary(
                    work, signal, f"fwd_{target}_{h}", horizon=h, seed=seed
                )
                for h in horizons
            }
            out["n_tests"] += len(horizons)
        out["grid"][target] = cells

    # ── Diagnostic 1: contemporaneous IC ───────────────────────────────────
    # Several signals share construction components with `opt_direction`
    # (`ce_writer_frac` uses the same CE buckets; `oi_outlier_direction` is the
    # same quadrant logic on a subset of strikes). A signal that is a near-mirror
    # of the target today will appear to "lead" it tomorrow purely by inheriting
    # the target's own reversal. This table is what exposes that: a forward IC is
    # only interesting if it is much larger than
    # ``contemporaneous_ic x target_self_ic``.
    out["contemporaneous"] = {}
    for target in targets:
        out["contemporaneous"][target] = {
            signal: float(
                daily_rank_ic(work, pred_col=signal, ret_col=target).mean()
            )
            for signal in signals
            if signal in work.columns and signal != target
        }

    # ── Diagnostic 2: raw within-symbol lead-lag ───────────────────────────
    # The decisive test, and the only one free of every confound above. A
    # cross-sectional IC removes date effects but not symbol fixed effects, and
    # the obvious repair — causal within-symbol z-scoring — is worse: it
    # subtracts a *slowly updating* expanding mean, so consecutive z-scores
    # share a common baseline error and appear strongly persistent even when the
    # raw series has no autocorrelation at all. Measured here, that route
    # reported a self-persistence of +0.298 where the raw series shows -0.009.
    #
    # So this table takes the plain per-symbol Pearson correlation of
    # ``signal(t)`` against ``target(t+lag)``: no ranking, no normalisation, no
    # cross-section. ``consistency`` is the share of symbols agreeing with the
    # mean's sign — the number that separates a broad effect from a few
    # outliers dragging an average.
    out["within_symbol_leadlag"] = {
        target: {
            signal: {
                str(lag): _lead_lag(work, signal, target, lag)
                for lag in (0, *horizons)
            }
            for signal in signals
            if signal in work.columns
        }
        for target in targets
    }

    primary = targets[0]

    # Shuffled within date: destroys the cross-sectional ordering while keeping
    # every marginal distribution. Must give IC ~ 0 — and if it ever comes back
    # significant, that is the false-positive rate showing itself, not a signal.
    work["_shuffled"] = (
        work.groupby("date", observed=True)[primary]
        .transform(lambda s: rng.permutation(s.to_numpy()))
    )
    out["baselines"]["shuffled_within_date"] = {
        str(h): _ic_summary(
            work, "_shuffled", f"fwd_{primary}_{h}", horizon=h, seed=seed
        )
        for h in horizons
    }

    # Size. The confound that turned the pooled GEX H1 result into an artefact:
    # if liquidity alone ranks forward direction, any signal correlated with
    # liquidity inherits the appearance of momentum.
    if "fut_oi_notional" in work.columns:
        out["baselines"]["size_fut_oi_notional"] = {
            str(h): _ic_summary(
                work, "fut_oi_notional", f"fwd_{primary}_{h}", horizon=h, seed=seed
            )
            for h in horizons
        }

    # With this many cells, some will clear a 95% interval by chance. State the
    # expected count rather than leaving the reader to work it out.
    out["expected_false_positives"] = 0.05 * float(
        out["n_tests"] + len(horizons) * len(out["baselines"])
    )
    return out


def run_flow_momentum(
    conn: Any,
    *,
    out_dir: Path = EVALUATION_DIR,
    parquet: bool = True,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Build the frame, measure continuation, persist. CLI entry point."""
    frame = build_momentum_frame(conn)
    result = momentum_table(frame, seed=seed)
    result["_meta"] = {
        "rows": int(len(frame)),
        "symbols": int(frame["symbol"].nunique()),
        "dates": int(frame["date"].nunique()),
        "period": f"{frame['date'].min().date()} → {frame['date'].max().date()}",
        "targets": list(TARGETS),
        "horizons": list(HORIZONS),
    }
    if parquet:
        DEFAULT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(DEFAULT_PARQUET, index=False)
        result["_meta"]["parquet"] = str(DEFAULT_PARQUET)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "flow_momentum.json").write_text(
        json.dumps(result, indent=2, default=float) + "\n"
    )
    return result
