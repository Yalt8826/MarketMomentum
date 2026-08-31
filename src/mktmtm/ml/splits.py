"""Time-based train/valid/test splitting for the direction model.

Implements the three sealed splits (TRAIN 2021-08-02 -> 2024-12-31,
VALID 2025-01-01 -> 2025-12-31, TEST 2026-01-01 -> 2026-08-28) with
strict no-lookahead guarantees: 2026 data is never touched before Phase 7.

Both splitters share the same anti-leak contract:

- Splits run over **unique dates**, never over rows. Every symbol on a date
  goes to the same side; a row-level split would scatter one date's names
  across the boundary and leak the market factor.
- An **embargo gap** sits between train and validation. The label spans
  ``t -> t+1``, so the last training date's outcome is realized on the next
  trading day; purging ``embargo_days`` dates from the train tail guarantees
  ``max(train_dates) + embargo_days < min(valid_dates)`` in calendar days.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pandas as pd

# The fixed-split embargo (days). The direction label spans t -> t+1, so the
# boundary must clear one day for the last train date's outcome to land in the
# gap, not on a validation date.
EMBARGO_DAYS = 1

# Sealed time windows from the implementation plan, section 0.
TRAIN_START = pd.Timestamp("2021-08-02")
TRAIN_END = pd.Timestamp("2024-12-31")
VALID_START = pd.Timestamp("2025-01-01")
VALID_END = pd.Timestamp("2025-12-31")
TEST_START = pd.Timestamp("2026-01-01")

__all__ = ["walk_forward_splits", "train_valid_test", "inner_holdout"]


def walk_forward_splits(
    dates: pd.Series,
    n_splits: int = 5,
    embargo_days: int = 1,
    min_train_dates: int = 250,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window splits over unique dates, with an embargo gap.

    Yields ``(train_idx, valid_idx)`` positional index arrays into ``dates``
    (hence into any frame carrying the same rows). Every row sharing a date
    lands on the same side.

    Fold ``k`` trains on every unique date strictly before its validation
    window, minus the last ``embargo_days`` dates from the tail, and validates
    on the ``k``-th contiguous chunk of the post-warmup date range. Train sets
    therefore grow monotonically, validation chunks are disjoint and time
    ordered, and the purge guarantees
    ``max(train_dates) + embargo_days < min(valid_dates)``.

    Parameters
    ----------
    dates:
        Row-aligned dates, one entry per frame row (duplicates expected —
        several symbols per date). The series must be free of NaT.
    n_splits:
        Number of validation chunks to produce.
    embargo_days:
        Number of unique trading dates purged from the train tail at each
        boundary. Zero disables the embargo (used only to prove the leak test
        fires).
    min_train_dates:
        Unique dates the first fold's train window must start with; the
        validation space is everything after it.

    Raises
    ------
    ValueError
        If the input is empty or contains NaT, or the parameter combination
        leaves no room for a valid first fold.
    """
    if n_splits < 1:
        raise ValueError(f"n_splits must be >= 1, got {n_splits}")
    if embargo_days < 0:
        raise ValueError(f"embargo_days must be >= 0, got {embargo_days}")
    if min_train_dates < 1:
        raise ValueError(f"min_train_dates must be >= 1, got {min_train_dates}")

    unique = np.unique(dates.to_numpy())
    if unique.size == 0:
        raise ValueError("dates must not be empty")
    if np.isnat(unique).any():
        raise ValueError("dates must not contain NaT")
    if unique.size <= min_train_dates:
        raise ValueError(
            f"need more than {min_train_dates} unique dates, got {unique.size}"
        )
    if embargo_days >= min_train_dates:
        raise ValueError(
            f"embargo_days ({embargo_days}) must be < min_train_dates "
            f"({min_train_dates}) or the first fold's train window is empty"
        )

    # Validation space = everything after the warm-up window, split into
    # n_splits contiguous chunks (all non-empty because of the size check).
    valid_space = unique.size - min_train_dates
    if valid_space < n_splits:
        raise ValueError(
            f"need at least min_train_dates + n_splits = "
            f"{min_train_dates + n_splits} unique dates, got {unique.size}"
        )

    row_dates = dates.to_numpy()
    for chunk in np.array_split(np.arange(valid_space), n_splits):
        lo = min_train_dates + int(chunk[0])
        hi = min_train_dates + int(chunk[-1]) + 1
        valid_dates = unique[lo:hi]
        # Train on everything before the validation window, then purge the
        # last embargo_days dates so the boundary clears the label horizon.
        train_dates = unique[: lo - embargo_days]
        train_idx = np.flatnonzero(np.isin(row_dates, train_dates))
        valid_idx = np.flatnonzero(np.isin(row_dates, valid_dates))
        yield train_idx, valid_idx


