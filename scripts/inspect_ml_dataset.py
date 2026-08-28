"""ML dataset inspection report (run on athena via `uv run python .inspect_ml.py`)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

from mktmtm.config.settings import settings
from mktmtm.ml.dataset import _read_sql
from mktmtm.storage.db import build_dsn

pd.set_option("display.width", 140)
pd.set_option("display.max_columns", 60)

conn = psycopg2.connect(build_dsn())

print("=" * 70)
print("1) COVERAGE")
print("=" * 70)
cur = conn.cursor()
cur.execute("SELECT count(*) FROM ml_dataset")
ml_rows = cur.fetchone()[0]
cur.execute("SELECT count(*), count(DISTINCT symbol), min(date), max(date) FROM ml_dataset")
_, n_sym, d_min, d_max = cur.fetchone()
cur.execute("SELECT count(*) FROM option_metrics")
om_rows = cur.fetchone()[0]
cur.execute("SELECT count(DISTINCT symbol) FROM option_metrics")
om_sym = cur.fetchone()[0]
print(f"ml_dataset rows        : {ml_rows:,}   (option_metrics: {om_rows:,})")
print(f"symbols                : {n_sym:,}   (option_metrics: {om_sym:,})")
print(f"date range             : {d_min}  ->  {d_max}")

df = _read_sql(conn, "SELECT * FROM ml_dataset")
conn.close()
df["date"] = pd.to_datetime(df["date"])

print()
print("=" * 70)
print("2) TARGETS")
print("=" * 70)
labeled = df["next_day_return"].notna()
print(f"rows with target       : {labeled.sum():,} / {len(df):,}  ({labeled.mean():.1%})")
lab = df[labeled]
print(f"direction balance      : up={int((lab['next_day_direction']==1).sum()):,}  "
      f"down={int((lab['next_day_direction']==0).sum()):,}  "
      f"(up {lab['next_day_direction'].mean():.2%} of labeled)")
r = lab["next_day_return"]
print(f"next_day_return        : mean {r.mean():+.4%}  median {r.median():+.4%}  "
      f"std {r.std():.4%}  min {r.min():+.2%}  max {r.max():+.2%}")
print(f"  |return| mean        : {r.abs().mean():.4%}   P90 {r.abs().quantile(.9):.4%}")

print()
print("=" * 70)
print("3) NULLS PER COLUMN (only non-empty)")
print("=" * 70)
nulls = df.isna().sum()
nulls = nulls[nulls > 0].sort_values(ascending=False)
if nulls.empty:
    print("(none)")
for col, n in nulls.items():
    print(f"  {col:<28} {n:>9,}  ({n/len(df):6.1%})")

print()
print("=" * 70)
print("4) OPEN-INTEREST CAPTURE SANITY")
print("=" * 70)
for c in ["ce_oi", "pe_oi", "chain_oi", "chain_oi_chg", "put_call_oi_ratio"]:
    s = df[c]
    zero = (s == 0).sum() if s.notna().any() else 0
    print(f"  {c:<18} nulls={s.isna().sum():>8,}  zeros={zero:>10,}  "
          f"mean={s.mean():>16,.0f}" if c != "put_call_oi_ratio" else
          f"  {c:<18} nulls={s.isna().sum():>8,}  mean={s.mean():>8.3f}  p50={s.median():>8.3f}")

print()
print("=" * 70)
print("5) KEY FEATURE DISTRIBUTIONS")
print("=" * 70)
feats = [
    "ce_iv_30", "pe_iv_30", "rv_yz", "iv_rv_spread", "term_slope",
    "put_call_iv_spread", "skew_25d", "atm_straddle", "put_call_vol_ratio",
    "put_call_oi_ratio", "otm_put_iv_premium", "chain_breadth",
    "ret_1d", "ret_5d", "ret_20d", "days_to_expiry_30d", "days_to_earnings",
]
print(df[feats].describe().T[["mean", "std", "min", "25%", "50%", "75%", "max"]].to_string())

print()
print("=" * 70)
print("6) SANITY FLAGS")
print("=" * 70)
print(f"  underlying_price <= 0          : {(df['underlying_price'] <= 0).sum():,}")
print(f"  ce_iv_30 outside [0, 200]      : {((df['ce_iv_30'] < 0) | (df['ce_iv_30'] > 200)).sum():,}")
print(f"  skew_25d outside [-100, 100]   : {((df['skew_25d'].abs() > 100)).sum():,}")
print(f"  atm_straddle <= 0              : {(df['atm_straddle'] <= 0).sum():,}")
print(f"  put_call_vol_ratio < 0         : {(df['put_call_vol_ratio'] < 0).sum():,}")
print(f"  put_call_oi_ratio < 0          : {(df['put_call_oi_ratio'] < 0).sum():,}")

print()
print("=" * 70)
print("7) TOP |CORRELATION| WITH next_day_return (labeled rows)")
print("=" * 70)
num = lab.select_dtypes(include=[np.number])
corr = num.corr()["next_day_return"].drop("next_day_return", errors="ignore")
top = corr.abs().sort_values(ascending=False).head(12)
for col, a in top.items():
    print(f"  {col:<26} r={corr[col]:+.4f}")

print()
print("=" * 70)
print("8) PARQUET EXPORT CHECK")
print("=" * 70)
pq_path = Path(settings.processed_data_dir) / "ml_dataset.parquet"
if pq_path.exists():
    pq = pd.read_parquet(pq_path)
    print(f"  {pq_path}")
    print(f"  parquet rows={len(pq):,}  pg rows={len(df):,}  match={len(pq) == len(df)}")
    print(f"  parquet cols={len(pq.columns)}  pg cols={len(df.columns)}")
else:
    print(f"  NOT FOUND: {pq_path}")
