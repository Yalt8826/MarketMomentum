"""Live PostgreSQL integration tests for the UI DB helpers.

Covers ``src/mktmtm/ui/queries.py`` against the real ``options_db``.
Credential resolution mirrors ``tests/integration/test_db_conn.py``:
PG_* vars come from ``MKTMTM_PG_ENV_FILE``, the athena reference env, or
the project ``.env``; when none resolve the tests are skipped.

Expected data (per ``docs/data/database-report.md``, 2026-08-17):
``option_metrics`` 186,234 rows / 2020-04-13 → 2026-08-14;
``backtest_results`` 7,076 rows / 3,204 traded / 3,872 skipped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2  # type: ignore[import-untyped]
import pytest
from dotenv import load_dotenv

from mktmtm.config.settings import PROJECT_ROOT, Settings
from mktmtm.storage.db import build_dsn
from mktmtm.ui import queries

pytestmark = pytest.mark.integration

# athena reference: <Major Project>/data_preprocessing/nse-options-last-5-years/store_in_db/.env
_ATHENA_REFERENCE_ENV = (
    PROJECT_ROOT.parents[1]
    / "data_preprocessing"
    / "nse-options-last-5-years"
    / "store_in_db"
    / ".env"
)


def _load_pg_env() -> Path | None:
    """Load PG_* vars from the first reachable env file; return its path or None."""
    candidates = [
        Path(os.environ["MKTMTM_PG_ENV_FILE"]) if os.environ.get("MKTMTM_PG_ENV_FILE") else None,
        _ATHENA_REFERENCE_ENV,
        PROJECT_ROOT / ".env",
    ]
    for path in candidates:
        if path is not None and path.is_file():
            load_dotenv(path, override=False)
            return path
    return None


def _pg_configured() -> bool:
    return any(
        os.environ.get(key)
        for key in ("PG_DSN", "PG_HOST", "PG_DB", "PG_USER", "PG_PASSWORD")
    )


@pytest.fixture(scope="module")
def conn() -> Any:
    """A live connection; skips the whole module when no PG is configured."""
    env_file = _load_pg_env()
    if not _pg_configured():
        pytest.skip(
            "no PG credentials available "
            f"(looked for env files: {env_file or 'none found'})"
        )
    cfg = Settings()
    connection = psycopg2.connect(build_dsn(cfg), connect_timeout=10)
    try:
        yield connection
    finally:
        connection.close()


def test_connect() -> None:
    """queries.connect() honours settings and yields a working connection."""
    conn = queries.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
    finally:
        conn.close()


def test_db_status_table_names(conn: Any) -> None:
    """db_status lists exactly the two UI-facing tables with row/size info."""
    status = queries.db_status(conn)
    assert status["ok"] is True
    names = {t["name"] for t in status["tables"]}
    assert names == {"option_metrics", "backtest_results"}
    for table in status["tables"]:
        assert {"name", "rows", "size_bytes"} <= set(table)
        assert table["rows"] > 0
        assert table["size_bytes"] > 0
    assert status["option_metrics"]["rows"] > 0
    assert status["backtest_results"]["rows"] > 0


def test_table_info(conn: Any) -> None:
    """table_info returns schema rows with name/type/nullable/default."""
    cols = queries.table_info(conn, "option_metrics")
    assert len(cols) >= 10
    first = cols[0]
    assert {"name", "type", "nullable", "default"} <= set(first)
    assert first["name"] == "symbol"
    assert first["type"] == "text"
    jsonb = [c for c in cols if c["name"] == "ce"]
    assert jsonb and jsonb[0]["type"] == "jsonb"


def test_sample_rows(conn: Any) -> None:
    """sample_rows returns at most n rows, ordered by the first column."""
    df = queries.sample_rows(conn, "option_metrics", n=20)
    assert isinstance(df, pd.DataFrame)
    assert len(df) <= 20
    assert "symbol" in df.columns and "date" in df.columns
    assert list(df["symbol"]) == sorted(df["symbol"])
    with pytest.raises(ValueError, match="not browsable"):
        queries.sample_rows(conn, "pg_tables", n=5)


def test_run_sql_ok(conn: Any) -> None:
    """run_sql executes arbitrary read-only SQL and returns a DataFrame."""
    df = queries.run_sql(conn, "SELECT count(*) AS n FROM option_metrics")
    assert list(df.columns) == ["n"]
    assert int(df.iloc[0]["n"]) > 0


def test_run_sql_error_is_clean(conn: Any) -> None:
    """run_sql raises a clean ValueError (message) instead of a driver crash."""
    with pytest.raises(ValueError, match="SQL error"):
        queries.run_sql(conn, "SELECT * FROM no_such_table_xyz")
    with pytest.raises(ValueError, match="empty"):
        queries.run_sql(conn, "   ")


def test_pnl_by_month(conn: Any) -> None:
    """pnl_by_month: (year, month, avg_pnl, traded), every year 2022–2026."""
    df = queries.pnl_by_month(conn)
    assert list(df.columns) == ["year", "month", "avg_pnl", "traded"]
    assert len(df) >= 5
    for year in range(2022, 2027):
        assert (df["year"] == year).any(), f"missing year {year}"
    assert df["traded"].sum() >= 1
    # months are 1..12
    assert df["month"].between(1, 12).all()


def test_symbol_leaderboard(conn: Any) -> None:
    """leaderboard: top-limit symbols by total P&L, descending."""
    df = queries.symbol_leaderboard(conn, limit=10)
    assert list(df.columns) == ["symbol", "months", "total_pnl"]
    assert len(df) <= 10
    assert len(df) > 0
    assert list(df["total_pnl"]) == sorted(df["total_pnl"], reverse=True)
    assert (df["months"] >= 1).all()


def test_skip_reasons_sum_to_3872(conn: Any) -> None:
    """skip_reasons sums to the 3,872 skipped months in the DB report (§5)."""
    df = queries.skip_reasons(conn)
    assert list(df.columns) == ["reason", "n"]
    assert int(df["n"].sum()) == 3872
    expected = {
        "Missing Exit Price": 2088,
        "Earnings conflict": 1305,
        "No Suitable Options": 380,
        "No Valid Exit Date": 99,
    }
    got = dict(zip(df["reason"], df["n"].astype(int), strict=True))
    assert got == expected


def test_coverage_year_month(conn: Any) -> None:
    """coverage spans 2020-04 → 2026-08, ordered by year then month."""
    df = queries.coverage_year_month(conn)
    assert list(df.columns) == ["year", "month", "n"]
    first = (int(df.iloc[0]["year"]), int(df.iloc[0]["month"]))
    last = (int(df.iloc[-1]["year"]), int(df.iloc[-1]["month"]))
    assert first == (2020, 4)
    assert last == (2026, 8)
    # monotonic (year, month) ordering
    keys = list(zip(df["year"].astype(int), df["month"].astype(int), strict=True))
    assert keys == sorted(keys)
    assert int(df["n"].sum()) > 100_000
