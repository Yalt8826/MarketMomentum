"""NSE bulk and block deals — the project's first per-symbol institutional label.

Every modelling stage so far was validated indirectly because no per-symbol
institutional ground truth was thought to exist. This module supplies one: a
named client, a named symbol, a named date, and a side.

**Acquisition is the hard part, and it is deliberately split in two.**

``bulk.csv`` / ``block.csv`` on NSE's archive host serve **only the latest
trading day** — measured 2026-09-05, they returned 156 bulk and 2 block deals,
all dated 2026-09-04. The historical endpoint
``/api/historical/bulk-deals?from=..&to=..`` is bot-protected and returns
**HTTP 503** to a programmatic client even with a fully warmed session (cookies
obtained, referring page fetched, browser headers and Sec-Fetch set); dated
archive files under ``bulk_DDMMYYYY.csv`` return 404. The same page downloads
fine from a browser, so the block is bot detection rather than access control.

Hence two paths, both landing in the same table:

- :func:`fetch_latest_deals` — the daily snapshot, for building history forward.
- :func:`load_deal_file` — parses a CSV downloaded by hand from
  https://www.nseindia.com/report-detail/display-bulk-and-block-deals, which is
  how five years of history gets in without waiting five years.

Both accept the same column layout, since the manual download and the daily
archive file share it.
"""

from __future__ import annotations

import io
import logging
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import requests

from mktmtm.config.settings import settings

logger = logging.getLogger(__name__)

__all__ = [
    "DEAL_COLUMNS",
    "DealKind",
    "fetch_latest_deals",
    "load_deal_file",
    "normalize_deals",
    "write_deals",
]

DealKind = Literal["bulk", "block"]

#: Column identity *and* order for the ``deals`` table; ``init_deals.sql``
#: mirrors this, the contract ``FLOW_COLUMNS`` has with its own DDL.
DEAL_COLUMNS = (
    "date", "symbol", "client_name", "side", "kind",
    "quantity", "price", "value", "security_name", "remarks",
)

_URLS: dict[str, str] = {
    "bulk": "https://nsearchives.nseindia.com/content/equities/bulk.csv",
    "block": "https://nsearchives.nseindia.com/content/equities/block.csv",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.nseindia.com/report-detail/display-bulk-and-block-deals",
    "Connection": "keep-alive",
}

# NSE's own header spellings, mapped onto the table's names. The daily archive
# file and the browser download agree on these.
_COLUMN_MAP = {
    "date": "date",
    "symbol": "symbol",
    "security name": "security_name",
    "client name": "client_name",
    "buy/sell": "side",
    "quantity traded": "quantity",
    "trade price / wght. avg. price": "price",
    "remarks": "remarks",
}

_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d")


class DealsError(RuntimeError):
    """Acquisition failed in a way the caller must not mistake for 'no deals'."""


def _parse_dates(values: pd.Series) -> pd.Series:
    """Parse NSE's date column with explicit formats, never by inference.

    ``pd.to_datetime`` infers ``%B`` from a chunk containing only May — the two
    are identical under ``%b`` — and then raises on the next June row. That bug
    killed a full flow-features build once; ``features/flow._parse_expiry``
    exists for the same reason.
    """
    text = values.astype(str).str.strip()
    for fmt in _DATE_FORMATS:
        parsed = pd.to_datetime(text, format=fmt, errors="coerce")
        if parsed.notna().mean() > 0.9:
            return parsed
    raise DealsError(
        f"could not parse deal dates; first values: {text.head(3).tolist()}"
    )


def normalize_deals(frame: pd.DataFrame, kind: DealKind) -> pd.DataFrame:
    """NSE's CSV layout -> :data:`DEAL_COLUMNS`. Pure — no I/O.

    Tolerates the header whitespace and letter-case drift between the daily
    archive file and the browser download, and accepts a file that already
    carries a ``kind`` column (a combined export).
    """
    work = frame.copy()
    work.columns = [str(c).strip().lower() for c in work.columns]
    renamed = {c: _COLUMN_MAP[c] for c in work.columns if c in _COLUMN_MAP}
    work = work.rename(columns=renamed)

    missing = {"date", "symbol", "client_name", "side"} - set(work.columns)
    if missing:
        raise DealsError(
            f"deal file is missing required columns {sorted(missing)}; "
            f"got {sorted(frame.columns)}"
        )

    work["date"] = _parse_dates(work["date"])
    for col in ("symbol", "client_name", "side"):
        work[col] = work[col].astype(str).str.strip()
    work["side"] = work["side"].str.upper().str[:4]
    if "kind" not in work.columns:
        work["kind"] = kind

    work["quantity"] = pd.to_numeric(
        work.get("quantity", pd.Series(dtype="float64"))
        .astype(str).str.replace(",", "", regex=False),
        errors="coerce",
    )
    work["price"] = pd.to_numeric(
        work.get("price", pd.Series(dtype="float64"))
        .astype(str).str.replace(",", "", regex=False),
        errors="coerce",
    )
    # Rupee value is what makes a deal comparable across a Rs 80 microcap and a
    # Rs 3,000 large cap; quantity alone is not.
    work["value"] = work["quantity"] * work["price"]

    for col in ("security_name", "remarks"):
        if col not in work.columns:
            work[col] = None

    work = work.loc[
        work["date"].notna() & work["symbol"].ne("") & work["side"].isin(["BUY", "SELL"])
    ]
    # The same client can appear twice in a day on the same side across
    # brokers; the table's key would reject the duplicate, so sum them here
    # rather than letting the UPSERT silently keep whichever arrived last.
    grouped = (
        work.groupby(["date", "symbol", "client_name", "side", "kind"], as_index=False)
        .agg(
            quantity=("quantity", "sum"),
            value=("value", "sum"),
            security_name=("security_name", "first"),
            remarks=("remarks", "first"),
        )
    )
    grouped["price"] = grouped["value"] / grouped["quantity"].where(
        grouped["quantity"] > 0
    )
    return grouped[list(DEAL_COLUMNS)].sort_values(["date", "symbol"], kind="stable")


