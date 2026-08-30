"""1-year validation run of the fixed MktMtm pipeline (run on athena).

Drives: fetch-yahoo (dual series) -> 1y bhavcopy cache -> etl -> make-dataset,
scoped to the last ~13 months so the run is fast. Uses a Settings override so
the *real* 5-year data dirs are untouched; ETL writes into the same PG
options_db (idempotent upsert on (symbol,date)), refreshing the recent window.
"""
from __future__ import annotations

import logging
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from mktmtm.config.settings import Settings
from mktmtm.data.bhavcopy import build_bhavcopy_cache, parse_bhavcopy_date
from mktmtm.data.yahoo import download_yahoo_prices

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("val1y")

ROOT = Path("/mnt/storage/Study/Major Project/Code/MktMtm")
REAL_REF = ROOT / "data" / "reference"
REAL_EXT = ROOT / "data" / "raw" / "bhavcopy" / "extracted"

VAL = ROOT / "data" / "validation_1y"
VAL_RAW = VAL / "raw"
VAL_EXT = VAL_RAW / "bhavcopy" / "extracted"
VAL_YH = VAL_RAW / "yahoo_finance"
VAL_PROC = VAL / "processed"

# Clean a previous validation tree so this run is hermetic.
if VAL.exists():
    shutil.rmtree(VAL)
VAL_EXT.mkdir(parents=True)
VAL_YH.mkdir(parents=True)
VAL_PROC.mkdir(parents=True)
(VAL / "reference").mkdir(parents=True)

# ── 1) Copy the last ~13 months of bhavcopy CSVs into the validation tree ──
cutoff = datetime.now() - timedelta(days=13 * 30)
n_files = 0
for fp in sorted(REAL_EXT.glob("*.csv")):
    d = parse_bhavcopy_date(fp.name)
    if d is not None and d >= cutoff:
        shutil.copy2(fp, VAL_EXT / fp.name)
        n_files += 1
log.info("Copied %d bhavcopy files (>= %s) into validation tree", n_files, cutoff.date())

# ── 2) Fresh Yahoo fetch (dual series) into the validation tree ──
n_yh = download_yahoo_prices(output_dir=VAL_YH, days=430)
log.info("Fetched Yahoo prices for %d symbols (dual series)", n_yh)

# ── 3) Build the 1-year bhavcopy cache ──
cache_path = VAL_PROC / "bhavcopy_cache.parquet"
build_bhavcopy_cache(VAL_EXT, cache_path)
log.info("1-year bhavcopy cache -> %s", cache_path)

# ── 4) Scoped Settings: validation data dirs, real reference, PG from .env ──
cfg = Settings(
    data_dir=VAL / "data",
    raw_data_dir=VAL_RAW,
    processed_data_dir=VAL_PROC,
    serving_data_dir=VAL / "serving",
    reference_data_dir=REAL_REF,
)
log.info("Settings: raw=%s processed=%s", cfg.raw_data_dir, cfg.processed_data_dir)

# ── 5) ETL into PG (upserts the recent ~13 months) ──
from mktmtm.storage import etl as etl_mod

etl_mod.main(settings_=cfg)
log.info("ETL complete")

# ── 6) make-dataset: build matrix for the recent window + parquet export ──
import psycopg2

from mktmtm.ml.dataset import build_dataset, export_parquet, write_dataset
from mktmtm.storage.db import build_dsn

conn = psycopg2.connect(build_dsn(cfg))
try:
    df = build_dataset(conn, symbols=None, limit_dates=None)
    if df.empty:
        log.error("option_metrics empty after ETL — aborting")
        sys.exit(1)
    written = write_dataset(conn, df)
    labeled = int(df["next_day_return"].notna().sum())
    log.info("ml_dataset: wrote %d rows (%d labeled)", written, labeled)
    out = VAL_PROC / "ml_dataset.parquet"
    export_parquet(df, out)
    log.info("parquet -> %s", out)
finally:
    conn.close()

# ── 7) Print a compact summary for the caller to verify ──
import numpy as np
import pandas as pd

df["date"] = pd.to_datetime(df["date"])
recent = df[df["date"] >= pd.Timestamp(cutoff.date())]
print("\n===== VALIDATION SUMMARY (recent ~13-month window) =====")
print(f"rows in window: {len(recent):,}")
print(f"ce_iv_30 == 500.0 : {(recent['ce_iv_30']==500.0).sum():,}")
print(f"pe_iv_30 == 0.0   : {(recent['pe_iv_30']==0.0).sum():,}")
print(f"ce_iv_30 outside (0,200]: {((recent['ce_iv_30'].isna())|(recent['ce_iv_30']<=0)|(recent['ce_iv_30']>200)).sum():,}")
print(f"ce_iv_30 isna      : {recent['ce_iv_30'].isna().sum():,}")
print(f"ce_ivp min         : {recent['ce_ivp'].min():.4f}")
sane = recent[(recent["ce_iv_30"].between(5,150)) & (recent["pe_iv_30"].between(5,150))]
if len(sane) > 50:
    print(f"sane-IV rows       : {len(sane):,}  corr(ce,pe)={sane['ce_iv_30'].corr(sane['pe_iv_30']):+.3f}  median|gap|={(sane['ce_iv_30']-sane['pe_iv_30']).abs().median():.1f}")
else:
    print("sane-IV rows       : too few to assess parity")
