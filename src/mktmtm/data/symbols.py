"""Load-only F&O catalog helper.

Reads the shipped 222-symbol catalog JSON (`src/mktmtm/data/catalog/`)
via ``importlib.resources``. This module NEVER regenerates the catalog —
the legacy `nse_fno_scripts.py` generator must not run on shipped code.
"""

from __future__ import annotations

import importlib.resources
import json
from typing import Any

__all__ = [
    "load_fno_catalog",
    "load_fno_symbols",
    "load_target_symbols",
    "extract_symbol",
]

_CATALOG_RESOURCE = "mktmtm.data.catalog"
_CATALOG_FILENAME = "nse_fno_scripts.json"


def load_fno_catalog() -> dict[str, Any]:
    """Load the full F&O catalog JSON (index_futures + individual_securities).

    Returns
    -------
    dict with keys ``index_futures`` and ``individual_securities``, each a
    list of ``{"symbol": ..., "name": ...}`` records.
    """
    ref = importlib.resources.files(_CATALOG_RESOURCE).joinpath(_CATALOG_FILENAME)
    with importlib.resources.as_file(ref) as path, path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def extract_symbol(item: str | dict[str, Any]) -> str | None:
    """Extract the symbol from a catalog record (dict) or plain string."""
    if isinstance(item, dict):
        sym = item.get("symbol") or item.get("name") or item.get("ticker") or list(item.values())[0]
        return str(sym).strip() if sym else None
    return str(item).strip()


def load_fno_symbols() -> list[str]:
    """Return the full ordered list of F&O symbols (index futures + securities)."""
    catalog = load_fno_catalog()
    raw_items: list[str | dict[str, Any]] = [
        *catalog.get("index_futures", []),
        *catalog.get("individual_securities", []),
    ]
    symbols = [extract_symbol(item) for item in raw_items]
    return [sym for sym in symbols if sym is not None]


def load_target_symbols() -> set[str] | None:
    """Return the target symbol set for bhavcopy filtering.

    Mirrors the legacy downloader contract: a set of symbols, or None when
    the catalog is unavailable (filtering disabled).
    """
    try:
        symbols = load_fno_symbols()
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return set(symbols) if symbols else None
