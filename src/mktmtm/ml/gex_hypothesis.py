"""Phase 6 — GEX/DEX hypothesis tests: H1 sign test, H2 positioning change, model.

Implements Phase 6 of ``docs/implementation/gex-dealer-positioning.md``:

- **Task 6.1 — H1, the sign test (no ML).** Split symbol-days by ``sign(gex)``,
  compare mean ``next_day_abs_return`` per group with a Welch t-test, and the
  cross-sectional rank IC of ``-gex`` against ``next_day_abs_return``.
- **Task 6.2 — H2, positioning change.** Do ``gex_chg`` / ``otm_put_oi_build``
  predict next-day absolute return *beyond* the ``gex`` level? Same protocol,
  plus a partial IC controlling for the level.
- **Task 6.3 — the model (only if 6.1 or 6.2 shows signal).** XGBoost regression
  (``objective="reg:squarederror"``) on ``next_day_abs_return``; config adapted
  from ``docs/implementation/xgboost-direction-model.md`` Task 6.1, early-stopped
  on ``inner_holdout`` — never on the reporting split.
- **Task 6.4 — robustness (mandatory).** Flip ``DEALER_SIGN``; liquidity filter
  (strikes with ``volume > 0``); index (NIFTY/BANKNIFTY) vs stock; expiry week.

Exit test: ``models/evaluation/gex_h1.json`` — sign test, ICs, all four
robustness checks. Uses the Phase 3–5 harness **unchanged**
(``ml/splits.py``, ``ml/metrics.py``, ``ml/baselines.py`` transferred from the
direction project); nothing here rebuilds the splitter or the metrics.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as sps  # type: ignore[import-untyped]

from mktmtm.ml.metrics import daily_rank_ic, icir
from mktmtm.ml.splits import inner_holdout, train_valid_test, walk_forward_splits
from mktmtm.options.dealer_exposure import DEALER_SIGN

logger = logging.getLogger(__name__)

__all__ = [
    "DEALER_SIGN",
    "FEATURE_COLUMNS",
    "INDEX_SYMBOLS",
    "PARAMS",
    "EARLY_STOPPING_ROUNDS",
    "run_exit_test",
    "sign_test",
    "positioning_change",
    "robustness_checks",
    "fit_gex_model",
]

# ───────────────────────────── sign convention ──────────────────────────────
# DEALER_SIGN is imported above, never restated here: options.dealer_exposure
# owns the convention and ml/dataset.py builds the SQL aggregates from that same
# constant. A local copy would let this hypothesis test silently disagree with
# the features it is testing — the failure mode etl.py/pipeline.process.py
# already demonstrates. The flip robustness check (Task 6.4) negates gex, which
# is exactly flipping both entries.

INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY")

# Columns the exit test must find on the frame (identity + feature + target).
# gex / gex_chg / otm_put_oi_build come from Phase 3, next_day_abs_return from
# Phase 5; the rest already exist on ml_dataset.
REQUIRED_COLUMNS = [
    "symbol",
    "date",
    "gex",
    "next_day_abs_return",
]

TARGET_COLUMN = "next_day_abs_return"

# ───────────────────── Task 6.3 — model configuration ──────────────────────
# Adapted from the direction model (xgboost-direction-model.md Task 6.1). The
# regularization rationale carries over unchanged: max_depth=4 bounds the
# interaction order, min_child_weight=100 is the noise guard (for squared error
# the hessian is 1, so this floors leaves at ~100 rows), reg_lambda=10 pulls
# thin leaves toward zero, subsample/colsample decorrelate trees, and the small
# learning rate with a large tree cap lets early stopping pick the count. The
# only change from the direction model is the objective/eval_metric pair:
# regression on next_day_abs_return instead of classification on direction.
PARAMS: dict[str, Any] = dict(
    objective="reg:squarederror",
    eval_metric="rmse",
    tree_method="hist",
    max_depth=4,
    min_child_weight=100,
    gamma=0.0,
    reg_lambda=10.0,
    reg_alpha=0.0,
    subsample=0.8,
    colsample_bytree=0.7,
    learning_rate=0.02,
    n_estimators=5000,  # early stopping picks the real count
    random_state=42,
    n_jobs=-1,
)

EARLY_STOPPING_ROUNDS = 200

# Candidate features for Task 6.3: Phase 3 dealer-positioning features first,
# then the standard option/vol features. The fit filters this list to the
# columns actually present on the frame.
FEATURE_COLUMNS = [
    "gex",
    "gex_chg",
    "dex",
    "dex_chg",
    "vanna_exp",
    "charm_exp",
    "oi_hhi",
    "oi_hhi_chg",
    "dist_to_call_wall",
    "otm_put_oi_build",
    "dist_to_flip",
    "ret_1d",
    "rv_yz",
    "ce_iv_30",
    "pe_iv_30",
    "iv_rv_spread",
    "term_slope",
    "put_call_iv_spread",
    "atm_straddle",
    "skew_25d",
    "put_call_vol_ratio",
    "put_call_oi_ratio",
    "chain_volume",
    "chain_oi",
    "chain_oi_chg",
    "chain_breadth",
    "days_to_expiry_30d",
]

MIN_FEATURES = 5

DEFAULT_OUT_PATH = Path("models/evaluation/gex_h1.json")


class MissingDataError(RuntimeError):
    """Raised when ml_dataset lacks columns Phases 2–5 have not landed yet."""


# ───────────────────────────── Task 6.1 — H1 ───────────────────────────────
def _ic_summary(ic: pd.Series) -> dict[str, float]:
    """mean / std / icir / t-stat / hit-rate of a per-date IC series."""
    ic = ic.dropna()
    n = int(len(ic))
    mean_ic = float(ic.mean()) if n else 0.0
    std = float(ic.std(ddof=1)) if n > 1 else 0.0
    t_stat = mean_ic / (std / np.sqrt(n)) if n > 1 and std > 0 else 0.0
    return {
        "n_days": n,
        "mean_ic": mean_ic,
        "ic_std": std,
        "icir": icir(ic),
        "ic_t_stat": t_stat,
        "hit_rate": float((ic > 0.0).mean()) if n else 0.0,
    }


def sign_test(
    frame: pd.DataFrame,
    gex_col: str = "gex",
    ret_col: str = TARGET_COLUMN,
    date_col: str = "date",
) -> dict[str, Any]:
    """Task 6.1 — split symbol-days by ``sign(gex)`` and compare abs-return.

    Returns per-group counts/means, a Welch t-test (equal variance not assumed),
    and the cross-sectional rank IC of ``-gex`` against ``ret_col``. A
    A *positive* ``mean_ic`` on ``-gex`` supports H1: negative dealer gamma
    (short gamma) is followed by higher realized volatility.
    """
    missing = [c for c in (gex_col, ret_col, date_col) if c not in frame.columns]
    if missing:
        raise MissingDataError(
            f"sign_test: missing columns {missing} on frame with "
            f"{list(frame.columns)}"
        )
    df = frame[[gex_col, ret_col, date_col]].dropna()
    if df.empty:
        raise MissingDataError(f"sign_test: no complete ({gex_col}, {ret_col}) rows")

    pos = df[df[gex_col] > 0]
    neg = df[df[gex_col] < 0]
    zero = df[df[gex_col] == 0]

    def _grp(g: pd.DataFrame) -> dict[str, float | int]:
        return {
            "n": int(len(g)),
            "mean_abs_return": float(g[ret_col].mean()) if len(g) else float("nan"),
            "std_abs_return": float(g[ret_col].std(ddof=1)) if len(g) > 1 else float("nan"),
        }

    result: dict[str, Any] = {
        "gex_positive": _grp(pos),
        "gex_negative": _grp(neg),
        "gex_zero": _grp(zero),
    }

    if len(pos) > 1 and len(neg) > 1:
        t_stat, p_value = sps.ttest_ind(
            pos[ret_col].to_numpy(), neg[ret_col].to_numpy(), equal_var=False
        )
        result["welch_t_stat"] = float(t_stat)
        result["welch_p_value"] = float(p_value)
        result["mean_diff_pos_minus_neg"] = float(
            pos[ret_col].mean() - neg[ret_col].mean()
        )
        # H1 direction: short gamma (gex<0) should be followed by MORE vol, so
        # neg_gex (positive when gex<0) should rank high when abs-return is high.
        result["h1_consistent"] = bool(
            p_value < 0.05 and float(pos[ret_col].mean()) < float(neg[ret_col].mean())
        )
    else:
        result["welch_t_stat"] = 0.0
        result["welch_p_value"] = float("nan")
        result["mean_diff_pos_minus_neg"] = float("nan")
        result["h1_consistent"] = False

    neg_gex = -df[gex_col]
    result["ic_neg_gex"] = _ic_summary(
        daily_rank_ic(
            df.assign(pred=neg_gex),
            pred_col="pred",
            ret_col=ret_col,
            date_col=date_col,
        )
    )
    return result


# ───────────────────────────── Task 6.2 — H2 ───────────────────────────────
def _partial_ic(
    frame: pd.DataFrame,
    feature_col: str,
    control_col: str,
    ret_col: str = TARGET_COLUMN,
    date_col: str = "date",
) -> pd.Series:
    """Per-date Spearman IC of ``feature`` on ``ret`` after removing ``control``.

    Within each date, both the feature and the target are rank-normalized and
    then OLS-residualized against the rank-normalized control; the Spearman
    correlation of the two residual series is the partial IC for that date.
    Rank-then-residualize keeps the control removal nonparametric and robust to
    the heavy-tailed exposure levels.
    """
    out: dict[Any, float] = {}
    for d, g in frame.groupby(date_col, sort=True):
        g = g[[feature_col, control_col, ret_col]].dropna()
        if len(g) < 5:
            continue
        x = g[feature_col].rank(pct=True).to_numpy()
        c = g[control_col].rank(pct=True).to_numpy()
        y = g[ret_col].rank(pct=True).to_numpy()
        c_aug = np.column_stack([np.ones_like(c), c])
        x_res = x - c_aug @ np.linalg.lstsq(c_aug, x, rcond=None)[0]
        y_res = y - c_aug @ np.linalg.lstsq(c_aug, y, rcond=None)[0]
        if x_res.std() < 1e-12 or y_res.std() < 1e-12:
            continue
        out[d] = float(np.corrcoef(x_res, y_res)[0, 1])
    return pd.Series(out, dtype="float64", name="partial_ic")


def positioning_change(
    frame: pd.DataFrame,
    features: tuple[str, ...] = ("gex_chg", "otm_put_oi_build"),
    gex_col: str = "gex",
    ret_col: str = TARGET_COLUMN,
    date_col: str = "date",
) -> dict[str, Any]:
    """Task 6.2 — does positioning *change* predict vol beyond the gex level?

    For each feature: the same sign-test protocol as 6.1, plus a partial IC that
    residualizes both the feature and the target against the ``gex`` level
    (per date, nonparametrically). A partial IC that survives the level
    control means the change carries information the level does not.
    """
    result: dict[str, Any] = {}
    present = [f for f in features if f in frame.columns]
    if not present:
        raise MissingDataError(
            "positioning_change: none of the H2 features present: "
            + ", ".join(features)
        )
    for feature in present:
        entry: dict[str, Any] = sign_test(
            frame, gex_col=feature, ret_col=ret_col, date_col=date_col
        )
        entry["partial_ic_controlling_gex"] = _ic_summary(
            _partial_ic(frame, feature, gex_col, ret_col, date_col)
        )
        result[feature] = entry
    return result


# ───────────────────────────── Task 6.3 — model ────────────────────────────
def _has_signal(h1: dict[str, Any], h2: dict[str, Any]) -> tuple[bool, str]:
    """Gate for Task 6.3: run the model only if 6.1 or 6.2 shows signal.

    H1 signal: significant Welch t-test (p < 0.05), group means ordered in the
    H1 direction (short gamma -> more vol), and a rank IC on ``-gex`` whose
    t-stat exceeds 2.
    H2 signal: either positioning-change feature's partial IC (controlling for
    the level) has a t-stat beyond 2 in either direction.
    """
    h1_ok = bool(
        h1.get("welch_p_value", 1.0) < 0.05
        and h1.get("h1_consistent", False)
        and abs(h1["ic_neg_gex"]["ic_t_stat"]) > 2.0
    )
    if h1_ok:
        return True, "H1 sign test significant (p<0.05, t>2 on -gex IC)"

    for feature, entry in h2.items():
        p = entry.get("partial_ic_controlling_gex", {})
        if abs(p.get("ic_t_stat", 0.0)) > 2.0 and p.get("n_days", 0) >= 30:
            return True, f"H2 partial IC significant for {feature} (|t|>2)"

    return False, "no signal in Task 6.1 or 6.2 — model skipped by plan"


def fit_gex_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_inner: pd.DataFrame,
    y_inner: pd.Series,
    params: dict[str, Any] | None = None,
    early_stopping_rounds: int = EARLY_STOPPING_ROUNDS,
) -> tuple[Any, int]:
    """Fit the XGBoost regressor, early-stopped on the inner holdout.

    The inner holdout is carved from the *training* window (``inner_holdout``),
    never from the reporting split — choosing the tree count on the fold's own
    validation rows would make the reported IC optimistically biased.
    """
    import xgboost as xgb

    model = xgb.XGBRegressor(
        **(PARAMS if params is None else params),
        early_stopping_rounds=early_stopping_rounds,
    )
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_inner, y_inner)],
        verbose=False,
    )
    assert model.best_iteration is not None, "early stopping must set best_iteration"
    return model, int(model.best_iteration)


def _model_metrics(model: Any, frame: pd.DataFrame, features: list[str]) -> dict[str, Any]:
    """Daily rank IC of the fitted model on ``frame`` (the reporting split)."""
    X = frame[features]
    pred = pd.Series(model.predict(X), index=frame.index, name="pred")
    ic = daily_rank_ic(
        frame.assign(pred=pred),
        pred_col="pred",
        ret_col=TARGET_COLUMN,
        date_col="date",
    )
    return _ic_summary(ic)


def run_model(
    frame: pd.DataFrame,
    features: list[str] | None = None,
    n_walk_forward_folds: int = 5,
    params: dict[str, Any] | None = None,
    embargo_days: int = 1,
) -> dict[str, Any]:
    """Task 6.3 — walk-forward XGBoost regression on ``next_day_abs_return``.

    Two honest numbers:

    - the fixed 2021-24 / 2025 / 2026 split (``train_valid_test``), fitted with
      an ``inner_holdout`` early stop, reported on VALID;
    - per-fold IC across the purged walk-forward folds (``walk_forward_splits``,
      each fold also early-stopped on its own inner holdout).

    ``embargo_days`` must equal the target's label horizon: 1 for the 1-day
    ``next_day_abs_return``. **Do not weaken it for a 5-day target** — running
    this on ``next_day_rv_5d`` requires ``embargo_days=5`` or five days of
    future leak into every fold.

    ``params`` overrides the sealed ``PARAMS`` (tests shrink the tree cap;
    production uses the sealed config).
    """
    feats = FEATURE_COLUMNS if features is None else features
    feats = [f for f in feats if f in frame.columns]
    if len(feats) < MIN_FEATURES:
        raise MissingDataError(
            f"run_model: only {len(feats)} candidate features present "
            f"(need >= {MIN_FEATURES}); Phases 2-5 data not landed?"
        )

    df = frame[[*feats, TARGET_COLUMN, "date", "symbol"]].dropna(
        subset=[TARGET_COLUMN]
    )
    if len(df) < 500:
        raise MissingDataError(f"run_model: only {len(df)} labeled rows")

    result: dict[str, Any] = {"features": feats, "n_rows": int(len(df))}

    # Fixed split: TRAIN -> inner_holdout -> VALID (TEST sealed).
    train, valid, _test = train_valid_test(df)
    inner_tr, inner_va = inner_holdout(train)
    model, best_iter = fit_gex_model(
        inner_tr[feats], inner_tr[TARGET_COLUMN],
        inner_va[feats], inner_va[TARGET_COLUMN],
        params=params,
    )
    result["fixed_split"] = {
        "best_iteration": best_iter,
        "train_rows": int(len(inner_tr)),
        "inner_holdout_rows": int(len(inner_va)),
        "valid_rows": int(len(valid)),
        "valid": _model_metrics(model, valid, feats),
    }

    # Walk-forward robustness: per-fold IC, early stop per fold on inner holdout.
    # embargo_days is the target's label horizon (1 for the 1-day abs return) —
    # never lower it for next_day_rv_5d (needs 5), per the plan's hard rule.
    folds: list[dict[str, Any]] = []
    for fold, (tr_idx, va_idx) in enumerate(
        walk_forward_splits(
            df["date"], n_splits=n_walk_forward_folds, embargo_days=embargo_days
        )
    ):
        tr = df.iloc[tr_idx]
        va = df.iloc[va_idx]
        inner_tr, inner_va = inner_holdout(tr, embargo_days=embargo_days)
        fold_model, fold_best = fit_gex_model(
            inner_tr[feats], inner_tr[TARGET_COLUMN],
            inner_va[feats], inner_va[TARGET_COLUMN],
            params=params,
        )
        ic = _ic_summary(
            daily_rank_ic(
                va.assign(pred=fold_model.predict(va[feats])),
                pred_col="pred",
                ret_col=TARGET_COLUMN,
                date_col="date",
            )
        )
        folds.append(
            {
                "fold": fold,
                "train_dates": int(tr["date"].nunique()),
                "valid_dates": int(va["date"].nunique()),
                "best_iteration": fold_best,
                "ic": ic,
            }
        )
    result["walk_forward"] = {"n_folds": n_walk_forward_folds, "folds": folds}
    return result


# ───────────────────────────── Task 6.4 — robustness ───────────────────────
def _expiry_week_mask(frame: pd.DataFrame) -> pd.Series:
    """True where the ~30d expiry lands within the next 7 days."""
    return frame["days_to_expiry_30d"] <= 7


def robustness_checks(
    frame: pd.DataFrame,
    gex_liq: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Task 6.4 — the mandatory robustness checks.

    Returns a dict keyed by check name; every check is a sign-test-shaped
    dict (plus ICs) so the JSON table is uniform. ``gex_liq`` is an optional
    per-(symbol, date) liquidity-filtered gex (computed from the chain with
    ``volume > 0`` strikes only — see ``compute_liquidity_filtered_gex``);
    when absent the check reports ``status: blocked`` with the reason.
    """
    out: dict[str, Any] = {}

    # 1. Flip DEALER_SIGN — negate gex (flipping both signs of the dict).
    flipped = frame.assign(gex=-frame["gex"])
    out["flip_dealer_sign"] = sign_test(flipped)

    # 2. Liquidity filter — GEX over strikes with volume > 0 only.
    if gex_liq is not None and not gex_liq.empty:
        merged = frame.merge(
            gex_liq[["symbol", "date", "gex_liq"]],
            on=["symbol", "date"],
            how="inner",
        ).drop(columns=["gex"]).rename(columns={"gex_liq": "gex"})
        if len(merged) < 100:
            out["liquidity_filter"] = {
                "status": "blocked",
                "reason": f"only {len(merged)} merged symbol-days",
            }
        else:
            out["liquidity_filter"] = sign_test(merged)
    else:
        out["liquidity_filter"] = {
            "status": "blocked",
            "reason": (
                "liquidity-filtered gex unavailable — requires chain gamma "
                "(Phase 2 ETL re-run) and the volume>0 aggregate"
            ),
        }

    # 3. Index vs stock — NIFTY/BANKNIFTY have a different dealer ecology.
    is_index = frame["symbol"].isin(INDEX_SYMBOLS)
    out["index_vs_stock"] = {
        "indices": sign_test(frame[is_index]) if is_index.any() else None,
        "stocks": sign_test(frame[~is_index]) if (~is_index).any() else None,
    }

    # 4. Expiry week — charm/vanna flows concentrate into monthly expiry.
    mask = _expiry_week_mask(frame) if "days_to_expiry_30d" in frame.columns else None
    if mask is None:
        out["expiry_week"] = {
            "status": "blocked",
            "reason": "days_to_expiry_30d missing from frame",
        }
    else:
        out["expiry_week"] = {
            "in_expiry_week": sign_test(frame[mask]) if mask.any() else None,
            "outside_expiry_week": sign_test(frame[~mask]) if (~mask).any() else None,
        }

    # 5-6. Confound controls. The four checks above all vary the *sample*; none
    # asks whether the pooled effect is dealer gamma or a size/IV proxy.
    out["sign_vs_magnitude"] = sign_vs_magnitude(frame)
    out["iv_control"] = iv_control(frame)
    out["within_symbol"] = within_symbol_test(frame)
    return out


