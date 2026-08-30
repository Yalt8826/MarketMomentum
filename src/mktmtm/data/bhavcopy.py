"""NSE bhavcopy download + format handling (legacy and UDiFF schemas).

Ported from the athena-verified `download_bhavcopy.py` and the bhavcopy
handling in `process_data.py` (2026-08-16), with type hints added and
bare ``base_dir`` joins replaced by ``Settings.raw_data_dir`` paths.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta

from mktmtm.config.settings import settings
from mktmtm.data.symbols import load_target_symbols
from mktmtm.progress import progress_stage, track

logger = logging.getLogger(__name__)

__all__ = [
    "UDIFF_CUTOFF_DATE",
    "UDIFF_COLUMN_MAP",
    "UDIFF_INSTRUMENT_MAP",
    "parse_bhavcopy_date",
    "normalize_bhavcopy_columns",
    "get_bhavcopy_url",
    "filter_and_save_csv",
    "download_and_process_bhavcopy",
    "run_pipeline",
]

# Cutoff date when NSE migrated to UDiFF format
UDIFF_CUTOFF_DATE = datetime(2024, 7, 8)

# UDiFF (NSE post-2024-07-08 format) -> legacy column names
UDIFF_COLUMN_MAP: dict[str, str] = {
    "TckrSymb": "SYMBOL",
    "FinInstrmTp": "INSTRUMENT",
    "XpryDt": "EXPIRY_DT",
    "StrkPric": "STRIKE_PR",
    "OptnTp": "OPTION_TYP",
    "OpnPric": "OPEN",
    "HghPric": "HIGH",
    "LwPric": "LOW",
    "ClsPric": "CLOSE",
    "SttlmPric": "SETTLE_PR",
    "TtlTradgVol": "CONTRACTS",
    "OpnIntrst": "OPEN_INT",
    "ChngInOpnIntrst": "CHG_IN_OI",
    # NSE's own underlying value, on the same unadjusted basis as the strikes.
    # Legacy-era files have no equivalent column; the ETL falls back to the
    # near-month futures close there (see storage.etl._unadjusted_spot).
    "UndrlygPric": "UNDERLYING_PRICE",
}

# UDiFF instrument-type codes -> legacy codes
UDIFF_INSTRUMENT_MAP: dict[str, str] = {
    "IDF": "FUTIDX",
    "STF": "FUTSTK",
    "IDO": "OPTIDX",
    "STO": "OPTSTK",
}

_BHAVCOPY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.nseindia.com/all-reports-derivatives",
    "Connection": "keep-alive",
}


def parse_bhavcopy_date(filename: str) -> datetime | None:
    """Extract the trade date from any bhavcopy filename scheme.

    Handles:  fo13APR2025bhav.csv     (legacy NSE)
              fo_bhav_12AUG2021.csv   (downloader, legacy schema)
              fo_bhav_20250812.csv    (downloader, UDiFF schema)
    Returns None for unparseable names.
    """
    stem = filename[:-4] if filename.lower().endswith(".csv") else filename
    if stem.startswith("fo_bhav_"):
        stem = stem[8:]
    elif stem.startswith("fo"):
        stem = stem[2:]
    if stem.endswith("bhav"):
        stem = stem[:-4]
    for fmt in ("%d%b%Y", "%Y%m%d"):
        try:
            return datetime.strptime(stem, fmt)
        except ValueError:
            continue
    return None


def normalize_bhavcopy_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map UDiFF (new NSE) columns onto the legacy bhavcopy schema.

    No-op when the frame already uses the legacy schema (SYMBOL present).
    Also converts UDiFF expiry dates (ISO) to the '%d-%b-%Y' strings the
    downstream parsers expect, and translates instrument-type codes.
    """
    if "SYMBOL" in df.columns:
        return df
    df = df.rename(columns={k: v for k, v in UDIFF_COLUMN_MAP.items() if k in df.columns})
    if "INSTRUMENT" in df.columns:
        df["INSTRUMENT"] = df["INSTRUMENT"].map(UDIFF_INSTRUMENT_MAP).fillna(df["INSTRUMENT"])
    if "EXPIRY_DT" in df.columns:
        df["EXPIRY_DT"] = pd.to_datetime(df["EXPIRY_DT"], errors="coerce").dt.strftime("%d-%b-%Y")
    return df


