"""Finish the 1-year validation: re-run ETL (idempotent upsert, completes the
remaining symbols) then make-dataset. Reuses the existing validation tree
(Yahoo dual-series + 1y bhavcopy cache are already built).
"""
from __future__ import annotations

import logging
from pathlib import Path

import psycopg2

from mktmtm.config.settings import Settings
from mktmtm.ml.dataset import build_dataset, export_parquet, write_dataset
from mktmtm.storage.db import build_dsn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("val1y-finish")

ROOT = Path("/mnt/storage/Study/Major Project/Code/MktMtm")
VAL = ROOT / "data" / "validation_1y"

cfg = Settings(
    data_dir=VAL / "data",
    raw_data_dir=VAL / "raw",
    processed_data_dir=VAL / "processed",
    serving_data_dir=VAL / "serving",
    reference_data_dir=ROOT / "data" / "reference",
)

from mktmtm.storage import etl as etl_mod

etl_mod.main(settings_=cfg)
log.info("ETL complete")

conn = psycopg2.connect(build_dsn(cfg))
try:
    df = build_dataset(conn, symbols=None, limit_dates=None)
    if df.empty:
        log.error("option_metrics empty after ETL — aborting")
        raise SystemExit(1)
    written = write_dataset(conn, df)
    labeled = int(df["next_day_return"].notna().sum())
    log.info("ml_dataset: wrote %d rows (%d labeled)", written, labeled)
    out = VAL / "processed" / "ml_dataset.parquet"
    export_parquet(df, out)
    log.info("parquet -> %s", out)
finally:
    conn.close()

import numpy as np
import pandas as pd

df["date"] = pd.to_datetime(df["date"])
recent = df[df["date"] >= pd.Timestamp((pd.Timestamp.now() - pd.Timedelta(days=13 * 30)).date())]
print("\n===== VALIDATION SUMMARY (recent ~13-month window) =====")
print(f"rows in window: {len(recent):,}")
print(f"ce_iv_30 == 500.0 : {(recent['ce_iv_30']==500.0).sum():,}")
print(f"pe_iv_30 == 0.0   : {(recent['pe_iv_30']==0.0).sum():,}")
print(f"ce_iv_30 isna      : {recent['ce_iv_30'].isna().sum():,}")
print(f"ce_iv_30 outside (0,200]: {((recent['ce_iv_30'].isna())|(recent['ce_iv_30']<=0)|(recent['ce_iv_30']>200)).sum():,}")
print(f"ce_ivp min         : {recent['ce_ivp'].min():.4f}")
sane = recent[(recent["ce_iv_30"].between(5,150)) & (recent["pe_iv_30"].between(5,150))]
if len(sane) > 50:
    print(f"sane-IV rows       : {len(sane):,}  corr(ce,pe)={sane['ce_iv_30'].corr(sane['pe_iv_30']):+.3f}  median|gap|={(sane['ce_iv_30']-sane['pe_iv_30']).abs().median():.1f}")
else:
    print("sane-IV rows       : too few to assess parity")
