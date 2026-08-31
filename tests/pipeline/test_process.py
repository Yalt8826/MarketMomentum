"""Integration-ish tests for the pipeline engine (mktmtm.pipeline.process).

Uses the shipped fixtures: 1 legacy bhavcopy row-sample
(``fo01APR2021bhav.csv``), 1 UDiFF bhavcopy row-sample
(``fo_bhav_20250812.csv``) merged with a real NIFTY row-sample from the
same trading day (``fo_bhav_20250812_nifty_rows.csv``), and the Yahoo
``NIFTY_processed.json`` fixture. All dirs are redirected to a tmp tree
via a ``Settings`` instance, so nothing touches the repo's real data dirs.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mktmtm.config.settings import Settings
from mktmtm.pipeline.process import main

TESTS_DIR = Path(__file__).resolve().parents[1]
FIXTURES = TESTS_DIR / "fixtures"
REFERENCE = TESTS_DIR.parent / "data" / "reference"

LEGACY_FIXTURE = FIXTURES / "fo01APR2021bhav.csv"
UDIFF_FIXTURE = FIXTURES / "fo_bhav_20250812.csv"
NIFTY_ROWS_FIXTURE = FIXTURES / "fo_bhav_20250812_nifty_rows.csv"
YAHOO_FIXTURE = FIXTURES / "NIFTY_processed.json"


@pytest.fixture()
def pipeline_settings(tmp_path: Path) -> Settings:
    """Build a Settings tree with fixture-derived inputs and a tmp output dir."""
    raw = tmp_path / "raw"
    extracted = raw / "bhavcopy" / "extracted"
    yahoo_dir = raw / "yahoo_finance"
    reference = tmp_path / "reference"
    processed = tmp_path / "processed"
    extracted.mkdir(parents=True)
    yahoo_dir.mkdir(parents=True)
    reference.mkdir(parents=True)

    # 1 legacy + 1 UDiFF bhavcopy file for the trading day.
    shutil.copy(LEGACY_FIXTURE, extracted / "fo01APR2021bhav.csv")
    udiff = pd.read_csv(UDIFF_FIXTURE)
    nifty_rows = pd.read_csv(NIFTY_ROWS_FIXTURE)
    merged = pd.concat([udiff, nifty_rows], ignore_index=True)
    merged.to_csv(extracted / "fo_bhav_20250812.csv", index=False)

    # Yahoo spot map + reference inputs.
    shutil.copy(YAHOO_FIXTURE, yahoo_dir / "NIFTY_processed.json")
    shutil.copy(REFERENCE / "ADJUSTED_MIFOR.csv", reference / "ADJUSTED_MIFOR.csv")
    shutil.copy(REFERENCE / "earning_dates.json", reference / "earning_dates.json")

    return Settings(
        data_dir=tmp_path / "data",
        raw_data_dir=raw,
        processed_data_dir=processed,
        serving_data_dir=tmp_path / "serving",
        reference_data_dir=reference,
    )


def _assert_no_nonfinite(obj: object, path: str = "") -> None:
    """Recursively assert there are no NaN/Inf floats anywhere in obj."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _assert_no_nonfinite(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_no_nonfinite(v, f"{path}[{i}]")
    elif isinstance(obj, float):
        assert np.isfinite(obj), f"non-finite float at {path}: {obj!r}"


