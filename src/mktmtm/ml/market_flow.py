"""Stage 1 — market-level institutional flow model.

Predicts tomorrow's market-wide institutional positioning change from today's
derived option and futures features. This is the only place in the project
where a real institutional-flow label exists, so it is where the core
hypothesis — *these features carry information about institutional activity* —
gets its first honest test. Everything else is evaluated against price, which
is a proxy.

The label comes from ``participant_oi``: NSE's participant-wise open interest,
published after the close, one row per (date, category) for Client / DII / FII
/ Pro / TOTAL. ``data.participant_oi.add_net_positions`` derives the net
positions and their day-over-day flow; the flow at ``t+1`` is what this module
predicts from features observed at ``t``. NSE publishes after the close and our
features are end-of-day, so the information genuinely preceded the outcome.

Two things about this task are easy to get wrong and are handled explicitly:

**The calendar is the competitor, not chance.** Controlling the expiry cycle
halved every measured next-day correlation (``d_roll`` -> Pro calls fell from
+0.389 to +0.174). A model that only rediscovers "positions roll near expiry"
has found nothing, because the expiry calendar is known years in advance. The
headline number is therefore the margin over ``calendar_only``, not the AUC.

**n is about 1,000.** After the causal normalization warm-up there are roughly
a thousand rows and a few dozen features. That rules out anything with many
parameters; see ``docs/implementation/model-00-overview.md`` for why there is
no neural network here. It also means the Ridge-versus-XGBoost comparison is
itself a result: at this size a boosting blowout is more likely a leak than a
discovery.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from mktmtm.ml.market_metrics import (
    block_bootstrap_margin_ci,
    directional_auc,
    summarize_market,
)
from mktmtm.ml.splits import walk_forward_splits
from mktmtm.progress import progress_stage

logger = logging.getLogger(__name__)

__all__ = [
    "CORE_FEATURES",
    "DIRECTION_FEATURES",
    "EXTENDED_FEATURES",
    "TARGET_PARTICIPANTS",
    "aggregate_symbol_day",
    "build_market_frame",
    "build_targets",
    "expanding_rank_normalize",
    "run_market_flow",
    "walk_forward_market",
]

#: Participants modelled. Pro is the proprietary-desk book, the closest proxy
#: for the dealer whose gamma the GEX features describe; Client is the
#: retail/other side and is mechanically close to its opposite. Fitting both
#: and requiring the predictions to come out anti-correlated is a free
#: structural check — the market is a closed book, so both sides cannot
#: increase net call exposure on the same day.
TARGET_PARTICIPANTS = ("Pro", "Client", "FII")

#: The default label: net stock-option call position, day-over-day.
DEFAULT_TARGET = "d_net_opt_stock_call"

#: Calendar features. Deliberately excluded from the rank normalization —
#: `fut_dte_med` is already in days and the day-of-week dummies are 0/1, and
#: percentile-transforming either would obscure exactly the baseline this
#: model has to beat.
CALENDAR_FEATURES = ("fut_dte_med", "dow_1", "dow_2", "dow_3", "dow_4")

#: The pinned core contract: identity *and* order. Each entry was either
#: measured as individually informative or is a required control.
CORE_FEATURES = (
    "basis_excess_med",      # strongest per-symbol signal measured (t = -20.7)
    "basis_excess_iqr",      # do symbols agree, or scatter?
    "basis_pct_med",
    "roll_ratio_med",
    "d_roll",                # strongest market-level link to Pro/Client
    "fut_oi_chg_notional_sum",
    "outlier_rate",
    "outlier_build_ratio",
    "outlier_call_frac",
    "gex_sum",
    "pcr_oi_med",
    "iv_30_med",             # the control that killed most raw correlations
    "chain_oi_chg_sum",
    *CALENDAR_FEATURES,
)

#: Stage 1b's direction features. Held separate so the ablation answers the
#: question the quadrant work left open: do they carry anything the
#: directionless columns did not?
DIRECTION_FEATURES = (
    "q_fut_direction_med",
    "opt_direction_med",
    "outlier_direction_med",
    "ce_writer_frac_med",
)

EXTENDED_FEATURES = (
    "rv_yz_med",
    "chain_breadth_med",
    "pc_iv_spread_med",
    "outlier_moneyness_wmean",
    "outlier_dte_wmean",
    "fut_oi_notional_sum",
    "dist_to_flip_med",
)

#: Today's participant flow, known at t. Measured autocorrelation is ~0
#: (FII lag-1 = -0.019), so these are expected to contribute almost nothing —
#: but "the model beats persistence" is only meaningful if persistence was
#: actually available to it.
_PERSISTENCE_BASE = (
    "d_net_opt_stock_call",
    "d_net_opt_stock_put",
    "d_net_fut_stock",
)

MIN_NORM_OBS = 250
N_SPLITS = 5
EMBARGO_DAYS = 1
RANDOM_SEED = 42

DEFAULT_PARQUET = Path("data/processed/market_flow.parquet")
EVALUATION_DIR = Path("models/evaluation")

_FLOW_SQL = """
SELECT f.symbol, f.date,
       f.fut_basis_excess, f.fut_basis_pct, f.fut_roll_ratio, f.fut_dte,
       f.fut_oi_chg_notional, f.fut_oi_notional,
       f.oi_outlier_count, f.oi_outlier_call_frac, f.oi_outlier_moneyness,
       f.oi_outlier_dte, f.oi_outlier_build_notional, f.oi_outlier_unwind_notional,
       f.q_fut_direction, f.opt_direction, f.oi_outlier_direction, f.ce_writer_frac,
       m.gex, m.put_call_oi_ratio, m.ce_iv_30, m.chain_oi_chg, m.rv_yz,
       m.chain_breadth, m.put_call_iv_spread, m.dist_to_flip
