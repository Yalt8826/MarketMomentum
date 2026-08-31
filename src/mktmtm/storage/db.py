"""PostgreSQL bulk writer: connection pool, batch upsert, retry, NaN scrub.

Ported from the athena-verified `store_in_db/db_util.py` (2026-08-16).
Unlike the legacy module — which read bare ``PG_*`` env vars at import
time — every connection parameter here comes from pydantic ``Settings``
(``pg_dsn`` / ``pg_host`` / ``pg_port`` / ``pg_db`` / ``pg_user`` /
``pg_password`` / ``pg_sslmode`` / ``batch_size``), so the writer is
configured exactly like the rest of the MktMtm app.

Public surface is API-compatible with the legacy ``DBWriter``
(``write_row(sym, date_key, d)`` + context manager), so the ETL ports
needed zero caller edits.
"""

from __future__ import annotations

import logging
import math
import time
from contextlib import suppress
from datetime import date, datetime
from typing import Any

import psycopg2
from psycopg2 import InterfaceError, OperationalError, pool
from psycopg2.extras import Json, execute_values

from mktmtm.config.settings import Settings, settings

logger = logging.getLogger(__name__)

__all__ = [
    "DBWriter",
    "build_dsn",
    "clean_for_json",
    "close_db_pool",
    "MAX_RETRY",
]

# Tolerate flaky networks (SSL EOF, bad MAC, ...) on batch inserts.
MAX_RETRY = 6

# UPSERT statement – *execute_values* expands the VALUES part.
_INSERT = (
    "INSERT INTO option_metrics ("
    "symbol,date,underlying_price,underlying_price_adj,interest_rate,strike_price,"
    "expiry_30d,expiry_60d,expiry_90d,upcoming_earning_date,"
    "rv_yz,gamma_flip,ce,pe,option_chain,extras) VALUES %s "
    "ON CONFLICT (symbol,date) DO UPDATE SET "
    "underlying_price       = EXCLUDED.underlying_price,"
    "underlying_price_adj   = EXCLUDED.underlying_price_adj,"
    "interest_rate          = EXCLUDED.interest_rate,"
    "strike_price           = EXCLUDED.strike_price,"
    "expiry_30d             = EXCLUDED.expiry_30d,"
    "expiry_60d             = EXCLUDED.expiry_60d,"
    "expiry_90d             = EXCLUDED.expiry_90d,"
    "upcoming_earning_date  = EXCLUDED.upcoming_earning_date,"
    "rv_yz                  = EXCLUDED.rv_yz,"
    "gamma_flip             = EXCLUDED.gamma_flip,"
    "ce                     = EXCLUDED.ce,"
    "pe                     = EXCLUDED.pe,"
    "option_chain           = EXCLUDED.option_chain,"
    "extras                 = EXCLUDED.extras;"
)

_POOL: pool.SimpleConnectionPool | None = None
_POOL_DSN: str | None = None


def build_dsn(cfg: Settings | None = None) -> str:
    """Build a PostgreSQL DSN from ``Settings``, honoring ``pg_dsn`` when set.

    ``cfg`` defaults to the module-level ``settings`` singleton. The
    ``postgresql://`` URI mirrors the legacy ``_dsn()`` format, with the
    SSL mode taken from ``pg_sslmode`` (default ``disable``).
    """
    cfg = cfg or settings
    if cfg.pg_dsn:
        return cfg.pg_dsn
    return (
        f"postgresql://{cfg.pg_user}:{cfg.pg_password}"
        f"@{cfg.pg_host}:{cfg.pg_port}/{cfg.pg_db}?sslmode={cfg.pg_sslmode}"
    )