# ───────────────────────────── loader + exit test ──────────────────────────
def _read_ml_dataset(
    conn: Any,
    symbols: list[str] | None = None,
    limit_dates: int | None = None,
) -> pd.DataFrame:
    """Read the full ml_dataset matrix (identity + features + targets)."""
    where: list[str] = []
    params: list[Any] = []
    if symbols:
        where.append(f"symbol IN ({', '.join(['%s'] * len(symbols))})")
        params.extend(symbols)
    if limit_dates:
        where.append(
            "date IN (SELECT DISTINCT date FROM ml_dataset ORDER BY date DESC LIMIT %s)"
        )
        params.append(limit_dates)
    sql = (
        "SELECT * FROM ml_dataset"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + " ORDER BY symbol, date"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    if not df.empty and "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def _chain_has_gamma(conn: Any) -> bool:
    """True if chain elements carry ``gamma`` (Phase 2's ETL re-run landed)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT option_chain FROM option_metrics "
                "WHERE jsonb_array_length(option_chain) > 0 LIMIT 1"
            )
            row = cur.fetchone()
    except Exception:
        return False
    if row is None or not row[0]:
        return False
    return "gamma" in row[0][0]


def compute_liquidity_filtered_gex(
    conn: Any,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    """GEX over strikes with ``volume > 0`` only, straight from the chain.

    The liquidity filter exists because IV on untraded strikes is
    settlement-derived model output, and the Phase 3 ``gex`` aggregate sums
    across *all* strikes. Requires ``gamma`` on the chain elements (Phase 2's
    ETL re-run); raises ``MissingDataError`` if the chain predates it.
    """
    if not _chain_has_gamma(conn):
        raise MissingDataError(
            "chain elements carry no gamma yet — Phase 2's ETL re-run has not "
            "landed; liquidity-filtered GEX cannot be computed"
        )
    where = ""
    params: list[Any] = []
    if symbols:
        where = f"WHERE m.symbol IN ({', '.join(['%s'] * len(symbols))})"
        params.extend(symbols)
    sql = f"""
SELECT m.symbol, m.date,
       COALESCE(SUM(
           CASE WHEN e->>'type' = 'CE' THEN {DEALER_SIGN['CE']}
                WHEN e->>'type' = 'PE' THEN {DEALER_SIGN['PE']}
                ELSE 0 END
           * COALESCE((e->>'oi')::bigint, 0)
           * COALESCE((e->>'gamma')::float, 0)
           * m.underlying_price * m.underlying_price * 0.01
       ), 0) AS gex_liq
FROM option_metrics m
LEFT JOIN LATERAL (
    SELECT e
    FROM jsonb_array_elements(m.option_chain) e
    WHERE COALESCE((e->>'volume')::bigint, 0) > 0
) e ON TRUE
{where}
GROUP BY m.symbol, m.date
"""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            df = pd.DataFrame(cur.fetchall(), columns=cols)
    except Exception as exc:
        raise MissingDataError(
            f"compute_liquidity_filtered_gex failed: {exc}"
        ) from exc
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def _validate(frame: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise MissingDataError(
            "ml_dataset is missing columns Phases 2-5 have not landed yet: "
            + ", ".join(missing)
        )


def run_exit_test(
    conn: Any,
    out_path: str | Path = DEFAULT_OUT_PATH,
    symbols: list[str] | None = None,
    limit_dates: int | None = None,
    run_model_if_signal: bool = True,
) -> dict[str, Any]:
    """Run Tasks 6.1, 6.2, 6.3 (gated), 6.4 and write ``models/evaluation/gex_h1.json``.

    Returns the full result dict (also written to ``out_path``). Raises
    ``MissingDataError`` when the dataset lacks the Phase 3/5 columns — the
    sibling phases must land (and the Phase 2 ETL re-run must complete) first.
    """
    import psycopg2  # noqa: F401 - used by compute_liquidity_filtered_gex

    frame = _read_ml_dataset(conn, symbols=symbols, limit_dates=limit_dates)
    _validate(frame)
    frame = frame.sort_values(["symbol", "date"]).reset_index(drop=True)

    result: dict[str, Any] = {
        "phase": "Phase 6 — hypothesis tests",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "rows": int(len(frame)),
            "symbols": int(frame["symbol"].nunique()),
            "dates": int(frame["date"].nunique()),
            "period": f"{frame['date'].min().date()} -> {frame['date'].max().date()}"
            if not frame.empty
            else None,
        },
    }

    # Task 6.1
    h1 = sign_test(frame)
    result["h1_sign_test"] = h1

    # Task 6.2
    h2 = positioning_change(frame)
    result["h2_positioning_change"] = h2

    # Task 6.3 — only if 6.1 or 6.2 shows signal
    signal, reason = _has_signal(h1, h2)
    result["model"] = {"ran": False, "reason": reason}
    if signal and run_model_if_signal:
        try:
            result["model"] = {"ran": True, "reason": reason, **run_model(frame)}
        except MissingDataError as exc:
            result["model"] = {
                "ran": False,
                "reason": f"signal present but model blocked: {exc}",
            }

    # Task 6.4 — mandatory regardless of 6.1
    try:
        gex_liq = compute_liquidity_filtered_gex(conn, symbols=symbols)
    except MissingDataError as exc:
        logger.warning("liquidity-filtered gex unavailable: %s", exc)
        gex_liq = pd.DataFrame()
    result["robustness"] = robustness_checks(frame, gex_liq=gex_liq)

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str) + "\n")
    logger.info("Wrote %s", path)
    return result


def main() -> None:
    """Run the Phase 6 exit test (needs a live PostgreSQL with ml_dataset)."""
    import argparse

    from mktmtm.storage.db import build_dsn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT_PATH))
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--limit-dates", type=int, default=None)
    args = parser.parse_args()

    import psycopg2

    with psycopg2.connect(build_dsn()) as conn:
        result = run_exit_test(
            conn,
            out_path=args.out,
            symbols=args.symbols,
            limit_dates=args.limit_dates,
        )
    print(json.dumps(result, indent=2, default=str)[:2000])


if __name__ == "__main__":
    main()


# ─────────────────── confound controls (Task 6.4, checks 5-6) ───────────────
# The cross-sectional H1 sign test compares gex<0 against gex>0 *across
# symbols*, and GEX scales with OI x gamma x S^2 — so it is also a size and
# liquidity ranking. Small, thinly-traded names carry small |gex| and are more
# volatile for reasons that have nothing to do with dealer hedging. These two
# checks separate the mechanism from the proxy; both were added after the first
# five-year run showed the headline IC surviving every original robustness check
# while failing these.
_MIN_Z_PERIODS = 60


def sign_vs_magnitude(
    frame: pd.DataFrame,
    gex_col: str = "gex",
    ret_col: str = TARGET_COLUMN,
    date_col: str = "date",
) -> dict[str, Any]:
    """Does the *sign* of GEX carry anything the magnitude does not?

    H1 is a claim about dealers being short gamma, so discarding the sign
    (ranking on ``-|gex|`` instead of ``-gex``) should destroy the signal. If
    the magnitude-only IC matches or beats the signed IC, the result is a size
    ranking wearing the dealer-gamma story's clothes.
    """
    signed = frame.assign(_f=-frame[gex_col])
    magnitude = frame.assign(_f=-frame[gex_col].abs())
    out = {
        "ic_signed": _ic_summary(daily_rank_ic(signed, "_f", ret_col, date_col)),
        "ic_magnitude_only": _ic_summary(
            daily_rank_ic(magnitude, "_f", ret_col, date_col)
        ),
    }
    out["sign_adds_information"] = bool(
        out["ic_signed"]["mean_ic"] > out["ic_magnitude_only"]["mean_ic"]
    )
    return out


def iv_control(
    frame: pd.DataFrame,
    features: tuple[str, ...] = (
        "gex", "gex_chg", "dex", "dist_to_flip", "vanna_exp", "charm_exp",
        "otm_put_oi_build",
    ),
    control_col: str = "ce_iv_30",
    ret_col: str = TARGET_COLUMN,
    date_col: str = "date",
) -> dict[str, Any]:
    """Partial IC of each dealer feature after removing ATM implied vol.

    The option market already publishes a volatility forecast; a dealer-exposure
    feature earns its place only by predicting realized vol *beyond* what IV
    says. ``gex`` is entered signed-negative (``-gex``) so a positive partial IC
    keeps meaning "short gamma precedes higher vol".
    """
    if control_col not in frame.columns:
        return {"status": "blocked", "reason": f"{control_col} missing from frame"}
    out: dict[str, Any] = {"control": control_col}
    baseline = frame.dropna(subset=[control_col, ret_col])
    out["ic_control_alone"] = _ic_summary(
        daily_rank_ic(baseline, control_col, ret_col, date_col)
    )
    for feat in features:
        if feat not in frame.columns:
            continue
        sub = frame.assign(_f=-frame[feat] if feat == "gex" else frame[feat])
        sub = sub.dropna(subset=["_f", control_col, ret_col])
        label = f"-{feat}" if feat == "gex" else feat
        out[label] = _ic_summary(_partial_ic(sub, "_f", control_col, ret_col, date_col))
    return out


def within_symbol_test(
    frame: pd.DataFrame,
    gex_col: str = "gex",
    iv_col: str = "ce_iv_30",
    ret_col: str = TARGET_COLUMN,
    symbol_col: str = "symbol",
    date_col: str = "date",
) -> dict[str, Any]:
    """H1 re-specified *within* each symbol, which kills the size confound.

    Each symbol's ``gex`` is z-scored against its **own** past — an expanding
    mean/std over strictly prior rows (``shift(1)``, ``min_periods``
    ``_MIN_Z_PERIODS``), so the statistic is causal and a symbol is only ever
    compared with itself. The cross-sectional size/liquidity differences that
    contaminate the pooled sign test cannot enter.

    H1 predicts a **negative** correlation: a symbol's own low-gex (short
    gamma) days precede its own larger moves. The partial figure repeats it
    controlling for the symbol's own IV z-score, so a surviving effect is
    information the symbol's own option market had not already priced.
    """
    missing = [c for c in (gex_col, ret_col, symbol_col, date_col) if c not in frame.columns]
    if missing:
        return {"status": "blocked", "reason": f"missing columns {missing}"}

    def _expanding_z(s: pd.Series) -> pd.Series:
        prior = s.shift(1)
        return (s - prior.expanding(min_periods=_MIN_Z_PERIODS).mean()) / prior.expanding(
            min_periods=_MIN_Z_PERIODS
        ).std()

    df = frame.dropna(subset=[gex_col, ret_col]).sort_values([symbol_col, date_col])
    grouped = df.groupby(symbol_col)
    df = df.assign(_gz=grouped[gex_col].transform(_expanding_z))
    if iv_col in df.columns:
        df = df.assign(_ivz=grouped[iv_col].transform(_expanding_z))
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["_gz"])
    if len(df) < 100:
        return {"status": "blocked", "reason": f"only {len(df)} rows survive the z-score warmup"}

    rho = float(df["_gz"].corr(df[ret_col], method="spearman"))
    lo, hi = df["_gz"].quantile(0.10), df["_gz"].quantile(0.90)
    low, high = df[df["_gz"] <= lo], df[df["_gz"] >= hi]
    welch = sps.ttest_ind(low[ret_col], high[ret_col], equal_var=False)
    out: dict[str, Any] = {
        "n_rows": int(len(df)),
        "min_periods": _MIN_Z_PERIODS,
        "spearman_gex_z_vs_abs_return": rho,
        "own_low_decile_mean_abs_return": float(low[ret_col].mean()),
        "own_high_decile_mean_abs_return": float(high[ret_col].mean()),
        "decile_ratio": float(low[ret_col].mean() / high[ret_col].mean()),
        "welch_t_stat": float(welch.statistic),
        "welch_p_value": float(welch.pvalue),
        # H1 predicts a negative correlation (low own-gex -> larger own move).
        "h1_consistent": bool(rho < 0),
        "per_year": {
            str(y): float(g["_gz"].corr(g[ret_col], method="spearman"))
            for y, g in df.groupby(df[date_col].dt.year)
        },
    }
    if "_ivz" in df.columns:
        sub = df.dropna(subset=["_ivz"])
        x = sub["_gz"].rank(pct=True).to_numpy()
        c = sub["_ivz"].rank(pct=True).to_numpy()
        y_ = sub[ret_col].rank(pct=True).to_numpy()
        c_aug = np.column_stack([np.ones_like(c), c])
        x_res = x - c_aug @ np.linalg.lstsq(c_aug, x, rcond=None)[0]
        y_res = y_ - c_aug @ np.linalg.lstsq(c_aug, y_, rcond=None)[0]
        out["partial_spearman_controlling_own_iv"] = float(np.corrcoef(x_res, y_res)[0, 1])
        out["n_rows_partial"] = int(len(sub))
    return out
