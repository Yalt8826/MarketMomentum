"""Stage 2 — per-symbol latent positioning regimes, via a pooled Gaussian HMM.

The per-symbol panel has 212,441 rows and **no label**: ``participant_oi`` is
market-wide, so nothing supervised can be fitted at this grain. This module
gives up on labels and asks a different question — do symbol-days fall into a
small number of persistent latent states? — then validates the answer against
the market-level series that *does* exist.

**Why an HMM and not clustering.** Positioning is persistent: an institution
accumulates over days or weeks, not in a single print. A transition matrix
encodes that directly; k-means or a Gaussian mixture treats every day as
independent and discards exactly the structure being tested for.
``sklearn.mixture.GaussianMixture`` is therefore not a substitute, however
convenient — it has no transitions.

**Why it can fail visibly.** If the fitted transition matrix has a near-uniform
diagonal then states do not persist, "regimes" are not present in these
observables, and the model has produced a reportable negative result. Most
unsupervised methods cannot fail this legibly; that property is deliberate and
should not be tuned away.

**Why the normalization is within-symbol.** RELIANCE's GEX and a mid-cap's
differ by orders of magnitude, so a pooled fit on raw values would discover
market capitalisation rather than regimes. That exact confound already
invalidated the pooled GEX H1 result. Every observable is z-scored against the
symbol's *own* expanding history, shifted one day so the statistics are causal.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mktmtm.ml.splits import TRAIN_END
from mktmtm.progress import progress_stage

logger = logging.getLogger(__name__)

__all__ = [
    "OBSERVABLES",
    "build_observations",
    "canonical_order",
    "causal_zscore",
    "fit_regimes",
    "run_flow_regimes",
    "sequence_lengths",
    "write_flow_regimes",
]

#: The six observables. Deliberately few: under a diagonal covariance the
#: parameter count still grows as n_states x n_features, and the fit destabilises
#: well before the row count suggests it should.
OBSERVABLES = (
    "fut_basis_excess",       # financing pressure vs carry
    "fut_roll_ratio",         # position in the roll cycle
    "oi_outlier_share",       # concentration of unusual OI
    "oi_outlier_direction",   # Stage 1b: direction of that unusual OI
    "q_fut_direction",        # Stage 1b: direction of futures positioning
    "gex",                    # dealer gamma exposure
)

#: `oi_outlier_direction` is non-null on only ~10-15% of symbol-days by design
#: — most days have no flagged strike at all. Requiring complete cases would
#: fit the model on unusual days only, so it is imputed to 0 and paired with
#: this indicator. The indicator is what stops the model reading "no unusual
#: activity" as "neutral direction"; it is the one sanctioned exception to the
#: project's NULL-never-0 rule, and it is sanctioned only because the pairing
#: preserves the distinction that rule protects.
_IMPUTED = "oi_outlier_direction"
INDICATOR = "has_outlier"

MODEL_COLUMNS = (*OBSERVABLES, INDICATOR)

MIN_OBS = 60          # matches the project-wide causal-z warm-up
WINSORIZE = 5.0       # a single 40-sigma print would capture a whole component
MIN_SEQUENCE = 90     # a symbol with less history than this cannot inform states
STATE_GRID = (2, 3, 4, 5, 6)
N_RESTARTS = 10
RANDOM_SEED = 42
MAX_PERSISTED_STATES = 6   # init_flow_regimes.sql carries p_state_0..5

DEFAULT_PARQUET = Path("data/processed/flow_regimes.parquet")
MODEL_PATH = Path("models/candidates/flow_hmm.joblib")
EVALUATION_DIR = Path("models/evaluation")

_OBSERVATION_SQL = """
SELECT f.symbol, f.date,
       f.fut_basis_excess, f.fut_roll_ratio, f.oi_outlier_share,
       f.oi_outlier_direction, f.q_fut_direction,
       m.gex
