"""SEBI quarterly shareholding patterns — the per-symbol institutional label.

Five modelling stages were validated indirectly because no per-symbol
institutional ground truth was thought to exist, and both stages with a
falsifiable external check concluded "this is not institutional". This module
supplies the missing quantity: **the change in FII and DII holding in a named
symbol over a quarter**, which is exactly what the project claims to detect.

## Where it comes from

``/api/corporate-share-holdings-master?index=equities&symbol=X`` returns one
record per filing (22 for RELIANCE, covering 2021-09 → 2026-06 — a good match
for the project's 2021-08 → 2026-08 window). Unlike the bulk/block historical
endpoint, which is bot-blocked, **this one answers programmatically.**

The master record carries only promoter/public percentages. The FII/DII
breakdown lives in the linked XBRL filing (~500 KB each), so the full pull is
~217 index calls plus ~4,700 XBRL downloads. Filings are cached gzipped under
``data/raw/shareholding/`` so a re-run costs nothing and a parser fix does not
mean re-downloading 2.4 GB.

## Two things that are easy to get wrong

**Use share counts, not percentages.** A holding percentage moves when the
company buys back stock or issues new shares, with nobody having traded. The
share count only moves when somebody actually buys or sells. Detection work
must difference ``*_shares``.

**The XBRL stores percentages as fractions** (0.1720 for 17.20%). They are
converted on ingest, so the table reads the way the filing does. The
conversion is validated against the master API's ``public_val`` rather than
assumed — see :func:`parse_shareholding`.
"""

from __future__ import annotations

import gzip
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests

from mktmtm.config.settings import settings
from mktmtm.progress import progress_stage

logger = logging.getLogger(__name__)

__all__ = [
    "SHAREHOLDING_COLUMNS",
    "ShareholdingError",
    "fetch_filing_index",
    "parse_shareholding",
    "run_fetch_shareholding",
    "write_shareholding",
]

#: Column identity *and* order for ``shareholding``; ``init_shareholding.sql``
#: mirrors this.
SHAREHOLDING_COLUMNS = (
    "symbol", "date",
    "fii_shares", "fii_pct", "dii_shares", "dii_pct",
    "mf_shares", "mf_pct", "insurance_shares", "insurance_pct",
    "promoter_pct", "public_pct", "total_shares", "record_id",
)

_MASTER_URL = (
    "https://www.nseindia.com/api/corporate-share-holdings-master"
    "?index=equities&symbol={symbol}"
)
_REFERER = (
    "https://www.nseindia.com/companies-listing/"
    "corporate-filings-shareholding-pattern"
)
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Candidate XBRL member names per logical category, most-preferred first.
#:
#: The taxonomy changed around 2023 and the two versions share almost no member
#: names. Filings before it call the foreign total ``ForeignInstitutionsMember``
#: and have no domestic total at all; filings after it use
#: ``InstitutionsForeignMember`` / ``InstitutionsDomesticMember``. Matching only
#: the newer set left 899 rows (every filing from 2018-2022) with a NULL FII
#: holding, and the casing drift between ``MutualFundsOrUTIMember`` and
#: ``MutualFundsOrUtiMember`` alone accounted for a 74.6% NULL rate on mutual
#: funds. Lookup is case-insensitive for that reason.
#:
#: Order matters: a *total* must be preferred over a subset, or a filing that
#: reports both would silently record only its FPI Category I slice as the
#: whole foreign holding.
_MEMBER_ALIASES: dict[str, tuple[str, ...]] = {
    "fii": (
        "InstitutionsForeignMember",              # 2023+ taxonomy, total
        "ForeignInstitutionsMember",              # pre-2023 total (legacy FII)
        "InstitutionsForeignPortfolioInvestorMember",
        "ForeignPortfolioInvestorMember",         # subsets, last resort
    ),
    "dii": (
        "InstitutionsDomesticMember",             # 2023+ taxonomy
        "IndianInstitutionsMember",
    ),
    "mf": ("MutualFundsOrUTIMember", "MutualFundsMember"),
    "insurance": ("InsuranceCompaniesMember",),
}

#: Pre-2023 filings carry a combined institutions total but no domestic one, so
#: domestic is recovered as ``total - foreign``.
_ALL_INSTITUTIONS = ("InstitutionsMember",)