def clean_for_json(o: Any) -> Any:
    """Replace NaN/Inf recursively so Postgres JSON never rejects the payload."""
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {k: clean_for_json(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean_for_json(x) for x in o]
    return o


def _d(s: str | None) -> date | None:
    try:
        return datetime.strptime(s, "%d-%b-%Y").date() if s else None
    except Exception:
        return None


def _get_pool(cfg: Settings) -> pool.SimpleConnectionPool:
    """Lazily build the module-level pool, keyed by DSN (8 = multi-thread head-room)."""
    global _POOL, _POOL_DSN
    dsn = build_dsn(cfg)
    if _POOL is None or dsn != _POOL_DSN:
        if _POOL is not None:
            _POOL.closeall()
        _POOL = psycopg2.pool.SimpleConnectionPool(
            1,
            8,
            dsn=dsn,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=10,
            keepalives_interval=5,
            keepalives_count=3,
        )
        _POOL_DSN = dsn
    assert _POOL is not None
    return _POOL


def close_db_pool() -> None:
    """Close and drop the module-level connection pool (idempotent)."""
    global _POOL, _POOL_DSN
    if _POOL is not None:
        _POOL.closeall()
        _POOL = None
        _POOL_DSN = None


class DBWriter:
    """Context-managed bulk writer.

    Buffers rows and bulk-UPSERTs with ``psycopg2.extras.execute_values``
    (5-10x faster than row-by-row inserts), retrying each batch up to
    ``MAX_RETRY`` times on transient errors with exponential backoff.

    Usage::

        with DBWriter() as w:
            w.write_row(sym, date_key, payload)   # call multiple times
    """

    def __init__(self, cfg: Settings | None = None):
        self._cfg = cfg or settings
        self._buf: list[tuple[Any, ...]] = []
        self._batch_size = self._cfg.batch_size

    # ––––– context helpers –––––
    def __enter__(self) -> DBWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ––––– public API –––––
    def write_row(self, sym: str, date_key: str, d: dict[str, Any]) -> None:
        """Buffer a row; flush automatically when the batch is full."""
        self._buf.append(self._build_params(sym, date_key, d))
        if len(self._buf) >= self._batch_size:
            self._flush()

    def close(self) -> None:
        """Flush any remaining buffered rows."""
        if self._buf:
            self._flush()

    # ––––– internal –––––
    @staticmethod
    def _build_params(sym: str, date_key: str, d: dict[str, Any]) -> tuple[Any, ...]:
        return (
            sym,
            datetime.strptime(date_key, "%d-%b-%Y").date(),
            d.get("underlying_price"),
            d.get("underlying_price_adj"),
            d.get("interest_rate"),
            d.get("strike_price"),
            _d(d.get("expiry_30d")),
            _d(d.get("expiry_60d")),
            _d(d.get("expiry_90d")),
            _d(d.get("upcoming_earning_date")),
            d.get("rv_yz"),
            d.get("gamma_flip"),
            Json(clean_for_json(d.get("ce") or {})),
            Json(clean_for_json(d.get("pe") or {})),
            Json(clean_for_json(d.get("option_chain") or [])),
            Json(clean_for_json(d.get("extras") or {})),
        )

    def _flush(self) -> None:
        tries, wait = 0, 1
        while True:
            conn = _get_pool(self._cfg).getconn()
            try:
                with conn.cursor() as cur:
                    # disable per-statement timeout for this session
                    cur.execute("SET statement_timeout = 0")
                    execute_values(cur, _INSERT, self._buf, page_size=self._batch_size)
                conn.commit()
            except (OperationalError, InterfaceError) as exc:
                # drop the poisoned connection so the pool won't hand it out again
                with suppress(Exception):
                    _get_pool(self._cfg).putconn(conn, close=True)
                tries += 1
                if tries > MAX_RETRY:
                    raise
                logger.warning(
                    "batch insert failed (%s) - retry %d in %ss", exc, tries, wait
                )
                time.sleep(wait)
                wait = min(wait * 2, 30)  # exponential back-off, cap 30 s
            else:
                _get_pool(self._cfg).putconn(conn)
                self._buf.clear()
                return