FROM flow_features f
JOIN ml_dataset m USING (symbol, date)
ORDER BY f.symbol, f.date
"""


# ═════════════════════════ observation matrix (pure) ═══════════════════════


def causal_zscore(
    frame: pd.DataFrame,
    columns: tuple[str, ...] | list[str],
    *,
    min_obs: int = MIN_OBS,
) -> pd.DataFrame:
    """Within-symbol expanding z-score, shifted one day. Pure — no I/O.

    The mean and standard deviation for date ``t`` are computed over that
    symbol's observations up to ``t-1``. The ``shift(1)`` is what makes it
    causal; without it every row is normalized using its own value and the
    whole panel leaks.

    Row order is load-bearing — the expanding window is a groupby operation —
    so the frame is sorted explicitly first even when it looks sorted already.
    """
    work = frame.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)
    grouped = work.groupby("symbol", observed=True)
    for col in columns:
        series = work[col].astype("float64")
        mean = grouped[col].transform(lambda s: s.expanding().mean().shift(1))
        std = grouped[col].transform(lambda s: s.expanding().std().shift(1))
        count = grouped[col].transform(lambda s: s.expanding().count().shift(1))
        z = (series - mean) / std.where(std > 0)
        work[col] = z.where(count >= min_obs).clip(-WINSORIZE, WINSORIZE)
    return work


def build_observations(panel: pd.DataFrame) -> pd.DataFrame:
    """``(symbol, date, *MODEL_COLUMNS)`` complete cases, ready for hmmlearn.

    Pure — no I/O. The imputation of ``oi_outlier_direction`` happens *before*
    the z-score, so the indicator and the imputed value describe the same rows.
    """
    work = panel.copy()
    work["date"] = pd.to_datetime(work["date"]).astype("datetime64[us]")
    work[INDICATOR] = work[_IMPUTED].notna().astype("float64")
    work[_IMPUTED] = work[_IMPUTED].fillna(0.0)

    scored = causal_zscore(work, OBSERVABLES)
    # The indicator is already 0/1 and carries no scale; z-scoring it would
    # turn a clean flag into a symbol-specific rate.
    scored[INDICATOR] = work.sort_values(
        ["symbol", "date"], kind="stable"
    ).reset_index(drop=True)[INDICATOR]

    complete = scored.loc[
        scored[list(MODEL_COLUMNS)].notna().all(axis=1)
    ].reset_index(drop=True)

    # hmmlearn treats each symbol as one sequence; a symbol with only a handful
    # of surviving days contributes noise to the transition matrix and nothing
    # to the emissions.
    sizes = complete.groupby("symbol", observed=True)["date"].transform("size")
    kept = complete.loc[sizes >= MIN_SEQUENCE].reset_index(drop=True)
    return kept.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def sequence_lengths(frame: pd.DataFrame) -> np.ndarray:
    """Per-symbol sequence lengths for hmmlearn's ``lengths=`` argument.

    ``hmmlearn`` takes one concatenated array plus the length of each sequence.
    If the rows are not grouped by symbol and ordered by date within each
    symbol, it raises nothing and silently learns transitions *across* symbol
    boundaries — treating the end of ADANIPORTS as flowing into the start of
    ASIANPAINT. The result looks plausible and is meaningless, so the ordering
    is asserted here rather than assumed.
    """
    ordered = frame["symbol"].to_numpy()
    changes = np.flatnonzero(ordered[1:] != ordered[:-1]) + 1
    starts = np.concatenate([[0], changes])
    if len(np.unique(ordered)) != len(starts):
        raise ValueError(
            "frame rows are not grouped by symbol — hmmlearn would learn "
            "transitions across symbol boundaries without complaining"
        )
    lengths = np.diff(np.concatenate([starts, [len(ordered)]]))
    if lengths.sum() != len(frame):
        raise ValueError(f"lengths sum to {lengths.sum()}, expected {len(frame)}")
    return lengths


def canonical_order(means: np.ndarray, direction_index: int) -> np.ndarray:
    """Permutation sorting states by ascending mean direction.

    ``hmmlearn`` numbers states arbitrarily and the numbering changes between
    restarts and refits. Persisting a raw state id would mean the integers in
    ``flow_regimes`` silently change meaning on every rebuild.
    """
    return np.argsort(means[:, direction_index], kind="stable")


# ═════════════════════════ fitting ══════════════════════════════════════════


def _fit_once(
    x: np.ndarray, lengths: np.ndarray, n_states: int, seed: int
) -> tuple[Any, float]:
    """One EM run. Returns ``(model, log_likelihood)``; ``-inf`` if it fails."""
    from hmmlearn.hmm import GaussianHMM  # type: ignore[import-untyped]

    model = GaussianHMM(
        n_components=n_states,
        covariance_type="diag",  # "full" overfits at 7 observables
        n_iter=200,
        tol=1e-3,
        random_state=seed,
    )
    try:
        model.fit(x, lengths=lengths)
        score = float(model.score(x, lengths=lengths))
    except (ValueError, np.linalg.LinAlgError) as exc:
        logger.warning("HMM fit failed (n_states=%d, seed=%d): %s", n_states, seed, exc)
        return model, float("-inf")
    return model, score


def _bic(log_likelihood: float, n_states: int, n_features: int, n_obs: int) -> float:
    """BIC for a diagonal-covariance Gaussian HMM.

    Free parameters: initial probabilities (k-1), transitions k(k-1), and per
    state a mean and variance for every feature (2kd).
    """
    k, d = n_states, n_features
    n_params = (k - 1) + k * (k - 1) + 2 * k * d
    return float(-2.0 * log_likelihood + n_params * np.log(max(n_obs, 1)))


def _select_states(
    bic_curve: dict[int, float], diagonals: dict[int, list[float]]
) -> tuple[int, dict[str, Any]]:
    """Choose ``n_states``, preferring BIC but refusing to trust it blindly.

    BIC is only a valid selector if it turns around inside the search grid. If
    it decreases monotonically to the largest ``k`` tried, the criterion has not
    identified anything — it is still improving and would keep improving, which
    is the signature of the model slicing a *continuous* factor into ever
    finer bands rather than finding discrete regimes.

    When that happens the fallback is the persistence requirement, which is a
    real property of the thing being modelled rather than a fit statistic: a
    positioning regime that does not survive to the next day is not a regime.
    Among state counts where **every** state has a self-transition above 0.5,
    take the lowest BIC. If no ``k`` qualifies, report the failure and hand
    back the BIC choice anyway so the caller can say so out loud.
    """
    grid = sorted(bic_curve)
    bic_choice = min(grid, key=lambda k: bic_curve[k])
    monotone = bic_choice == grid[-1] and len(grid) > 1

    persistent = [k for k in grid if min(diagonals.get(k, [0.0])) > 0.5]
    if monotone and persistent:
        chosen = min(persistent, key=lambda k: bic_curve[k])
        reason = "persistence (BIC did not turn around inside the grid)"
    elif monotone:
        chosen = bic_choice
        reason = "BIC, unidentified — no k had all states persistent"
    else:
        chosen = bic_choice
        reason = "BIC (curve turned around inside the grid)"

    return chosen, {
        "criterion": reason,
        "bic_choice": bic_choice,
        "bic_monotone_to_grid_edge": monotone,
        "persistent_k": persistent,
        "chosen": chosen,
    }


def fit_regimes(
    observations: pd.DataFrame,
    *,
    train_end: pd.Timestamp = TRAIN_END,
    state_grid: tuple[int, ...] = STATE_GRID,
    n_restarts: int = N_RESTARTS,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Select ``n_states`` by BIC on the training window, then decode everything.

    **Fitting happens on training dates only.** Fitting on all history and then
    "discovering" states that explain it is look-ahead, and it is the easiest
    mistake in this stage because nothing in the code would complain. The
    Viterbi decode of an already-fitted model is a legitimate forward
    operation; the *fit* is what must be sealed.

    EM converges to a local optimum, so each state count is fitted
    ``n_restarts`` times from different seeds and the best training
    log-likelihood wins. The spread across restarts is reported — a wide spread
    means the likelihood surface is rough and the states are not robust, which
    belongs in the write-up rather than hidden behind the best run.
    """
    train = observations.loc[observations["date"] <= train_end]
    if train.empty:
        raise ValueError(f"no observations on or before train_end {train_end.date()}")

    x_train = train[list(MODEL_COLUMNS)].to_numpy(dtype="float64")
    len_train = sequence_lengths(train)

    bic_curve: dict[int, float] = {}
    restart_spread: dict[int, float] = {}
    diagonals: dict[int, list[float]] = {}
    candidates: dict[int, Any] = {}

    with progress_stage(
        "Fitting regime HMM", total=len(state_grid) * n_restarts, unit="fits"
    ) as bar:
        for n_states in state_grid:
            scores: list[float] = []
            best_local: tuple[Any, float] = (None, float("-inf"))
            for r in range(n_restarts):
                model, score = _fit_once(x_train, len_train, n_states, seed + r)
                if np.isfinite(score):
                    scores.append(score)
                    if score > best_local[1]:
                        best_local = (model, score)
                bar.advance(label=f"k={n_states}")
            if best_local[0] is None:
                continue
            bic = _bic(best_local[1], n_states, len(MODEL_COLUMNS), len(x_train))
            bic_curve[n_states] = bic
            restart_spread[n_states] = (
                float(np.max(scores) - np.min(scores)) if len(scores) > 1 else 0.0
            )
            diagonals[n_states] = np.diag(best_local[0].transmat_).tolist()
            candidates[n_states] = best_local[0]

    if not candidates:
        raise ValueError("every HMM fit failed — check the observation matrix")

    n_states, selection = _select_states(bic_curve, diagonals)
    model = candidates[n_states]
    order = canonical_order(model.means_, MODEL_COLUMNS.index("q_fut_direction"))
    rank = np.empty(n_states, dtype=int)
    rank[order] = np.arange(n_states)

    x_all = observations[list(MODEL_COLUMNS)].to_numpy(dtype="float64")
    len_all = sequence_lengths(observations)
    raw_states = model.predict(x_all, lengths=len_all)
    posterior = model.predict_proba(x_all, lengths=len_all)

    decoded = observations[["symbol", "date"]].copy()
    decoded["state"] = rank[raw_states]
    decoded["state_prob"] = posterior[np.arange(len(posterior)), raw_states]
    for canonical, original in enumerate(order):
        decoded[f"p_state_{canonical}"] = posterior[:, original]

    transitions = model.transmat_[np.ix_(order, order)]
    means = pd.DataFrame(
        model.means_[order], columns=list(MODEL_COLUMNS)
    ).round(4)

    return {
        "decoded": decoded,
        "model": model,
        "n_states": n_states,
        "canonical_order": order.tolist(),
        "bic_curve": {str(k): v for k, v in bic_curve.items()},
        "restart_loglik_spread": {str(k): v for k, v in restart_spread.items()},
        "diagonal_by_k": {str(k): v for k, v in diagonals.items()},
        "min_diagonal_by_k": {
            str(k): float(min(v)) for k, v in diagonals.items()
        },
        "selection": selection,
        "transition_matrix": transitions.tolist(),
        "state_means": means.to_dict(orient="records"),
        "diagonal": np.diag(transitions).tolist(),
        # The falsification test: a state should usually persist to the next
        # day. A near-uniform diagonal means regimes are not present in these
        # observables, which is a result, not a bug to tune away.
        "persistence_passes": bool(np.all(np.diag(transitions) > 0.5)),
        "train_end": str(pd.Timestamp(train_end).date()),
        "train_rows": int(len(train)),
        "scored_rows": int(len(observations)),
    }