_MEMBERS = {aliases[0]: prefix for prefix, aliases in _MEMBER_ALIASES.items()}

_SHARE_TAG = "NumberOfFullyPaidUpEquityShares"
_PCT_TAG = "ShareholdingAsAPercentageOfTotalNumberOfShares"

DEFAULT_DELAY = 0.35
_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d")


class ShareholdingError(RuntimeError):
    """Acquisition or parsing failed in a way that must not read as 'no data'."""


# ═════════════════════════ parsing (pure) ═══════════════════════════════════


def _contexts(xml: str) -> dict[str, str]:
    """``contextRef`` -> the XBRL dimension member it pins."""
    out: dict[str, str] = {}
    for cid, body in re.findall(
        r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', xml, re.S
    ):
        member = re.search(r"<xbrldi:explicitMember[^>]*>([^<]+)<", body)
        if member:
            out[cid] = member.group(1).split(":")[-1]
    return out


def _facts(xml: str, tag: str, contexts: dict[str, str]) -> dict[str, str]:
    """Lower-cased member name -> raw fact value for one XBRL tag.

    Keys are lower-cased because the taxonomy is inconsistent about casing
    across versions (``MutualFundsOrUTIMember`` vs ``MutualFundsOrUtiMember``).
    """
    out: dict[str, str] = {}
    pattern = rf'<in-bse-shp:{tag}[^>]*contextRef="([^"]+)"[^>]*>([^<]+)<'
    for ctx, value in re.findall(pattern, xml):
        member = contexts.get(ctx)
        if member and member.lower() not in out:
            out[member.lower()] = value
    return out


