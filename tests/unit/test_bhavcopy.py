"""Unit tests for mktmtm.data.bhavcopy (parse/normalize/URL + real fixtures)."""

from datetime import datetime
from pathlib import Path

import pandas as pd

from mktmtm.data.bhavcopy import (
    UDIFF_CUTOFF_DATE,
    get_bhavcopy_url,
    normalize_bhavcopy_columns,
    parse_bhavcopy_date,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

UDIFF_FIXTURE = FIXTURES / "fo_bhav_20250812.csv"
LEGACY_FIXTURE = FIXTURES / "fo01APR2021bhav.csv"


def test_parse_bhavcopy_date_legacy_nse_scheme() -> None:
    """fo13APR2025bhav.csv (legacy NSE)."""
    assert parse_bhavcopy_date("fo13APR2025bhav.csv") == datetime(2025, 4, 13)


def test_parse_bhavcopy_date_downloader_legacy_scheme() -> None:
    """fo_bhav_12AUG2021.csv (downloader, legacy schema)."""
    assert parse_bhavcopy_date("fo_bhav_12AUG2021.csv") == datetime(2021, 8, 12)


def test_parse_bhavcopy_date_downloader_udiff_scheme() -> None:
    """fo_bhav_20250812.csv (downloader, UDiFF schema)."""
    assert parse_bhavcopy_date("fo_bhav_20250812.csv") == datetime(2025, 8, 12)


def test_parse_bhavcopy_date_unparseable_returns_none() -> None:
    assert parse_bhavcopy_date("not_a_bhavcopy.txt") is None
    assert parse_bhavcopy_date("fo_bhav_.csv") is None
    assert parse_bhavcopy_date("") is None


def test_get_bhavcopy_url_legacy_schema() -> None:
    url, formatted_date, is_udiff = get_bhavcopy_url(datetime(2021, 8, 12))
    assert (
        url
        == "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/2021/AUG/fo12AUG2021bhav.csv.zip"
    )
    assert formatted_date == "12AUG2021"
    assert is_udiff is False


def test_get_bhavcopy_url_udiff_schema() -> None:
    url, formatted_date, is_udiff = get_bhavcopy_url(datetime(2025, 8, 12))
    assert (
        url
        == "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20250812_F_0000.csv.zip"
    )
    assert formatted_date == "20250812"
    assert is_udiff is True


def test_get_bhavcopy_url_cutoff_boundary() -> None:
    # On/after cutoff -> UDiFF; just before -> legacy
    assert get_bhavcopy_url(UDIFF_CUTOFF_DATE)[2] is True
    before = datetime(2024, 7, 7)
    assert get_bhavcopy_url(before)[2] is False


def test_normalize_udiff_fixture_maps_codes_and_expiry() -> None:
    """Real UDiFF fixture: 8 rows, 4 instrument codes, 28-Oct-2025 expiry."""
    raw = pd.read_csv(UDIFF_FIXTURE)
    assert len(raw) == 8  # real-fixture row count

    df = normalize_bhavcopy_columns(raw)

    assert "SYMBOL" in df.columns
    assert set(df["INSTRUMENT"]) == {"FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"}
    assert "28-Oct-2025" in set(df["EXPIRY_DT"])
    assert set(df["SYMBOL"]) == {"BANKNIFTY", "ABCAPITAL"}
    assert "STRIKE_PR" in df.columns
    assert "CONTRACTS" in df.columns
    # open interest columns survive the UDiFF -> legacy mapping
    assert "OPEN_INT" in df.columns
    assert "CHG_IN_OI" in df.columns
    assert (df["OPEN_INT"] > 0).any()


def test_normalize_legacy_fixture_is_noop() -> None:
    """Real legacy fixture already uses the legacy schema -> unchanged."""
    raw = pd.read_csv(LEGACY_FIXTURE)
    assert len(raw) == 9
    df = normalize_bhavcopy_columns(raw)
    assert "SYMBOL" in df.columns
    assert set(df["INSTRUMENT"]) == {"FUTIDX"}
    assert df["EXPIRY_DT"].iloc[0] == "29-Apr-2021"


def test_normalize_udiff_maps_idf_to_futidx() -> None:
    """IDF -> FUTIDX, STF -> FUTSTK, IDO -> OPTIDX, STO -> OPTSTK."""
    df = normalize_bhavcopy_columns(pd.read_csv(UDIFF_FIXTURE))
    by_code: dict[str, set[str]] = {}
    for fin_instrm_tp, instr in zip(
        ["IDF", "IDF", "STF", "STF", "IDO", "IDO", "STO", "STO"],
        df["INSTRUMENT"],
        strict=True,
    ):
        by_code.setdefault(fin_instrm_tp, set()).add(instr)
    assert by_code["IDF"] == {"FUTIDX"}
    assert by_code["STF"] == {"FUTSTK"}
    assert by_code["IDO"] == {"OPTIDX"}
    assert by_code["STO"] == {"OPTSTK"}


def test_build_bhavcopy_cache_matches_direct_reads(tmp_path) -> None:
    """Cache rows (normalized + DATE) equal per-date direct CSV reads."""
    from mktmtm.data.bhavcopy import build_bhavcopy_cache

    out = build_bhavcopy_cache(FIXTURES, tmp_path / "cache.parquet")
    cache = pd.read_parquet(out)
    assert "DATE" in cache.columns
    assert cache["DATE"].notna().all()
    # both fixtures parse
    assert cache["DATE"].nunique() == 2

    # per-symbol day frames must match direct CSV reads
    for sym in ("BANKNIFTY", "ABCAPITAL"):
        for date_obj, from_cache in _days_from_cache(cache, sym):
            direct = _read_day_direct(sym, date_obj)
            assert len(from_cache) == len(direct), (sym, date_obj)
            if not direct.empty:
                assert (from_cache["SYMBOL"] == sym).all()
                # EXPIRY_DT normalized identically
                assert set(from_cache["EXPIRY_DT"]) == set(direct["EXPIRY_DT"])


def _days_from_cache(cache: pd.DataFrame, sym: str):
    sub = cache[cache["SYMBOL"] == sym]
    for date_obj in sub["DATE"].unique():
        yield date_obj, sub[sub["DATE"] == date_obj].reset_index(drop=True)


def _read_day_direct(sym: str, date_obj) -> pd.DataFrame:
    from mktmtm.data.bhavcopy import parse_bhavcopy_date

    candidates = list(FIXTURES.glob("*.csv"))
    for fp in candidates:
        if parse_bhavcopy_date(fp.name) == date_obj:
            raw = pd.read_csv(fp)
            df = normalize_bhavcopy_columns(raw)
            return df[df["SYMBOL"] == sym].reset_index(drop=True)
    return pd.DataFrame()
