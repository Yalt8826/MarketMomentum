"""Unit tests for mktmtm.storage.db (DBWriter) — no live database.

Covers: DSN building from Settings, recursive NaN/Inf scrubbing for JSON
columns, parameter building (dates, JSON wrapping), and buffer flush
behaviour at the configured batch size. Nothing here touches PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from psycopg2.extras import Json

from mktmtm.config.settings import Settings
from mktmtm.storage.db import DBWriter, build_dsn, clean_for_json


def _params(sym: str = "NIFTY", date_key: str = "12-Aug-2025", d: dict | None = None):
    return DBWriter._build_params(sym, date_key, d or {})


# ─────────────────────────── build_dsn ──────────────────────────────────────
def test_build_dsn_from_settings_fields() -> None:
    cfg = Settings(
        pg_host="db.internal",
        pg_port=5433,
        pg_db="options_db",
        pg_user="alice",
        pg_password="s3cret",
        pg_sslmode="disable",
    )
    assert build_dsn(cfg) == (
        "postgresql://alice:s3cret@db.internal:5433/options_db?sslmode=disable"
    )


def test_build_dsn_pg_dsn_override() -> None:
    cfg = Settings(pg_dsn="postgresql://override:pass@host:5432/db")
    assert build_dsn(cfg) == "postgresql://override:pass@host:5432/db"


def test_build_dsn_defaults() -> None:
    """Explicit defaults (env-independent) produce the documented DSN."""
    cfg = Settings(
        pg_dsn=None,
        pg_host="localhost",
        pg_port=5432,
        pg_db="mktmtm",
        pg_user="postgres",
        pg_password="",
        pg_sslmode="disable",
    )
    assert build_dsn(cfg) == "postgresql://postgres:@localhost:5432/mktmtm?sslmode=disable"


# ─────────────────────────── NaN / Inf scrub ────────────────────────────────
def test_clean_for_json_scalar_nan_inf() -> None:
    assert clean_for_json(float("nan")) is None
    assert clean_for_json(float("inf")) is None
    assert clean_for_json(float("-inf")) is None


def test_clean_for_json_keeps_finite_and_non_floats() -> None:
    assert clean_for_json(1.5) == 1.5
    assert clean_for_json(0) == 0
    assert clean_for_json("nan") == "nan"  # strings untouched
    assert clean_for_json(None) is None
    assert clean_for_json(True) is True


def test_clean_for_json_recursive() -> None:
    payload = {
        "iv_30": float("nan"),
        "nested": {"a": [1.0, float("inf"), 2.0]},
        "ok": 3.25,
    }
    cleaned = clean_for_json(payload)
    assert cleaned["iv_30"] is None
    assert cleaned["nested"]["a"] == [1.0, None, 2.0]
    assert cleaned["ok"] == 3.25


# ─────────────────────────── parameter building ─────────────────────────────
def test_build_params_shape_and_scalars() -> None:
    d = {
        "underlying_price": 24500.5,
        "underlying_price_adj": 24500.5,
        "interest_rate": 6.25,
        "strike_price": 24500.0,
        "rv_yz": 0.121,
        "gamma_flip": 23800.0,
    }
    p = _params(d=d)
    assert len(p) == 16
    assert p[0] == "NIFTY"
    assert p[1] == date(2025, 8, 12)
    assert p[2] == 24500.5
    assert p[3] == 24500.5  # underlying_price_adj
    assert p[4] == 6.25
    assert p[5] == 24500.0
    assert p[10] == 0.121
    assert p[11] == 23800.0  # gamma_flip (Phase 3)


def test_build_params_parses_date_fields() -> None:
    d = {
        "expiry_30d": "28-Oct-2025",
        "expiry_60d": "27-Nov-2025",
        "expiry_90d": "26-Dec-2025",
        "upcoming_earning_date": "14-Nov-2025",
    }
    p = _params(d=d)
    assert p[6] == date(2025, 10, 28)
    assert p[7] == date(2025, 11, 27)
    assert p[8] == date(2025, 12, 26)
    assert p[9] == date(2025, 11, 14)


def test_build_params_missing_or_bad_dates_become_none() -> None:
    d = {"expiry_30d": None, "expiry_60d": "not-a-date", "expiry_90d": "31-Feb-2025"}
    p = _params(d=d)
    assert p[6] is None
    assert p[7] is None
    assert p[8] is None


def test_build_params_wraps_json_columns() -> None:
    d = {"ce": {"iv_30": 12.5}, "pe": {}, "option_chain": [{"strike": 24500}], "extras": None}
    p = _params(d=d)
    assert isinstance(p[12], Json)
    assert isinstance(p[13], Json)
    assert isinstance(p[14], Json)
    assert isinstance(p[15], Json)
    assert json.loads(p[12].dumps(p[12].adapted)) == {"iv_30": 12.5}
    # empty dicts / missing fields default to {} or []
    assert json.loads(p[13].dumps(p[13].adapted)) == {}
    assert json.loads(p[15].dumps(p[15].adapted)) == {}


def test_build_params_nan_scrub_inside_json() -> None:
    """NaN inside a JSON column must become null before reaching Postgres."""
    d = {
        "ce": {"iv_30": float("nan"), "greeks": {"delta": float("inf")}, "ok": 1.5},
        "option_chain": [{"iv": float("nan")}],
    }
    p = _params(d=d)
    ce = json.loads(p[12].dumps(p[12].adapted))
    assert ce["iv_30"] is None
    assert ce["greeks"]["delta"] is None
    assert ce["ok"] == 1.5
    chain = json.loads(p[14].dumps(p[14].adapted))
    assert chain[0]["iv"] is None


def test_build_params_invalid_date_key_raises() -> None:
    with pytest.raises(ValueError):
        _params(date_key="12/08/2025")


# ─────────────────────────── buffering / flushing ───────────────────────────
def test_write_row_buffers_and_flushes_at_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings(batch_size=2)
    writer = DBWriter(cfg)
    flushed: list[int] = []

    def fake_flush() -> None:
        flushed.append(len(writer._buf))
        writer._buf.clear()

    monkeypatch.setattr(writer, "_flush", fake_flush)

    writer.write_row("NIFTY", "12-Aug-2025", {"ce": {}})
    assert len(writer._buf) == 1
    assert flushed == []  # not yet at batch size

    writer.write_row("NIFTY", "13-Aug-2025", {"ce": {}})
    assert flushed == [2]  # flushed once the buffer hit batch_size
    assert writer._buf == []


def test_close_flushes_remaining_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings(batch_size=100)
    writer = DBWriter(cfg)
    flushed: list[int] = []

    def fake_flush() -> None:
        flushed.append(len(writer._buf))
        writer._buf.clear()

    monkeypatch.setattr(writer, "_flush", fake_flush)

    writer.write_row("NIFTY", "12-Aug-2025", {"ce": {}})
    writer.write_row("NIFTY", "13-Aug-2025", {"ce": {}})
    assert flushed == []
    writer.close()
    assert flushed == [2]
    assert writer._buf == []


def test_close_idempotent_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = DBWriter(Settings(batch_size=100))
    flushed: list[int] = []
    monkeypatch.setattr(writer, "_flush", lambda: flushed.append(1))
    writer.close()
    writer.close()
    assert flushed == []


def test_context_manager_flushes_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings(batch_size=100)
    flushed: list[int] = []

    def fake_flush() -> None:
        flushed.append(len(writer._buf))
        writer._buf.clear()

    with DBWriter(cfg) as writer:
        monkeypatch.setattr(writer, "_flush", fake_flush)
        writer.write_row("NIFTY", "12-Aug-2025", {"ce": {}})
    assert flushed == [1]
