"""Earnings-date loading + upcoming-earnings lookup.

Ported from the earnings handling in the athena-verified `process_data.py`
(2026-08-16), with type hints added. The reference earnings JSON ships
under ``Settings.reference_data_dir``.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from mktmtm.config.settings import settings

__all__ = [
    "load_earnings_dates",
    "get_upcoming_earning_date",
    "default_earnings_json_path",
]


def default_earnings_json_path() -> Path:
    """Location of the tracked earning_dates.json reference file."""
    return settings.reference_data_dir / "earning_dates.json"


def load_earnings_dates(json_path: Path | None = None) -> dict[str, list[date]]:
    """Load earnings dates from the reference JSON into {symbol: [dates]}.

    Only ``stock_results`` events with a parseable 'YYYY-MM-DD' date and a
    trading symbol are kept (mirrors the legacy pipeline). Dates per symbol
    are sorted ascending.
    """
    path = json_path or default_earnings_json_path()
    with path.open("r", encoding="utf-8") as fh:
        earnings_data: list[dict[str, Any]] = json.load(fh)

    earnings_map: dict[str, list[date]] = {}
    for item in earnings_data:
        if item.get("event_type") != "stock_results":
            continue
        symbol = item.get("trading_symbol")
        raw_date = item.get("date")
        if not isinstance(symbol, str) or not isinstance(raw_date, str):
            continue
        try:
            dt = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        earnings_map.setdefault(symbol, []).append(dt)

    for dates in earnings_map.values():
        dates.sort()
    return earnings_map


def get_upcoming_earning_date(
    symbol: str,
    date_key: str,
    earnings_map: dict[str, list[date]],
) -> str | None:
    """Return the first earnings date strictly after ``date_key`` ('%d-%b-%Y').

    Returns the date formatted as '%d-%b-%Y', or None when the symbol has
    no later earnings event (or no data at all).
    """
    dates = earnings_map.get(symbol)
    if not dates:
        return None
    try:
        current = datetime.strptime(date_key, "%d-%b-%Y").date()
    except ValueError:
        return None
    for dt in dates:
        if dt > current:
            return dt.strftime("%d-%b-%Y")
    return None
