"""Unit tests for the Phase 2 feature contract and loader (ml/features.py).

Exercises the pipeline on small synthetic frames: the F4 gate arithmetic,
the >1% missingness-indicator contract, the collinear-duplicate / constant
column drops, per-date rank normalization, and cross-call stability.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.dataset import DATASET_COLUMNS, IDENTITY_COLUMNS, TARGET_COLUMNS
from mktmtm.ml.features import DROP_COLUMNS, NULL_RATE_THRESHOLD, load_training_frame

_SYMBOLS = ["AAA", "BBB", "CCC"]
_DATES = pd.to_datetime(["2024-01-02", "2024-01-03"])


def _complete(rows: list[dict[str, object]]) -> pd.DataFrame:
    """Fill a row list into a contract-complete frame (every DATASET_COLUMN)."""
    frame = pd.DataFrame(rows)
    for col in DATASET_COLUMNS:
        if col not in frame.columns:
            frame[col] = 0.5
    return frame


def _base_frame() -> pd.DataFrame:
    """3 symbols x 2 dates; every row passes the F4 gate."""
    rows = [
        {"symbol": s, "date": d} for s, d in itertools.product(_SYMBOLS, _DATES)
    ]
    frame = _complete(rows)
    frame["ce_iv_30"] = 15.0
    frame["pe_iv_30"] = 18.0
    frame["next_day_direction"] = 1.0
    return frame


def _write(frame: pd.DataFrame, tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "synth.parquet"
    frame.to_parquet(path, index=False)
    return path


def _kept_features() -> list[str]:
    """The 42 continuous features the loader keeps (contract minus drops)."""
    return [c for c in DATASET_COLUMNS if c not in (*IDENTITY_COLUMNS, *TARGET_COLUMNS, *DROP_COLUMNS)]


# ───────────────────────────── Task 2.1 — the gate ─────────────────────────────


def test_gate_arithmetic(tmp_path: Path) -> None:
    """Each F4 leg fires on its own row; only the two compliant rows survive."""
    rows = [
        dict(symbol="OK1", date=_DATES[0], ce_iv_30=15.0, pe_iv_30=18.0, next_day_direction=1.0),
        dict(symbol="CE_ZERO", date=_DATES[0], ce_iv_30=0.0, pe_iv_30=18.0, next_day_direction=1.0),
        dict(symbol="CE_NEG", date=_DATES[0], ce_iv_30=-5.0, pe_iv_30=18.0, next_day_direction=1.0),
        dict(symbol="CE_HIGH", date=_DATES[0], ce_iv_30=250.0, pe_iv_30=18.0, next_day_direction=1.0),
        dict(symbol="CE_200", date=_DATES[0], ce_iv_30=200.0, pe_iv_30=18.0, next_day_direction=1.0),
        dict(symbol="PE_NAN", date=_DATES[0], ce_iv_30=15.0, pe_iv_30=np.nan, next_day_direction=1.0),
        dict(symbol="PE_ZERO", date=_DATES[0], ce_iv_30=15.0, pe_iv_30=0.0, next_day_direction=1.0),
        dict(symbol="PE_HIGH", date=_DATES[0], ce_iv_30=15.0, pe_iv_30=201.0, next_day_direction=1.0),
        dict(symbol="LABEL_NAN", date=_DATES[0], ce_iv_30=15.0, pe_iv_30=18.0, next_day_direction=np.nan),
    ]
    out, _ = load_training_frame(_write(_complete(rows), tmp_path), normalize=False)
    assert out["symbol"].tolist() == ["OK1", "CE_200"]


def test_gate_flag_off_keeps_rows(tmp_path: Path) -> None:
    """gate=False skips the F4 filter entirely."""
    frame = _base_frame()
    frame.loc[0, "next_day_direction"] = np.nan
    path = _write(frame, tmp_path)
    assert len(load_training_frame(path)[0]) == 5
    assert len(load_training_frame(path, gate=False)[0]) == 6


# ───────────────────── Task 2.2 — missingness indicators ──────────────────────


def test_missingness_indicators(tmp_path: Path) -> None:
    """>1% NULL features get int8 _is_null companions matching .isna(); the rest do not."""
    frame = _base_frame()  # 6 rows, all pass the gate
    frame.loc[0:1, "skew_25d"] = np.nan  # 2/6 = 33% -> indicator
    frame.loc[2, "ret_5d"] = np.nan  # 1/6 = 16.7% -> indicator
    frame.loc[3:5, "pe_vega"] = np.nan  # dropped column: 50% NULL, no indicator
    out, features = load_training_frame(_write(frame, tmp_path), normalize=False)

    assert "skew_25d_is_null" in out.columns
    assert "ret_5d_is_null" in out.columns
    assert "pe_vega_is_null" not in out.columns
    assert "ce_ivp_is_null" not in out.columns  # 0% NULL

    assert out["skew_25d_is_null"].dtype == np.dtype("int8")
    assert out["skew_25d_is_null"].equals(out["skew_25d"].isna().astype(np.int8))
    assert out["ret_5d_is_null"].equals(out["ret_5d"].isna().astype(np.int8))

    # exactly the >1% features: all 42 kept continuous features + 2 indicators
    assert set(features) == set(_kept_features()) | {"skew_25d_is_null", "ret_5d_is_null"}
    assert "interest_rate" not in out.columns


def test_missingness_threshold_boundary(tmp_path: Path) -> None:
    """1 NaN in 200 rows (0.5%) stays under the 1% bar; 3 NaNs (1.5%) clears it."""
    rows = [
        dict(symbol=f"S{i:03d}", date=_DATES[i % 2], ce_iv_30=15.0, pe_iv_30=18.0, next_day_direction=1.0)
        for i in range(200)
    ]
    frame = _complete(rows)
    frame.loc[0, "skew_25d"] = np.nan  # 0.5% -> no indicator
    frame.loc[1:3, "ret_5d"] = np.nan  # 1.5% -> indicator
    out, _ = load_training_frame(_write(frame, tmp_path), normalize=False)
    assert "skew_25d_is_null" not in out.columns
    assert "ret_5d_is_null" in out.columns
    assert round(frame["skew_25d"].isna().mean(), 4) < NULL_RATE_THRESHOLD
    assert frame["ret_5d"].isna().mean() > NULL_RATE_THRESHOLD


# ─────────────────────── Tasks 2.3 / 2.4 — dropped columns ────────────────────


def test_dropped_columns_absent(tmp_path: Path) -> None:
    """pe_vega, pe_rho, ce_volume, pe_volume, interest_rate never reach the frame."""
    out, features = load_training_frame(_write(_base_frame(), tmp_path), normalize=False)
    for col in DROP_COLUMNS:
        assert col not in out.columns
        assert col not in features
    # row order and identity columns survive untouched
    assert out["symbol"].tolist() == ["AAA", "AAA", "BBB", "BBB", "CCC", "CCC"]
    assert list(out.columns[:2]) == ["symbol", "date"]
    assert list(out.columns[-2:]) == TARGET_COLUMNS


# ─────────────────────── Task 2.5 — rank normalization ────────────────────────


def test_rank_normalization_per_date(tmp_path: Path) -> None:
    """Ranks live in [0, 1] and depend only on within-date relative order."""
    frame = _base_frame()
    frame.loc[frame["date"] == _DATES[0], "underlying_price"] = [10.0, 20.0, 30.0]
    frame.loc[frame["date"] == _DATES[1], "underlying_price"] = [1000.0, 2000.0, 3000.0]
    frame.loc[0, "skew_25d"] = np.nan  # ranked NaN stays NaN; indicator flags it
    out, _ = load_training_frame(_write(frame, tmp_path))

    r0 = out.loc[out["date"] == _DATES[0], "underlying_price"].to_numpy()
    r1 = out.loc[out["date"] == _DATES[1], "underlying_price"].to_numpy()
    assert np.allclose(r0, [1 / 3, 2 / 3, 1.0])  # same relative order, different levels
    assert np.allclose(r1, [1 / 3, 2 / 3, 1.0])  # each date ranked independently
    assert ((out["underlying_price"] >= 0.0) & (out["underlying_price"] <= 1.0)).all()
    assert pd.isna(out.loc[0, "skew_25d"])  # missingness preserved, not rank-imputed
    assert out["skew_25d_is_null"].tolist() == [1, 0, 0, 0, 0, 0]  # indicator stays raw 0/1


def test_normalize_flag_off_keeps_raw_values(tmp_path: Path) -> None:
    frame = _base_frame()
    frame.loc[frame["date"] == _DATES[0], "underlying_price"] = [10.0, 20.0, 30.0]
    out, _ = load_training_frame(_write(frame, tmp_path), normalize=False)
    assert out.loc[out["date"] == _DATES[0], "underlying_price"].tolist() == [10.0, 20.0, 30.0]


# ───────────────────────────── contract stability ─────────────────────────────


def test_stable_across_calls(tmp_path: Path) -> None:
    """Two calls return the same row count, column set, and values."""
    path = _write(_base_frame(), tmp_path)
    a, fa = load_training_frame(path)
    b, fb = load_training_frame(path)
    assert a.shape == b.shape
    assert list(a.columns) == list(b.columns)
    assert fa == fb
    assert a.equals(b)


def test_missing_required_column_raises(tmp_path: Path) -> None:
    """A parquet that lost a contract column is a loud error, not silent NaN."""
    frame = _base_frame().drop(columns=["skew_25d"])
    with pytest.raises(ValueError, match="skew_25d"):
        load_training_frame(_write(frame, tmp_path))


# ──────────────────── feature_names pinning — inference stability ────────────────────


def _frame_with_null_rate(col: str, rate: float) -> pd.DataFrame:
    """20 dates x 3 symbols, with ``col`` NULL on exactly ``rate`` of rows."""
    dates = pd.to_datetime([f"2024-01-{d:02d}" for d in range(1, 21)])
    rows = [{"symbol": s, "date": d} for s, d in itertools.product(_SYMBOLS, dates)]
    frame = _complete(rows)
    frame["ce_iv_30"] = 15.0
    frame["pe_iv_30"] = 18.0
    frame["next_day_direction"] = 1.0
    n_null = round(len(frame) * rate)
    frame.loc[: n_null - 1, col] = np.nan
    return frame


def test_pin_keeps_the_feature_vector_stable_when_a_null_rate_moves(tmp_path: Path) -> None:
    """The regression this guards: a NULL rate crossing 1% must not change the columns.

    Unpinned, a feature at 5% NULL earns an ``_is_null`` companion and one at
    0% does not — so the same model would see a different feature vector on a
    refreshed dataset. Pinning holds the contract fixed either way.
    """
    victim = "ret_10d"
    above = _write(_frame_with_null_rate(victim, 0.05), tmp_path / "above")
    below = _write(_frame_with_null_rate(victim, 0.0), tmp_path / "below")

    _, names_above = load_training_frame(parquet_path=above)
    _, names_below = load_training_frame(parquet_path=below)
    indicator = f"{victim}_is_null"
    assert indicator in names_above, "5% NULL must earn an indicator unpinned"
    assert indicator not in names_below, "0% NULL must not, unpinned — the instability"

    # Pinned to the trained contract, the 0%-NULL dataset still produces it.
    frame, names = load_training_frame(parquet_path=below, feature_names=names_above)
    assert names == names_above
    assert list(frame.columns[2 : 2 + len(names)]) == names_above
    assert (frame[indicator] == 0).all(), "column present, correctly all-zero"


def test_pin_drops_indicators_the_saved_model_never_saw(tmp_path: Path) -> None:
    """An indicator that appears in new data but not in the contract is dropped."""
    path = _write(_frame_with_null_rate("ret_10d", 0.05), tmp_path / "p")
    _, unpinned = load_training_frame(parquet_path=path)
    pinned_contract = [c for c in unpinned if c != "ret_10d_is_null"]

    _, names = load_training_frame(parquet_path=path, feature_names=pinned_contract)
    assert names == pinned_contract
    assert "ret_10d_is_null" not in names


def test_pin_preserves_caller_order(tmp_path: Path) -> None:
    """The returned block is the pinned list, in the pinned order."""
    path = _write(_frame_with_null_rate("ret_10d", 0.05), tmp_path / "p")
    _, unpinned = load_training_frame(parquet_path=path)
    shuffled = list(reversed(unpinned))

    frame, names = load_training_frame(parquet_path=path, feature_names=shuffled)
    assert names == shuffled
    assert list(frame.columns[2 : 2 + len(names)]) == shuffled


def test_pin_raises_on_a_feature_the_data_cannot_produce(tmp_path: Path) -> None:
    path = _write(_frame_with_null_rate("ret_10d", 0.05), tmp_path / "p")
    with pytest.raises(ValueError, match="cannot be produced"):
        load_training_frame(parquet_path=path, feature_names=["not_a_real_feature"])


def test_unpinned_default_is_unchanged(tmp_path: Path) -> None:
    """Pinning is opt-in: omitting it reproduces the threshold-derived contract."""
    path = _write(_frame_with_null_rate("ret_10d", 0.05), tmp_path / "p")
    _, a = load_training_frame(parquet_path=path)
    _, b = load_training_frame(parquet_path=path, feature_names=None)
    assert a == b