def test_pipeline_produces_populated_nifty_timestamp(pipeline_settings: Settings) -> None:
    """Engine run over the fixtures yields >=1 populated NIFTY timestamp."""
    output = main(pipeline_settings)

    assert output == pipeline_settings.processed_data_dir / "processed_data.json"
    assert output.exists()

    with output.open("r", encoding="utf-8") as fh:
        result = json.load(fh)

    nifty = result["historical"]["scripts"]["NIFTY"]
    assert nifty["exchange"] == "NSE"
    assert nifty["segment"] == "Options"

    timestamps = nifty["timestamps"]
    assert "12-Aug-2025" in timestamps, "NIFTY must have a 12-Aug-2025 timestamp"

    rec = timestamps["12-Aug-2025"]
    # Spot from the Yahoo fixture; rate falls back to the latest MIFOR date.
    assert rec["underlying_price"] == pytest.approx(24487.400390625)
    assert isinstance(rec["interest_rate"], float) and rec["interest_rate"] > 0

    # Monthly expiries from the 3 NIFTY FUT rows.
    assert rec["expiry_30d"] == "28-Aug-2025"
    assert rec["expiry_60d"] == "30-Sep-2025"
    assert rec["expiry_90d"] == "28-Oct-2025"

    # Nearest common strike across the 3 monthly expiries (spot 24487.4).
    assert rec["strike_price"] == pytest.approx(24500.0)

    # Option chain: 3 expiries x CE/PE at 24500, all with IV > 0.
    chain = rec["option_chain"]
    assert len(chain) == 6
    assert all(row["iv"] > 0 for row in chain)
    assert all(row["delta"] != 0.0 for row in chain)

    # CE/PE blocks fully populated with positive IVs and greeks.
    for side in ("ce", "pe"):
        block = rec[side]
        assert block["iv_30"] > 0
        assert block["iv_60"] > 0
        assert block["iv_90"] > 0
        assert block["volume"] > 0
        assert set(block["greeks"]) == {
            "delta", "gamma", "theta", "vega", "rho", "vanna", "charm",
        }

    # Single day -> YZ can't warm up, and the 252-day IV window needs at least
    # _IV_MIN_PERIODS (60) observations behind a point before it will report a
    # percentile. One day is not a percentile of anything, so both are None
    # rather than the degenerate 100.0 the old 30-day window produced.
    assert rec["rv_yz"] is None
    assert rec["ce"]["ivp"] is None
    assert rec["ce"]["ivr"] is None

    # Every symbol from the catalog is present; no NaN/Inf anywhere.
    assert "BANKNIFTY" in result["historical"]["scripts"]
    assert "ABCAPITAL" in result["historical"]["scripts"]
    _assert_no_nonfinite(result)


def test_pipeline_legacy_fixture_parses_without_error(pipeline_settings: Settings) -> None:
    """The legacy-schema fixture is consumed by the engine without failing.

    BANKNIFTY/FINNIFTY (legacy FUTIDX rows) have no Yahoo spot in the
    fixture set, so they produce OHLC history but no timestamps; the run
    must still complete and write valid JSON.
    """
    output = main(pipeline_settings)

    with output.open("r", encoding="utf-8") as fh:
        result = json.load(fh)

    # Legacy file's symbols get no timestamps (no spot) but the run survives.
    assert result["historical"]["scripts"]["BANKNIFTY"]["timestamps"] == {}
    assert result["historical"]["scripts"]["FINNIFTY"]["timestamps"] == {}


def test_pipeline_limit_days_takes_most_recent_files(pipeline_settings: Settings) -> None:
    """``limit_days=1`` bounds the run to the most recent bhavcopy file.

    Fixtures contain two files: ``fo01APR2021bhav.csv`` (2021-04-01, legacy
    schema, no spot) and ``fo_bhav_20250812.csv`` (2025-08-12, UDiFF, NIFTY
    spot present). A 1-day bounded run must pick the *newest* file, so the
    NIFTY timestamp for 12-Aug-2025 must appear in the output. Regression
    test for the smoke-run bug where ``--limit-days`` sliced the oldest
    files instead of the most recent.
    """
    output = main(pipeline_settings, limit_days=1)

    with output.open("r", encoding="utf-8") as fh:
        result = json.load(fh)

    timestamps = result["historical"]["scripts"]["NIFTY"]["timestamps"]
    assert "12-Aug-2025" in timestamps, "limit_days=1 must process the most recent file"
