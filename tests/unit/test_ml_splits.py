"""Unit tests for mktmtm.ml.splits — purged walk-forward splitter (pure pandas, no DB).

The exit-test contract, from the implementation plan Phase 3:

- for every fold, ``max(train_dates) + embargo < min(valid_dates)``;
- no date appears on both sides of a fold;
- no ``(symbol, date)`` appears twice within a fold;
- train sets grow monotonically;
- a splitter without the embargo fails the boundary assertion (negative test).
"""

from __future__ import annotations

import pandas as pd
import pytest

from mktmtm.ml.splits import (
    EMBARGO_DAYS,
    TEST_START,
    TRAIN_END,
    TRAIN_START,
    VALID_END,
    VALID_START,
    train_valid_test,
    walk_forward_splits,
)


def _assert_embargo_gap(train: pd.DataFrame, valid: pd.DataFrame, embargo_days: int) -> None:
    """The boundary assertion: the purge must clear the label horizon."""
    assert train["date"].max() + pd.Timedelta(days=embargo_days) < valid["date"].min()


def _synthetic_frame(n_symbols: int = 3) -> pd.DataFrame:
    """Several symbols across the full 2021-08 .. 2026-08 span, one row per (symbol, date)."""
    dates = pd.bdate_range("2021-01-04", "2026-08-28")
    rows = [(f"S{i}", d) for i in range(n_symbols) for d in dates]
    frame = pd.DataFrame(rows, columns=["symbol", "date"])
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def test_walk_forward_default_folds_are_leak_free() -> None:
    frame = _synthetic_frame()
    folds = list(walk_forward_splits(frame["date"]))  # n_splits=5, embargo=1, min_train=250
    assert len(folds) == 5

    train_dates: list[set[pd.Timestamp]] = []
    valid_dates: list[set[pd.Timestamp]] = []
    train_sizes: list[int] = []

    for train_idx, valid_idx in folds:
        train = frame.iloc[train_idx]
        valid = frame.iloc[valid_idx]
        assert len(train) > 0 and len(valid) > 0

        # 1. max(train) + embargo < min(valid), and train is strictly before valid
        _assert_embargo_gap(train, valid, 1)
        assert train["date"].max() < valid["date"].min()

        # 2. no date on both sides (the whole date travels as one unit)
        t_dates = set(train["date"].unique())
        v_dates = set(valid["date"].unique())
        assert not (t_dates & v_dates)

        # 3. no (symbol, date) twice: rows are unique within a side and disjoint across sides
        t_keys = set(zip(train["symbol"], train["date"], strict=True))
        v_keys = set(zip(valid["symbol"], valid["date"], strict=True))
        assert len(t_keys) == len(train)
        assert len(v_keys) == len(valid)
        assert not (t_keys & v_keys)

        train_dates.append(t_dates)
        valid_dates.append(v_dates)
        train_sizes.append(len(train))

    # 4. expanding: train sets grow monotonically (rows and unique dates)
    assert train_sizes == sorted(train_sizes)
    assert len(set(train_sizes)) == len(train_sizes)
    for k in range(len(folds) - 1):
        assert train_dates[k] < train_dates[k + 1]  # strict subset

    # validation windows are disjoint, time ordered, and cover the post-warmup range exactly
    for k in range(len(folds) - 1):
        assert not (valid_dates[k] & valid_dates[k + 1])
        assert max(valid_dates[k]) < min(valid_dates[k + 1])
    all_unique = pd.Series(frame["date"].unique()).sort_values().reset_index(drop=True)
    assert set().union(*valid_dates) == set(all_unique.iloc[250:])


def test_walk_forward_positions_map_onto_shuffled_frame() -> None:
    """Indices are positional: they must stay correct on an unsorted frame."""
    frame = _synthetic_frame()
    shuffled = frame.sample(frac=1.0, random_state=0).reset_index(drop=True)
    folds = list(
        walk_forward_splits(shuffled["date"], n_splits=4, embargo_days=1, min_train_dates=100)
    )
    assert len(folds) == 4
    for train_idx, valid_idx in folds:
        train = shuffled.iloc[train_idx]
        valid = shuffled.iloc[valid_idx]
        assert not (set(train_idx) & set(valid_idx))
        _assert_embargo_gap(train, valid, 1)
        assert not (set(train["date"].unique()) & set(valid["date"].unique()))
        assert not (set(zip(train["symbol"], train["date"], strict=True)) & set(zip(valid["symbol"], valid["date"], strict=True)))


def test_splitter_without_embargo_fails_boundary_assertion() -> None:
    """Negative test: the boundary assertion can actually detect a missing embargo."""
    # Consecutive calendar days (no weekends): every boundary is day k | day k+1,
    # so a no-embargo splitter always violates max(train) + 1 < min(valid).
    dates = pd.Series(pd.date_range("2021-01-04", periods=120, freq="D"))
    folds = list(walk_forward_splits(dates, n_splits=4, embargo_days=0, min_train_dates=20))
    assert len(folds) == 4
    for train_idx, valid_idx in folds:
        max_train = dates.iloc[train_idx].max()
        min_valid = dates.iloc[valid_idx].min()
        assert max_train < min_valid  # still chronologically ordered…
        assert not (max_train + pd.Timedelta(days=1) < min_valid)  # …but the embargo is missing