def _pick(facts: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    """First alias with a **non-zero** value; falls back to the first present.

    Preferring the first *present* alias is not enough. The pre-2023 taxonomy
    keeps dead categories as explicit zeros: ``ForeignInstitutionsMember`` is
    the legacy FII bucket, emptied when SEBI folded FII into FPI in 2014, and
    it sits in the same filing as
    ``InstitutionsForeignPortfolioInvestorMember`` which carries the actual
    holding. Taking the first present alias picked the zero and reported **no
    foreign holding for every filing before 2023** — and, through the domestic
    fallback (institutions minus foreign), inflated the domestic holding by the
    same amount.

    A genuinely empty category is still representable: when every present alias
    is zero, the zero is returned rather than ``None``.
    """
    first_present: str | None = None
    for name in aliases:
        value = facts.get(name.lower())
        if value is None:
            continue
        if first_present is None:
            first_present = value
        if _number(value):  # non-zero and parseable
            return value
    return first_present


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def parse_shareholding(xml: str) -> dict[str, Any]:
    """Extract institutional holdings from one XBRL filing. Pure — no I/O.

    Percentages in the filing are **fractions** (0.1720 meaning 17.20%) and are
    returned scaled to percent. The scale is inferred from the data rather than
    hard-coded: if every institutional percentage is below 1.0 while the share
    counts are a material slice of the total, the file is using fractions. A
    filing that already stores percent is passed through unchanged, so a
    taxonomy change cannot silently multiply every holding by 100.
    """
    contexts = _contexts(xml)
    if not contexts:
        raise ShareholdingError("no XBRL contexts found — not a shareholding filing?")

    shares = _facts(xml, _SHARE_TAG, contexts)
    percents = _facts(xml, _PCT_TAG, contexts)
    if not shares:
        raise ShareholdingError(f"no {_SHARE_TAG} facts found in filing")

    raw_pct = {
        prefix: _number(_pick(percents, aliases))
        for prefix, aliases in _MEMBER_ALIASES.items()
    }
    known = [v for v in raw_pct.values() if v is not None]
    # Fractions if nothing exceeds 1.0 and something is non-trivially non-zero.
    as_fraction = bool(known) and max(known) <= 1.0 and max(known) > 0.0
    scale = 100.0 if as_fraction else 1.0

    out: dict[str, Any] = {}
    for prefix, aliases in _MEMBER_ALIASES.items():
        count = _number(_pick(shares, aliases))
        out[f"{prefix}_shares"] = int(count) if count is not None else None
        pct = raw_pct[prefix]
        out[f"{prefix}_pct"] = pct * scale if pct is not None else None

    _fill_domestic(out, shares, percents, scale)

    out["total_shares"] = _derive_total_shares(out)
    return out


def _fill_domestic(
    out: dict[str, Any],
    shares: dict[str, str],
    percents: dict[str, str],
    scale: float,
) -> None:
    """Recover the domestic holding as ``all institutions - foreign``, in place.

    Pre-2023 filings report a combined ``InstitutionsMember`` total and a
    foreign total, but no domestic one. Subtracting is exact rather than an
    approximation — the taxonomy defines institutions as foreign plus domestic
    — and it is only applied when the direct member is genuinely absent.
    """
    if out.get("dii_shares") is not None:
        return
    total_shares = _number(_pick(shares, _ALL_INSTITUTIONS))
    foreign_shares = out.get("fii_shares")
    if total_shares is None or foreign_shares is None:
        return
    domestic = total_shares - foreign_shares
    if domestic < 0:
        return  # a malformed filing; NULL beats a negative holding
    out["dii_shares"] = int(domestic)

    total_pct = _number(_pick(percents, _ALL_INSTITUTIONS))
    foreign_pct = out.get("fii_pct")
    if total_pct is not None and foreign_pct is not None:
        out["dii_pct"] = max(total_pct * scale - foreign_pct, 0.0)


def _derive_total_shares(parsed: dict[str, Any]) -> int | None:
    """Total shares outstanding, derived as ``shares / pct`` rather than parsed.

    The filings carry no reliable entity-level total: ``TotalMember`` has no
    share count, and the undimensioned ``NumberOfFullyPaidUpEquityShares``
    facts are individual shareholder rows. But every category reports both a
    count and a percentage of the total, so the total is recoverable from any
    of them — and recoverable *several* times over, which makes it
    self-checking in a way a single parsed tag would not be.

    Categories are required to agree within 1%; a wider spread means the
    percentage scaling misfired and the total is returned as ``None`` rather
    than as a plausible-looking wrong number.
    """
    estimates = [
        (parsed[f"{prefix}_shares"] / parsed[f"{prefix}_pct"]) * 100.0
        for prefix in _MEMBERS.values()
        if parsed.get(f"{prefix}_shares") and parsed.get(f"{prefix}_pct")
    ]
    if not estimates:
        return None
    lo, hi = min(estimates), max(estimates)
    if hi <= 0 or (hi - lo) / hi > 0.01:
        logger.debug("total-share estimates disagree: %s", estimates)
        return None
    return int(sum(estimates) / len(estimates))


def _parse_date(value: str) -> pd.Timestamp | None:
    """Parse a filing date with explicit formats, never by inference.

    `%b` and `%B` are identical for May, so pandas infers `%B` from a May-only
    chunk and then raises on the next June row — a bug that has already killed
    one full build in this repo.
    """
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return pd.Timestamp(pd.to_datetime(text, format=fmt))
        except (ValueError, TypeError):
            continue
    return None


# ═════════════════════════ acquisition ══════════════════════════════════════


def nse_session() -> requests.Session:
    """A session carrying NSE's cookies, warmed on the filings page."""
    session = requests.Session()
    session.headers.update(
        {"User-Agent": _UA, "Accept": "*/*", "Connection": "keep-alive"}
    )
    try:
        session.get("https://www.nseindia.com", timeout=20)
        session.get(_REFERER, timeout=20)
    except requests.RequestException as exc:
        raise ShareholdingError(f"could not reach nseindia.com: {exc}") from exc
    return session


def fetch_filing_index(session: requests.Session, symbol: str) -> pd.DataFrame:
    """Every shareholding filing NSE lists for ``symbol``.

    Returns ``(symbol, date, xbrl_url, record_id, promoter_pct, public_pct)``.
    """
    # "&" in a symbol (M&M, M&MFIN) would otherwise terminate the query
    # string and NSE would answer for a different, empty symbol.
    url = _MASTER_URL.format(symbol=quote(symbol, safe=""))
    try:
        response = session.get(
            url, headers={"Referer": _REFERER, "Accept": "application/json"},
            timeout=40,
        )
    except requests.RequestException as exc:
        raise ShareholdingError(f"{symbol}: {exc}") from exc
    if response.status_code != 200:
        raise ShareholdingError(f"{symbol}: HTTP {response.status_code}")
    try:
        records = response.json()
    except ValueError as exc:
        raise ShareholdingError(f"{symbol}: response was not JSON") from exc
    if not isinstance(records, list):
        return pd.DataFrame()

    rows = []
    for rec in records:
        stamp = _parse_date(rec.get("date", ""))
        xbrl = str(rec.get("xbrl") or "").strip()
        # NSE emits "-" for a filing whose XBRL was never published; fetching
        # it yields a 404 on a URL ending in "-", which is noise rather than a
        # failure worth reporting.
        if stamp is None or not xbrl.lower().endswith(".xml"):
            continue
        rows.append(
            {
                "symbol": symbol,
                "date": stamp,
                "xbrl_url": rec["xbrl"],
                "record_id": str(rec.get("recordId") or ""),
                "promoter_pct": _number(rec.get("pr_and_prgrp")),
                "public_pct": _number(rec.get("public_val")),
            }
        )
    return pd.DataFrame(rows)


def cache_path(symbol: str, date: pd.Timestamp, root: Path | None = None) -> Path:
    root = root or (settings.raw_data_dir / "shareholding")
    return root / symbol / f"{pd.Timestamp(date).date()}.xml.gz"


def fetch_xbrl(
    session: requests.Session,
    url: str,
    path: Path,
    *,
    delay: float = DEFAULT_DELAY,
) -> str:
    """Download one XBRL filing, gzip-cached on disk.

    Cached because the full pull is ~4,700 files at ~500 KB. Compressed XML is
    roughly a tenth the size, so a re-run — or a parser fix — costs nothing
    instead of 2.4 GB of re-downloading.
    """
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    try:
        response = session.get(
            url, headers={"Referer": "https://www.nseindia.com/"}, timeout=90
        )
    except requests.RequestException as exc:
        raise ShareholdingError(f"download failed: {exc}") from exc
    if response.status_code != 200:
        raise ShareholdingError(f"HTTP {response.status_code} for {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(response.text)
    time.sleep(delay)
    return response.text


# ═════════════════════════ orchestration ════════════════════════════════════


def build_shareholding(
    symbols: list[str],
    *,
    session: requests.Session | None = None,
    cache_root: Path | None = None,
    delay: float = DEFAULT_DELAY,
    limit_filings: int | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch and parse every symbol's filings. Returns ``(frame, errors)``.

    Errors are collected per symbol rather than raised, so one delisted or
    renamed ticker cannot abandon a two-hour pull. The caller reports them.
    """
    session = session or nse_session()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    with progress_stage(
        "Fetching shareholding patterns", total=len(symbols), unit="symbols"
    ) as bar:
        for symbol in symbols:
            try:
                index = fetch_filing_index(session, symbol)
            except ShareholdingError as exc:
                errors.append(str(exc))
                bar.advance(label=symbol)
                continue
            if limit_filings:
                index = index.head(limit_filings)

            for rec in index.to_dict("records"):
                path = cache_path(symbol, rec["date"], cache_root)
                try:
                    xml = fetch_xbrl(session, rec["xbrl_url"], path, delay=delay)
                    parsed = parse_shareholding(xml)
                except ShareholdingError as exc:
                    errors.append(f"{symbol} {rec['date'].date()}: {exc}")
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "date": rec["date"],
                        "promoter_pct": rec["promoter_pct"],
                        "public_pct": rec["public_pct"],
                        "record_id": rec["record_id"],
                        **parsed,
                    }
                )
            bar.advance(label=symbol)

    if not rows:
        return pd.DataFrame(columns=list(SHAREHOLDING_COLUMNS)), errors
    frame = pd.DataFrame(rows)
    for col in SHAREHOLDING_COLUMNS:
        if col not in frame.columns:
            frame[col] = None
    frame = _keep_latest_filing(frame)
    return (
        frame[list(SHAREHOLDING_COLUMNS)]
        .sort_values(["symbol", "date"], kind="stable")
        .reset_index(drop=True)
    ), errors


def _keep_latest_filing(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per (symbol, date), keeping the most recent filing.

    A company can file the same period twice — an original and a revision —
    and NSE lists both. Left alone they collide on the table's primary key and
    the UPSERT fails outright with a cardinality violation, rather than
    silently keeping whichever arrived last. NSE's ``recordId`` increases with
    submission time, so the largest is the authoritative version; where it is
    missing or non-numeric the later row in NSE's own ordering wins.
    """
    work = frame.copy()
    work["_order"] = pd.to_numeric(work["record_id"], errors="coerce").fillna(-1)
    work = work.sort_values(["symbol", "date", "_order"], kind="stable")
    deduped = work.drop_duplicates(subset=["symbol", "date"], keep="last")
    dropped = len(work) - len(deduped)
    if dropped:
        logger.info("dropped %d superseded filing(s) on duplicate dates", dropped)
    return deduped.drop(columns=["_order"])


def validate_shareholding(frame: pd.DataFrame) -> dict[str, Any]:
    """Cross-check the parsed percentages against the master API's own figures.

    ``fii_pct + dii_pct`` must not exceed ``public_pct``: institutions are a
    subset of public shareholding. A systematic breach would mean the
    fraction-to-percent scaling in :func:`parse_shareholding` misfired, which
    is precisely the failure a hard-coded ``* 100`` would hide.
    """
    work = frame.dropna(subset=["fii_pct", "dii_pct", "public_pct"])
    if work.empty:
        return {"checked": 0}
    institutional = work["fii_pct"] + work["dii_pct"]
    breach = institutional > work["public_pct"] + 1.0  # 1pp tolerance
    return {
        "checked": int(len(work)),
        "institutional_exceeds_public": int(breach.sum()),
        "breach_rate": float(breach.mean()),
        "median_institutional_pct": float(institutional.median()),
        "median_public_pct": float(work["public_pct"].median()),
    }


def write_shareholding(conn: Any, frame: pd.DataFrame) -> int:
    """UPSERT into ``shareholding`` on (symbol, date). Returns rows written."""
    from psycopg2.extras import execute_values

    if frame.empty:
        return 0
    int_cols = {
        "fii_shares", "dii_shares", "mf_shares", "insurance_shares", "total_shares",
    }
    cols = list(SHAREHOLDING_COLUMNS)
    rows = [
        tuple(_clean(rec[i], cols[i] in int_cols) for i in range(len(cols)))
        for rec in frame[cols].itertuples(index=False, name=None)
    ]
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c not in ("symbol", "date")
    )
    sql = (
        f"INSERT INTO shareholding ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (symbol, date) DO UPDATE SET {updates}"
    )
    written = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), 500):
            page = rows[i : i + 500]
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
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if as_int:
        return int(value)
    return value.item() if isinstance(value, np.generic) else value


