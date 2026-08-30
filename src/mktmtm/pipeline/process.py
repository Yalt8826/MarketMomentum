"""Pipeline engine: bhavcopy files -> per-day option metrics -> processed JSON.

Ported from the athena-verified `process_data.py` engine loop (2026-08-16),
with bare ``base_dir`` joins replaced by ``Settings`` paths and the engine
reusing ``mktmtm.data.*`` (catalog, yahoo spot map, rates, earnings) and
``mktmtm.options.*`` (BS pricing/IV, YZ vol, chain helpers).

Flow: load catalog -> yahoo spot map -> interest rates -> earnings ->
iterate bhavcopy files (build daily OHLC + option-chain snapshots + ATM
strike selection) -> rolling Yang-Zhang RV -> rolling IV percentile/rank ->
sort timestamps -> NaN/Inf scrub -> upcoming earnings -> interpolate RV ->
write ``processed_data.json`` into ``Settings.processed_data_dir``.

Two defects in the original engine are fixed here (verified by the Phase 3
fixture test): ``r_decimal`` was referenced before assignment inside the
option-chain loop (a swallowed ``NameError`` left ``option_chain`` empty),
and the IV-rank write-back crashed on a flat IV series (``None * 100.0``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mktmtm.config.settings import Settings, settings
from mktmtm.data.bhavcopy import normalize_bhavcopy_columns, parse_bhavcopy_date
from mktmtm.data.earnings import get_upcoming_earning_date, load_earnings_dates
from mktmtm.data.rates import get_rate_with_fallback, load_interest_rates
from mktmtm.data.symbols import load_fno_symbols
from mktmtm.options.chain import (
    get_time_to_expiry_in_years,
    pick_strike_nearest_underlying,
    unadjusted_spot_from_bhavcopy,
)
from mktmtm.options.pricing import black_scholes_greeks, implied_volatility_bisection
from mktmtm.options.volatility import compute_yz_rolling_vol
from mktmtm.progress import track

logger = logging.getLogger(__name__)

__all__ = [
    "main",
    "load_spot_prices",
    "discover_bhavcopy_files",
    "compute_rolling_iv_percentile_and_rank",
    "replace_nan_inf",
    "fill_missing_rv_with_interpolation",
]

OUTPUT_FILENAME = "processed_data.json"

_YZ_WINDOW = 30
_YZ_MAX_LOOKBACK = 90

# Trailing window for IV percentile / rank, in trading days. One year (252) is
# the standard; the previous value of 30 was forced by having only a month of
# history and made ``ivp`` a 30-valued step function (min 1/30 = 3.33%).
# ``ivp``/``ivr`` are among the strongest signals measured in
# docs/data/ml-dataset-report.md §7, so the short window was costing real
# information. Rows with fewer than ``_IV_MIN_PERIODS`` observations behind them
# get NULL rather than a percentile computed off a handful of points.
_IV_WINDOW = 252
_IV_MIN_PERIODS = 60


def load_spot_prices(
    symbols: list[str], yahoo_dir: Path
) -> dict[str, dict[str, dict[str, float]]]:
    """Read ``<SYMBOL>_processed.json`` files into spot-price maps.

    Returns ``{symbol: {date_key: {"adjusted": float, "raw": float}}}``. The
    **raw** (unadjusted) close is the basis for ATM-strike selection and
    IV/Greeks — it shares the same corporate-action basis as NSE bhavcopy
    strikes/settles. The **adjusted** close is the basis for returns/labels.
    Mirrors the legacy engine's Yahoo spot map; missing/empty files are skipped.
    When a stored timestamp lacks one series (old fixtures), both are set from
    the value present (for an index, adjusted == raw, so this is lossless).
    """
    spot_price_map: dict[str, dict[str, dict[str, float]]] = {}
    for symbol in symbols:
        json_path = yahoo_dir / f"{symbol}_processed.json"
        if not json_path.exists():
            continue
        try:
            with json_path.open("r", encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)
            timestamps: dict[str, Any] = data["historical"]["scripts"][symbol]["timestamps"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue

        prices: dict[str, dict[str, float]] = {}
        for date_key, ts_data in timestamps.items():
            if not isinstance(ts_data, dict):
                continue
            adj = ts_data.get("underlying_price")
            raw = ts_data.get("underlying_price_raw")
            if adj is None and raw is None:
                continue
            if not isinstance(adj, (int, float)) or not np.isfinite(adj):
                adj = raw if isinstance(raw, (int, float)) and np.isfinite(raw) else None
            if not isinstance(raw, (int, float)) or not np.isfinite(raw):
                raw = adj
            if adj is None and raw is None:
                continue
            prices[date_key] = {"adjusted": float(adj), "raw": float(raw)}
        spot_price_map[symbol] = prices
    return spot_price_map


def discover_bhavcopy_files(bhavcopy_dir: Path) -> list[tuple[Path, datetime]]:
    """List ``*.csv`` bhavcopy files in the extracted dir, sorted by trade date."""
    files: list[tuple[Path, datetime]] = []
    for path in bhavcopy_dir.glob("*.csv"):
        date_obj = parse_bhavcopy_date(path.name)
        if date_obj is not None:
            files.append((path, date_obj))
    files.sort(key=lambda item: item[1])
    return files


def compute_rolling_iv_percentile_and_rank(
    iv_df: pd.DataFrame,
    window_size: int = _IV_WINDOW,
    min_periods: int = _IV_MIN_PERIODS,
) -> pd.DataFrame:
    """Compute rolling IV percentile + min-max rank over a trailing window.

    Input frame must have a numeric ``iv_30`` column. Adds ``ivp`` (0..1
    percentile) and ``ivr`` (0..1 min-max rank, None when the window is
    flat) columns.

    Windows with fewer than ``min_periods`` finite observations yield None: a
    percentile computed off a handful of points is noise wearing a percentile's
    clothes, and it is better to train on NULL than on that.
    """
    iv_30d_percentiles: list[float | None] = []
    iv_30d_ranks: list[float | None] = []
    vals = np.asarray(iv_df["iv_30"].to_numpy(dtype=float))

    for i in range(len(vals)):
        start = max(0, i - window_size + 1)
        sub = vals[start : i + 1]
        current_val = vals[i]
        # NaNs must be excluded, not merely skipped by the comparison: they
        # would otherwise inflate the denominator and deflate every percentile.
        sub = sub[np.isfinite(sub)]
        if len(sub) < min_periods or not np.isfinite(current_val):
            iv_30d_percentiles.append(None)
            iv_30d_ranks.append(None)
            continue

        # 1) Percentile
        count_le = np.sum(sub <= current_val)
        percentile = count_le / len(sub)

        # 2) Rank (min-max)
        sub_min, sub_max = np.min(sub), np.max(sub)
        rank = (current_val - sub_min) / (sub_max - sub_min) if sub_max > sub_min else None

        iv_30d_percentiles.append(float(percentile))
        iv_30d_ranks.append(float(rank) if rank is not None else None)

    out = iv_df.copy()
    out["ivp"] = iv_30d_percentiles
    out["ivr"] = iv_30d_ranks
    return out


def replace_nan_inf(obj: Any) -> Any:
    """Recursively replace non-finite floats with None (in place)."""
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            obj[key] = replace_nan_inf(obj[key])
        return obj
    if isinstance(obj, list):
        for idx in range(len(obj)):
            obj[idx] = replace_nan_inf(obj[idx])
        return obj
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def fill_missing_rv_with_interpolation(result: dict[str, Any]) -> dict[str, Any]:
    """Fill missing ``rv_yz`` values by linear interpolation per symbol.

    Earliest values stay None when there is nothing to interpolate from.
    """
    for script in result["historical"]["scripts"].values():
        timestamps: dict[str, Any] = script["timestamps"]

        dates: list[datetime] = []
        rv_values: list[Any] = []
        for date_str, data in timestamps.items():
            try:
                date = datetime.strptime(date_str, "%d-%b-%Y")
            except ValueError:
                continue
            dates.append(date)
            rv_values.append(data.get("rv_yz"))

        if not dates:
            continue

        df = pd.DataFrame({"date": dates, "rv": rv_values}).sort_values("date")
        # Coerce to numeric first: None entries make the column object-dtype,
        # which pandas 3 refuses to interpolate.
        df["rv"] = pd.to_numeric(df["rv"], errors="coerce")
        df["rv_interpolated"] = df["rv"].interpolate(method="linear")

        for _, row in df.iterrows():
            date_str = row["date"].strftime("%d-%b-%Y")
            if date_str in timestamps and timestamps[date_str].get("rv_yz") is None:
                if pd.notna(row["rv_interpolated"]):
                    timestamps[date_str]["rv_yz"] = float(row["rv_interpolated"])
                else:
                    timestamps[date_str]["rv_yz"] = None

    return result


def _oi_int(v: Any) -> int:
    """Open interest -> int; missing/NaN -> 0 (pre-OI bhavcopy rows degrade gracefully)."""
    if v is None:
        return 0
    try:
        f = float(v)
        return int(f) if np.isfinite(f) else 0
    except (TypeError, ValueError):
        return 0


def _iv_pct(
    market_price: float, S: float, K: float, T: float, r: float, is_call: bool
) -> float | None:
    """Implied vol in percent, or None when the solver returns None (no sentinels)."""
    iv = implied_volatility_bisection(market_price, S, K, T, r, is_call=is_call)
    return iv * 100.0 if iv is not None else None


def _build_chain_rows(
    options_data: pd.DataFrame,
    symbol: str,
    spot_price: float,
    date_key: str,
    r_decimal: float,
) -> list[dict[str, Any]]:
    """Snapshot every option row of ``symbol`` with IV + delta computed."""
    chain_rows: list[dict[str, Any]] = []
    for _, row in options_data.iterrows():
        if row["INSTRUMENT"] not in ("OPTSTK", "OPTIDX") or row["SYMBOL"] != symbol:
            continue
        try:
            expiry_str = pd.to_datetime(row["EXPIRY_DT"]).strftime("%d-%b-%Y")
            t_row = get_time_to_expiry_in_years(date_key, expiry_str)
            iv_row = implied_volatility_bisection(
                market_price=float(row["SETTLE_PR"]),
                S=spot_price,
                K=float(row["STRIKE_PR"]),
                T=t_row,
                r=r_decimal,
                is_call=(row["OPTION_TYP"] == "CE"),
            )
            delta_row = (
                black_scholes_greeks(
                    spot_price,
                    float(row["STRIKE_PR"]),
                    t_row,
                    r_decimal,
                    iv_row,
                    is_call=(row["OPTION_TYP"] == "CE"),
                )["delta"]
                if iv_row is not None
                else None
            )
            chain_rows.append(
                {
                    "expiry": expiry_str,
                    "strike": float(row["STRIKE_PR"]),
                    "type": row["OPTION_TYP"],
                    "settle": float(row["SETTLE_PR"]),
                    "open": float(row["OPEN"]),
                    "high": float(row["HIGH"]),
                    "low": float(row["LOW"]),
                    "close": float(row["CLOSE"]),
                    "volume": int(row["CONTRACTS"]),
                    "oi": _oi_int(row.get("OPEN_INT")),
                    "chg_oi": _oi_int(row.get("CHG_IN_OI")),
                    "iv": (iv_row * 100.0) if iv_row is not None else None,
                    "delta": delta_row,
                }
            )
        except (TypeError, ValueError, KeyError):
            continue  # skip malformed rows
    return chain_rows


def main(settings_: Settings | None = None, limit_days: int | None = None) -> Path:
    """Run the full processing engine and write ``processed_data.json``.

    Returns the output file path. All data/reference/output dirs come from
    ``Settings`` (defaults to the module-level singleton). Pass ``limit_days``
    to bound the number of bhavcopy files processed (most recent first).
    """
    cfg = settings_ if settings_ is not None else settings

    # 0) Load F&O catalog ------------------------------------------------ #
    symbols = load_fno_symbols()
    logger.info("Processing data for %d symbols", len(symbols))

    # 1) Underlying spot prices from Yahoo processed JSON ----------------- #
    yahoo_dir = cfg.raw_data_dir / "yahoo_finance"
    spot_price_map = load_spot_prices(symbols, yahoo_dir)

    # 2) Bhavcopy files (sorted by trade date) ---------------------------- #
    bhavcopy_dir = cfg.raw_data_dir / "bhavcopy" / "extracted"
    bhavcopy_files = discover_bhavcopy_files(bhavcopy_dir)
    if limit_days is not None:
        bhavcopy_files = bhavcopy_files[-limit_days:]
        logger.info("Bounded run: using %d bhavcopy files", len(bhavcopy_files))
    logger.info("Found %d bhavcopy files to process", len(bhavcopy_files))

    # 3) Interest rates + earnings ---------------------------------------- #
    daily_interest_map = load_interest_rates(cfg.reference_data_dir / "ADJUSTED_MIFOR.csv")
    earnings_map = load_earnings_dates(cfg.reference_data_dir / "earning_dates.json")

    # 4) Result structure -------------------------------------------------- #
    result: dict[str, Any] = {"historical": {"scripts": {}}}
    for symbol in symbols:
        result["historical"]["scripts"][symbol] = {
            "exchange": "NSE",
            "segment": "Options",
            "timestamps": {},
        }

    # 5) Process bhavcopy files ------------------------------------------- #
    daily_ohlc_map: dict[str, list[dict[str, Any]]] = {sym: [] for sym in symbols}

    for file_path, date_obj in track(
        bhavcopy_files,
        "Enriching bhavcopy days",
        unit="days",
        label=lambda fd: fd[1].strftime("%d %b %Y"),
    ):
        try:
            date_key = date_obj.strftime("%d-%b-%Y")
            # debug, not info: the progress bar already names the current day, and
            # at 2500 days this line alone would be 2500 lines of noise.
            logger.debug("Processing %s => %s", file_path.name, date_key)

            fallback_rate = get_rate_with_fallback(date_obj.date(), daily_interest_map)
            r_decimal = (fallback_rate / 100.0) if fallback_rate else 0.0

            df = normalize_bhavcopy_columns(pd.read_csv(file_path))

            for symbol in symbols:
                symbol_data = df[df["SYMBOL"] == symbol].copy()
                if symbol_data.empty:
                    continue

                # Nearest-expiry FUT (or option) row provides the daily bar.
                futures_data = symbol_data[
                    symbol_data["INSTRUMENT"].isin(["FUTSTK", "FUTIDX", "OPTSTK", "OPTIDX"])
                ].copy()
                if futures_data.empty:
                    continue

                futures_data["EXPIRY_DT"] = pd.to_datetime(
                    futures_data["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce"
                ).fillna(datetime(2100, 1, 1))
                futures_data = futures_data.sort_values(by="EXPIRY_DT")
                fut_row = futures_data.iloc[0]

                daily_ohlc_map[symbol].append(
                    {
                        "Date": date_obj,
                        "Open": float(fut_row["OPEN"]),
                        "High": float(fut_row["HIGH"]),
                        "Low": float(fut_row["LOW"]),
                        "Close": float(fut_row["CLOSE"]),
                    }
                )

                spot_rec = spot_price_map.get(symbol, {}).get(date_key)
                if spot_rec is None:
                    continue
                # Yahoo supplies only the *adjusted* basis (for returns/labels);
                # the strike/IV basis has to come from the bhavcopy itself, or a
                # split turns the ATM strike into a number from the wrong region
                # of the chain. Must stay in step with storage.etl.
                spot_adj = spot_rec["adjusted"]
                if not np.isfinite(spot_adj):
                    continue
                fut_only = futures_data[
                    futures_data["INSTRUMENT"].isin(["FUTSTK", "FUTIDX"])
                ]
                picked_spot = unadjusted_spot_from_bhavcopy(
                    symbol_data, fut_only, date_key, r_decimal
                )
                if picked_spot is None:
                    continue
                spot_price = picked_spot[0]
                if not np.isfinite(spot_price) or spot_price <= 0:
                    continue

                timestamps = result["historical"]["scripts"][symbol]["timestamps"]
                if date_key not in timestamps:
                    timestamps[date_key] = {
                        "underlying_price": spot_price,
                        "underlying_price_adj": spot_adj,
                        "interest_rate": fallback_rate,
                        "upcoming_earning_date": None,
                        "expiry_30d": None,
                        "expiry_60d": None,
                        "expiry_90d": None,
                    }

                # Up to 3 future expiries for the day.
                all_fut_expiries = sorted(futures_data["EXPIRY_DT"].dropna().unique())
                if len(all_fut_expiries) >= 1:
                    timestamps[date_key]["expiry_30d"] = all_fut_expiries[0].strftime("%d-%b-%Y")
                if len(all_fut_expiries) >= 2:
                    timestamps[date_key]["expiry_60d"] = all_fut_expiries[1].strftime("%d-%b-%Y")
                if len(all_fut_expiries) >= 3:
                    timestamps[date_key]["expiry_90d"] = all_fut_expiries[2].strftime("%d-%b-%Y")

                # Option-chain snapshot.
                options_data = symbol_data[
                    symbol_data["INSTRUMENT"].isin(["OPTSTK", "OPTIDX"])
                ].copy()
                if options_data.empty:
                    continue
                options_data["EXPIRY_DT"] = pd.to_datetime(
                    options_data["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce"
                )
                timestamps[date_key]["option_chain"] = _build_chain_rows(
                    options_data, symbol, spot_price, date_key, r_decimal
                )

                # ATM strangle selection across the next 3 monthly expiries.
                chosen = pick_strike_nearest_underlying(spot_price, options_data)
                if chosen is None:
                    continue
                ce30, ce60, ce90, pe30, pe60, pe90, chosen_strike = chosen

                # Time to expiry from the FUT expiries (T=0 when too few).
                if len(all_fut_expiries) >= 3:
                    t_30 = get_time_to_expiry_in_years(
                        date_key, all_fut_expiries[0].strftime("%d-%b-%Y")
                    )
                    t_60 = get_time_to_expiry_in_years(
                        date_key, all_fut_expiries[1].strftime("%d-%b-%Y")
                    )
                    t_90 = get_time_to_expiry_in_years(
                        date_key, all_fut_expiries[2].strftime("%d-%b-%Y")
                    )
                else:
                    t_30 = t_60 = t_90 = 0.0

                ce_30d_price = float(ce30["SETTLE_PR"])
                pe_30d_price = float(pe30["SETTLE_PR"])

                ce_iv_30 = _iv_pct(ce_30d_price, spot_price, float(chosen_strike), t_30, r_decimal, True)
                pe_iv_30 = _iv_pct(pe_30d_price, spot_price, float(chosen_strike), t_30, r_decimal, False)
                ce_iv_60 = _iv_pct(float(ce60["SETTLE_PR"]), spot_price, float(chosen_strike), t_60, r_decimal, True)
                pe_iv_60 = _iv_pct(float(pe60["SETTLE_PR"]), spot_price, float(chosen_strike), t_60, r_decimal, False)
                ce_iv_90 = _iv_pct(float(ce90["SETTLE_PR"]), spot_price, float(chosen_strike), t_90, r_decimal, True)
                pe_iv_90 = _iv_pct(float(pe90["SETTLE_PR"]), spot_price, float(chosen_strike), t_90, r_decimal, False)

                ce_volume = int(options_data[options_data["OPTION_TYP"] == "CE"]["CONTRACTS"].sum())
                pe_volume = int(options_data[options_data["OPTION_TYP"] == "PE"]["CONTRACTS"].sum())

                if ce_iv_30 is not None and ce_iv_30 > 0 and t_30 > 0:
                    greeks_ce_30 = black_scholes_greeks(
                        spot_price,
                        float(chosen_strike),
                        t_30,
                        r_decimal,
                        ce_iv_30 / 100.0,
                        is_call=True,
                    )
                else:
                    greeks_ce_30 = {
                        "delta": 0.0,
                        "gamma": 0.0,
                        "theta": 0.0,
                        "vega": 0.0,
                        "rho": 0.0,
                    }

                if pe_iv_30 is not None and pe_iv_30 > 0 and t_30 > 0:
                    greeks_pe_30 = black_scholes_greeks(
                        spot_price,
                        float(chosen_strike),
                        t_30,
                        r_decimal,
                        pe_iv_30 / 100.0,
                        is_call=False,
                    )
                else:
                    greeks_pe_30 = {
                        "delta": 0.0,
                        "gamma": 0.0,
                        "theta": 0.0,
                        "vega": 0.0,
                        "rho": 0.0,
                    }

                timestamps[date_key].update(
                    {
                        "strike_price": float(chosen_strike),
                        "rv_yz": None,
                        "ce": {
                            "iv_30": ce_iv_30,
                            "iv_60": ce_iv_60,
                            "iv_90": ce_iv_90,
                            "volume": ce_volume,
                            "oi": _oi_int(ce30.get("OPEN_INT")),
                            "last_price_30d": ce_30d_price,
                            "close": float(ce30["CLOSE"]),
                            "open": float(ce30["OPEN"]),
                            "high": float(ce30["HIGH"]),
                            "low": float(ce30["LOW"]),
                            "ivp": None,
                            "ivr": None,
                            "greeks": greeks_ce_30,
                        },
                        "pe": {
                            "iv_30": pe_iv_30,
                            "iv_60": pe_iv_60,
                            "iv_90": pe_iv_90,
                            "volume": pe_volume,
                            "oi": _oi_int(pe30.get("OPEN_INT")),
                            "last_price_30d": pe_30d_price,
                            "close": float(pe30["CLOSE"]),
                            "open": float(pe30["OPEN"]),
                            "high": float(pe30["HIGH"]),
                            "low": float(pe30["LOW"]),
                            "ivp": None,
                            "ivr": None,
                            "greeks": greeks_pe_30,
                        },
                    }
                )

        except Exception as exc:  # noqa: BLE001 - keep the engine resilient per file
            logger.warning("Error processing %s: %s", file_path, exc)

    # 6) Rolling Yang-Zhang realized vol per symbol ------------------------ #
    for symbol in track(symbols, "Computing Yang-Zhang RV", unit="symbols", label=str):
        rows = daily_ohlc_map[symbol]
        if not rows:
            continue

        ohlc_df = pd.DataFrame(rows)
        yz_series = compute_yz_rolling_vol(
            ohlc_df, window=_YZ_WINDOW, max_lookback=_YZ_MAX_LOOKBACK, trading_periods=252
        )

        timestamps = result["historical"]["scripts"][symbol]["timestamps"]
        for date_idx_raw, yz_val in yz_series.items():
            date_idx: Any = date_idx_raw
            date_key = pd.Timestamp(date_idx).strftime("%d-%b-%Y")
            if date_key in timestamps:
                timestamps[date_key]["rv_yz"] = float(yz_val) if np.isfinite(yz_val) else None

    # 7) Rolling IV percentile & rank (CE + PE) ---------------------------- #
    for symbol in track(symbols, "Computing IV percentile/rank", unit="symbols", label=str):
        time_map = result["historical"]["scripts"][symbol]["timestamps"]
        if not time_map:
            continue

        for side in ("ce", "pe"):
            iv_rows: list[dict[str, Any]] = []
            for d_str, rec in time_map.items():
                side_iv = rec.get(side, {}).get("iv_30")
                if side_iv is None:
                    continue
                try:
                    dt_obj = datetime.strptime(d_str, "%d-%b-%Y")
                except ValueError:
                    continue
                iv_rows.append({"Date": dt_obj, "iv_30": side_iv})

            if not iv_rows:
                continue
            df_side = pd.DataFrame(iv_rows).dropna(subset=["iv_30"])
            df_side = df_side.sort_values("Date").reset_index(drop=True)
            df_side = compute_rolling_iv_percentile_and_rank(df_side, window_size=_IV_WINDOW)

            for i in range(len(df_side)):
                date_val: Any = df_side.loc[i, "Date"]
                pctl: Any = df_side.loc[i, "ivp"]
                rnk: Any = df_side.loc[i, "ivr"]
                d_str = pd.Timestamp(date_val).strftime("%d-%b-%Y")
                time_map[d_str][side]["ivp"] = (float(pctl) * 100.0) if pctl is not None else None
                time_map[d_str][side]["ivr"] = (float(rnk) * 100.0) if rnk is not None else None

    # 8) Sort timestamps by date + NaN/Inf scrub --------------------------- #
    for symbol in track(symbols, "Sorting + scrubbing", unit="symbols", label=str):
        time_map = result["historical"]["scripts"][symbol]["timestamps"]
        if not time_map:
            continue

        temp_list: list[tuple[datetime, str, dict[str, Any]]] = []
        for d_str, rec in time_map.items():
            try:
                dt_obj = datetime.strptime(d_str, "%d-%b-%Y")
            except ValueError:
                continue
            temp_list.append((dt_obj, d_str, rec))

        temp_list.sort(key=lambda item: item[0])
        result["historical"]["scripts"][symbol]["timestamps"] = {
            d_str: rec for _, d_str, rec in temp_list
        }

    replace_nan_inf(result)

    # 9) Upcoming earnings dates ------------------------------------------- #
    for symbol in symbols:
        time_map = result["historical"]["scripts"][symbol]["timestamps"]
        for d_str in time_map:
            upcoming = get_upcoming_earning_date(symbol, d_str, earnings_map)
            if upcoming is not None:
                time_map[d_str]["upcoming_earning_date"] = upcoming

    # 10) Interpolate missing RV ------------------------------------------- #
    result = fill_missing_rv_with_interpolation(result)

    # 11) Write processed JSON ---------------------------------------------- #
    cfg.processed_data_dir.mkdir(parents=True, exist_ok=True)
    output_file = cfg.processed_data_dir / OUTPUT_FILENAME
    with output_file.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    logger.info("Processed data saved to %s", output_file)
    return output_file