FROM flow_features f
JOIN ml_dataset m USING (symbol, date)
ORDER BY f.date, f.symbol
"""

_PARTICIPANT_SQL = "SELECT * FROM participant_oi ORDER BY date, client_type"


# ═════════════════════════ aggregation (pure) ══════════════════════════════


def aggregate_symbol_day(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse the per-symbol panel to one row per trading date. Pure — no I/O.

    Ratios and rates use the **median**, not the mean: the cross-section
    contains outliers by construction, since the outlier detector exists to
    find them. Notionals are **summed**, because they are additive and the
    tails are the signal. The basis family also reports a cross-sectional
    p75-p25, because a day when every symbol's basis moves together is a
    different market from one where they scatter, and the median hides that.
    """
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"]).astype("datetime64[us]")
    grouped = work.groupby("date", sort=True)

    out = pd.DataFrame(index=grouped.size().index)
    out["basis_excess_med"] = grouped["fut_basis_excess"].median()
    out["basis_excess_iqr"] = grouped["fut_basis_excess"].quantile(
        0.75
    ) - grouped["fut_basis_excess"].quantile(0.25)
    out["basis_pct_med"] = grouped["fut_basis_pct"].median()
    out["roll_ratio_med"] = grouped["fut_roll_ratio"].median()
    out["fut_dte_med"] = grouped["fut_dte"].median()
    out["fut_oi_chg_notional_sum"] = grouped["fut_oi_chg_notional"].sum(min_count=1)
    out["fut_oi_notional_sum"] = grouped["fut_oi_notional"].sum(min_count=1)

    # Share of symbols with any flagged strike that day.
    out["outlier_rate"] = grouped["oi_outlier_count"].apply(
        lambda s: float((s.fillna(0) > 0).mean())
    )
    build = grouped["oi_outlier_build_notional"].sum(min_count=1)
    unwind = grouped["oi_outlier_unwind_notional"].sum(min_count=1)
    total = build + unwind
    out["outlier_build_ratio"] = build / total.where(total > 0)

    # Notional-weighted means, accumulated as numerator/denominator sums and
    # divided once: a 400-crore build should move the reported call share far
    # more than a 4-crore one. Vectorised rather than a groupby.apply, which
    # re-enters pandas' indexing machinery once per date.
    weight = work["oi_outlier_build_notional"].fillna(0) + work[
        "oi_outlier_unwind_notional"
    ].fillna(0)
    for name, col in (
        ("outlier_call_frac", "oi_outlier_call_frac"),
        ("outlier_moneyness_wmean", "oi_outlier_moneyness"),
        ("outlier_dte_wmean", "oi_outlier_dte"),
    ):
        usable = work[col].notna() & (weight > 0)
        work["_num"] = (work[col] * weight).where(usable)
        work["_den"] = weight.where(usable)
        regrouped = work.groupby("date", sort=True)
        num = regrouped["_num"].sum(min_count=1)
        den = regrouped["_den"].sum(min_count=1)
        out[name] = num / den.where(den > 0)
    work = work.drop(columns=["_num", "_den"], errors="ignore")

    out["gex_sum"] = grouped["gex"].sum(min_count=1)
    out["chain_oi_chg_sum"] = grouped["chain_oi_chg"].sum(min_count=1)
    out["pcr_oi_med"] = grouped["put_call_oi_ratio"].median()
    out["iv_30_med"] = grouped["ce_iv_30"].median()
    out["rv_yz_med"] = grouped["rv_yz"].median()
    out["chain_breadth_med"] = grouped["chain_breadth"].median()
    out["pc_iv_spread_med"] = grouped["put_call_iv_spread"].median()
    out["dist_to_flip_med"] = grouped["dist_to_flip"].median()

    # Stage 1b direction features.
    out["q_fut_direction_med"] = grouped["q_fut_direction"].median()
    out["opt_direction_med"] = grouped["opt_direction"].median()
    out["outlier_direction_med"] = grouped["oi_outlier_direction"].median()
    out["ce_writer_frac_med"] = grouped["ce_writer_frac"].median()

    out = out.reset_index()
    # Differenced features come after aggregation, never before: differencing
    # per symbol and then averaging the differences answers a different
    # question, and drops every symbol that entered the panel that day.
    out["d_roll"] = out["roll_ratio_med"].diff()

    dow = out["date"].dt.dayofweek
    for d in range(1, 5):  # Monday is the reference level
        out[f"dow_{d}"] = (dow == d).astype("float64")
    return out


