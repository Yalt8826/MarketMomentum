"""Refresh ``data/reference/earning_dates.json`` from NSE corporate announcements.

The file shipped with the project only covers 2022-04 .. 2025-05, which leaves
``ml_dataset.days_to_earnings`` NULL for every recent row. This script fetches
NSE's corporate-announcements feed month-by-month (the date-range endpoint,
verified 2026-08-27) and records, per catalog symbol, every **Outcome of Board
Meeting** announcement date as an earnings date.

Approximation note: in India, quarterly results are announced at board
meetings, and NSE files the outcome as an "Outcome of Board Meeting"
announcement — most such meetings are results meetings. Treating every outcome
as an earnings date makes ``days_to_earnings`` a mild under-estimate when a
board meets for a non-results purpose just before results. Acceptable for a
first model; swap in a paid/corporate-database source later for exact dates.

Usage (on athena):
    uv run python scripts/refresh_earnings.py            # 2025-05-01 .. today
    uv run python scripts/refresh_earnings.py --start 2026-01-01 --out data/reference/earning_dates.json

Merge semantics: existing records are kept (deduped by (trading_symbol, date));
new events are appended. The loader (``data/earnings.py``) only consumes
``event_type == "stock_results"`` records with ``trading_symbol`` + ``date``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from mktmtm.config.settings import settings
from mktmtm.data.symbols import load_fno_symbols

DEFAULT_OUT = settings.reference_data_dir / "earning_dates.json"
API = "https://www.nseindia.com/api/corporate-announcements"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
RESULTS_DESC = "Outcome of Board Meeting"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://www.nseindia.com/", timeout=15)  # cookie warmup
    except Exception:
        pass
    time.sleep(0.5)
    return s


def fetch_month(s: requests.Session, start: date) -> list[dict]:
    """One month of announcements (start .. end of month)."""
    end = start.replace(day=28) + timedelta(days=4)
    end = end.replace(day=1) - timedelta(days=1)
    url = (
        f"{API}?index=equities"
        f"&from_date={start.strftime('%d-%m-%Y')}&to_date={end.strftime('%d-%m-%Y')}"
    )
    r = s.get(url, timeout=60)
    r.raise_for_status()
    j = r.json()
    return j if isinstance(j, list) else []


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh earning_dates.json from NSE announcements")
    ap.add_argument("--start", default="2025-05-01", help="Start date YYYY-MM-DD (default 2025-05-01 = end of shipped coverage)")
    ap.add_argument("--end", default=date.today().isoformat(), help="End date YYYY-MM-DD (default today)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output JSON path")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    catalog = set(load_fno_symbols())
    print(f"catalog symbols: {len(catalog)}  window: {start} .. {end}", flush=True)

    s = _session()
    events: dict[tuple[str, str], dict] = {}

    # keep existing records
    if args.out.exists():
        for rec in json.loads(args.out.read_text(encoding="utf-8")):
            if rec.get("event_type") == "stock_results" and rec.get("trading_symbol") and rec.get("date"):
                events[(str(rec["trading_symbol"]), str(rec["date"]))] = rec

    month = start.replace(day=1)
    added = 0
    while month <= end:
        try:
            recs = fetch_month(s, month)
        except Exception as exc:  # noqa: BLE001 - keep going on transient API hiccups
            print(f"  ! month {month:%Y-%m} failed: {exc!r}", flush=True)
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            continue
        hits = [
            r for r in recs
            if r.get("desc") == RESULTS_DESC
            and r.get("symbol") in catalog
            and r.get("sort_date")
        ]
        for r in hits:
            sym = str(r["symbol"])
            dt = str(r["sort_date"])[:10]
            key = (sym, dt)
            if key in events:
                continue
            events[key] = {
                "id": len(events) + 1,
                "date": dt,
                "stock_name": r.get("sm_name"),
                "trading_symbol": sym,
                "event_type": "stock_results",
            }
            added += 1
        print(f"  {month:%Y-%m}: {len(recs)} announcements, {len(hits)} results events "
              f"(cumulative {len(events)})", flush=True)
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        time.sleep(args.delay)

    out = sorted(events.values(), key=lambda r: (r["date"], r["trading_symbol"]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {len(out)} earnings events (+{added} new) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
