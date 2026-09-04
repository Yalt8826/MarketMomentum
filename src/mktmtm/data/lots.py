"""F&O lot-size loading (data/reference/lot_sizes.csv) + date lookup.

The reference CSV ships under ``Settings.reference_data_dir`` and holds a
sparse per-symbol change-point schedule:

    symbol,effective_from,lot_size,source

``effective_from`` is the first trade date the lot applies to new contracts;
``source`` is ``nse_circular`` (index symbols, from NSE lot-revision
circulars), ``derived`` (stock symbols, reconstructed from bhavcopy
value/volume and validated against NSE data), or ``nse_bhavcopy``.

Bhavcopy reports open interest in **shares, not contracts** — the lot size
converts the other way, dividing OPEN_INT to recover a contract count. This
file previously asserted the reverse. Measured 2026-09-04: OPEN_INT divides
exactly by the lot on 98% of 2026 FUTSTK rows, and summing OPEN_INT/lot over
all stock futures reproduces participant_oi's TOTAL contract count to within
0.02%, where the raw sum is off by a factor of ~2,158.

NSE revises lots periodically (quarterly review circulars plus corporate-action
adjustments), so any conversion needs the schedule below rather than a constant.
See docs/data/lot-size-reference.md for the build procedure and validation
coverage, and docs/implementation/flow-features.md for the measurement.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from mktmtm.config.settings import settings

logger = logging.getLogger(__name__)

__all__ = [
    "LotSizeSchedule",
    "load_lot_sizes",
    "default_lot_sizes_csv_path",
]


def default_lot_sizes_csv_path() -> Path:
    """Location of the tracked lot_sizes.csv reference file."""
    return settings.reference_data_dir / "lot_sizes.csv"


class LotSizeSchedule:
    """Per-symbol lot-size schedule with date lookup.

    Each symbol holds sorted ``(effective_from, lot_size)`` change points.
    Lookup returns the lot in effect on the requested date, or ``None`` when
    the symbol has no entry on or before that date.
    """

    def __init__(self, schedule: dict[str, list[tuple[date, int]]]) -> None:
        self._schedule = schedule

    @property
    def symbols(self) -> set[str]:
        return set(self._schedule)

    def get(self, symbol: str, day: date) -> int | None:
        """Lot size for ``symbol`` on ``day`` (last change point <= day)."""
        points = self._schedule.get(symbol.strip().upper())
        if not points:
            return None
        # points sorted ascending by effective_from; binary search for the day
        lo, hi = 0, len(points)
        while lo < hi:
            mid = (lo + hi) // 2
            if points[mid][0] <= day:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None
        return points[lo - 1][1]

    def effective_changes(self, symbol: str) -> list[tuple[date, int]]:
        """All ``(effective_from, lot_size)`` change points for ``symbol``."""
        return list(self._schedule.get(symbol, []))


def load_lot_sizes(csv_path: Path | None = None) -> LotSizeSchedule:
    """Load the lot-size reference CSV into a :class:`LotSizeSchedule`.

    Rows are parsed as ``symbol,effective_from (YYYY-MM-DD),lot_size(int)``;
    a trailing ``source`` column is tolerated and ignored. Malformed rows are
    skipped with a warning.
    """
    path = csv_path or default_lot_sizes_csv_path()
    if not path.exists():
        logger.warning("lot_sizes.csv not found at %s — returning empty schedule", path)
        return LotSizeSchedule({})

    df = pd.read_csv(path, dtype={"symbol": str})
    schedule: dict[str, list[tuple[date, int]]] = {}
    for row in df.itertuples(index=False):
        try:
            sym = str(row.symbol).strip().upper()
            eff = pd.to_datetime(str(row.effective_from), errors="coerce")
            lot = int(str(row.lot_size))
        except (AttributeError, ValueError, TypeError):
            logger.warning("skipping malformed lot_sizes.csv row: %r", row)
            continue
        if not sym or sym == "NAN" or pd.isna(eff) or lot <= 0:
            logger.warning("skipping invalid lot_sizes.csv row: %r", row)
            continue
        schedule.setdefault(sym, []).append((eff.date(), lot))

    for points in schedule.values():
        points.sort(key=lambda p: p[0])
    return LotSizeSchedule(schedule)
