"""Feature contract and loader for the next-day direction model.

``load_training_frame`` is the single entry point that turns
``data/processed/ml_dataset.parquet`` into the model-ready frame consumed by
the splitter, metrics, and trainer (Phases 3-7). Every transformation is
explicit and unit-tested; no model, no training.

Pipeline (in order, each togglable by the flags):

1. **Gate** (``gate=True``) — the F4 gate from ``docs/data/iv-integrity-audit.md``
   extended to the PE leg. Rows where either IV leg is missing or outside
   (0, 200] or whose label is missing are dropped outright; the two legs go
   NULL together, so gating both removes only ~91 extra rows while killing a
   whole class of rows that would otherwise need imputation.
2. **Prune** — drop the collinear duplicates (``pe_vega``, ``pe_rho``,
   ``ce_volume``, ``pe_volume``) and the date-constant ``interest_rate``.
3. **Missingness indicators** — every *surviving* feature with a NULL rate
   above 1% on the gated frame gets a companion ``<feature>_is_null`` int8
   column, computed from the raw NaNs *before* normalization. This makes the
   strong NULL-vs-label association an explicit feature instead of something
   smuggled in through XGBoost's learned NaN split direction.
4. **Normalize** (``normalize=True``) — per-date cross-sectional rank
   normalization to [0, 1] (``groupby("date")[col].rank(pct=True)``) on every
   remaining continuous feature; the ``_is_null`` indicators stay raw 0/1.
   Ranking makes each feature answer "where does this name sit *today*" and
   neutralizes the non-stationary feature levels across the five years.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from mktmtm.ml.dataset import DATASET_COLUMNS, IDENTITY_COLUMNS, TARGET_COLUMNS

__all__ = ["DROP_COLUMNS", "GATE", "NULL_RATE_THRESHOLD", "load_training_frame"]

# All 47 option-derived features, in dataset order. Column identity and order
# stay single-sourced from DATASET_COLUMNS in ml/dataset.py (CLAUDE.md
# invariant): adding a feature upstream automatically flows into the loader.
FEATURE_COLUMNS = [c for c in DATASET_COLUMNS if c not in IDENTITY_COLUMNS + TARGET_COLUMNS]

# Task 2.3 / 2.4 — collinear duplicates and the date-constant column, dropped
# outright: pe_vega is r=1.000 with ce_vega (ATM call/put vega are identical in
# Black-Scholes), pe_rho is r=0.993 with ce_rho, ce_volume/pe_volume are
# r=0.997-0.999 with chain_volume/put_call_vol_ratio, and interest_rate is
# constant within 100% of dates.
DROP_COLUMNS = ["pe_vega", "pe_rho", "ce_volume", "pe_volume", "interest_rate"]

# Task 2.1 — the F4 gate, both legs. Rows failing any leg are dropped before
# any other transformation. Documented as a string for the audit trail; the
# mask itself is built explicitly in _gate_mask (pandas cannot eval chained
# comparisons).
GATE = "(0 < ce_iv_30 <= 200) & (0 < pe_iv_30 <= 200) & next_day_direction.notna()"

# Task 2.2 — a feature with a NULL rate above this gets an _is_null companion.
NULL_RATE_THRESHOLD = 0.01

_DEFAULT_PARQUET = (
    Path(__file__).resolve().parents[3] / "data" / "processed" / "ml_dataset.parquet"
)


def _gate_mask(frame: pd.DataFrame) -> pd.Series:
    """Boolean mask for GATE, in explicit form (matches the documented string)."""
    return (
        (frame["ce_iv_30"] > 0)
        & (frame["ce_iv_30"] <= 200)
        & (frame["pe_iv_30"] > 0)
        & (frame["pe_iv_30"] <= 200)
        & frame["next_day_direction"].notna()
    )


def _add_missingness_indicators(
    frame: pd.DataFrame,
    features: list[str],
    pinned: list[str] | None = None,
) -> list[str]:
    """Add ``<feature>_is_null`` int8 columns for features with a >1% NULL rate.

    Measured on the gated rows — the rows that actually reach the model — from
    the raw NaN state, before normalization touches anything. Returns the
    created column names, in ``features`` order.

    When ``pinned`` is given it *replaces* the threshold as the selection rule:
    exactly those indicators are built, whatever the observed NULL rate. This is
    what keeps a saved model's feature vector stable — see ``load_training_frame``.
    """
    if pinned is not None:
        wanted = set(pinned)
        built: list[str] = []
        for col in features:
            name = f"{col}_is_null"
            if name in wanted:
                frame[name] = frame[col].isna().astype(np.int8)
                built.append(name)
        return built

    indicators: list[str] = []
    for col in features:
        if frame[col].isna().mean() > NULL_RATE_THRESHOLD:
            name = f"{col}_is_null"
            frame[name] = frame[col].isna().astype(np.int8)
            indicators.append(name)
    return indicators


def _rank_normalize(frame: pd.DataFrame, features: list[str]) -> None:
    """Rank-normalize ``features`` to [0, 1] within each date, in place.

    ``rank(pct=True)`` keeps NaN as NaN (default ``na_option="keep"``), so raw
    missingness survives for XGBoost's missing-value handling; the ``_is_null``
    indicators make it visible as an explicit feature.
    """
    frame[features] = frame.groupby("date")[features].rank(pct=True)


def load_training_frame(
    parquet_path: Path | None = None,
    gate: bool = True,
    normalize: bool = True,
    feature_names: list[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Return (frame, feature_names). Frame keeps symbol, date, both targets.

    ``parquet_path`` defaults to ``data/processed/ml_dataset.parquet`` in the
    repo root. ``gate`` toggles the F4 gate (both legs), ``normalize`` toggles
    the per-date rank normalization; the missingness indicators and the
    collinear-duplicate / constant-column drops are unconditional parts of the
    contract.

    The returned frame's columns are, in order: ``symbol``, ``date``, the
    surviving continuous features (dataset order), the ``<feature>_is_null``
    indicators (parent-feature order), then the two targets. ``feature_names``
    is the model feature list — the surviving continuous features plus the
    indicators — exactly the columns a Phase 3+ consumer should use as ``X``.

    **Pinning.** By default the indicator set is *derived from the data*: a
    feature earns an ``_is_null`` companion by exceeding ``NULL_RATE_THRESHOLD``
    on the gated rows. That makes the feature vector a function of the dataset,
    which is fine while training but unsafe at inference — ``ret_10d`` sits at a
    1.2% NULL rate against a 1% threshold, so a modest data refresh would silently
    add or drop a column and misalign a saved model. Pass ``feature_names`` (the
    list recorded in the model's evaluation JSON) to pin the contract instead:
    exactly those indicators are built regardless of observed rate, and the
    returned frame's feature block is exactly that list, in that order.

    Raises
    ------
    ValueError
        If the parquet is missing a ``DATASET_COLUMNS`` column, or if a pinned
        feature cannot be produced from it.
    """
    path = parquet_path if parquet_path is not None else _DEFAULT_PARQUET
    frame = pd.read_parquet(path)

    required = [*IDENTITY_COLUMNS, *FEATURE_COLUMNS, *TARGET_COLUMNS]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(
            f"parquet at {path} is missing required columns {missing}; "
            "expected the DATASET_COLUMNS contract from ml/dataset.py"
        )

    if gate:
        frame = frame.loc[_gate_mask(frame)]
    frame = frame.reset_index(drop=True)

    kept = [c for c in FEATURE_COLUMNS if c not in DROP_COLUMNS]
    frame = frame.drop(columns=[c for c in DROP_COLUMNS if c in frame.columns])

    pinned_indicators = (
        None
        if feature_names is None
        else [c for c in feature_names if c.endswith("_is_null")]
    )
    indicators = _add_missingness_indicators(frame, kept, pinned=pinned_indicators)
    if normalize:
        _rank_normalize(frame, kept)

    resolved = [*kept, *indicators]
    if feature_names is not None:
        unproducible = [c for c in feature_names if c not in frame.columns]
        if unproducible:
            raise ValueError(
                f"pinned feature_names cannot be produced from {path}: {unproducible}; "
                "the parquet's DATASET_COLUMNS contract no longer matches the saved model"
            )
        resolved = list(feature_names)

    return frame[["symbol", "date", *resolved, *TARGET_COLUMNS]], resolved