def test_walk_forward_validates_inputs() -> None:
    with pytest.raises(ValueError, match="unique dates"):
        list(walk_forward_splits(pd.Series(pd.bdate_range("2021-01-04", periods=60))))
    with pytest.raises(ValueError, match="min_train_dates"):
        list(walk_forward_splits(pd.Series(pd.bdate_range("2021-01-04", periods=8)), n_splits=5, min_train_dates=5))
    with pytest.raises(ValueError, match="embargo_days"):
        list(walk_forward_splits(pd.Series(pd.bdate_range("2021-01-04", periods=300)), embargo_days=250))
    with pytest.raises(ValueError, match="empty"):
        list(walk_forward_splits(pd.Series([], dtype="datetime64[ns]")))
    with pytest.raises(ValueError, match="NaT"):
        list(walk_forward_splits(pd.Series([pd.NaT, pd.Timestamp("2021-01-04")])))


def test_train_valid_test_sealed_split() -> None:
    frame = _synthetic_frame()
    train, valid, test = train_valid_test(frame)

    # sealed windows from the plan
    assert train["date"].min() == TRAIN_START
    assert train["date"].max() <= TRAIN_END
    assert valid["date"].min() >= VALID_START
    assert valid["date"].max() <= VALID_END
    assert test["date"].min() >= TEST_START

    # embargo applied at both boundaries
    _assert_embargo_gap(train, valid, EMBARGO_DAYS)
    _assert_embargo_gap(valid, test, EMBARGO_DAYS)

    # no (symbol, date) appears in more than one split
    def keys(df: pd.DataFrame) -> set[tuple[str, pd.Timestamp]]:
        return set(zip(df["symbol"], df["date"], strict=True))

    assert not (keys(train) & keys(valid))
    assert not (keys(train) & keys(test))
    assert not (keys(valid) & keys(test))

    # every row is accounted for: the three splits, the rows before the sealed
    # TRAIN start (2021-08-02), and the two embargo-purged dates
    n_pre = frame["date"].lt(TRAIN_START).sum()
    purged_dates = {pd.Timestamp("2024-12-31"), pd.Timestamp("2025-12-31")}
    n_purged = frame["date"].isin(purged_dates).sum()
    assert len(train) + len(valid) + len(test) + n_pre + n_purged == len(frame)


def test_train_valid_test_raises_on_missing_period() -> None:
    dates = pd.bdate_range("2021-01-04", "2025-12-31")
    frame = pd.DataFrame([("S0", d) for d in dates], columns=["symbol", "date"])
    frame["date"] = pd.to_datetime(frame["date"])
    with pytest.raises(ValueError, match="test split is empty"):
        train_valid_test(frame)


def test_train_valid_test_custom_boundaries() -> None:
    """Phase 8: --train-end / --valid-end overrides move all three windows."""
    frame = _synthetic_frame()
    train_end = pd.Timestamp("2024-06-30")
    valid_end = pd.Timestamp("2025-06-30")
    train, valid, test = train_valid_test(
        frame, train_end=train_end, valid_end=valid_end
    )

    assert train["date"].min() == TRAIN_START
    assert train["date"].max() <= train_end
    assert valid["date"].min() > train_end
    assert valid["date"].max() <= valid_end
    assert test["date"].min() > valid_end

    # the embargo gap is still enforced at both boundaries
    _assert_embargo_gap(train, valid, EMBARGO_DAYS)
    _assert_embargo_gap(valid, test, EMBARGO_DAYS)

    # no (symbol, date) appears in more than one split
    def keys(df: pd.DataFrame) -> set[tuple[str, pd.Timestamp]]:
        return set(zip(df["symbol"], df["date"], strict=True))

    assert not (keys(train) & keys(valid))
    assert not (keys(train) & keys(test))
    assert not (keys(valid) & keys(test))


def test_train_valid_test_custom_boundaries_defaults_equal_sealed() -> None:
    """Passing the sealed constants explicitly reproduces the default split exactly."""
    frame = _synthetic_frame()
    default = train_valid_test(frame)
    explicit = train_valid_test(
        frame, train_end=TRAIN_END, valid_end=VALID_END
    )
    for left, right in zip(default, explicit, strict=True):
        assert left.equals(right)


def test_train_valid_test_rejects_invalid_boundaries() -> None:
    frame = _synthetic_frame()
    with pytest.raises(ValueError, match="train_end .* must be < valid_end"):
        train_valid_test(
            frame,
            train_end=pd.Timestamp("2025-06-30"),
            valid_end=pd.Timestamp("2024-06-30"),
        )
    # equal ends are caught by the ordering validation
    with pytest.raises(ValueError, match="train_end .* must be < valid_end"):
        train_valid_test(
            frame,
            train_end=pd.Timestamp("2024-06-30"),
            valid_end=pd.Timestamp("2024-06-30"),
        )
    # a non-trading gap between the ends leaves an empty VALID window
    with pytest.raises(ValueError, match="valid split is empty"):
        train_valid_test(
            frame,
            train_end=pd.Timestamp("2024-06-28"),
            valid_end=pd.Timestamp("2024-06-30"),
        )
    # a valid_end at/after the last data date leaves an empty TEST window
    with pytest.raises(ValueError, match="test split is empty"):
        train_valid_test(frame, valid_end=pd.Timestamp("2026-08-28"))