def build_targets(
    participants: pd.DataFrame,
    *,
    participant: str,
    column: str = DEFAULT_TARGET,
    kind: Literal["sign", "magnitude"] = "sign",
) -> pd.DataFrame:
    """``(date, y, y_magnitude, persistence)`` for one participant. Pure — no I/O.

    ``y`` at date ``t`` is built from the flow observed at ``t+1``: the label is
    tomorrow's positioning change, predicted from today's features. The last
    date has no tomorrow and gets ``NaN`` — an inference row, never a training
    row, the same rule ``ml/dataset.py`` applies to its targets.

    ``persistence`` is *today's* flow, which the persistence baseline needs and
    which is legitimately known at ``t``.
    """
    part = participants.loc[participants["client_type"] == participant].copy()
    if part.empty:
        raise ValueError(f"participant_oi has no rows for client_type={participant!r}")
    part["date"] = pd.to_datetime(part["date"]).astype("datetime64[us]")
    part = part.sort_values("date", kind="stable").reset_index(drop=True)

    today = part[column].astype("float64")
    tomorrow = today.shift(-1)
    out = pd.DataFrame(
        {
            "date": part["date"],
            "y": (tomorrow > 0).astype("float64").where(tomorrow.notna()),
            "y_magnitude": tomorrow,
            "persistence": today,
        }
    )
    if kind == "magnitude":
        out["y"] = tomorrow
    return out


def expanding_rank_normalize(
    frame: pd.DataFrame,
    features: list[str] | tuple[str, ...],
    *,
    min_obs: int = MIN_NORM_OBS,
) -> pd.DataFrame:
    """Percentile-transform each feature against its own past only. Pure — no I/O.

    The value at ``t`` becomes the fraction of observations strictly before
    ``t`` that were smaller. A global ``StandardScaler`` or a whole-sample rank
    would leak the future distribution into every training row — the same bug
    class ``validate_no_lookahead`` exists to catch elsewhere in this pipeline,
    and the reason ``test_expanding_normalize_has_no_lookahead`` asserts that
    appending future rows leaves earlier values bit-identical.

    Rows before ``min_obs`` observations exist are ``NaN``, not 0.5. This costs
    the first trading year; that is the price of an honest normalization.
    """
    out = frame.copy()
    n = len(frame)
    for col in features:
        values = frame[col].to_numpy(dtype="float64")
        scored = np.full(n, np.nan)
        for t in range(n):
            history = values[:t]
            history = history[~np.isnan(history)]
            if history.size < min_obs or np.isnan(values[t]):
                continue
            scored[t] = float((history < values[t]).mean())
        out[col] = scored
    return out


# ═════════════════════════ assembly (I/O) ═══════════════════════════════════


