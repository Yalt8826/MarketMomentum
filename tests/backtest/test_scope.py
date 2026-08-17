"""Unit tests for backtest run-scope resolution — no live database.

``resolve_scope`` is the pure helper behind ``run_backtest``'s scope overrides
(--symbols/--years/--months). These tests pin its default window (last 4 years
inclusive × all catalog symbols × all 12 months), its explicit overrides, its
invalid-input filtering, and an end-to-end check that ``run_backtest`` only
processes the resolved scope against a fake DB.
"""

from __future__ import annotations

from typing import Self

import pytest

from mktmtm.backtest import strangle as strangle_mod
from mktmtm.backtest.strangle import resolve_scope


def test_resolve_scope_defaults_to_full_window() -> None:
    symbols, years, months = resolve_scope(
        None, None, None, all_symbols=["A", "B"], current_year=2026
    )

    assert symbols == ["A", "B"]
    assert years == [2022, 2023, 2024, 2025, 2026]  # last 4 years, inclusive
    assert months == list(range(1, 13))


def test_resolve_scope_explicit_overrides() -> None:
    symbols, years, months = resolve_scope(
        ["NIFTY"], [2023], [1, 2], all_symbols=["A", "B"], current_year=2026
    )

    assert symbols == ["NIFTY"]
    assert years == [2023]
    assert months == [1, 2]


def test_resolve_scope_filters_invalid_years() -> None:
    symbols, years, months = resolve_scope(
        None, [2030], None, all_symbols=["A", "B"], current_year=2026
    )

    assert symbols == ["A", "B"]
    assert years == []  # 2030 is outside the supported window → filtered out
    assert months == list(range(1, 13))


# ─────────────────────── run_backtest honours the scope ─────────────────────


class _FakeCursor:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_run_backtest_only_processes_resolved_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    processed: list[tuple[str, int, int]] = []

    def fake_load_symbols_info() -> tuple[list[str], set[str]]:
        return ["NIFTY", "BANKNIFTY"], {"NIFTY", "BANKNIFTY"}

    def fake_get_conn(_cfg: object) -> _FakeConn:
        return _FakeConn()

    def fake_run_month(
        symbol: str, year: int, month: int, _index: set[str], _cur: object
    ) -> dict[str, object]:
        processed.append((symbol, year, month))
        return {"symbol": symbol, "year": year, "month": month}

    monkeypatch.setattr(strangle_mod, "load_symbols_info", fake_load_symbols_info)
    monkeypatch.setattr(strangle_mod, "_get_conn", fake_get_conn)
    monkeypatch.setattr(strangle_mod, "run_backtest_month", fake_run_month)

    count = strangle_mod.run_backtest(None, symbols=["NIFTY"], years=[2023], months=[1, 2])

    assert count == 2
    assert processed == [("NIFTY", 2023, 1), ("NIFTY", 2023, 2)]
