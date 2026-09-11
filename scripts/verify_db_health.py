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

print("\n" + "=" * 70)
print("6) DERIVED FEATURE TABLES")
print("=" * 70)
# These five tables did not exist when this gate was written, and it kept
# reporting PASS while validating none of them.

ff = q("SELECT count(*), count(distinct symbol) FROM flow_features")[0]
if ff[0] != mrows:
    bad(f"flow_features rows ({ff[0]:,}) != ml_dataset rows ({mrows:,}) — the 1:1 join is broken")
else:
    ok(f"flow_features 1:1 with ml_dataset ({ff[0]:,} rows, {ff[1]} symbols)")

# Direction scores are bounded ratios by construction; anything outside [-1, 1]
# means a denominator went wrong.
oob = q(
    "SELECT count(*) FROM flow_features WHERE "
    "q_fut_direction NOT BETWEEN -1 AND 1 OR opt_direction NOT BETWEEN -1 AND 1 "
    "OR oi_outlier_direction NOT BETWEEN -1 AND 1"
)[0][0]
if oob:
    bad(f"{oob:,} flow_features rows have a direction score outside [-1, 1]")
else:
    ok("direction scores within [-1, 1]")

# The outlier detector's flag rate was calibrated to 10-17%; drift outside that
# means the thresholds no longer mean what docs/implementation says they mean.
flag = q("SELECT avg((oi_outlier_count > 0)::int) FROM flow_features")[0][0]
if flag is None or not (0.05 <= float(flag) <= 0.30):
    bad(f"outlier flag rate {float(flag or 0):.1%} outside the calibrated 5-30% band")
else:
    ok(f"outlier flag rate {float(flag):.1%} (calibrated band 10-17%)")

for name in ("flow_regimes", "flow_attribution", "shareholding", "deals"):
    n = q(f"SELECT count(*) FROM {name}")[0][0]
    if n == 0:
        print(f"  {name:18s} empty (not built)")
    else:
        print(f"  {name:18s} {n:>9,} rows")

print("\n" + "=" * 70)
print("7) SHAREHOLDING — SCHEMA-BOUNDARY DISCONTINUITY")
print("=" * 70)
# Both silent data defects this session were XBRL taxonomy changes that zeroed
# or mis-mapped a holder column on one side of ~2023 only. Neither was caught
# by a test; both were caught by noticing a discontinuity. This encodes that
# check: a holding that jumps at the boundary is a parser defect until proven
# otherwise.
shp = q("SELECT count(*) FROM shareholding")[0][0]
if not shp:
    print("  shareholding empty — skipping")
else:
    for col in ("fii_shares", "dii_shares", "mf_shares"):
        zeros = q(
            f"SELECT date_part('year', date)::int AS y, "
            f"avg(({col} IS NULL OR {col} = 0)::int) "
            f"FROM shareholding WHERE date >= '2021-01-01' GROUP BY 1 ORDER BY 1"
        )
        by_year = {int(y): float(r) for y, r in zeros}
        early = [v for k, v in by_year.items() if k <= 2022]
        late = [v for k, v in by_year.items() if k >= 2023]
        if not early or not late:
            continue
        gap = (sum(early) / len(early)) - (sum(late) / len(late))
        if gap > 0.20:
            bad(
                f"{col}: null-or-zero rate is {gap:.0%} higher before 2023 than after "
                f"— a taxonomy-boundary defect (see docs/implementation/phase-2-report.md §5)"
            )
        else:
            ok(f"{col}: no null/zero discontinuity at the 2023 taxonomy boundary ({gap:+.1%})")

    # Median holdings should evolve smoothly; a step change is the same defect
    # wearing different clothes.
    med = q(
        "SELECT date_part('year', date)::int AS y, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY fii_pct) "
        "FROM shareholding WHERE date >= '2021-01-01' AND fii_pct IS NOT NULL "
        "GROUP BY 1 ORDER BY 1"
    )
    series = [(int(y), float(v)) for y, v in med if v is not None]
    jumps = [
        (a[0], b[0], b[1] - a[1])
        for a, b in zip(series, series[1:], strict=False)
        if abs(b[1] - a[1]) > 5.0
    ]
    if jumps:
        for y0, y1, d in jumps:
            bad(f"median fii_pct jumps {d:+.1f}pp between {y0} and {y1} — suspect the parser")
    else:
        ok("median fii_pct evolves smoothly year to year")

print("\n" + "=" * 70)
print("8) OPTION CHAIN INTEGRITY (degenerate price surfaces)")
print("=" * 70)

# A whole expiry priced at a single value is not a price surface. Found in the
# Phase 0 backtest: on NMDC 2022-10-25 all 206 chain elements carried an
# identical settle of 130.75 against a spot of 130.71 — the underlying's price
# written into every option's premium, every delta 0.0 because the IV solver
# cannot converge on prices that make no sense.
#
# Neither obvious per-contract filter catches it. Volume does not: affected
# strikes include one with 2,492 lots traded. The no-arbitrage bound (a call
# cannot be worth more than the stock) does not either, because the corrupt
# value sits at 99.97% of spot. The degeneracy itself is the signature.
#
# 39 of the 47 historical cases (83%) fall on an EXPIRY DATE: the expiring
# series settles against the underlying, and that value has been written into
# every contract's premium. Watch this count around expiries. Reported rather
# than failed: the rows are a real record of what was published, and every
# consumer should skip them rather than have them silently deleted.
degenerate = q(
    """
    SELECT om.date::text, om.symbol
    FROM option_metrics om
    WHERE om.date >= (SELECT max(date) - INTERVAL '400 days' FROM option_metrics)
    GROUP BY om.date, om.symbol, om.option_chain
    HAVING (
        SELECT count(DISTINCT e->>'settle')
        FROM jsonb_array_elements(om.option_chain) e
    ) = 1
    ORDER BY om.date DESC
    """
)
if degenerate:
    by_date: dict[str, list[str]] = {}
    for d, sym in degenerate:
        by_date.setdefault(d, []).append(sym)
    print(
        f"  ⚠ {len(degenerate)} degenerate symbol-day(s) in the last 400 days "
        f"across {len(by_date)} date(s)"
    )
    for d in sorted(by_date, reverse=True)[:6]:
        syms = by_date[d]
        print(f"      {d}: {len(syms)} symbol(s) — {', '.join(sorted(syms)[:6])}")
    print(
        "    These carry one settle across an entire chain. Consumers must skip "
        "them (see backtest.strangle.chain_is_degenerate); they are NOT a gate "
        "failure, because the rows faithfully record what was published."
    )
else:
    ok("no degenerate option chains in the last 400 days")

conn.close()
print("\n" + ("=" * 70))
print("RESULT:", "FAIL — fix the ✗ items before training" if fail else "PASS — database is ready for ML training")
sys.exit(1 if fail else 0)
