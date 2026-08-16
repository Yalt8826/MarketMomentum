"""Interest-rate loading (ADJUSTED_MIFOR.csv) + nearest-date fallback.

Ported from the athena-verified `process_data.py` (2026-08-16), with type
hints added. The reference CSV ships under ``Settings.reference_data_dir``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pandas as pd

from mktmtm.config.settings import settings

__all__ = [
    "load_interest_rates",
    "get_rate_with_fallback",
    "default_rate_csv_path",
]


def default_rate_csv_path() -> Path:
    """Location of the tracked ADJUSTED_MIFOR.csv reference file."""
    return settings.reference_data_dir / "ADJUSTED_MIFOR.csv"


def load_interest_rates(csv_path: Path | None = None) -> dict[date, float]:
    """Load the FBIL adjusted MIFOR CSV into a {date: rate} map.

    The CSV has two preamble lines before the header; the first row per
    date is used (matching the legacy pipeline). Dates parse as
    '%d %b %Y' (e.g. '04 Apr 2025').
    """
    path = csv_path or default_rate_csv_path()
    ir_df = pd.read_csv(path, skiprows=2)
    ir_df["Date_parsed"] = pd.to_datetime(ir_df["Date"], format="%d %b %Y", errors="coerce")

    daily_interest_map: dict[date, float] = {}
    for dt in ir_df["Date_parsed"].dropna().unique():
        # pick the first row for that date
        first_row = ir_df[ir_df["Date_parsed"] == dt].iloc[0]
        daily_interest_map[dt.date()] = float(first_row["FBIL ADJUSTED MIFOR(%)"])
    return daily_interest_map


def get_rate_with_fallback(date_obj: date, daily_interest_map: Mapping[date, float]) -> float:
    """Get the interest rate for the given date.

    If not available, fallback to the nearest earlier date. If no earlier
    date exists, fallback to the nearest later date. Empty map -> 0.0.
    """
    if date_obj in daily_interest_map:
        return daily_interest_map[date_obj]

    all_dates = sorted(daily_interest_map.keys())

    if not all_dates:
        return 0.0  # Safe default fallback

    # Find the nearest earlier date
    pos = bisect_right(all_dates, date_obj)

    # Try earlier
    if pos > 0:
        left_date = all_dates[pos - 1]
        return daily_interest_map[left_date]

    # Else try right
    if pos < len(all_dates):
        right_date = all_dates[pos]
        return daily_interest_map[right_date]

    # As absolute fallback (should never reach here)
    return daily_interest_map[all_dates[0]]