# Canonical column set the ETL consumes (normalized schema minus unused extras).
_CACHE_COLUMNS = [
    "INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP",
    "OPEN", "HIGH", "LOW", "CLOSE", "SETTLE_PR", "CONTRACTS",
    "OPEN_INT", "CHG_IN_OI", "UNDERLYING_PRICE",
]


def build_bhavcopy_cache(bhavcopy_dir: Path, out_path: Path) -> Path:
    """One-pass parse of every bhavcopy CSV into a single parquet cache.

    The threaded ETL re-reads and re-parses every CSV once per symbol
    (~222 symbols x ~1,235 files), which dominates its multi-hour runtime.
    This cache is built once in a few minutes and lets the ETL slice rows
    by symbol instead of re-parsing files.

    Rows are normalized to the legacy schema (same as
    ``normalize_bhavcopy_columns``) plus a ``DATE`` column derived from the
    trade date encoded in each filename, restricted to the canonical column
    set above. Row groups are streamed with ``ParquetWriter`` so peak memory
    stays at one file (~tens of MB), never the concatenated dataset.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    writer: pq.ParquetWriter | None = None
    n_files = skipped = 0
    first = True
    csv_files = sorted(Path(bhavcopy_dir).glob("*.csv"))
    for fp in track(
        csv_files, "Building bhavcopy cache", unit="files", label=lambda p: p.name
    ):
        date_obj = parse_bhavcopy_date(fp.name)
        if date_obj is None:
            continue
        df: pd.DataFrame | None = None
        for skip in (0, 2):  # header-variant tolerance, mirrors storage.etl._load_bhavcopy
            try:
                cand = pd.read_csv(fp, skiprows=skip)
                if "SYMBOL" in cand.columns or "TckrSymb" in cand.columns:
                    df = cand
                    break
            except Exception:
                continue
        if df is None:
            skipped += 1
            continue
        normalized = normalize_bhavcopy_columns(df)
        normalized["DATE"] = pd.Timestamp(date_obj)
        for col in _CACHE_COLUMNS:
            if col not in normalized.columns:
                normalized[col] = pd.NA
        # uniform dtypes so every row group shares one parquet schema;
        # categorical (dictionary-encoded) text columns keep the cache small
        for col in _CACHE_COLUMNS:
            if col in ("INSTRUMENT", "SYMBOL", "EXPIRY_DT", "OPTION_TYP"):
                normalized[col] = normalized[col].astype("category")
            else:
                normalized[col] = pd.to_numeric(normalized[col], errors="coerce").astype("float64")
        table = pa.Table.from_pandas(normalized[["DATE", *_CACHE_COLUMNS]], preserve_index=False)
        if first:
            writer = pq.ParquetWriter(out_path, table.schema)
            first = False
        assert writer is not None
        writer.write_table(table)
        n_files += 1

    if writer is None:
        raise ValueError(f"no bhavcopy files parsed under {bhavcopy_dir}")
    writer.close()
    logging.getLogger(__name__).info(
        "bhavcopy cache: %d files (%d skipped) -> %s", n_files, skipped, out_path,
    )
    return out_path


def get_bhavcopy_url(date: datetime) -> tuple[str, str, bool]:
    """Generate appropriate URL based on the date schema (Legacy vs UDiFF).

    Returns (url, formatted_date, is_udiff).
    """
    if date >= UDIFF_CUTOFF_DATE:
        # New UDiFF Format
        formatted_date = date.strftime("%Y%m%d")
        url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{formatted_date}_F_0000.csv.zip"
        is_udiff = True
    else:
        # Legacy Format
        year = date.strftime("%Y")
        month = date.strftime("%b").upper()
        day = date.strftime("%d")
        formatted_date = f"{day}{month}{year}"
        url = f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{year}/{month}/fo{formatted_date}bhav.csv.zip"
        is_udiff = False

    return url, formatted_date, is_udiff


def bhavcopy_dirs() -> tuple[Path, Path]:
    """Locations for extracted and filtered bhavcopy CSVs under raw data."""
    base = settings.raw_data_dir / "bhavcopy"
    return base / "extracted", base / "filtered"


def filter_and_save_csv(
    raw_csv_path: Path,
    filtered_csv_path: Path,
    target_symbols: set[str] | None,
    is_udiff: bool,
) -> bool:
    """Filter raw Bhavcopy CSV based on target symbols."""
    try:
        df = pd.read_csv(raw_csv_path)

        # Standardize symbol column lookup across schema formats
        symbol_col: str | None = None
        for col in ("TckrSymb", "SYMBOL", "INSTRUMENT_NAME", "Symbol"):
            if col in df.columns:
                symbol_col = col
                break

        if symbol_col and target_symbols:
            # Clean symbol text and filter
            df[symbol_col] = df[symbol_col].astype(str).str.strip()
            df = df[df[symbol_col].isin(target_symbols)]

        df.to_csv(filtered_csv_path, index=False)
        return True
    except Exception:
        return False


def download_and_process_bhavcopy(
    date: datetime,
    session: requests.Session,
    target_symbols: set[str] | None,
    extracted_dir: Path,
    filtered_dir: Path,
) -> bool:
    """Fetch, extract, and filter Bhavcopy for a given trading day."""
    url, formatted_date, is_udiff = get_bhavcopy_url(date)

    csv_file_name = f"fo_bhav_{formatted_date}.csv"
    extracted_csv_path = extracted_dir / csv_file_name
    filtered_csv_path = filtered_dir / f"filtered_{csv_file_name}"

    # Skip if already processed
    if (filtered_csv_path if target_symbols else extracted_csv_path).exists():
        return True

    time.sleep(1)  # Respect server rate limits

    try:
        response = session.get(url, headers=_BHAVCOPY_HEADERS, timeout=30)

        if response.status_code == 200:
            # Unzip in-memory
            with zipfile.ZipFile(io.BytesIO(response.content)) as zip_ref:
                for file_info in zip_ref.infolist():
                    if file_info.filename.endswith(".csv"):
                        # Extract raw file
                        file_info.filename = csv_file_name
                        zip_ref.extract(file_info, extracted_dir)
                        break

            # Apply symbol filtering if target list exists
            if target_symbols and extracted_csv_path.exists():
                filter_and_save_csv(extracted_csv_path, filtered_csv_path, target_symbols, is_udiff)

            return True
        elif response.status_code == 404:
            # No data (Holiday / Weekend / Unreleased)
            return False
        else:
            return False

    except Exception:
        return False


def run_pipeline(
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    max_workers: int = 1,
) -> int:
    """Download + filter bhavcopy for a 5-year (default) range of weekdays.

    Returns the number of successfully processed trading days.
    """
    if end_date is None:
        end_date = datetime.now()
    if start_date is None:
        start_date = end_date - relativedelta(years=5)

    target_symbols = load_target_symbols()

    # Generate dates excluding weekends
    dates: list[datetime] = []
    curr = start_date
    while curr <= end_date:
        if curr.weekday() < 5:  # Monday through Friday
            dates.append(curr)
        curr += timedelta(days=1)

    extracted_dir, filtered_dir = bhavcopy_dirs()
    extracted_dir.mkdir(parents=True, exist_ok=True)
    filtered_dir.mkdir(parents=True, exist_ok=True)

    # Session setup to retrieve NSE cookies
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    with suppress(Exception):
        session.get("https://www.nseindia.com", headers=session.headers, timeout=20)

    # as_completed rather than executor.map: map yields in submission order, so a
    # single slow day would stall the bar while later days quietly finished.
    ok = 0
    with (
        progress_stage("Downloading bhavcopy", total=len(dates), unit="days") as bar,
        ThreadPoolExecutor(max_workers=max_workers) as executor,
    ):
        futures = {
            executor.submit(
                download_and_process_bhavcopy,
                d, session, target_symbols, extracted_dir, filtered_dir,
            ): d
            for d in dates
        }
        for future in as_completed(futures):
            day = futures[future]
            try:
                got = future.result()
            except Exception:
                logger.exception("Bhavcopy download failed for %s", day.date())
                got = False
            ok += bool(got)
            bar.advance(label=f"{day.strftime('%d %b %Y')}  ok={ok}")

    logger.info("Bhavcopy: %d/%d weekdays had data (holidays return 404)", ok, len(dates))
    return ok
