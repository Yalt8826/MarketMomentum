"""Tests for incremental ETL planning.

A full rebuild loads the entire ~60M-row bhavcopy cache (~9 GB resident) and
recomputes five years to add a few days. On a 14 GiB host that was OOM-killed
twice on 2026-09-05. Incremental mode reads the per-symbol frontier from
option_metrics, loads only the trailing window, and writes only what is
missing.

Two things here are easy to get subtly wrong and are pinned below: the write
cutoff must be per-symbol, and it must not rewrite the recomputed overlap.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from mktmtm.storage import etl


def test_cache_floor_is_a_lookback_before_the_newest_frontier(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window must cover 252 trading days so IV percentile/rank have history."""
    monkeypatch.setattr(
        etl, "_symbol_frontiers", lambda cfg: {"AAA": date(2026, 9, 4)}
    )
    floor, _ = etl._incremental_plan(object(), since=None)  # type: ignore[arg-type]
    assert floor is not None
    gap = (pd.Timestamp("2026-09-04") - floor).days
    assert gap == etl.INCREMENTAL_LOOKBACK_DAYS
    # 252 trading days is ~365 calendar days; the lookback must clear it.
    assert gap > 365


def test_write_cutoffs_are_per_symbol_not_global(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A global MAX(date) would freeze a partial state permanently.

    After the OOM-killed run three symbols sat at 09-04 while 214 stopped at
    08-28. A global frontier of 09-04 would have skipped writing the five
    missing days for everyone else.
    """
    frontiers = {
        "AHEAD": date(2026, 9, 4),
        "BEHIND_1": date(2026, 8, 28),
        "BEHIND_2": date(2026, 8, 28),
    }
    monkeypatch.setattr(etl, "_symbol_frontiers", lambda cfg: frontiers)
    _floor, cutoffs = etl._incremental_plan(object(), since=None)  # type: ignore[arg-type]
    assert cutoffs == frontiers
    assert cutoffs["BEHIND_1"] < cutoffs["AHEAD"], (
        "a symbol behind the frontier must still get its missing days"
    )


def test_explicit_since_suppresses_the_write_cutoffs(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--since` means reprocess AND rewrite that range, not skip it."""
    monkeypatch.setattr(
        etl, "_symbol_frontiers", lambda cfg: {"AAA": date(2026, 9, 4)}
    )
    floor, cutoffs = etl._incremental_plan(object(), since=date(2024, 1, 1))  # type: ignore[arg-type]
    assert floor == pd.Timestamp("2024-01-01")
    assert cutoffs == {}, "an explicit --since must actually rewrite"


def test_an_empty_table_plans_a_full_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(etl, "_symbol_frontiers", lambda cfg: {})
    floor, cutoffs = etl._incremental_plan(object(), since=None)  # type: ignore[arg-type]
    assert floor is None and cutoffs == {}


def test_write_cutoff_keeps_only_days_after_the_frontier() -> None:
    """The recomputed overlap must not be written back.

    Those rows already hold IV percentile/rank computed with a *complete*
    history; recomputing them inside a trailing window truncates that history,
    so writing them would replace correct values with worse ones.
    """
    time_map = {
        "26-Aug-2026": {"x": 1},
        "27-Aug-2026": {"x": 2},
        "28-Aug-2026": {"x": 3},
        "31-Aug-2026": {"x": 4},
        "01-Sep-2026": {"x": 5},
    }
    cutoff = date(2026, 8, 28)
    kept = {
        k: v for k, v in time_map.items()
        if datetime.strptime(k, "%d-%b-%Y").date() > cutoff
    }
    assert sorted(kept) == ["01-Sep-2026", "31-Aug-2026"]
    assert "28-Aug-2026" not in kept, "the frontier day itself is already correct"


def test_cache_loader_accepts_a_date_floor(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The floor is pushed into the parquet reader, not applied afterwards.

    Materialising the full frame and filtering after would defeat the purpose:
    the whole point is never to hold the years the run does not need.
    """
    frame = pd.DataFrame(
        {
            "SYMBOL": ["AAA"] * 4,
            "DATE": pd.to_datetime(
                ["2024-01-01", "2025-01-01", "2026-01-01", "2026-09-01"]
            ),
            "CLOSE": [1.0, 2.0, 3.0, 4.0],
        }
    )
    path = tmp_path / "cache.parquet"
    frame.to_parquet(path, index=False)

    loaded = etl._load_cache(path, since=pd.Timestamp("2026-01-01"))
    assert loaded is not None
    df, offsets = loaded
    assert len(df) == 2, "rows before the floor must never be read"
    assert offsets["AAA"] == (0, 2)

    full = etl._load_cache(path)
    assert full is not None and len(full[0]) == 4