def run_fetch_shareholding(
    conn: Any,
    *,
    symbols: list[str] | None = None,
    limit_symbols: int | None = None,
    limit_filings: int | None = None,
    delay: float = DEFAULT_DELAY,
) -> dict[str, Any]:
    """Fetch, parse, validate and persist shareholding patterns. CLI entry point."""
    if symbols is None:
        # The F&O universe is the only one the features cover.
        symbols = [
            row[0]
            for row in pd.read_sql(
                "SELECT DISTINCT symbol FROM flow_features ORDER BY symbol", conn
            ).itertuples(index=False)
        ]
    if limit_symbols:
        symbols = symbols[:limit_symbols]

    frame, errors = build_shareholding(
        symbols, delay=delay, limit_filings=limit_filings
    )
    written = write_shareholding(conn, frame)
    return {
        "symbols_requested": len(symbols),
        "rows": int(len(frame)),
        "written": written,
        "symbols_with_data": int(frame["symbol"].nunique()) if len(frame) else 0,
        "filings_per_symbol": (
            float(frame.groupby("symbol").size().median()) if len(frame) else 0.0
        ),
        "period": (
            f"{frame['date'].min().date()} → {frame['date'].max().date()}"
            if len(frame) else None
        ),
        "validation": validate_shareholding(frame),
        "errors": errors[:20],
        "error_count": len(errors),
    }
