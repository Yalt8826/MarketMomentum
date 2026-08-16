"""Live PostgreSQL integration tests (port of store_in_db/db_conn_test.py).

These tests require a reachable PostgreSQL instance. Credentials come from
PG_* environment variables, loaded (in priority order) from:

1. ``MKTMTM_PG_ENV_FILE`` — explicit path to an env file, or
2. the athena reference ``data_preprocessing/nse-options-last-5-years/
   store_in_db/.env`` (which sets ``PG_SSLMODE=disable``), or
3. a local ``.env`` next to the project.

When no credentials resolve at all the tests are skipped (e.g. CI without
a database); on athena they connect for real.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2  # type: ignore[import-untyped]
import pytest
from dotenv import load_dotenv

from mktmtm.config.settings import PROJECT_ROOT, Settings
from mktmtm.storage.db import DBWriter, build_dsn, close_db_pool

pytestmark = pytest.mark.integration

# athena reference: <Major Project>/data_preprocessing/nse-options-last-5-years/store_in_db/.env
_ATHENA_REFERENCE_ENV = (
    PROJECT_ROOT.parents[1]
    / "data_preprocessing"
    / "nse-options-last-5-years"
    / "store_in_db"
    / ".env"
)

_SQL_DIR = PROJECT_ROOT / "src" / "mktmtm" / "storage" / "sql"


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


def _fresh_settings() -> Settings:
    """A Settings instance that picks up the PG_* env vars just loaded."""
    return Settings()


def _connect(cfg: Settings) -> Any:
    return psycopg2.connect(build_dsn(cfg), connect_timeout=10)


def test_connect_to_postgres() -> None:
    """db_conn_test equivalent: a live SELECT 1 against the configured PG."""
    env_file = _load_pg_env()
    if not _pg_configured():
        pytest.skip(
            "no PG credentials available "
            f"(looked for env files: {env_file or 'none found'})"
        )
    cfg = _fresh_settings()
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
    finally:
        conn.close()


def test_init_option_metrics_ddl_applies() -> None:
    """The shipped option_metrics DDL (CREATE IF NOT EXISTS + indexes) is valid."""
    env_file = _load_pg_env()
    if not _pg_configured():
        pytest.skip(
            "no PG credentials available "
            f"(looked for env files: {env_file or 'none found'})"
        )
    cfg = _fresh_settings()
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            ddl = (_SQL_DIR / "init_option_metrics.sql").read_text(encoding="utf-8")
            cur.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def test_db_writer_roundtrip() -> None:
    """DBWriter end-to-end: buffered batch upsert lands readable JSON rows."""
    env_file = _load_pg_env()
    if not _pg_configured():
        pytest.skip(
            "no PG credentials available "
            f"(looked for env files: {env_file or 'none found'})"
        )
    cfg = _fresh_settings()
    symbol = "ZZTEST"
    try:
        # make sure the table exists (idempotent DDL)
        conn = _connect(cfg)
        try:
            with conn.cursor() as cur:
                cur.execute((_SQL_DIR / "init_option_metrics.sql").read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()

        payload = {
            "underlying_price": 24000.0,
            "interest_rate": 6.5,
            "strike_price": 24000.0,
            "expiry_30d": "30-Jan-2026",
            "expiry_60d": "27-Feb-2026",
            "expiry_90d": "26-Mar-2026",
            "upcoming_earning_date": None,
            "rv_yz": 0.1212,
            "ce": {"iv_30": 12.3, "greeks": {"delta": 0.5}},
            "pe": {"iv_30": 12.7},
            "option_chain": [
                {"expiry": "30-Jan-2026", "strike": 24000.0, "type": "CE", "iv": 12.3}
            ],
            "extras": {"source_file": "integration_test"},
        }
        with DBWriter(cfg) as writer:
            writer.write_row(symbol, "02-Jan-2026", payload)

        conn = _connect(cfg)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT underlying_price, rv_yz, ce->>'iv_30', pe->>'iv_30', "
                    "extras->>'source_file' FROM option_metrics WHERE symbol = %s",
                    (symbol,),
                )
                row = cur.fetchone()
                assert row is not None
                assert row[0] == pytest.approx(24000.0)
                assert row[1] == pytest.approx(0.1212)
                assert row[2] == "12.3"
                assert row[3] == "12.7"
                assert row[4] == "integration_test"
                cur.execute("DELETE FROM option_metrics WHERE symbol = %s", (symbol,))
            conn.commit()
        finally:
            conn.close()
    finally:
        close_db_pool()