# ═════════════════════════ persistence + validation ════════════════════════

_REGIME_COLUMNS = (
    "symbol", "date", "state", "state_prob",
    *[f"p_state_{i}" for i in range(MAX_PERSISTED_STATES)],
)


def write_flow_regimes(conn: Any, frame: pd.DataFrame) -> int:
    """UPSERT into ``flow_regimes`` on (symbol, date). Returns rows written."""
    from psycopg2.extras import execute_values

    if frame.empty:
        return 0
    out = frame.copy()
    for col in _REGIME_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan

    cols = list(_REGIME_COLUMNS)
    rows = [
        tuple(_clean(rec[i], cols[i] == "state") for i in range(len(cols)))
        for rec in out[cols].itertuples(index=False, name=None)
    ]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("symbol", "date"))
    sql = (
        f"INSERT INTO flow_regimes ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (symbol, date) DO UPDATE SET {updates}"
    )
    written = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), 1000):
            page = rows[i : i + 1000]
            execute_values(cur, sql, page, page_size=len(page))
            written += len(page)
    conn.commit()
    return written


def _clean(value: Any, as_int: bool) -> Any:
    """psycopg2 cannot adapt numpy scalars; pandas hands out exactly those."""
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    if isinstance(value, pd.Timestamp | np.datetime64):
        return pd.Timestamp(value).date()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if as_int:
        return int(value)
    return value.item() if isinstance(value, np.generic) else value