def build_market_frame(
    conn: Any,
    *,
    participant: str = "Pro",
    column: str = DEFAULT_TARGET,
    normalize: bool = True,
) -> pd.DataFrame:
    """One row per trading date: market features at ``t``, label from ``t+1``."""
    panel = pd.read_sql(_FLOW_SQL, conn)
    if panel.empty:
        raise ValueError("flow_features JOIN ml_dataset returned no rows")
    participants = pd.read_sql(_PARTICIPANT_SQL, conn)

    from mktmtm.data.participant_oi import add_net_positions

    nets = add_net_positions(participants)
    targets = build_targets(nets, participant=participant, column=column)

    features = aggregate_symbol_day(panel)
    # Inner join: participant_oi runs a few days past the feature tables, and
    # those dates have no features. The last feature date has no tomorrow and
    # so no label; it is kept here and dropped at training time, so the frame
    # can still serve inference.
    frame = features.merge(targets, on="date", how="inner")
    frame = frame.sort_values("date", kind="stable").reset_index(drop=True)

    if normalize:
        normalizable = [
            c
            for c in (*CORE_FEATURES, *DIRECTION_FEATURES, *EXTENDED_FEATURES)
            if c not in CALENDAR_FEATURES and c in frame.columns
        ]
        frame = expanding_rank_normalize(frame, normalizable)
    return frame


# ═════════════════════════ models ═══════════════════════════════════════════


def _fit_predict(
    train: pd.DataFrame,
    valid: pd.DataFrame,
    features: list[str],
    *,
    model: str,
    seed: int = RANDOM_SEED,
) -> np.ndarray:
    """Fit on ``train``, return P(y=1) for ``valid``. Rows with NaN are dropped."""
    x_train = train[features]
    ok = x_train.notna().all(axis=1) & train["y"].notna()
    x_train, y_train = x_train.loc[ok], train.loc[ok, "y"].astype(int)
    if len(x_train) < 50 or y_train.nunique() < 2:
        return np.full(len(valid), np.nan)

    x_valid = valid[features]
    if model == "ridge":
        from sklearn.linear_model import (  # type: ignore[import-untyped]
            LogisticRegression,
        )

        from mktmtm.ml.splits import inner_holdout

        # C is selected on a holdout carved from the train tail, never on the
        # rows this fold is then scored on.
        best_c, best_auc = 1.0, -np.inf
        try:
            inner_train, inner_valid = inner_holdout(train.loc[ok], frac=0.2)
        except ValueError:
            inner_train, inner_valid = train.loc[ok], train.loc[ok]
        for c in (0.01, 0.03, 0.1, 0.3, 1.0, 3.0):
            fitted = _fit_logit(inner_train, features, c, seed)
            if fitted is None:
                continue
            scores = _predict(fitted, inner_valid[features])
            auc = directional_auc(
                inner_valid["y"].reset_index(drop=True),
                pd.Series(scores).reset_index(drop=True),
            )
            if auc > best_auc:
                best_c, best_auc = c, auc
        # sklearn 1.9 deprecates penalty= in favour of l1_ratio=; 0 is pure L2.
        estimator = LogisticRegression(
            l1_ratio=0.0, C=best_c, solver="liblinear", random_state=seed,
            max_iter=2000,
        )
        estimator.fit(x_train.to_numpy(), y_train.to_numpy())
        return _predict(estimator, x_valid)

    if model == "xgboost":
        import xgboost as xgb

        from mktmtm.ml.splits import inner_holdout

        try:
            inner_train, inner_valid = inner_holdout(train.loc[ok], frac=0.2)
        except ValueError:
            inner_train, inner_valid = train.loc[ok], train.loc[ok]
        estimator = xgb.XGBClassifier(
            objective="binary:logistic",
            eval_metric="auc",
            max_depth=2,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.6,
            min_child_weight=20,
            reg_lambda=5.0,
            reg_alpha=1.0,
            n_estimators=2000,
            early_stopping_rounds=100,
            random_state=seed,
            verbosity=0,
        )
        estimator.fit(
            inner_train[features].to_numpy(),
            inner_train["y"].astype(int).to_numpy(),
            eval_set=[(inner_valid[features].to_numpy(),
                       inner_valid["y"].astype(int).to_numpy())],
            verbose=False,
        )
        return np.asarray(estimator.predict_proba(x_valid.to_numpy())[:, 1])

    raise ValueError(f"unknown model {model!r}")


def _fit_logit(
    frame: pd.DataFrame, features: list[str], c: float, seed: int
) -> Any | None:
    from sklearn.linear_model import LogisticRegression

    ok = frame[features].notna().all(axis=1) & frame["y"].notna()
    if ok.sum() < 30 or frame.loc[ok, "y"].nunique() < 2:
        return None
    estimator = LogisticRegression(
        l1_ratio=0.0, C=c, solver="liblinear", random_state=seed, max_iter=2000
    )
    estimator.fit(frame.loc[ok, features].to_numpy(), frame.loc[ok, "y"].astype(int))
    return estimator


