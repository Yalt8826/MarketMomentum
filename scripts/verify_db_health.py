"""DB health gate before ML training (run on athena via `uv run python scripts/verify_db_health.py`).

Exits 1 on structural flaws (duplicate keys, empty chains, missing spot,
parquet mismatch, missing ml_dataset rows). Hygiene items that belong in the
training loader (garbage IVs, NULL skew, short symbol histories) are reported
but do not fail the gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import psycopg2

from mktmtm.config.settings import settings
from mktmtm.data.symbols import load_fno_symbols
from mktmtm.storage.db import build_dsn

fail = False


def bad(msg: str) -> None:
    global fail
    fail = True
    print(f"  ✗ {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


conn = psycopg2.connect(build_dsn())
cur = conn.cursor()


def q(sql: str, params: list | None = None) -> list:
    cur.execute(sql, params or [])
    return cur.fetchall()


print("=" * 70)
print("1) OPTION_METRICS (ETL completeness)")
print("=" * 70)
rows = q("SELECT count(*) FROM option_metrics")[0][0]
syms = q("SELECT count(DISTINCT symbol) FROM option_metrics")[0][0]
dmin, dmax = q("SELECT min(date), max(date) FROM option_metrics")[0]
print(f"  rows={rows:,}  symbols={syms}  range={dmin} -> {dmax}")
if q("SELECT count(*) FROM (SELECT symbol, date FROM option_metrics GROUP BY 1,2 HAVING count(*)>1) x")[0][0]:
    bad("duplicate (symbol, date) rows")
else:
    ok("no duplicate (symbol, date)")
if q("SELECT count(*) FROM option_metrics WHERE jsonb_array_length(option_chain) = 0")[0][0]:
    bad("rows with empty option_chain")
else:
    ok("no empty option chains")
if q("SELECT count(*) FROM option_metrics WHERE underlying_price IS NULL")[0][0]:
    bad("rows with NULL underlying_price")
else:
    ok("no missing spot prices")

# Catalog coverage. The 2026-08-30 build silently lost 75 of 222 symbols to a
# corrupt cache offset map and still passed every check above, because nothing
# compared the loaded symbols against the catalog they were supposed to come from.
catalog = set(load_fno_symbols())
loaded = {r[0] for r in q("SELECT DISTINCT symbol FROM option_metrics")}
absent = sorted(catalog - loaded)
# A symbol with no Yahoo spot file cannot get underlying_price_adj, so it can
# carry no returns or labels and is legitimately absent — Yahoo serves no working
# ticker for FINNIFTY/MIDCPNIFTY and none at all for LTIM/PEL/TATAMOTORS. Deriving
# the exemption from the files on disk (rather than hardcoding a list) keeps this
# honest if Yahoo coverage changes either way. Anything absent *despite* having
# spot data is the F8 failure mode and must fail the gate.
_yahoo_dir = Path(settings.raw_data_dir) / "yahoo_finance"
no_spot = {s for s in absent if not (_yahoo_dir / f"{s}_processed.json").exists()}
unexplained = [s for s in absent if s not in no_spot]
if unexplained:
    bad(
        f"{len(unexplained)}/{len(catalog)} catalog symbols absent from option_metrics "
        f"despite having spot data: {', '.join(unexplained[:12])}"
        f"{' ...' if len(unexplained) > 12 else ''}"
    )
else:
    ok(
        f"{len(loaded & catalog)}/{len(catalog)} catalog symbols present"
        + (f" ({len(no_spot)} absent, no Yahoo spot source: {', '.join(sorted(no_spot))})"
           if no_spot else "")
    )

print("\n" + "=" * 70)
print("2) ML_DATASET (targets + coverage)")
print("=" * 70)
mrows = q("SELECT count(*) FROM ml_dataset")[0][0]
msyms = q("SELECT count(DISTINCT symbol) FROM ml_dataset")[0][0]
mdmin, mdmax = q("SELECT min(date), max(date) FROM ml_dataset")[0]
labeled = q("SELECT count(*) FROM ml_dataset WHERE next_day_return IS NOT NULL")[0][0]
unlabeled = mrows - labeled
print(f"  rows={mrows:,}  symbols={msyms}  range={mdmin} -> {mdmax}")
print(f"  labeled={labeled:,} ({100.0*labeled/mrows:.1f}%)  unlabeled={unlabeled:,}")
if mrows != rows:
    bad(f"ml_dataset rows ({mrows:,}) != option_metrics rows ({rows:,})")
else:
    ok("every option_metrics row has an ml_dataset row")
up, down = q(
    "SELECT count(*) FILTER (WHERE next_day_direction=1), "
    "count(*) FILTER (WHERE next_day_direction=0) FROM ml_dataset"
)[0]
print(f"  balance: up={up:,} ({100.0*up/(up+down):.1f}%)  down={down:,}")
# unlabeled rows must be exactly the inference day + corporate-action screening
last_day = q("SELECT count(*) FROM ml_dataset WHERE next_day_return IS NULL AND date = (SELECT max(date) FROM ml_dataset)")[0][0]
screened = unlabeled - last_day
print(f"  unlabeled audit: {last_day:,} last-day inference rows + {screened:,} screened (corporate actions / series ends)")

print("\n" + "=" * 70)
print("3) TARGET-NULL AUDIT (is every NULL explained?)")
print("=" * 70)
# symbols whose last row is not the global max date — their series ended early
early_ends = q(
    "SELECT count(*) FROM (SELECT symbol FROM ml_dataset "
    "WHERE next_day_return IS NULL AND date < (SELECT max(date) FROM ml_dataset) "
    "GROUP BY symbol) s"
)[0][0]
print(f"  symbols with mid-series NULL labels: {early_ends} (expected: corporate-action screening / F&O removals)")

print("\n" + "=" * 70)
print("4) HYGIENE (training-loader concerns, not gate failures)")
print("=" * 70)
total = mrows
# NULL counts here, not just out-of-range: once the IV solver returns None
# instead of 0.0/500.0 sentinels, corruption shows up as NULL and an
# "ce_iv_30 <= 0 OR > 200" test sees a clean table that is 18% unusable.
garbage = q("SELECT count(*) FROM ml_dataset WHERE ce_iv_30 IS NULL OR ce_iv_30 <= 0 OR ce_iv_30 > 200")[0][0]
print(f"  unusable ce_iv_30 (NULL, <=0 or >200): {garbage:,} ({100.0*garbage/total:.1f}%) — filter: keep 0 < ce_iv_30 <= 200")
print(f"      {'year':6s}{'unusable':>18s}{'of which NULL':>16s}")
for y, g, nul, t in q(
    "SELECT extract(year from date)::int, "
    "count(*) FILTER (WHERE ce_iv_30 IS NULL OR ce_iv_30 <= 0 OR ce_iv_30 > 200), "
    "count(*) FILTER (WHERE ce_iv_30 IS NULL), count(*) "
    "FROM ml_dataset GROUP BY 1 ORDER BY 1"
):
    print(f"      {y:<6d}{g:>8,}/{t:>7,} ({100.0*g/t:4.1f}%){nul:>10,} ({100.0*nul/t:4.1f}%)")

# Strike-vs-spot agreement is the direct read on whether spot and the strikes
# share a corporate-action basis. A monotone decay backwards through time is the
# signature of an adjusted price series being matched to unadjusted strikes.
print()
dev = q(
    "SELECT round((count(*) FILTER (WHERE abs(strike_price/nullif(underlying_price,0)-1) > 0.10))"
    "::numeric / count(*)::numeric, 4) FROM option_metrics WHERE strike_price IS NOT NULL"
)[0][0]
print(f"  |strike/spot - 1| > 10%: {float(dev):.2%} overall (price-basis mismatch indicator)")
for y, f in q(
    "SELECT extract(year from date)::int, "
    "round((count(*) FILTER (WHERE abs(strike_price/nullif(underlying_price,0)-1) > 0.10))"
    "::numeric / count(*)::numeric, 4) "
    "FROM option_metrics WHERE strike_price IS NOT NULL GROUP BY 1 ORDER BY 1"
):
    print(f"      {y}: {float(f):6.2%}")
if float(dev) > 0.02:
    bad(
        f"strike/spot mismatch on {float(dev):.2%} of rows (>2%) — spot and strikes are "
        f"not on the same corporate-action basis; check storage.etl._unadjusted_spot"
    )
else:
    ok("strike and spot share one corporate-action basis")
outliers = q("SELECT count(*) FROM ml_dataset WHERE abs(next_day_return) > 0.30")[0][0]
print(f"  |next_day_return| > 0.30: {outliers:,} (0 = corporate-action fix working)")
for col in ("skew_25d", "ce_ivr", "ce_ivp", "days_to_earnings"):
    n = q(f"SELECT count(*) FROM ml_dataset WHERE {col} IS NULL")[0][0]
    print(f"  {col:18s} NULL: {n:>7,} ({100.0*n/total:5.1f}%)")
short = q("SELECT count(*) FROM (SELECT symbol FROM option_metrics GROUP BY symbol HAVING count(*) < 500) s")[0][0]
print(f"  symbols with <500 days of history: {short} (newer F&O listings — fine, use chronological splits)")

print("\n" + "=" * 70)
print("5) PARQUET EXPORT")
print("=" * 70)
pq_path = Path(settings.processed_data_dir) / "ml_dataset.parquet"
if not pq_path.exists():
    bad(f"parquet missing: {pq_path}")
else:
    pq = pd.read_parquet(pq_path)
    if len(pq) != mrows:
        bad(f"parquet rows ({len(pq):,}) != ml_dataset rows ({mrows:,})")
    else:
        ok(f"{pq_path.name} rows match ({len(pq):,}, {len(pq.columns)} cols)")

conn.close()
print("\n" + ("=" * 70))
print("RESULT:", "FAIL — fix the ✗ items before training" if fail else "PASS — database is ready for ML training")
sys.exit(1 if fail else 0)
