"""Read-only database helpers for the Streamlit control panel.

Pure and Streamlit-free so the module stays testable: every function
takes a psycopg2 connection (see ``connect()``) and returns plain dicts
or pandas DataFrames. The Explorer / Analytics pages in
``frontend_app.py`` are the only Streamlit consumers.

Queries mirror the appendix of ``docs/data/database-report.md`` so the
numbers shown in the UI always match the documented database contents.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import psycopg2

from mktmtm.config.settings import settings

# Tables the Explorer UI can browse/sample. ``sample_rows`` refuses
# anything outside this whitelist (the name is interpolated into SQL).
BROWSE_TABLES = ("option_metrics", "backtest_results")


def connect() -> Any:
    """Open a psycopg2 connection from ``settings`` (10 s connect timeout).

    The caller owns the connection; Streamlit pages keep one cached
    connection and close it when the process exits.
    """
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_db,
        user=settings.pg_user,
        password=settings.pg_password,
        sslmode=settings.pg_sslmode,
        connect_timeout=10,
    )


def db_status(conn: Any) -> dict[str, Any]:
    """Compact health snapshot of the two UI-facing tables.

    Rows/sizes come from ``pg_stat_user_tables`` (cheap, no full scans).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relname, n_live_tup, pg_table_size(relid) "
            "FROM pg_stat_user_tables "
            "WHERE relname IN ('option_metrics', 'backtest_results') "
            "ORDER BY relname"
        )
        rows = cur.fetchall()
    tables = [
        {"name": name, "rows": live_rows, "size_bytes": size}
        for name, live_rows, size in rows
    ]
    per_table: dict[str, dict[str, Any]] = {
        t["name"]: {"rows": t["rows"], "size_bytes": t["size_bytes"]} for t in tables
    }
    return {
        "ok": True,
        "tables": tables,
        "option_metrics": per_table.get("option_metrics", {}),
        "backtest_results": per_table.get("backtest_results", {}),
    }


def table_info(conn: Any, table: str) -> list[dict[str, Any]]:
    """Describe a table's columns via ``information_schema``.

    Returns one dict per column: ``name``, ``type``, ``nullable``, ``default``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        )
        cols = cur.fetchall()
    return [
        {
            "name": name,
            "type": dtype,
            "nullable": nullable == "YES",
            "default": default,
        }
        for name, dtype, nullable, default in cols
    ]


def sample_rows(conn: Any, table: str, n: int = 20) -> pd.DataFrame:
    """First ``n`` rows of a whitelisted table (ordered by its first column)."""
    if table not in BROWSE_TABLES:
        allowed = ", ".join(BROWSE_TABLES)
        raise ValueError(f"table not browsable: {table!r} (allowed: {allowed})")
    return pd.read_sql(f"SELECT * FROM {table} ORDER BY 1 LIMIT {int(n)}", conn)


def run_sql(conn: Any, sql: str) -> pd.DataFrame:
    """Run a read-only SQL statement and return the result set.

    Failures raise ``ValueError`` with a clean message instead of crashing
    the UI; the pages catch it and render ``st.error``.
    """
    if not sql or not sql.strip():
        raise ValueError("SQL query is empty")
    try:
        return pd.read_sql(sql, conn)
    except Exception as exc:  # surface any driver error cleanly
        # A failed statement leaves the transaction aborted; roll back so a
        # bad query in the SQL box cannot poison the shared UI connection.
        conn.rollback()
        raise ValueError(f"SQL error: {exc}") from exc


def pnl_by_month(conn: Any) -> pd.DataFrame:
    """Monthly backtest P&L: ``(year, month, avg_pnl, traded)``."""
    return pd.read_sql(
        "SELECT year, month, round(avg(pnl_points)::numeric, 2) AS avg_pnl, "
        "count(*) FILTER (WHERE pnl_points IS NOT NULL) AS traded "
        "FROM backtest_results GROUP BY year, month ORDER BY year, month",
        conn,
    )


def symbol_leaderboard(conn: Any, limit: int = 10) -> pd.DataFrame:
    """Top ``limit`` symbols by cumulative P&L (traded months only)."""
    return pd.read_sql(
        "SELECT symbol, count(*) AS months, "
        "round(sum(pnl_points)::numeric, 2) AS total_pnl "
        "FROM backtest_results WHERE pnl_points IS NOT NULL "
        "GROUP BY symbol ORDER BY total_pnl DESC LIMIT %s",
        conn,
        params=(int(limit),),
    )


def skip_reasons(conn: Any) -> pd.DataFrame:
    """Skip-reason breakdown from ``backtest_results`` (skipped months only).

    The backtester writes granular reasons (e.g. ``Earnings on 2025-01-30``);
    fold them into the four headline categories of the DB report §5 so the
    chart stays readable and matches the documented numbers.
    """
    return pd.read_sql(
        "SELECT CASE WHEN skipped_reason LIKE 'Earnings%' "
        "THEN 'Earnings conflict' ELSE skipped_reason END AS reason, "
        "count(*) AS n FROM backtest_results "
        "WHERE skipped_reason IS NOT NULL GROUP BY reason ORDER BY n DESC",
        conn,
    )


def coverage_year_month(conn: Any) -> pd.DataFrame:
    """Rows per ``(year, month)`` in ``option_metrics`` — the coverage heatmap."""
    return pd.read_sql(
        "SELECT EXTRACT(YEAR FROM date)::int AS year, "
        "EXTRACT(MONTH FROM date)::int AS month, count(*) AS n "
        "FROM option_metrics GROUP BY year, month ORDER BY year, month",
        conn,
    )