def _predict(estimator: Any, x: pd.DataFrame) -> np.ndarray:
    """P(y=1), NaN wherever a feature is missing rather than imputed."""
    out = np.full(len(x), np.nan)
    ok = x.notna().all(axis=1).to_numpy()
    if ok.any():
        out[ok] = estimator.predict_proba(x.loc[ok].to_numpy())[:, 1]
    return out


def walk_forward_market(
    frame: pd.DataFrame,
    features: list[str] | tuple[str, ...],
    *,
    model: str = "ridge",
    n_splits: int = N_SPLITS,
    seed: int = RANDOM_SEED,
) -> pd.DataFrame:
    """Out-of-sample predictions from expanding-window folds with an embargo.

    Returns ``(date, y, y_magnitude, pred, fold)`` for the validation rows of
    every fold. Rows whose features are still in the normalization warm-up are
    dropped before splitting, so the first fold trains on real values.
    """
    feats = list(features)
    usable = frame.loc[
        frame[feats].notna().all(axis=1) & frame["y"].notna()
    ].reset_index(drop=True)
    if len(usable) < 300:
        raise ValueError(
            f"only {len(usable)} usable rows after the normalization warm-up — "
            "too few for a walk-forward evaluation"
        )

    parts: list[pd.DataFrame] = []
    for fold, (train_idx, valid_idx) in enumerate(
        walk_forward_splits(
            usable["date"], n_splits=n_splits, embargo_days=EMBARGO_DAYS,
            min_train_dates=250,
        )
    ):
        train, valid = usable.iloc[train_idx], usable.iloc[valid_idx]
        pred = _fit_predict(train, valid, feats, model=model, seed=seed)
        parts.append(
            valid[["date", "y", "y_magnitude", "persistence"]]
            .assign(pred=pred, fold=fold)
            .reset_index(drop=True)
        )
    return pd.concat(parts, ignore_index=True)


def score_static_baselines(scored: pd.DataFrame, seed: int = RANDOM_SEED) -> dict[str, pd.Series]:
    """Baselines needing no fit, aligned to the rows a model was scored on."""
    rng = np.random.default_rng(seed)
    majority = float(scored["y"].mean() > 0.5)
    return {
        # Always predict the majority class: accuracy = base rate, AUC = 0.5.
        "constant": pd.Series(majority, index=scored.index),
        "random_uniform": pd.Series(rng.random(len(scored)), index=scored.index),
        # Sign of today's flow. Measured autocorrelation is ~0, so this should
        # land near 0.5 — which is itself worth reporting.
        "persistence": (scored["persistence"] > 0).astype("float64"),
        # Leakage tripwire: tomorrow's flow, the label itself. Must score near
        # 1.0. If it does not, the harness is broken — a stronger signal than
        # any passing test.
        "leak_tripwire": (scored["y_magnitude"] > 0).astype("float64"),
    }


# ═════════════════════════ orchestration ════════════════════════════════════