def train_valid_test(
    frame: pd.DataFrame,
    *,
    train_end: pd.Timestamp | None = None,
    valid_end: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, ...]:
    """The fixed 2021-24 / 2025 / 2026 split from the plan, embargo applied.

    Splits ``frame`` on its ``date`` column into the three windows:

    - TRAIN: 2021-08-02 .. ``train_end``, minus the last ``EMBARGO_DAYS`` dates
    - VALID: ``train_end`` < date <= ``valid_end``, minus the last
      ``EMBARGO_DAYS`` dates
    - TEST:  date > ``valid_end``

    ``train_end`` / ``valid_end`` default to the sealed ``TRAIN_END`` /
    ``VALID_END`` boundaries (Phase 8 CLI override); the defaults reproduce the
    sealed 2021-24 / 2025 / 2026 split exactly, since ``VALID_START`` and
    ``TEST_START`` are the day after the corresponding end.

    The purge carves the embargo gap out of each earlier side's tail, so both
    boundaries satisfy ``max(left) + EMBARGO_DAYS < min(right)`` in calendar
    days. Row order within each returned frame is the input frame's order.

    Raises
    ------
    ValueError
        If ``train_end >= valid_end`` or any of the three windows is empty.
    """
    train_end_ts = TRAIN_END if train_end is None else pd.Timestamp(train_end)
    valid_end_ts = VALID_END if valid_end is None else pd.Timestamp(valid_end)
    if train_end_ts >= valid_end_ts:
        raise ValueError(
            f"train_end ({train_end_ts.date()}) must be < valid_end "
            f"({valid_end_ts.date()}) so the VALID window is non-empty"
        )

    dates = frame["date"]
    train = frame[(dates >= TRAIN_START) & (dates <= train_end_ts)]
    valid = frame[(dates > train_end_ts) & (dates <= valid_end_ts)]
    test = frame[dates > valid_end_ts]
    train = _purge_tail(train)
    valid = _purge_tail(valid)
    for name, part in (("train", train), ("valid", valid), ("test", test)):
        if part.empty:
            raise ValueError(
                f"{name} split is empty: frame has no rows in the "
                f"{name} window (train_end {train_end_ts.date()}, "
                f"valid_end {valid_end_ts.date()})"
            )
    return train, valid, test


def inner_holdout(
    train: pd.DataFrame,
    frac: float = 0.2,
    embargo_days: int = EMBARGO_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Carve an early-stopping holdout out of the tail of a training window.

    Returns ``(inner_train, inner_valid)``. The last ``frac`` of ``train``'s
    unique dates become ``inner_valid``; the dates before them, minus an
    ``embargo_days`` purge, become ``inner_train``.

    This exists so a walk-forward fold can early-stop without touching the rows
    it is then scored on. Selecting the tree count on the fold's own validation
    set and reporting the score from that same set makes the fold IC
    optimistically biased — the number stops being out-of-sample. Splitting the
    tail off instead keeps the fold's validation rows untouched by any fitting
    decision.

    Raises
    ------
    ValueError
        If ``frac`` is outside (0, 1), or ``train`` holds too few unique dates
        to leave both sides non-empty after the purge.
    """
    if not 0.0 < frac < 1.0:
        raise ValueError(f"frac must be in (0, 1), got {frac}")
    if embargo_days < 0:
        raise ValueError(f"embargo_days must be >= 0, got {embargo_days}")

    unique = np.unique(train["date"].to_numpy())
    n_valid = max(1, int(round(len(unique) * frac)))
    cut = len(unique) - n_valid
    if cut - embargo_days < 1:
        raise ValueError(
            f"train window has {len(unique)} unique dates — too few to carve a "
            f"{frac:.0%} inner holdout with a {embargo_days}-date embargo"
        )

    valid_dates = unique[cut:]
    train_dates = unique[: cut - embargo_days]
    rows = train["date"].to_numpy()
    return (
        train[np.isin(rows, train_dates)],
        train[np.isin(rows, valid_dates)],
    )


def _purge_tail(part: pd.DataFrame, embargo_days: int = EMBARGO_DAYS) -> pd.DataFrame:
    """Drop rows on the last ``embargo_days`` unique dates of ``part``."""
    if embargo_days <= 0 or part.empty:
        return part
    tail = np.unique(part["date"].to_numpy())[-embargo_days:]
    return part[~np.isin(part["date"].to_numpy(), tail)]
