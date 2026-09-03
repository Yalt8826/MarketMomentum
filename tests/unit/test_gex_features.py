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


# ───────────────────────────── UPDATE SQL construction ──────────────────────


class _CapturingCursor:
    """Records the SQL + args execute_values would send (no DB)."""

    def __init__(self) -> None:
        self.sql: str | None = None
        self.args: list[tuple[object, ...]] = []
        self.template: str | None = None
        # psycopg2 exposes the affected-row count here; _update_gex_columns
        # accumulates it across pages to report actual writes.
        self.rowcount = 0

    def execute(self, sql: str, args: tuple[object, ...] | None = None) -> None:
        self.sql = sql
        if args:
            self.args = list(args)

    def __enter__(self) -> _CapturingCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _CapturingConn:
    """Connection whose cursor captures the UPDATE; commit/close are no-ops."""

    def __init__(self) -> None:
        self.cursor_ctx = _CapturingCursor()

    def cursor(self) -> _CapturingCursor:
        return self.cursor_ctx

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_update_gex_columns_casts_values(monkeypatch) -> None:
    """The UPDATE must cast every VALUES column explicitly.

    Regression: execute_values with an untyped VALUES list fails when a GEX
    column is all-NULL (gex before gamma lands in the chain) — Postgres infers
    text and the SET breaks. The per-row template must carry the casts.
    """
    def fake_execute_values(cur, sql, rows, template=None, page_size=100):  # type: ignore[no-untyped-def]
        cur.sql = sql
        cur.args = [tuple(r) for r in rows]
        cur.template = template
        cur.rowcount = len(cur.args)  # psycopg2 reports affected rows here

    monkeypatch.setattr("mktmtm.gex_features.execute_values", fake_execute_values)
    conn = _CapturingConn()
    df = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "date": pd.to_datetime(["2026-01-05", "2026-01-06"]),
            "gex": [None, None],  # all-NULL column — the regression case
            "dex": [1.0, 2.0],
        }
    )
    from mktmtm.gex_features import _update_gex_columns

    written = _update_gex_columns(conn, df)

    assert written == 2
    assert conn.cursor_ctx.sql is not None
    assert "UPDATE ml_dataset m SET" in conn.cursor_ctx.sql
    assert "FROM (VALUES %s) AS v(symbol, date, dex, gex)" in conn.cursor_ctx.sql
    assert conn.cursor_ctx.template is not None
    assert "%s::text" in conn.cursor_ctx.template  # symbol cast
    assert "%s::date" in conn.cursor_ctx.template  # date cast
    assert conn.cursor_ctx.template.count("%s::double precision") == 2  # gex + dex
    # GEX_COLUMNS order is dex before gex; both are in the frame.
    assert conn.cursor_ctx.args == [
        ("AAA", pd.Timestamp("2026-01-05").date(), 1.0, None),
        ("AAA", pd.Timestamp("2026-01-06").date(), 2.0, None),
    ]


def test_update_gex_columns_reports_actual_writes(monkeypatch) -> None:
    """``rows_updated`` must count rows written, not rows attempted.

    Regression: the counter returned ``len(rows)``, so a recompute against an
    ``ml_dataset`` missing the (symbol, date) pairs reported full success while
    writing nothing — which is exactly how a stale dataset stays invisible. It
    must also accumulate across pages: ``execute_values`` issues one statement
    per page and leaves ``cur.rowcount`` holding only the last page's count.
    """
    import mktmtm.gex_features as gf

    # Only the first row of each page has an ml_dataset twin.
    def fake_execute_values(cur, sql, rows, template=None, page_size=100):  # type: ignore[no-untyped-def]
        cur.sql = sql
        cur.args = [tuple(r) for r in rows]
        cur.template = template
        cur.rowcount = 1

    monkeypatch.setattr(gf, "execute_values", fake_execute_values)
    monkeypatch.setattr(gf, "_PAGE_SIZE", 2)
    conn = _CapturingConn()
    df = pd.DataFrame(
        {
            "symbol": ["AAA"] * 4,
            "date": pd.to_datetime(
                ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
            ),
            "dex": [1.0, 2.0, 3.0, 4.0],
        }
    )

    # 4 rows / page size 2 = 2 pages, 1 write each.
    assert gf._update_gex_columns(conn, df) == 2


def test_export_ml_dataset_parquet_uses_dataset_columns(tmp_path, monkeypatch) -> None:
    """Regression: gex-features writes to PostgreSQL only, so without an export
    the parquet keeps the GEX values it was built with. That is how the
    2026-09-04 DEALER_SIGN correction left data/processed/ml_dataset.parquet
    holding the old sign for all 212,441 rows while the table held the new one.

    Column identity and order must follow DATASET_COLUMNS (the single source of
    truth), and rows must come back ordered (symbol, date) — row order is
    load-bearing for every momentum feature downstream.
    """
    import pandas as pd

    import mktmtm.gex_features as gf
    from mktmtm.ml.dataset import DATASET_COLUMNS

    captured: dict[str, str] = {}

    def fake_read_sql(sql, conn):
        captured["sql"] = sql
        return pd.DataFrame({c: [1.0, 2.0] for c in DATASET_COLUMNS})

    monkeypatch.setattr(gf.pd, "read_sql", fake_read_sql)

    out, n = gf.export_ml_dataset_parquet(object(), tmp_path / "ml_dataset.parquet")

    assert n == 2
    assert out.exists()
    assert "ORDER BY symbol, date" in captured["sql"]
    for column in DATASET_COLUMNS:
        assert column in captured["sql"], f"{column} missing from the export query"
    assert list(pd.read_parquet(out).columns) == list(DATASET_COLUMNS)