def load_deal_file(path: Path | str, kind: DealKind | None = None) -> pd.DataFrame:
    """Parse a manually downloaded NSE deal CSV.

    ``kind`` may be omitted when the filename says ``bulk`` or ``block``, which
    NSE's own downloads do.
    """
    path = Path(path)
    if kind is None:
        stem = path.stem.lower()
        if "block" in stem:
            kind = "block"
        elif "bulk" in stem:
            kind = "bulk"
        else:
            raise DealsError(
                f"cannot infer deal kind from filename {path.name!r} — "
                "pass kind='bulk' or kind='block'"
            )
    return normalize_deals(pd.read_csv(path), kind)


def fetch_latest_deals(kind: DealKind, session: Any = None) -> pd.DataFrame:
    """The latest trading day's deals from NSE's archive host.

    **Latest day only.** This endpoint carries no history, so it builds a record
    going forward rather than backfilling one. See the module docstring for why
    the historical API cannot be used programmatically.
    """
    session = session or requests.Session()
    session.headers.update(_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=20)
        response = session.get(_URLS[kind], timeout=45)
    except requests.RequestException as exc:
        raise DealsError(f"could not reach NSE for {kind} deals: {exc}") from exc
    if response.status_code != 200:
        raise DealsError(
            f"NSE returned HTTP {response.status_code} for {kind} deals"
        )
    return normalize_deals(pd.read_csv(io.StringIO(response.text)), kind)


def write_deals(conn: Any, frame: pd.DataFrame) -> int:
    """UPSERT into ``deals``. Returns rows written."""
    from psycopg2.extras import execute_values

    if frame.empty:
        return 0
    cols = list(DEAL_COLUMNS)
    rows = [
        tuple(_clean(rec[i], cols[i] == "quantity") for i in range(len(cols)))
        for rec in frame[cols].itertuples(index=False, name=None)
    ]
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols
        if c not in ("date", "symbol", "client_name", "side", "kind")
    )
    sql = (
        f"INSERT INTO deals ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (date, symbol, client_name, side, kind) DO UPDATE SET {updates}"
    )
    written = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), 1000):
            page = rows[i : i + 1000]
            execute_values(cur, sql, page, page_size=len(page))
            written += len(page)
    conn.commit()
    return written


def _clean(value: Any, as_int: bool) -> Any:
    """psycopg2 cannot adapt numpy scalars; pandas hands out exactly those."""
    import numpy as np

    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date_cls):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if as_int:
        return int(value)
    return value.item() if isinstance(value, np.generic) else value


def run_fetch_deals(
    conn: Any,
    *,
    files: list[Path] | None = None,
    fetch_latest: bool = True,
) -> dict[str, Any]:
    """Load manual files and/or the daily snapshot into ``deals``."""
    parts: list[pd.DataFrame] = []
    errors: list[str] = []

    for path in files or []:
        try:
            parts.append(load_deal_file(path))
        except (DealsError, OSError, ValueError) as exc:
            errors.append(f"{Path(path).name}: {exc}")

    if fetch_latest:
        session = requests.Session()
        for kind in ("bulk", "block"):
            try:
                parts.append(fetch_latest_deals(kind, session=session))
            except DealsError as exc:
                errors.append(f"latest {kind}: {exc}")

    if not parts:
        return {"rows": 0, "written": 0, "errors": errors}

    frame = pd.concat(parts, ignore_index=True).drop_duplicates(
        subset=["date", "symbol", "client_name", "side", "kind"], keep="last"
    )
    written = write_deals(conn, frame)
    return {
        "rows": int(len(frame)),
        "written": written,
        "symbols": int(frame["symbol"].nunique()),
        "dates": int(frame["date"].nunique()),
        "period": (
            f"{frame['date'].min().date()} → {frame['date'].max().date()}"
            if len(frame) else None
        ),
        "by_kind": frame["kind"].value_counts().to_dict(),
        "errors": errors,
    }


def default_deal_dir() -> Path:
    """Where manually downloaded deal CSVs are expected."""
    return settings.raw_data_dir / "deals"
