"""Chunked single-pass ETL variant (kept alongside the threaded ETL).

Ported from the athena-verified `store_in_db/process_and_store.py`
(2026-08-16). Iterates bhavcopy files once, in date order, and streams
every symbol's payload straight into the ``DBWriter`` buffer — no
per-symbol time map held in memory. Unlike the threaded ``etl.py`` this
variant skips the per-symbol rolling IV percentile/rank pass.

Sharding is honored via ``ETL_CHUNKS`` / ``ETL_CHUNK`` (e.g.
``ETL_CHUNKS=8 ETL_CHUNK=3`` runs only files ``[len*3/8, len*4/8)``),
which lets several processes split a full backfill across shards. All
paths come from ``Settings``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mktmtm.config.settings import Settings, settings
from mktmtm.data.bhavcopy import normalize_bhavcopy_columns, parse_bhavcopy_date
from mktmtm.data.earnings import load_earnings_dates
from mktmtm.data.rates import get_rate_with_fallback, load_interest_rates
from mktmtm.data.symbols import load_fno_symbols
from mktmtm.options.chain import (
    get_time_to_expiry_in_years,
    pick_strike_nearest_underlying,
)
from mktmtm.options.pricing import black_scholes_greeks, implied_volatility_bisection
from mktmtm.options.volatility import compute_yz_rolling_vol
from mktmtm.pipeline.process import discover_bhavcopy_files, load_spot_prices
from mktmtm.storage.db import DBWriter, close_db_pool

logger = logging.getLogger(__name__)

__all__ = [
    "WINDOW",
    "MAX_LOOKBACK",
    "main",
]

# Yang-Zhang rolling-vol parameters.
WINDOW, MAX_LOOKBACK = 30, 90


# ────────────────────────────────────────────────────────────────────────────
def _safe_T(date_key: str, expiry: str | None) -> float:
    return get_time_to_expiry_in_years(date_key, expiry) if expiry else 0.0


def _load_bhavcopy(path: Path) -> pd.DataFrame:
    """Read an NSE bhav-copy CSV; retry with skiprows=2 if first lines are metadata."""
    for skip in (0, 2):
        df = pd.read_csv(path, skiprows=skip)
        if "SYMBOL" in df.columns or "TckrSymb" in df.columns:
            return normalize_bhavcopy_columns(df)
    raise ValueError(f"'SYMBOL' column missing in {path.name}")


def _iv_bisection_pct(
    price: float, spot: float, strike: float, t: float, r_dec: float, call: bool
) -> float:
    """Bisection IV as a percentage for a single option row."""
    return implied_volatility_bisection(price, spot, strike, t, r_dec, is_call=call) * 100


def _greeks_or_zeros(
    iv30: float, call: bool, spot: float, strike: float, t_30: float, r_dec: float
) -> dict[str, float]:
    """30-day ATM Greeks, or an all-zero dict when they cannot be computed."""
    if iv30 > 0 and t_30 > 0:
        return black_scholes_greeks(spot, strike, t_30, r_dec, iv30 / 100.0, is_call=call)
    return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}


# ────────────────────────────────────────────────────────────────────────────
def main(settings_: Settings | None = None) -> None:
    cfg = settings_ if settings_ is not None else settings

    chunks = int(os.environ.get("ETL_CHUNKS", "1"))
    chunk = int(os.environ.get("ETL_CHUNK", "0"))

    # ── symbol list ───────────────────────────────────────────────────────
    symbols = load_fno_symbols()

    # ── spot-price cache (Yahoo) ──────────────────────────────────────────
    spot_price_map = load_spot_prices(symbols, cfg.raw_data_dir / "yahoo_finance")

    # ── daily interest-rate map ───────────────────────────────────────────
    ir_map = load_interest_rates(cfg.reference_data_dir / "ADJUSTED_MIFOR.csv")

    # ── earnings map ──────────────────────────────────────────────────────
    earnings_map = load_earnings_dates(cfg.reference_data_dir / "earning_dates.json")

    # ── holder for realised-vol bars ──────────────────────────────────────
    daily_ohlc: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}

    db_writer = DBWriter(cfg)

    # ── iterate bhav-copies (optionally sharded) ─────────────────────────
    bhavcopy_dir = cfg.raw_data_dir / "bhavcopy" / "extracted"
    files = [fp for fp, _ in discover_bhavcopy_files(bhavcopy_dir)]
    if chunks > 1:
        lo = len(files) * chunk // chunks
        hi = len(files) * (chunk + 1) // chunks
        files = files[lo:hi]
        logger.info("chunk %d/%d: files %d:%d (%d)", chunk, chunks, lo, hi, len(files))

    for csv in files:
        try:
            fname = csv.name
            date_obj = parse_bhavcopy_date(fname)
            if date_obj is None:
                continue
            date_key = date_obj.strftime("%d-%b-%Y")
            r_dec = get_rate_with_fallback(date_obj.date(), ir_map) / 100.0
            df = _load_bhavcopy(csv)
        except Exception as exc:
            logger.error("ERROR setup %s: %s", csv.name, exc)
            continue

        logger.info("Processing %s", fname)

        for sym in symbols:
            sym_df = df[df["SYMBOL"] == sym]
            if sym_df.empty:
                continue

            # ── nearest-expiry futures bar ──────────────────────────────
            fut = sym_df[sym_df["INSTRUMENT"].isin(["FUTSTK", "FUTIDX"])].copy()
            if fut.empty:
                continue
            fut["EXPIRY_DT"] = pd.to_datetime(
                fut["EXPIRY_DT"], errors="coerce"
            ).fillna(pd.Timestamp("2100-01-01"))
            fut_row = fut.sort_values("EXPIRY_DT").iloc[0]

            if all(float(fut_row[x]) > 0 for x in ("OPEN", "HIGH", "LOW", "CLOSE")):
                daily_ohlc[sym].append(
                    dict(
                        Date=date_obj,
                        Open=float(fut_row["OPEN"]),
                        High=float(fut_row["HIGH"]),
                        Low=float(fut_row["LOW"]),
                        Close=float(fut_row["CLOSE"]),
                    )
                )

            rv_yz = None
            if len(daily_ohlc[sym]) >= WINDOW:
                yz = compute_yz_rolling_vol(
                    pd.DataFrame(daily_ohlc[sym][-MAX_LOOKBACK:]),
                    window=WINDOW,
                    max_lookback=MAX_LOOKBACK,
                    trading_periods=252,
                )
                if not yz.empty and np.isfinite(yz.iloc[-1]):
                    rv_yz = float(yz.iloc[-1])

            # ── spot price ──────────────────────────────────────────────
            spot = spot_price_map.get(sym, {}).get(date_key) or float(fut_row["CLOSE"])

            # ── upcoming earnings ───────────────────────────────────────
            upcoming = next(
                (dt.strftime("%d-%b-%Y") for dt in earnings_map.get(sym, []) if dt > date_obj),
                None,
            )

            # ── payload skeleton ────────────────────────────────────────
            payload: dict[str, Any] = {
                "underlying_price": spot,
                "interest_rate": r_dec * 100,
                "expiry_30d": None,
                "expiry_60d": None,
                "expiry_90d": None,
                "rv_yz": rv_yz,
                "strike_price": None,
                "upcoming_earning_date": upcoming,
            }

            # futures expiries
            exps = sorted(fut["EXPIRY_DT"].dropna().unique())
            if len(exps) > 0:
                payload["expiry_30d"] = exps[0].strftime("%d-%b-%Y")
            if len(exps) > 1:
                payload["expiry_60d"] = exps[1].strftime("%d-%b-%Y")
            if len(exps) > 2:
                payload["expiry_90d"] = exps[2].strftime("%d-%b-%Y")

            # ── option chain snapshot ───────────────────────────────────
            opts = sym_df[sym_df["INSTRUMENT"].isin(["OPTSTK", "OPTIDX"])].copy()
            opts["EXPIRY_DT"] = pd.to_datetime(opts["EXPIRY_DT"], errors="coerce")

            chain: list[dict[str, Any]] = []
            for _, row in opts.iterrows():
                try:
                    exp = row["EXPIRY_DT"].strftime("%d-%b-%Y")
                    t = get_time_to_expiry_in_years(date_key, exp)
                    iv = implied_volatility_bisection(
                        float(row["SETTLE_PR"]),
                        spot,
                        float(row["STRIKE_PR"]),
                        t,
                        r_dec,
                        is_call=row["OPTION_TYP"] == "CE",
                    )
                    delta = black_scholes_greeks(
                        spot,
                        float(row["STRIKE_PR"]),
                        t,
                        r_dec,
                        iv,
                        is_call=row["OPTION_TYP"] == "CE",
                    )["delta"]
                    chain.append(
                        dict(
                            expiry=exp,
                            strike=float(row["STRIKE_PR"]),
                            type=row["OPTION_TYP"],
                            settle=float(row["SETTLE_PR"]),
                            iv=iv * 100 if iv else 0,
                            delta=delta,
                            volume=int(row["CONTRACTS"]),
                        )
                    )
                except Exception:
                    pass
            payload["option_chain"] = chain

            # ── fill CE / PE detail ────────────────────────────────────
            nearest = pick_strike_nearest_underlying(spot, opts)
            if not nearest:
                continue
            ce30, ce60, ce90, pe30, pe60, pe90, strike = nearest
            if any(x is None for x in (ce30, ce60, ce90, pe30, pe60, pe90)):
                continue
            payload["strike_price"] = strike

            t_30 = _safe_T(date_key, payload["expiry_30d"])
            t_60 = _safe_T(date_key, payload["expiry_60d"])
            t_90 = _safe_T(date_key, payload["expiry_90d"])

            # CE block
            iv30_ce = _iv_bisection_pct(float(ce30["SETTLE_PR"]), spot, strike, t_30, r_dec, True)
            payload["ce"] = {
                "iv_30": iv30_ce,
                "iv_60": _iv_bisection_pct(float(ce60["SETTLE_PR"]), spot, strike, t_60, r_dec, True),
                "iv_90": _iv_bisection_pct(float(ce90["SETTLE_PR"]), spot, strike, t_90, r_dec, True),
                "volume": int(opts[opts["OPTION_TYP"] == "CE"]["CONTRACTS"].sum()),
                "last_price_30d": float(ce30["SETTLE_PR"]),
                "close": float(ce30["CLOSE"]),
                "open": float(ce30["OPEN"]),
                "high": float(ce30["HIGH"]),
                "low": float(ce30["LOW"]),
                "ivp": None,
                "ivr": None,
                "greeks": _greeks_or_zeros(iv30_ce, True, spot, strike, t_30, r_dec),
            }

            # PE block
            iv30_pe = _iv_bisection_pct(float(pe30["SETTLE_PR"]), spot, strike, t_30, r_dec, False)
            payload["pe"] = {
                "iv_30": iv30_pe,
                "iv_60": _iv_bisection_pct(float(pe60["SETTLE_PR"]), spot, strike, t_60, r_dec, False),
                "iv_90": _iv_bisection_pct(float(pe90["SETTLE_PR"]), spot, strike, t_90, r_dec, False),
                "volume": int(opts[opts["OPTION_TYP"] == "PE"]["CONTRACTS"].sum()),
                "last_price_30d": float(pe30["SETTLE_PR"]),
                "close": float(pe30["CLOSE"]),
                "open": float(pe30["OPEN"]),
                "high": float(pe30["HIGH"]),
                "low": float(pe30["LOW"]),
                "ivp": None,
                "ivr": None,
                "greeks": _greeks_or_zeros(iv30_pe, False, spot, strike, t_30, r_dec),
            }

            payload["extras"] = {"source_file": fname}
            db_writer.write_row(sym, date_key, payload)

    db_writer.close()
    close_db_pool()


# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
