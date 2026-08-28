"""Integration: build_dataset against the live PostgreSQL (skips when empty).

Run after fetch/process/etl have populated ``option_metrics``:
    uv run pytest tests/integration/test_ml_dataset.py
"""

from __future__ import annotations

import psycopg2
import pytest

from mktmtm.ml.dataset import build_dataset
from mktmtm.storage.db import build_dsn

pytestmark = pytest.mark.integration


def _has_option_data() -> bool:
    conn = psycopg2.connect(build_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM option_metrics LIMIT 1)")
            row = cur.fetchone()
            return bool(row and row[0])
    finally:
        conn.close()


@pytest.mark.skipif(
    not _has_option_data(),
    reason="option_metrics is empty — run fetch/process/etl first",
)
def test_build_dataset_smoke() -> None:
    """Smallest honest end-to-end check: 5 most recent dates, full feature set."""
    conn = psycopg2.connect(build_dsn())
    try:
        df = build_dataset(conn, limit_dates=5)
        assert not df.empty
        for col in ("next_day_return", "next_day_direction", "skew_25d", "put_call_oi_ratio"):
            assert col in df.columns
        # the most recent date of each symbol carries no target
        assert df["next_day_return"].notna().sum() < len(df)
    finally:
        conn.close()
