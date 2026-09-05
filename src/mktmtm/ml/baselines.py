"""Baseline scorers for the per-symbol cross-sectional direction task.

Reference estimators whose scores calibrate a ranker: constant up-rate,
seeded noise, one-day reversal, and a leakage tripwire. Pure functions over a
frame that already carries ``next_day_return`` / ``next_day_direction`` /
``ret_1d``; pair them with ``ml.metrics.summarize``.

**These are for the cross-sectional task**, which the project has moved away
from (see ``docs/models/xgboost-direction-model-card.md`` for why). The
market-level equivalent is ``ml.market_flow.score_static_baselines``, which
follows the same pattern — including the tripwire, which is the part worth
copying: a baseline that *should* score near-perfectly is a stronger check on
a harness than any passing test.

This module previously exposed ``build_baselines_table``, which loaded the
gated training frame via ``mktmtm.ml.features.load_training_frame``. That
module was never merged onto this branch and now exists only at the
``archive/xgboost-direction`` tag, so the entry point raised ``ImportError``
whenever it was called. It has been removed rather than left as a trap; the
scoring functions below never depended on it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

PRED_COL = "pred"
RETURN_COL = "next_day_return"
DIRECTION_COL = "next_day_direction"
REVERSAL_FEATURE = "ret_1d"

RANDOM_SEED = 42

# Ordered row names of the baseline table.
BASELINE_NAMES = (
    "constant_up",
    "random_uniform",
    "reversal_minus_ret_1d",
    "leak_tripwire_ret_1d",
)

def constant_up_scores(frame: pd.DataFrame) -> pd.Series:
    """Always predict up: pred = 1.0 for every row.

    Accuracy of this baseline is the base rate of the evaluated split
    (≈ 0.4999 on VALID); its rank IC is zero by construction.
    """
    return pd.Series(1.0, index=frame.index, name=PRED_COL)


def random_uniform_scores(frame: pd.DataFrame, seed: int = RANDOM_SEED) -> pd.Series:
    """I.i.d. uniform scores on [0, 1), seeded for reproducibility.

    A pure-noise reference: IC ≈ 0, ICIR ≈ 0 by construction.
    """
    rng = np.random.default_rng(seed)
    return pd.Series(rng.random(len(frame)), index=frame.index, name=PRED_COL)


def reversal_scores(frame: pd.DataFrame) -> pd.Series:
    """Short-horizon reversal: pred = -ret_1d.

    Names that fell hardest yesterday are ranked highest today. One-day
    reversal is a real cross-sectional effect, so this is the baseline the
    model actually has to beat.
    """
    return (-frame[REVERSAL_FEATURE]).rename(PRED_COL)


def raw_ret_1d_scores(frame: pd.DataFrame) -> pd.Series:
    """Leakage tripwire: pred = ret_1d raw, sign unflipped.

    A large |IC| of either sign here is the signature of the historical
    look-ahead bug (the label realized *after* this feature was observed
    must not be predictable from it). Expect |IC| < 0.05.
    """
    return frame[REVERSAL_FEATURE].rename(PRED_COL)


def _baseline_scores(
    frame: pd.DataFrame, seed: int = RANDOM_SEED
) -> dict[str, pd.Series]:
    return {
        "constant_up": constant_up_scores(frame),
        "random_uniform": random_uniform_scores(frame, seed=seed),
        "reversal_minus_ret_1d": reversal_scores(frame),
        "leak_tripwire_ret_1d": raw_ret_1d_scores(frame),
    }


def score_baselines(frame: pd.DataFrame, seed: int = RANDOM_SEED) -> pd.DataFrame:
    """Score every baseline with the Phase 4 harness on ``frame``.

    ``frame`` must carry ``date``, ``next_day_return``, ``next_day_direction``
    and (for the reversal rows) ``ret_1d``. Returns one row per baseline,
    indexed by baseline name, with the ``summarize`` metrics as columns.
    """
    from mktmtm.ml.metrics import summarize  # Phase 4 harness

    records: dict[str, dict[str, Any]] = {}
    for name, scores in _baseline_scores(frame, seed=seed).items():
        try:
            records[name] = summarize(frame.assign(pred=scores.values))
        except Exception as exc:  # one broken baseline must not kill the table
            records[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return pd.DataFrame.from_dict(records, orient="index")
