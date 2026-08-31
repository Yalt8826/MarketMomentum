"""Baseline models for the direction task.

Reference estimators whose scores calibrate the XGBoost result: constant
up-rate, per-symbol momentum persistence, and similar trivial predictors.
"""

from __future__ import annotations

import json
from pathlib import Path
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

DEFAULT_OUT_PATH = "models/evaluation/baselines.json"


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


def build_baselines_table(
    parquet_path: str | Path | None = None,
    out_path: str | Path = DEFAULT_OUT_PATH,
    seed: int = RANDOM_SEED,
) -> pd.DataFrame:
    """Load the gated frame, cut VALID with the Phase 3 splitter, score baselines.

    Writes the table to ``out_path`` as JSON together with provenance (split,
    period, row/date counts). ``parquet_path`` defaults to the repo's
    ``data/processed/ml_dataset.parquet`` (resolved by the Phase 2 loader).
    Features are loaded *unnormalized* on purpose: the reversal and tripwire
    baselines consume raw ``ret_1d`` — per-date rank normalization would leave
    the rank IC identical but would obscure the raw-signature tripwire this
    phase exists to run.
    """
    from mktmtm.ml.features import load_training_frame  # Phase 2 loader
    from mktmtm.ml.splits import train_valid_test  # Phase 3 splitter

    frame, _ = load_training_frame(
        Path(parquet_path) if parquet_path is not None else None,
        gate=True,
        normalize=False,
    )
    _train, valid, _test = train_valid_test(frame)

    table = score_baselines(valid, seed=seed)

    payload = {
        "split": "VALID",
        "period": f"{valid['date'].min().date()} → {valid['date'].max().date()}",
        "rows": int(len(valid)),
        "dates": int(valid["date"].nunique()),
        "baselines": table.to_dict(orient="index"),
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(payload, indent=2) + "\n")
    return table


def main() -> None:
    """Run the Phase 5 exit test and print the baseline table."""
    table = build_baselines_table()
    print(table.to_string())


if __name__ == "__main__":
    main()
