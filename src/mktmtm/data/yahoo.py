"""Yahoo Finance daily-close downloader for NSE underlyings.

Ported from the athena-verified `download_yahoo_data.py` (2026-08-16),
with type hints added and output paths wired to ``Settings.raw_data_dir``.

Raw JSON dumps go to   <raw_data_dir>/yahoo_finance/<SYMBOL>_raw.json
Cleaned JSON (only date -> underlying_price) goes to
   <raw_data_dir>/yahoo_finance/<SYMBOL>_processed.json
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from mktmtm.config.settings import settings
from mktmtm.data.symbols import load_fno_symbols
from mktmtm.progress import track

logger = logging.getLogger(__name__)

__all__ = [
    "INDEX_MAP",
    "resolve_yahoo_ticker",
    "extract_symbol_str",
    "download_yahoo_prices",
]

# ---- Map common index futures to Yahoo tickers ---------------------------- #
# Verified live against Yahoo's chart API (2026-08-27): ^NSEI = NIFTY 50,
# ^NSEBANK = NIFTY BANK, ^NSMIDCP = NIFTY NEXT 50. Yahoo no longer serves
# FINNIFTY / MIDCPNIFTY (^NSEFIN / ^NIFMIDCP50 return Not Found), so those
# two catalog symbols have no spot source and stay absent from the dataset.
INDEX_MAP: dict[str, str] = {
    # symbol in your JSON    : Yahoo ticker
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^NSEFIN",  # broken on Yahoo (Not Found) — kept for the record
    "MIDCPNIFTY": "^NIFMIDCP50",  # broken on Yahoo (Not Found) — kept for the record
    "NIFTYNXT50": "^NSMIDCP",
    "SENSEX": "^BSESN",
}

_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
    "Connection": "keep-alive",
}

_DAY_SECONDS = 24 * 60 * 60


def resolve_yahoo_ticker(symbol: str, meta: dict[str, Any] | None = None) -> str:
    """Decide which ticker to hit on Yahoo Finance.

    Priority:
      1. explicit 'yahoo_symbol' in JSON
      2. hard-coded index mapping
      3. fallback to '<symbol>.NS'
    """
    if meta and meta.get("yahoo_symbol"):
        return str(meta["yahoo_symbol"])

    if symbol in INDEX_MAP:
        return INDEX_MAP[symbol]

    return f"{symbol}.NS"


def extract_symbol_str(item: str | dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Safely extract symbol name whether item is a dict or string."""
    if isinstance(item, dict):
        sym = item.get("symbol") or item.get("name") or list(item.values())[0]
        return str(sym).strip(), item
    return str(item).strip(), {}


def _segment_for(symbol: str) -> str:
    return "Index" if symbol in INDEX_MAP else "Equity"


def download_yahoo_prices(
    symbols: list[str] | None = None,
    output_dir: Path | None = None,
    days: int = 2500,
    symbols_meta: dict[str, dict[str, Any]] | None = None,
) -> int:
    """Download ~`days` of daily closes for each symbol from Yahoo Finance.

    Defaults to 2500 days (~7y) so a bare ``fetch-yahoo`` covers the full
    5-year bhavcopy window; the old 365-day default silently truncated the
    dataset to one year.

    Writes ``<SYMBOL>_raw.json`` and ``<SYMBOL>_processed.json`` into
    ``output_dir`` (default ``Settings.raw_data_dir / "yahoo_finance"``).

    Returns the number of symbols successfully processed.
    """
    if symbols is None:
        symbols = load_fno_symbols()
    if output_dir is None:
        output_dir = settings.raw_data_dir / "yahoo_finance"
    output_dir.mkdir(parents=True, exist_ok=True)
    if symbols_meta is None:
        symbols_meta = {}

    end_ts = int(time.time())
    start_ts = end_ts - (days * _DAY_SECONDS)

    session = requests.Session()
    session.headers.update(_HEADERS)

    # Warm up session to obtain Yahoo cookies
    with suppress(Exception):
        session.get("https://finance.yahoo.com", timeout=10)

    processed_count = 0
    for symbol in track(
        symbols, "Downloading Yahoo closes", unit="symbols", label=lambda s: str(s)
    ):
        item_meta = symbols_meta.get(symbol, {})
        ticker = resolve_yahoo_ticker(symbol, item_meta)

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1d&period1={start_ts}&period2={end_ts}"
            f"&includeAdjustedClose=true&region=IN"
        )

        time.sleep(0.5)  # Respect rate limits

        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            stock_data = resp.json()
        except Exception:
            continue

        # ------------------- save raw -------------------------------------- #
        raw_path = output_dir / f"{symbol}_raw.json"
        with raw_path.open("w") as fh:
            json.dump(stock_data, fh, indent=2)

        # ------------------- process -------------------------------------- #
        processed: dict[str, Any] = {
            "historical": {
                "scripts": {
                    symbol: {
                        "exchange": "NSE",
                        "segment": _segment_for(symbol),
                        "timestamps": {},
                    }
                }
            }
        }

        try:
            result = stock_data["chart"]["result"][0]
            ts_list = result.get("timestamp", [])
            indicators = result.get("indicators", {})
            closes = indicators.get("quote", [{}])[0].get("close", [])

            # Prefer the split/dividend-adjusted series. The raw ``quote.close``
            # carries corporate-action discontinuities straight into the returns
            # (and therefore into the next-day label) — see
            # docs/data/ml-dataset-report.md §3. We already request it via
            # includeAdjustedClose=true; it was simply being discarded.
            adj_list = indicators.get("adjclose") or []
            adj_closes = adj_list[0].get("adjclose", []) if adj_list else []
            if not adj_closes:
                logger.warning(
                    "%s: no adjusted close in Yahoo response — falling back to raw "
                    "close; corporate actions will show as spurious returns",
                    symbol,
                )

            for idx, ts in enumerate(ts_list):
                close_px = adj_closes[idx] if idx < len(adj_closes) else None
                if close_px is None:  # adjclose can be sparse; fall back per-point
                    close_px = closes[idx] if idx < len(closes) else None
                if close_px is None:
                    continue
                date_key = datetime.fromtimestamp(ts).strftime("%d-%b-%Y")
                processed["historical"]["scripts"][symbol]["timestamps"][date_key] = {
                    "underlying_price": close_px
                }
        except (KeyError, IndexError, TypeError):
            continue

        # save processed
        proc_path = output_dir / f"{symbol}_processed.json"
        with proc_path.open("w") as fh:
            json.dump(processed, fh, indent=2)

        processed_count += 1

    return processed_count
