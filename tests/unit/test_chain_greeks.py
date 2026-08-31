"""Unit tests for the four chain Greeks (delta/gamma/vanna/charm) in both engines.

Covers the Phase 2 contract:

* a converged fixture row carries all four Greeks, all finite;
* a non-converged row (``iv is None``) yields the zero-Greek fallback, not a
  crash and not NaN/NULL — the chain's ``delta`` is consumed by the backtester
  and by ``idx_ce_delta``;
* ``storage.etl._build_chain_rows`` and ``pipeline.process._build_chain_rows``
  produce the *same* Greeks for the same fixture input — the anti-divergence
  test that hard rule 1 (enrichment lives in two places) exists to protect.

Nothing here touches PostgreSQL or the network.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mktmtm.data.bhavcopy import normalize_bhavcopy_columns
from mktmtm.pipeline.process import _build_chain_rows as process_build_chain_rows
from mktmtm.storage.etl import _build_chain_rows as etl_build_chain_rows

TESTS_DIR = Path(__file__).resolve().parents[1]
FIXTURES = TESTS_DIR / "fixtures"
NIFTY_ROWS_FIXTURE = FIXTURES / "fo_bhav_20250812_nifty_rows.csv"

GREEK_KEYS = ("delta", "gamma", "vanna", "charm")

DATE_KEY = "12-Aug-2025"
SPOT = 24487.400390625
R_DECIMAL = 0.065


def _nifty_options() -> pd.DataFrame:
    """Normalized NIFTY option rows from the shipped fixture (3 expiries x CE/PE)."""
    raw = pd.read_csv(NIFTY_ROWS_FIXTURE)
    df = normalize_bhavcopy_columns(raw)
    opts = df[df["INSTRUMENT"].isin(["OPTSTK", "OPTIDX"])].copy()
    opts["EXPIRY_DT"] = pd.to_datetime(opts["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce")
    return opts


def _nonconverged_options() -> pd.DataFrame:
    """One CE row whose IV cannot converge (settle below intrinsic, T>0)."""
    return pd.DataFrame(
        [
            {
                "SYMBOL": "NIFTY",
                "INSTRUMENT": "OPTIDX",
                "EXPIRY_DT": pd.Timestamp("2025-08-28"),
                "STRIKE_PR": 24000.0,   # spot 24487.4 -> intrinsic 487.4
                "OPTION_TYP": "CE",
                "SETTLE_PR": 100.0,     # below intrinsic -> bisection returns None
                "OPEN": 100.0,
                "HIGH": 110.0,
                "LOW": 90.0,
                "CLOSE": 100.0,
                "CONTRACTS": 100,
                "OPEN_INT": 1000.0,
                "CHG_IN_OI": 10.0,
            }
        ]
    )


def test_converged_fixture_carries_all_four_greeks_finite() -> None:
    """A converged chain row has delta/gamma/vanna/charm, all finite, iv > 0."""
    rows = etl_build_chain_rows(_nifty_options(), "NIFTY", SPOT, DATE_KEY, R_DECIMAL)

    assert len(rows) == 6  # 3 expiries x CE/PE at 24500, like the pipeline test
    for row in rows:
        assert row["iv"] is not None and row["iv"] > 0
        for key in GREEK_KEYS:
            assert key in row, f"chain row missing {key}"
            assert isinstance(row[key], (int, float))
            assert np.isfinite(row[key]), f"{key}={row[key]} is not finite"

    # The near-ATM row should have a meaningful (non-zero) gamma — this is the
    # value Phase 3 will aggregate into GEX.
    assert any(abs(r["gamma"]) > 1e-9 for r in rows)


def test_nonconverged_iv_yields_zero_greek_fallback() -> None:
    """iv is None -> every Greek is 0.0, never a crash or a NaN."""
    rows = etl_build_chain_rows(_nonconverged_options(), "NIFTY", SPOT, DATE_KEY, R_DECIMAL)

    assert len(rows) == 1
    row = rows[0]
    assert row["iv"] is None
    for key in GREEK_KEYS:
        assert row[key] == 0.0, f"{key} must fall back to 0.0, got {row[key]!r}"


def test_zero_expiry_time_falls_back_cleanly() -> None:
    """T <= 0 (expired row) -> iv None and zero Greeks, matching the guard."""
    df = _nonconverged_options()
    df.loc[0, "EXPIRY_DT"] = pd.Timestamp("2025-08-12")  # same day as DATE_KEY
    df.loc[0, "SETTLE_PR"] = 200.0  # price would be fine; only T blocks it

    rows = etl_build_chain_rows(df, "NIFTY", SPOT, DATE_KEY, R_DECIMAL)
    assert len(rows) == 1
    assert rows[0]["iv"] is None
    assert all(rows[0][k] == 0.0 for k in GREEK_KEYS)


def test_etl_and_process_produce_identical_greeks() -> None:
    """Anti-divergence: both engines must agree on every Greek for the same input."""
    opts = _nifty_options()

    etl_rows = etl_build_chain_rows(opts, "NIFTY", SPOT, DATE_KEY, R_DECIMAL)
    process_rows = process_build_chain_rows(opts, "NIFTY", SPOT, DATE_KEY, R_DECIMAL)

    assert len(etl_rows) == len(process_rows) == 6
    for etl_row, proc_row in zip(etl_rows, process_rows, strict=True):
        for key in GREEK_KEYS:
            assert etl_row[key] == pytest.approx(proc_row[key], abs=1e-12), (
                f"engine divergence on {key}: etl={etl_row[key]} process={proc_row[key]} "
                f"strike={etl_row['strike']} type={etl_row['type']} expiry={etl_row['expiry']}"
            )
        assert etl_row["iv"] == pytest.approx(proc_row["iv"], abs=1e-9)


def test_etl_and_process_agree_on_nonconverged_fallback() -> None:
    """The zero fallback is identical too — no engine invents a value."""
    df = _nonconverged_options()

    etl_rows = etl_build_chain_rows(df, "NIFTY", SPOT, DATE_KEY, R_DECIMAL)
    process_rows = process_build_chain_rows(df, "NIFTY", SPOT, DATE_KEY, R_DECIMAL)

    assert len(etl_rows) == len(process_rows) == 1
    for key in GREEK_KEYS:
        assert etl_rows[0][key] == process_rows[0][key] == 0.0