def validate_regimes(conn: Any, decoded: pd.DataFrame) -> dict[str, Any]:
    """Aggregate state occupancy per date, then check it against the real label.

    An unsupervised model with no external validation is a rearrangement of the
    data. This correlates the *differenced* daily occupancy of each state
    against ``participant_oi`` flows. Differenced because level correlations
    between two trending series are spurious — the defect already recorded for
    the flow features.

    The structural check is the same one used everywhere in this project:
    ``Client`` should move opposite to ``FII`` and ``Pro``. If an occupancy
    share rises with institutional buying *and* with client buying, something
    is inverted.
    """
    from mktmtm.data.participant_oi import add_net_positions

    occupancy = (
        decoded.assign(one=1.0)
        .pivot_table(index="date", columns="state", values="one", aggfunc="sum")
        .fillna(0.0)
    )
    occupancy = occupancy.div(occupancy.sum(axis=1), axis=0)
    occupancy.columns = [f"occ_state_{int(c)}" for c in occupancy.columns]

    nets = add_net_positions(pd.read_sql("SELECT * FROM participant_oi", conn))
    nets["date"] = pd.to_datetime(nets["date"]).astype("datetime64[us]")
    flows = nets.pivot_table(
        index="date", columns="client_type", values="d_net_opt_stock_call"
    )

    joined = occupancy.join(flows, how="inner").sort_index()
    d_occ = joined[occupancy.columns].diff()

    out: dict[str, Any] = {"dates": int(len(joined))}
    for participant in ("FII", "Pro", "Client"):
        if participant not in joined.columns:
            continue
        out[participant] = {
            col: float(d_occ[col].corr(joined[participant]))
            for col in occupancy.columns
        }
    # Occupancy of each state should track FII and Client in opposite
    # directions if the states carry institutional information at all.
    opposed = []
    for col in occupancy.columns:
        fii = out.get("FII", {}).get(col)
        client = out.get("Client", {}).get(col)
        if fii is not None and client is not None:
            opposed.append(bool(np.sign(fii) != np.sign(client)))
    out["states_with_opposed_fii_client_signs"] = int(sum(opposed))
    out["n_states"] = len(occupancy.columns)
    return out


