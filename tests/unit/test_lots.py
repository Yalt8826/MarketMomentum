"""Unit tests for the lot-size reference loader (src/mktmtm/data/lots.py)."""

from __future__ import annotations

from datetime import date

from mktmtm.data.lots import LotSizeSchedule, load_lot_sizes


def _write_schedule(tmp_path, text: str):
    p = tmp_path / "lot_sizes.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_and_lookup_across_change_points(tmp_path):
    p = _write_schedule(
        tmp_path,
        "symbol,effective_from,lot_size,source\n"
        "NIFTY,2021-08-02,50,nse_circular\n"
        "NIFTY,2024-04-26,25,nse_circular\n"
        "NIFTY,2024-11-21,75,nse_circular\n"
        "RELIANCE,2021-08-02,250,derived\n"
        "RELIANCE,2024-10-28,500,nse_bhavcopy\n",
    )
    sched = load_lot_sizes(p)
    assert sched.get("NIFTY", date(2021, 8, 2)) == 50
    assert sched.get("NIFTY", date(2023, 1, 1)) == 50
    assert sched.get("NIFTY", date(2024, 4, 26)) == 25
    assert sched.get("NIFTY", date(2024, 11, 20)) == 25
    assert sched.get("NIFTY", date(2024, 11, 21)) == 75
    assert sched.get("NIFTY", date(2026, 8, 28)) == 75
    assert sched.get("RELIANCE", date(2024, 10, 27)) == 250
    assert sched.get("RELIANCE", date(2024, 10, 28)) == 500


def test_lookup_before_first_change_point_returns_none(tmp_path):
    p = _write_schedule(tmp_path, "symbol,effective_from,lot_size,source\nTCS,2021-10-29,150,derived\n")
    sched = load_lot_sizes(p)
    assert sched.get("TCS", date(2021, 8, 2)) is None
    assert sched.get("TCS", date(2021, 10, 29)) == 150
    assert sched.get("TCS", date(2022, 1, 1)) == 150


def test_unknown_symbol_returns_none(tmp_path):
    p = _write_schedule(tmp_path, "symbol,effective_from,lot_size,source\nSBIN,2021-08-02,1500,derived\n")
    sched = load_lot_sizes(p)
    assert sched.get("NOSUCH", date(2022, 1, 1)) is None
    assert "SBIN" in sched.symbols


def test_malformed_rows_are_skipped(tmp_path):
    p = _write_schedule(
        tmp_path,
        "symbol,effective_from,lot_size,source\n"
        "OK,2021-08-02,100,derived\n"
        "BAD1,not-a-date,100,derived\n"
        "BAD2,2021-08-02,zero,derived\n"
        ",2021-08-02,100,derived\n",
    )
    sched = load_lot_sizes(p)
    assert sched.get("OK", date(2021, 8, 2)) == 100
    assert sched.symbols == {"OK"}


def test_missing_file_returns_empty_schedule(tmp_path):
    sched = load_lot_sizes(tmp_path / "nope.csv")
    assert sched.symbols == set()
    assert sched.get("NIFTY", date(2022, 1, 1)) is None


def test_effective_changes_are_sorted(tmp_path):
    p = _write_schedule(
        tmp_path,
        "symbol,effective_from,lot_size,source\n"
        "ABC,2024-11-21,30,derived\n"
        "ABC,2023-04-28,15,derived\n"
        "ABC,2021-01-01,25,derived\n",
    )
    sched = load_lot_sizes(p)
    assert sched.effective_changes("ABC") == [
        (date(2021, 1, 1), 25),
        (date(2023, 4, 28), 15),
        (date(2024, 11, 21), 30),
    ]


def test_symbol_case_is_normalized(tmp_path):
    p = _write_schedule(tmp_path, "symbol,effective_from,lot_size,source\nreliance,2021-08-02,250,derived\n")
    sched = load_lot_sizes(p)
    assert sched.get("RELIANCE", date(2021, 8, 2)) == 250
    assert sched.get("Reliance", date(2021, 8, 2)) == 250


def test_empty_schedule_lookup(tmp_path):
    p = _write_schedule(tmp_path, "symbol,effective_from,lot_size,source\n")
    sched = load_lot_sizes(p)
    assert sched.get("NIFTY", date(2022, 1, 1)) is None


def test_lot_size_schedule_direct_construction():
    sched = LotSizeSchedule({"X": [(date(2022, 1, 1), 75), (date(2023, 6, 1), 100)]})
    assert sched.get("X", date(2021, 12, 31)) is None
    assert sched.get("X", date(2022, 1, 1)) == 75
    assert sched.get("X", date(2023, 5, 31)) == 75
    assert sched.get("X", date(2023, 6, 1)) == 100
