"""Threaded per-symbol ETL: bhavcopy -> option_metrics (PostgreSQL).

Ported from the athena-verified `store_in_db/store_s.py` (2026-08-16).
Bare ``base_dir`` joins are replaced by ``Settings`` paths and the
legacy ``process_data`` imports now come from the typed ``mktmtm``
packages (``options.*`` math, ``data.*`` loaders, ``pipeline`` helpers).

Each symbol runs in its own worker thread; every worker builds the full
per-symbol time map (chain snapshot, ATM strangle IVs, YZ realized vol,
IV percentile/rank, upcoming earnings) and pushes it through the shared
``DBWriter`` pool. ``ETL_CHUNKS``/``ETL_CHUNK`` are honored by
``mktmtm.storage.etl_chunked`` instead — this module always scans every
bhavcopy file.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from pandas.errors import ParserError

from mktmtm.config.settings import Settings, settings
from mktmtm.data.bhavcopy import normalize_bhavcopy_columns, parse_bhavcopy_date
from mktmtm.data.earnings import get_upcoming_earning_date, load_earnings_dates
from mktmtm.data.rates import get_rate_with_fallback, load_interest_rates
from mktmtm.data.symbols import load_fno_symbols
from mktmtm.options.chain import (
    get_time_to_expiry_in_years,
    pick_strike_nearest_underlying,
)
from mktmtm.options.pricing import black_scholes_greeks, implied_volatility_bisection
from mktmtm.options.volatility import compute_yz_rolling_vol
from mktmtm.pipeline.process import discover_bhavcopy_files, load_spot_prices
from mktmtm.progress import progress_stage
from mktmtm.storage.db import DBWriter, close_db_pool

logger = logging.getLogger(__name__)

__all__ = [
    "WINDOW",
    "MAX_LOOKBACK",
    "_process_symbol",
    "main",
]

# Yang-Zhang rolling-vol parameters.
WINDOW, MAX_LOOKBACK = 30, 90

# Trailing window for IV percentile / rank, in trading days. One year (252) is
# the standard; a 30-day window made ``ivp`` a 30-valued step function (min
# 1/30 = 3.33%) and wasted information (process.py already uses 252 — this must
# match, since etl is the path that actually writes the ML dataset). Rows with
# fewer than ``_IV_MIN_PERIODS`` finite observations behind them get NULL rather
# than a percentile computed off a handful of points.
_IV_WINDOW = 252
_IV_MIN_PERIODS = 60


# ───────────────────────── helper utilities ────────────────────────────────
def _safe_T(date_key: str, expiry: str | None) -> float:
    return get_time_to_expiry_in_years(date_key, expiry) if expiry else 0.0


def _oi_int(v: Any) -> int:
    """Open interest -> int; missing/NaN -> 0 (pre-OI bhavcopy rows degrade gracefully)."""
    if v is None:
        return 0
    try:
        f = float(v)
        return int(f) if np.isfinite(f) else 0
    except (TypeError, ValueError):
        return 0


def _load_bhavcopy(fp: Path) -> pd.DataFrame | None:
    """Try reading with 0/2 skipped rows to handle header variants."""
    for skip in (0, 2):
        try:
            df = pd.read_csv(fp, skiprows=skip)
            if "SYMBOL" in df.columns or "TckrSymb" in df.columns:
                return normalize_bhavcopy_columns(df)
        except (ParserError, OSError):
            pass
    logger.warning("skip corrupt bhavcopy: %s", fp.name)
    return None


def _load_cache(cache_path: Path) -> tuple[pd.DataFrame, dict[str, tuple[int, int]]] | None:
    """Load the bhavcopy parquet cache as a single DataFrame + per-symbol offsets.

    Built by ``mktmtm preparse-bhavcopy`` (see ``data/bhavcopy.build_bhavcopy_cache``).
    Slicing per symbol from this cache replaces the per-symbol re-parse of every
    CSV, cutting ETL runtime by roughly 4x.

    NOTE on memory: the cache is ~60M rows (1,254 trading days). Two earlier
    versions OOM-killed the ETL on a 14 Gi host:
      * a per-symbol dict of DataFrames duplicated every row in memory (12.5 GB peak);
      * boolean-masking the whole frame per symbol was memory-safe but re-scanned
        60M rows 222 times (impractically slow).
    We therefore keep a *single* sorted DataFrame and a tiny ``{symbol: (start,
    stop)}`` position map built with ``searchsorted`` (no data copy). Per-symbol
    slices are ``df.iloc[start:stop]`` — one copy of each symbol's rows, fast and
    memory-light. Peak RSS stays near one frame (~9 GB) instead of two.
    """
    if not cache_path.exists():
        return None
    df = pd.read_parquet(cache_path)
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df = df.sort_values(["SYMBOL", "DATE"]).reset_index(drop=True)
    sym_col = df["SYMBOL"].to_numpy()
    syms = pd.unique(sym_col)
    starts = np.searchsorted(sym_col, syms, side="left")
    ends = np.searchsorted(sym_col, syms, side="right")
    offsets = {str(s): (int(st), int(en)) for s, st, en in zip(syms, starts, ends)}
    logger.info("bhavcopy cache: %d rows, %d symbols (%s)", len(df), len(offsets), cache_path)
    return df, offsets


def _iter_symbol_days(
    sym: str,
    bhav_files: list[Path],
    cache: tuple[pd.DataFrame, dict[str, tuple[int, int]]] | None,
) -> Iterator[tuple[datetime, pd.DataFrame]]:
    """Yield (trade_date, symbol_rows) from the parquet cache, else from CSVs."""
    if cache is not None:
        df, offsets = cache
        span = offsets.get(sym)
        if span is None:
            return
        start, stop = span
        sub = df.iloc[start:stop]
        for date_obj in sub["DATE"].unique():
            yield date_obj, sub[sub["DATE"] == date_obj]
        return
    for fp in bhav_files:
        date_obj = parse_bhavcopy_date(fp.name)
        if date_obj is None:
            continue
        df = _load_bhavcopy(fp)
        if df is None:
            continue
        df_sym = df[df["SYMBOL"] == sym]
        if df_sym.empty:
            continue
        yield date_obj, df_sym


def _iv_safe(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    is_call: bool,
) -> float | None:
    """Implied vol helper that never raises; returns pct or None (no sentinels)."""
    try:
        iv = implied_volatility_bisection(market_price, S, K, T, r, is_call=is_call)
    except Exception:
        return None
    if iv is None or not np.isfinite(iv) or iv <= 0:
        return None
    return iv * 100


def _iv_stats(
    rows: list[tuple[datetime, float]],
) -> dict[Any, dict[str, float]]:
    """Rolling 252-day IV percentile/rank over ``(datetime, iv)`` rows.

    Mirrors ``process.compute_rolling_iv_percentile_and_rank`` (same window and
    min-periods), so etl and process produce identical ivp/ivr. Points with
    fewer than ``_IV_MIN_PERIODS`` finite observations behind them are NULL, not
    a percentile off a handful of points.
    """
    if len(rows) < _IV_MIN_PERIODS:
        return {}
    df = pd.DataFrame(rows, columns=["Date", "iv"]).dropna().sort_values("Date")
    iv_vals = df["iv"].to_numpy(dtype=float)
    ivp: list[float | None] = []
    ivr: list[float | None] = []
    for i in range(len(iv_vals)):
        start = max(0, i - _IV_WINDOW + 1)
        sub = iv_vals[start : i + 1]
        sub = sub[np.isfinite(sub)]
        cur = iv_vals[i]
        if len(sub) < _IV_MIN_PERIODS or not np.isfinite(cur):
            ivp.append(None)
            ivr.append(None)
            continue
        pct = float(np.sum(sub <= cur) / len(sub))
        rng = float(sub.max() - sub.min())
        rank = float((cur - sub.min()) / rng) if rng > 0 else None
        ivp.append(pct * 100.0)
        ivr.append(rank * 100.0 if rank is not None else None)
    df["ivp"], df["ivr"] = ivp, ivr
    return cast(
        dict[Any, dict[str, float]],
        df.set_index("Date")[["ivp", "ivr"]].to_dict("index"),
    )


# ═════════════════════════ main worker ═════════════════════════════════════
def _process_symbol(
    sym: str,
    bhav_files: list[Path],
    spot_price_map: dict[str, dict[str, float]],
    daily_rate: Mapping[date, float],
    earnings_map: dict[str, list[date]],
    cfg: Settings,
    cache: tuple[pd.DataFrame, dict[str, tuple[int, int]]] | None = None,
) -> None:
    logger.debug("┌─ processing %-10s", sym)
    time_map: dict[str, dict[str, Any]] = {}
    daily_ohlc: list[dict[str, Any]] = []

    for date_obj, df_sym in _iter_symbol_days(sym, bhav_files, cache):
        date_key = date_obj.strftime("%d-%b-%Y")

        if len(time_map) % 100 == 0 and len(time_map) > 0:
            logger.debug("... %s rows accumulated for %s", len(time_map), sym)

        spot_rec = spot_price_map.get(sym, {}).get(date_key)
        if spot_rec is None:
            continue
        # Raw (unadjusted) close is the correct basis for ATM-strike selection
        # and IV/Greeks — it shares the corporate-action basis of NSE bhavcopy
        # strikes/settles (see docs/data/iv-integrity-audit.md).
        spot_raw = spot_rec["raw"]
        # Adjusted close is stored separately for returns/labels in ml_dataset.
        spot_adj = spot_rec["adjusted"]
        if not np.isfinite(spot_raw) or not np.isfinite(spot_adj):
            continue

        rate = get_rate_with_fallback(date_obj.date(), daily_rate)
        r_dec = (rate or 0) / 100.0

        # ───────── FUTURES (for OHLC + expiries) ─────────
        fut = df_sym[df_sym["INSTRUMENT"].isin(["FUTSTK", "FUTIDX"])].copy()
        if not fut.empty:
            fut["EXPIRY_DT"] = pd.to_datetime(
                fut["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce"
            )
            fut = fut.sort_values("EXPIRY_DT")
            fr = fut.iloc[0]
            daily_ohlc.append(
                dict(
                    Date=date_obj,
                    Open=float(fr["OPEN"]),
                    High=float(fr["HIGH"]),
                    Low=float(fr["LOW"]),
                    Close=float(fr["CLOSE"]),
                )
            )

        rec = time_map.setdefault(
            date_key,
            dict(
                underlying_price=spot_raw,
                underlying_price_adj=spot_adj,
                interest_rate=rate,
                upcoming_earning_date=None,
                expiry_30d=None,
                expiry_60d=None,
                expiry_90d=None,
                rv_yz=None,
                strike_price=None,
                ce={},
                pe={},
                option_chain=[],
            ),
        )

        if not fut.empty:
            fut_exp = sorted(fut["EXPIRY_DT"].dropna().unique())
            if len(fut_exp) > 0:
                rec["expiry_30d"] = fut_exp[0].strftime("%d-%b-%Y")
            if len(fut_exp) > 1:
                rec["expiry_60d"] = fut_exp[1].strftime("%d-%b-%Y")
            if len(fut_exp) > 2:
                rec["expiry_90d"] = fut_exp[2].strftime("%d-%b-%Y")

        # ───────── OPTIONS (chain + IV/Greeks) ─────────
        opts = df_sym[df_sym["INSTRUMENT"].isin(["OPTSTK", "OPTIDX"])].copy()
        if opts.empty:
            continue
        opts["EXPIRY_DT"] = pd.to_datetime(
            opts["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce"
        )

        # full chain snapshot – useful later
        for _, row in opts.iterrows():
            expiry = pd.to_datetime(row["EXPIRY_DT"]).strftime("%d-%b-%Y")
            t_row = _safe_T(date_key, expiry)
            iv = _iv_safe(
                market_price=float(row["SETTLE_PR"]),
                S=float(spot_raw),
                K=float(row["STRIKE_PR"]),
                T=t_row,
                r=r_dec,
                is_call=(row["OPTION_TYP"] == "CE"),
            )
            delta = 0.0
            if t_row > 0 and iv is not None and iv > 0:
                with suppress(Exception):
                    delta = black_scholes_greeks(
                        float(spot_raw),
                        float(row["STRIKE_PR"]),
                        t_row,
                        r_dec,
                        iv / 100,
                        is_call=(row["OPTION_TYP"] == "CE"),
                    )["delta"]

            rec["option_chain"].append(
                dict(
                    expiry=expiry,
                    strike=float(row["STRIKE_PR"]),
                    type=row["OPTION_TYP"],
                    settle=float(row["SETTLE_PR"]),
                    open=float(row["OPEN"]),
                    high=float(row["HIGH"]),
                    low=float(row["LOW"]),
                    close=float(row["CLOSE"]),
                    volume=int(row["CONTRACTS"]),
                    oi=_oi_int(row.get("OPEN_INT")),
                    chg_oi=_oi_int(row.get("CHG_IN_OI")),
                    iv=iv,
                    delta=delta,
                )
            )

        picked = pick_strike_nearest_underlying(spot_raw, opts)
        if not picked:
            continue
        ce30, ce60, ce90, pe30, pe60, pe90, strike = picked
        if any(x is None for x in (ce30, ce60, ce90, pe30, pe60, pe90)):
            continue  # incomplete chain

        rec["strike_price"] = strike

        t_30 = _safe_T(date_key, rec["expiry_30d"])
        t_60 = _safe_T(date_key, rec["expiry_60d"])
        t_90 = _safe_T(date_key, rec["expiry_90d"])

        iv_ce30 = _iv_safe(
            market_price=float(ce30["SETTLE_PR"]),
            S=float(spot_raw),
            K=float(strike),
            T=t_30,
            r=r_dec,
            is_call=True,
        )
        iv_pe30 = _iv_safe(
            market_price=float(pe30["SETTLE_PR"]),
            S=float(spot_raw),
            K=float(strike),
            T=t_30,
            r=r_dec,
            is_call=False,
        )

        rec["ce"] = dict(
            iv_30=iv_ce30,
            iv_60=_iv_safe(
                market_price=float(ce60["SETTLE_PR"]),
                S=float(spot_raw), K=float(strike), T=t_60, r=r_dec, is_call=True,
            ),
            iv_90=_iv_safe(
                market_price=float(ce90["SETTLE_PR"]),
                S=float(spot_raw), K=float(strike), T=t_90, r=r_dec, is_call=True,
            ),
            volume=int(opts[opts["OPTION_TYP"] == "CE"]["CONTRACTS"].sum()),
            oi=_oi_int(ce30.get("OPEN_INT")),
            last_price_30d=float(ce30["SETTLE_PR"]),
            close=float(ce30["CLOSE"]),
            open=float(ce30["OPEN"]),
            high=float(ce30["HIGH"]),
            low=float(ce30["LOW"]),
            ivp=None,
            ivr=None,
            greeks=black_scholes_greeks(
                float(spot_raw), float(strike), t_30, r_dec,
                iv_ce30 / 100 if iv_ce30 else 0.0, True,
            )
            if t_30 and iv_ce30
            else {},
        )

        rec["pe"] = dict(
            iv_30=iv_pe30,
            iv_60=_iv_safe(
                market_price=float(pe60["SETTLE_PR"]),
                S=float(spot_raw), K=float(strike), T=t_60, r=r_dec, is_call=False,
            ),
            iv_90=_iv_safe(
                market_price=float(pe90["SETTLE_PR"]),
                S=float(spot_raw), K=float(strike), T=t_90, r=r_dec, is_call=False,
            ),
            volume=int(opts[opts["OPTION_TYP"] == "PE"]["CONTRACTS"].sum()),
            oi=_oi_int(pe30.get("OPEN_INT")),
            last_price_30d=float(pe30["SETTLE_PR"]),
            close=float(pe30["CLOSE"]),
            open=float(pe30["OPEN"]),
            high=float(pe30["HIGH"]),
            low=float(pe30["LOW"]),
            ivp=None,
            ivr=None,
            greeks=black_scholes_greeks(
                float(spot_raw), float(strike), t_30, r_dec,
                iv_pe30 / 100 if iv_pe30 else 0.0, False,
            )
            if t_30 and iv_pe30
            else {},
        )

    # ───────── realized volatility (YZ) ─────────
    if daily_ohlc:
        yz = compute_yz_rolling_vol(
            pd.DataFrame(daily_ohlc),
            window=WINDOW,
            max_lookback=MAX_LOOKBACK,
            trading_periods=252,
        )
        for idx_raw, rv in yz.items():
            idx: Any = idx_raw
            d_str = pd.Timestamp(idx).strftime("%d-%b-%Y")
            if d_str in time_map and np.isfinite(rv):
                time_map[d_str]["rv_yz"] = rv

    # ───────── upcoming earnings ─────────
    for d_str in time_map:
        upcoming = get_upcoming_earning_date(sym, d_str, earnings_map)
        if upcoming is not None:
            time_map[d_str]["upcoming_earning_date"] = upcoming

    # ───────── IV percentile / rank ─────────
    ce_stat = _iv_stats(
        [
            (datetime.strptime(k, "%d-%b-%Y"), v["ce"]["iv_30"])
            for k, v in time_map.items()
            if v.get("ce", {}).get("iv_30")
        ]
    )
    pe_stat = _iv_stats(
        [
            (datetime.strptime(k, "%d-%b-%Y"), v["pe"]["iv_30"])
            for k, v in time_map.items()
            if v.get("pe", {}).get("iv_30")
        ]
    )

    for k, rec in time_map.items():
        dt = datetime.strptime(k, "%d-%b-%Y")
        if dt in ce_stat:
            rec["ce"]["ivp"], rec["ce"]["ivr"] = ce_stat[dt]["ivp"], ce_stat[dt]["ivr"]
        if dt in pe_stat:
            rec["pe"]["ivp"], rec["pe"]["ivr"] = pe_stat[dt]["ivp"], pe_stat[dt]["ivr"]

    # ───────── write to DB ─────────
    if time_map:
        logger.debug("--> building %s rows for %s, writing to DB ...", len(time_map), sym)
        with DBWriter(cfg) as w:
            for d_str, payload in time_map.items():
                w.write_row(sym, d_str, payload)
        logger.debug("└─ %s rows stored for %s", len(time_map), sym)
    else:
        logger.debug("└─ no data rows for %s", sym)


# ═════════════════════════ orchestrator ════════════════════════════════════
def main(settings_: Settings | None = None) -> None:
    cfg = settings_ if settings_ is not None else settings

    # ───────── symbol list ─────────
    symbols = load_fno_symbols()
    logger.info("total symbols: %s", len(symbols))

    # ───────── interest-rate map ─────────
    daily_rate = load_interest_rates(cfg.reference_data_dir / "ADJUSTED_MIFOR.csv")

    # ───────── earnings map ─────────
    earnings_map = load_earnings_dates(cfg.reference_data_dir / "earning_dates.json")

    # ───────── spot-price map (Yahoo) ─────────
    spot_price_map = load_spot_prices(symbols, cfg.raw_data_dir / "yahoo_finance")

    # ───────── bhavcopy list + parquet cache ─────────
    bhavcopy_dir = cfg.raw_data_dir / "bhavcopy" / "extracted"
    bhav_files = [fp for fp, _ in discover_bhavcopy_files(bhavcopy_dir)]
    logger.info("bhav-copies to scan: %s", len(bhav_files))
    cache = _load_cache(cfg.processed_data_dir / "bhavcopy_cache.parquet")

    # ───────── threaded execution ─────────
    failed = 0
    with (
        progress_stage("Loading symbols into Postgres", total=len(symbols), unit="symbols") as bar,
        ThreadPoolExecutor(max_workers=cfg.max_workers) as pool,
    ):
        futures = {
            pool.submit(
                _process_symbol, s, bhav_files, spot_price_map, daily_rate, earnings_map,
                cfg, cache,
            ): s
            for s in symbols
        }
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                failed += 1
                logger.exception("worker for %s failed: %s", sym, exc)
            bar.advance(label=f"{sym}{f'  failed={failed}' if failed else ''}")

    if failed:
        logger.warning("ETL finished with %d/%d symbols failed", failed, len(symbols))

    # ───────── graceful pool shutdown ─────────
    close_db_pool()
    logger.info("connection pool closed")


# ═════════════════════════ entry-point ═════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("manual abort - shutting down")