def run_flow_regimes(
    conn: Any,
    *,
    train_end: pd.Timestamp = TRAIN_END,
    state_grid: tuple[int, ...] = STATE_GRID,
    n_restarts: int = N_RESTARTS,
    seed: int = RANDOM_SEED,
    parquet: bool = True,
    out_dir: Path = EVALUATION_DIR,
) -> dict[str, Any]:
    """Build observations, fit, decode, persist and validate. CLI entry point."""
    panel = pd.read_sql(_OBSERVATION_SQL, conn)
    if panel.empty:
        raise ValueError("flow_features JOIN ml_dataset returned no rows")

    observations = build_observations(panel)
    retention = len(observations) / len(panel) if len(panel) else 0.0
    logger.info(
        "observation matrix: %d of %d symbol-days (%.1f%% retained), %d symbols",
        len(observations), len(panel), 100 * retention,
        observations["symbol"].nunique(),
    )

    result = fit_regimes(
        observations, train_end=train_end, state_grid=state_grid,
        n_restarts=n_restarts, seed=seed,
    )
    decoded = result.pop("decoded")
    model = result.pop("model")

    if result["n_states"] > MAX_PERSISTED_STATES:
        logger.warning(
            "BIC chose %d states but flow_regimes carries p_state_0..%d; "
            "the extra posteriors are not persisted",
            result["n_states"], MAX_PERSISTED_STATES - 1,
        )

    written = write_flow_regimes(conn, decoded)
    if parquet:
        DEFAULT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
        decoded.to_parquet(DEFAULT_PARQUET, index=False)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        import joblib  # type: ignore[import-untyped]

        joblib.dump(model, MODEL_PATH)
    except ImportError:  # joblib rides in with sklearn, but do not hard-depend
        logger.warning("joblib unavailable — fitted model not persisted")

    result["validation"] = validate_regimes(conn, decoded)
    result["rows_written"] = written
    result["retention"] = retention
    result["symbols"] = int(observations["symbol"].nunique())
    result["parquet"] = str(DEFAULT_PARQUET) if parquet else None

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "flow_regimes.json").write_text(
        json.dumps(result, indent=2, default=float) + "\n"
    )
    return result