def _git_sha() -> str:
    """Short HEAD SHA for provenance, or 'unknown' outside a repo."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def _feature_sets() -> dict[str, tuple[str, ...]]:
    """The ablation ladder. `calendar_only` is the baseline that actually matters."""
    return {
        "calendar_only": CALENDAR_FEATURES,
        "core": CORE_FEATURES,
        "core+direction": (*CORE_FEATURES, *DIRECTION_FEATURES),
        "core+direction+extended": (
            *CORE_FEATURES, *DIRECTION_FEATURES, *EXTENDED_FEATURES
        ),
    }


def evaluate_participant(
    frame: pd.DataFrame,
    *,
    participant: str,
    models: tuple[str, ...] = ("ridge", "xgboost"),
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Every (feature set x model) combination, plus baselines and the margin CI."""
    results: dict[str, Any] = {}
    predictions: dict[str, pd.DataFrame] = {}

    # Every model is scored on the SAME rows. Without this the calendar
    # baseline runs on ~1,000 dates while the flow models run on ~560, because
    # `fut_basis_excess` is NULL whenever dte < 5 and so the market-level
    # median vanishes for the whole of every expiry week. Comparing absolute
    # AUCs across different row sets would flatter whichever model happened to
    # be evaluated on the easier dates — and here that is the baseline, on the
    # very days the calendar is most informative.
    every_feature = sorted({f for fs in _feature_sets().values() for f in fs})
    common = frame.loc[
        frame[every_feature].notna().all(axis=1) & frame["y"].notna()
    ].reset_index(drop=True)
    dropped = int(len(frame) - len(common))

    combinations = [
        (set_name, features, model)
        for set_name, features in _feature_sets().items()
        for model in models
        # The calendar baseline is a control, not a horse race entrant.
        if not (set_name == "calendar_only" and model != "ridge")
    ]
    with progress_stage(
        f"Fitting {participant} flow models", total=len(combinations), unit="fits"
    ) as bar:
        for set_name, features, model in combinations:
            key = f"{set_name}:{model}"
            bar.set_label(key)
            try:
                scored = walk_forward_market(
                    common, features, model=model, seed=seed
                )
            except ValueError as exc:
                results[key] = {"error": str(exc)}
                bar.advance()
                continue
            predictions[key] = scored
            results[key] = summarize_market(
                scored, score_col="pred", label_col="y",
                magnitude_col="y_magnitude",
            )
            bar.advance()

    reference = predictions.get("calendar_only:ridge")
    if reference is not None:
        baselines = score_static_baselines(reference, seed=seed)
        results["baselines"] = {
            name: summarize_market(
                reference.assign(pred=scores), score_col="pred", label_col="y",
                magnitude_col="y_magnitude",
            )
            for name, scores in baselines.items()
        }

        # The headline: margin over the calendar, with a block-bootstrap CI.
        # Paired on the same blocks, so the interval reflects the comparison
        # rather than two independent noisy estimates.
        margins: dict[str, Any] = {}
        for key, scored in predictions.items():
            if key == "calendar_only:ridge":
                continue
            joined = scored.merge(
                reference[["date", "pred"]], on="date", how="inner",
                suffixes=("", "_cal"),
            )
            margin, lo, hi = block_bootstrap_margin_ci(
                joined["y"], joined["pred"], joined["pred_cal"], seed=seed
            )
            margins[key] = {
                "auc_margin_over_calendar": margin,
                "ci_low": lo,
                "ci_high": hi,
                "significant": bool(np.isfinite(lo) and lo > 0.0),
            }
        results["calendar_margin"] = margins

    results["_meta"] = {
        "participant": participant,
        "rows": int(len(frame)),
        "rows_scored": int(len(common)),
        "rows_dropped_incomplete": dropped,
        "period": f"{frame['date'].min().date()} → {frame['date'].max().date()}",
        "label_base_rate": float(frame["y"].mean(skipna=True)),
        "git_sha": _git_sha(),
    }
    return results


def run_market_flow(
    conn: Any,
    *,
    participants: tuple[str, ...] = TARGET_PARTICIPANTS,
    models: tuple[str, ...] = ("ridge", "xgboost"),
    column: str = DEFAULT_TARGET,
    out_dir: Path = EVALUATION_DIR,
    parquet: bool = True,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Build, evaluate and persist the market-level flow model. CLI entry point."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}
    best_preds: dict[str, pd.DataFrame] = {}

    for participant in participants:
        logger.info("building market frame for %s", participant)
        frame = build_market_frame(conn, participant=participant, column=column)
        if parquet and participant == participants[0]:
            DEFAULT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(DEFAULT_PARQUET, index=False)
        result = evaluate_participant(
            frame, participant=participant, models=models, seed=seed
        )
        (out_dir / f"market_flow_{participant.lower()}.json").write_text(
            json.dumps(result, indent=2, default=float) + "\n"
        )
        summary[participant] = result

        # Kept for the structural check below.
        with contextlib.suppress(ValueError):
            best_preds[participant] = walk_forward_market(
                frame, (*CORE_FEATURES, *DIRECTION_FEATURES), model="ridge", seed=seed
            )

    # Pro and Client are opposite sides of a closed book. If the model predicts
    # both will increase net call exposure on the same day it has learned
    # noise, so this correlation is a structural validity check, not a metric.
    if {"Pro", "Client"} <= set(best_preds):
        joined = best_preds["Pro"].merge(
            best_preds["Client"], on="date", suffixes=("_pro", "_client")
        )
        corr = float(joined["pred_pro"].corr(joined["pred_client"]))
        summary["_structural_check"] = {
            "pro_client_prediction_corr": corr,
            "expected": "negative — opposite sides of a closed book",
            "passes": bool(corr < 0.0),
        }
        (out_dir / "market_flow_structural.json").write_text(
            json.dumps(summary["_structural_check"], indent=2) + "\n"
        )
    return summary
