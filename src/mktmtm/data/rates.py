"""Interest-rate loading (ADJUSTED_MIFOR.csv) + nearest-date fallback.

Ported from the athena-verified `process_data.py` (2026-08-16), with type
hints added. The reference CSV ships under ``Settings.reference_data_dir``.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pandas as pd

from mktmtm.config.settings import settings

logger = logging.getLogger(__name__)

__all__ = [
    "load_interest_rates",
    "get_rate_with_fallback",
    "rate_is_stale",
    "default_rate_csv_path",
]

# Beyond this many days from the nearest observation, a fallback rate stops
# being an interpolation and becomes a fabricated constant.
_MAX_RATE_STALENESS_DAYS = 7

# Warn once per (direction, boundary date) rather than once per bhavcopy file —
# a multi-year backfill would otherwise emit hundreds of thousands of lines.
_stale_warned: set[tuple[str, date]] = set()


def _warn_if_stale(wanted: date, used: date, max_staleness_days: int, how: str) -> None:
    gap = abs((used - wanted).days)
    if gap <= max_staleness_days:
        return
    key = (how, used)
    if key in _stale_warned:
        return
    _stale_warned.add(key)
    logger.warning(
        "Interest rate for %s %s %s (%d days away) — ADJUSTED_MIFOR.csv does not "
        "cover this period. The rate is a frozen constant here; treat "
        "interest_rate as unusable as a model feature for these dates and "
        "refresh the FBIL reference file.",
        wanted, how, used, gap,
    )


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


def get_rate_with_fallback(
    date_obj: date,
    daily_interest_map: Mapping[date, float],
    max_staleness_days: int = _MAX_RATE_STALENESS_DAYS,
) -> float:
    """Get the interest rate for the given date.

    If not available, fallback to the nearest earlier date. If no earlier
    date exists, fallback to the nearest later date. Empty map -> 0.0.

    Carry-forward beyond ``max_staleness_days`` is *logged*, because the silent
    version of this behaviour is how ``interest_rate`` came to be a single frozen
    constant (7.7556) for every row after 2025-04-03 — the point where
    ADJUSTED_MIFOR.csv ends. A constant feature is not merely useless: over a
    long history it is populated in the training era and frozen in the live one,
    so a model learns to lean on it and then meets a wall. The same applies at
    the near end (the CSV starts 2021-06-15; earlier dates get back-filled).
    See docs/data/ml-dataset-report.md §4.

    The value is still returned — option pricing needs *some* rate, and the last
    known one beats zero. The caller decides whether to keep it as a *feature*.
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
        _warn_if_stale(date_obj, left_date, max_staleness_days, "carried forward from")
        return daily_interest_map[left_date]

    # Else try right
    if pos < len(all_dates):
        right_date = all_dates[pos]
        _warn_if_stale(date_obj, right_date, max_staleness_days, "back-filled from")
        return daily_interest_map[right_date]

    # As absolute fallback (should never reach here)
    return daily_interest_map[all_dates[0]]


def rate_is_stale(
    date_obj: date,
    daily_interest_map: Mapping[date, float],
    max_staleness_days: int = _MAX_RATE_STALENESS_DAYS,
) -> bool:
    """True when the rate for ``date_obj`` is an out-of-range fallback.

    Lets callers record NULL for the *feature* while still pricing with the
    carried-forward rate.
    """
    if date_obj in daily_interest_map:
        return False
    all_dates = sorted(daily_interest_map.keys())
    if not all_dates:
        return True
    nearest = min(all_dates, key=lambda d: abs((d - date_obj).days))
    return abs((nearest - date_obj).days) > max_staleness_days
