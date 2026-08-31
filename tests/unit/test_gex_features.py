"""Unit tests for mktmtm.gex_features — the ``gex-features`` recompute.

The recompute touches the DB, so the SQL path is exercised only via the
integration route; these tests monkeypatch the per-symbol frame builder and
the column writer to pin the *contract*: progress advances once per symbol
with the symbol as label, failures are collected in ``skipped`` instead of
killing the run, and the summary dict has the shape the CLI table renders.
"""

from __future__ import annotations

import pandas as pd

from mktmtm.gex_features import GEX_COLUMNS, recompute_dealer_aggregates


class _FakeBar:
    def __init__(self) -> None:
        self.labels: list[str] = []

    def advance(self, n: int = 1, label: str | None = None) -> None:
        if label is not None:
            self.labels.append(label)


class _FakeConn:
    """Minimal stand-in for the psycopg2 connection (close + commit only)."""

    def close(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def cursor(self):  # pragma: no cover - only reached if a test mis-wires
        raise AssertionError("no SQL should run in these unit tests")


def _symbol_frame(symbol: str, n_rows: int = 3) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=n_rows)
    return pd.DataFrame(
        {
            "symbol": [symbol] * n_rows,
            "date": dates,
            **{c: [1.0] * n_rows for c in GEX_COLUMNS},
        }
    )


def test_recompute_advances_once_per_symbol_with_label(monkeypatch) -> None:
    """progress_stage advances per symbol, label = current symbol."""
    bar = _FakeBar()

    def fake_stage(conn, symbol, limit_dates):
        return _symbol_frame(symbol)

    monkeypatch.setattr("mktmtm.gex_features.progress_stage", _ctx(bar))
    monkeypatch.setattr("mktmtm.gex_features._recompute_symbol", fake_stage)
    monkeypatch.setattr("mktmtm.gex_features._all_symbols", lambda conn: ["AAA", "BBB", "CCC"])
    monkeypatch.setattr("mktmtm.gex_features._update_gex_columns", lambda conn, df: len(df))

    summary = recompute_dealer_aggregates(_FakeConn())

    assert summary["symbols"] == 3
    assert summary["rows_updated"] == 9
    assert summary["skipped"] == []
    assert bar.labels == ["AAA", "BBB", "CCC"]
    assert summary["columns"] == GEX_COLUMNS


def test_recompute_empty_symbol_lands_in_skipped(monkeypatch) -> None:
    bar = _FakeBar()
    calls: list[str] = []

    def fake_stage(conn, symbol, limit_dates):
        calls.append(symbol)
        if symbol == "EMPTY":
            return pd.DataFrame(columns=["symbol", "date", *GEX_COLUMNS])
        return _symbol_frame(symbol, n_rows=2)

    monkeypatch.setattr("mktmtm.gex_features.progress_stage", _ctx(bar))
    monkeypatch.setattr("mktmtm.gex_features._recompute_symbol", fake_stage)
    monkeypatch.setattr("mktmtm.gex_features._all_symbols", lambda conn: ["AAA", "EMPTY", "CCC"])
    monkeypatch.setattr("mktmtm.gex_features._update_gex_columns", lambda conn, df: len(df))

    summary = recompute_dealer_aggregates(_FakeConn())

    assert summary["symbols"] == 3
    assert summary["rows_updated"] == 4
    assert summary["skipped"] == ["EMPTY"]
    assert calls == ["AAA", "EMPTY", "CCC"]
    assert bar.labels == ["AAA", "EMPTY", "CCC"]


def test_recompute_failed_symbol_does_not_kill_run(monkeypatch) -> None:
    bar = _FakeBar()

    def fake_stage(conn, symbol, limit_dates):
        if symbol == "BROKEN":
            raise RuntimeError("chain exploded")
        return _symbol_frame(symbol)

    monkeypatch.setattr("mktmtm.gex_features.progress_stage", _ctx(bar))
    monkeypatch.setattr("mktmtm.gex_features._recompute_symbol", fake_stage)
    monkeypatch.setattr("mktmtm.gex_features._all_symbols", lambda conn: ["AAA", "BROKEN"])
    monkeypatch.setattr("mktmtm.gex_features._update_gex_columns", lambda conn, df: len(df))

    summary = recompute_dealer_aggregates(_FakeConn())

    assert summary["rows_updated"] == 3
    assert len(summary["skipped"]) == 1
    assert summary["skipped"][0].startswith("BROKEN (RuntimeError)")
    assert bar.labels == ["AAA", "BROKEN"]


def test_recompute_uses_requested_symbol_subset(monkeypatch) -> None:
    bar = _FakeBar()
    seen: list[str] = []

    def fake_stage(conn, symbol, limit_dates):
        seen.append(symbol)
        return _symbol_frame(symbol)

    monkeypatch.setattr("mktmtm.gex_features.progress_stage", _ctx(bar))
    monkeypatch.setattr("mktmtm.gex_features._recompute_symbol", fake_stage)
    # _all_symbols must NOT be consulted when a subset is passed
    monkeypatch.setattr(
        "mktmtm.gex_features._all_symbols", lambda conn: (_ for _ in ()).throw(AssertionError("unused"))
    )
    monkeypatch.setattr("mktmtm.gex_features._update_gex_columns", lambda conn, df: len(df))

    summary = recompute_dealer_aggregates(_FakeConn(), symbols=["RELIANCE", "TCS"])

    assert seen == ["RELIANCE", "TCS"]
    assert summary["symbols"] == 2
    assert summary["rows_updated"] == 6


def _ctx(bar: _FakeBar):
    """Context-manager stand-in for progress_stage that yields the fake bar."""

    from contextlib import contextmanager

    @contextmanager
    def _inner(description: str, total: int | None = None, unit: str = "items"):
        yield bar

    return _inner
