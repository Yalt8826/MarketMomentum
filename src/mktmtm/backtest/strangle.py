"""Short-strangle + weekly delta-hedge backtester.

Ported from the athena-verified `backtest/backtester.py` (2026-08-16).

Strategy per month: on the first trading day, sell the ~20-delta call and
put of the 30-day expiry, then once a week close the cheaper leg and
re-sell it near the price of the other leg (delta-hedge adjustment),
finally buying back both legs on the exit day (last trading day on or
before the index-Tuesday / stock-Wednesday before expiry).

Compared to the legacy script:

* every connection parameter comes from pydantic ``Settings`` (via
  ``mktmtm.storage.db.build_dsn``), never bare ``PG_*`` env vars;
* the symbol catalog is read through ``mktmtm.data.symbols`` instead of a
  filesystem JSON path;
* ``find_exit_day`` takes a ``Callable[[date], bool]`` trading-day check
  instead of a live cursor, so its logic is unit-testable without a DB
  (``run_backtest_month`` passes the trading-day map it already built).

DB reads use a small module-level pool (min 1 / max 2 connections) with
bounded retry, mirroring the legacy ``_get_conn`` behaviour.
"""

from __future__ import annotations

import calendar
import logging
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import date, datetime, timedelta
from typing import Any

import psycopg2
from psycopg2 import InterfaceError, OperationalError, pool
from psycopg2.extras import Json

from mktmtm.backtest.economics import (
    BROKERAGE_SCENARIOS,
    CostRates,
    MarginModel,
    price_strangle_trade,
    rate_sets,
)
from mktmtm.config.settings import Settings, settings
from mktmtm.data.lots import load_lot_sizes
from mktmtm.data.symbols import load_fno_catalog
from mktmtm.storage.db import build_dsn

logger = logging.getLogger(__name__)

__all__ = [
    "find_exit_day",
    "find_option_near_price",
    "find_options_near_delta",
    "find_weekly_hedge_dates",
    "get_metrics_for_day",
    "chain_is_degenerate",
    "get_option_data",
    "get_trading_days_in_range",
    "insert_backtest_result",
    "is_index",
    "load_symbols_info",
    "price_result",
    "main",
    "resolve_scope",
    "run_backtest",
    "run_backtest_month",
]

# Tolerate flaky reads; the bulk writer (storage/db) retries more.
MAX_RETRY = 3

# Column name carrying PNL from leg adjustments (see backtest_results.sql).
_HEDGE_PNL_KEY = "hedge_pnl_points"

_POOL: pool.SimpleConnectionPool | None = None
_POOL_CFG: Settings | None = None


# --- DB connection (adapted from storage/db, min/max 2 for reads) -----------

def _get_pool(cfg: Settings) -> pool.SimpleConnectionPool:
    """Lazily build the module-level read pool, keyed by the Settings instance."""
    global _POOL, _POOL_CFG
    if _POOL is None or cfg is not _POOL_CFG:
        if _POOL is not None:
            _POOL.closeall()
        _POOL = psycopg2.pool.SimpleConnectionPool(
            1,
            2,
            dsn=build_dsn(cfg),
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=10,
            keepalives_interval=5,
            keepalives_count=3,
        )
        _POOL_CFG = cfg
    assert _POOL is not None
    return _POOL


def close_backtest_pool() -> None:
    """Close and drop the module-level read pool (idempotent)."""
    global _POOL, _POOL_CFG
    if _POOL is not None:
        _POOL.closeall()
        _POOL = None
        _POOL_CFG = None


@contextmanager
def _get_conn(cfg: Settings) -> Iterator[Any]:
    """Yield a pooled connection, retrying transient failures with backoff."""
    conn: Any = None
    tries, wait = 0, 1
    while True:
        try:
            conn = _get_pool(cfg).getconn()
            yield conn
            break
        except (OperationalError, InterfaceError) as exc:
            if conn is not None:  # drop the poisoned connection
                with suppress(Exception):  # pool teardown must not mask
                    _get_pool(cfg).putconn(conn, close=True)
                conn = None
            tries += 1
            if tries > MAX_RETRY:
                logger.error("DB connection failed after %d retries: %s", MAX_RETRY, exc)
                raise
            logger.warning("DB connection failed (%s) - retry %d in %ss", exc, tries, wait)
            time.sleep(wait)
            wait = min(wait * 2, 10)
        finally:
            if conn is not None and not conn.closed:
                with suppress(Exception):  # pool teardown must not mask
                    _get_pool(cfg).putconn(conn)


# --- Symbol catalog ---------------------------------------------------------

def load_symbols_info() -> tuple[list[str], set[str]]:
    """Load all F&O symbols and the index-symbol subset from the shipped catalog.

    Returns
    -------
    (all_symbols, index_symbols): sorted list of every catalog symbol and the
    set of index futures symbols, mirroring the legacy helper.
    """
    catalog = load_fno_catalog()
    index_symbols = {
        item["symbol"] for item in catalog.get("index_futures", []) if item.get("symbol")
    }
    stock_symbols = {
        item["symbol"] for item in catalog.get("individual_securities", []) if item.get("symbol")
    }
    all_symbols = sorted(index_symbols | stock_symbols)
    logger.info(
        "Loaded %d symbols (%d indices). Example: %s",
        len(all_symbols),
        len(index_symbols),
        all_symbols[0] if all_symbols else None,
    )
    return all_symbols, index_symbols


def is_index(symbol: str, index_symbols: set[str]) -> bool:
    """Whether ``symbol`` is an index (vs an individual security)."""
    return symbol in index_symbols


# --- DB reads ---------------------------------------------------------------

def get_trading_days_in_range(
    cursor: Any,
    symbol: str,
    start_date: date,
    end_date: date,
) -> list[date]:
    """Fetch all trading days with data for a symbol within a date range."""
    query = """
        SELECT date FROM option_metrics
        WHERE symbol = %s AND date >= %s AND date <= %s
        ORDER BY date ASC;
    """
    try:
        cursor.execute(query, (symbol, start_date, end_date))
        return [row[0] for row in cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001 - legacy behaviour: degrade to empty
        logger.error(
            "Error fetching trading days for %s between %s and %s: %s",
            symbol,
            start_date,
            end_date,
            exc,
        )
        return []


def get_metrics_for_day(cursor: Any, symbol: str, day: date) -> dict[str, Any] | None:
    """Fetch the full option_metrics record for a specific symbol and day."""
    query = """
        SELECT underlying_price, interest_rate, expiry_30d, upcoming_earning_date, option_chain
        FROM option_metrics
        WHERE symbol = %s AND date = %s;
    """
    try:
        cursor.execute(query, (symbol, day))
        row = cursor.fetchone()
        if row:
            expiry_30d = row[2]
            upcoming_earning_date = row[3]
            underlying_price = float(row[0]) if row[0] is not None else None
            return {
                "underlying_price": underlying_price,
                "interest_rate": row[1],
                "expiry_30d": expiry_30d,
                "upcoming_earning_date": upcoming_earning_date,
                "option_chain": row[4],  # already parsed as list/dict by psycopg2
            }
        return None
    except Exception as exc:  # noqa: BLE001 - legacy behaviour: degrade to None
        logger.error("Error fetching metrics for %s on %s: %s", symbol, day, exc)
        return None


# --- Pure chain helpers -----------------------------------------------------

def find_options_near_delta(
    option_chain: list[dict[str, Any]],
    expiry_date: date,
    target_delta: float,
    option_type: str,
    *,
    require_liquidity: bool = True,
    spot: float | None = None,
) -> dict[str, Any] | None:
    """Find the option (CE or PE) closest to the target delta for a given expiry.

    Only options with a finite delta, numeric strike and numeric settle are
    eligible; the closest by absolute delta difference wins.

    ``require_liquidity`` additionally demands the contract actually traded that
    day (:func:`_is_tradeable`). A 20-delta strike that never traded cannot be
    sold at its settlement mark, and for deep strikes that mark is a placeholder
    rather than a price.
    """
    if not option_chain or not expiry_date:
        return None

    target_expiry_str = expiry_date.strftime("%d-%b-%Y")
    relevant_options: list[dict[str, Any]] = []
    for opt in option_chain:
        delta = opt.get("delta")
        strike = opt.get("strike")
        settle = opt.get("settle")
        if (
            opt.get("type") == option_type
            and opt.get("expiry") == target_expiry_str
            and isinstance(delta, (int, float))
            and math.isfinite(delta)
            and isinstance(strike, (int, float))
            and isinstance(settle, (int, float))
            and (not require_liquidity or _is_tradeable(opt))
            and _is_plausible_price(opt, spot)
        ):
            relevant_options.append(opt)

    if not relevant_options:
        return None

    return min(relevant_options, key=lambda opt: abs(float(opt["delta"]) - target_delta))


def get_option_data(
    option_chain: list[dict[str, Any]],
    expiry_date: date,
    strike_price: float,
    option_type: str,
    *,
    spot: float | None = None,
) -> dict[str, Any] | None:
    """Extract the specific option from the chain by expiry, strike and type.

    With ``spot`` supplied, a row violating the no-arbitrage bounds is treated
    as absent rather than returned — an exit priced off a corrupt mark is a
    fabricated P&L, and a missing exit is at least visible as a skip.
    """
    if not option_chain or not expiry_date or strike_price is None:
        return None
    target_expiry_str = expiry_date.strftime("%d-%b-%Y")
    for opt in option_chain:
        settle = opt.get("settle")
        delta = opt.get("delta")
        if (
            opt.get("type") == option_type
            and opt.get("expiry") == target_expiry_str
            and opt.get("strike") == strike_price
            and isinstance(settle, (int, float))
            and math.isfinite(settle)
            and isinstance(delta, (int, float))
            and math.isfinite(delta)
            and _is_plausible_price(opt, spot)
        ):
            return opt
    return None


def _is_tradeable(opt: dict[str, Any]) -> bool:
    """Did this contract actually trade today?

    NSE assigns a settlement price to every listed strike, including ones that
    never traded — and for deep in-the-money strikes that price is a flat
    placeholder rather than a valuation. Measured on NMDC 2022-10-25: every call
    from strike 72.5 to 100 carried an identical settle of 130.75 against a spot
    of 130.71, all with ``volume = 0``, ``oi = 0`` and ``delta = 0.0``.

    Selecting one of those is not optimistic pricing, it is fiction: the
    backtester sold a "130.75" call and marked it at the same placeholder later,
    producing losses of up to 16x the margin posted. 32 of 7,683 trades (0.4%)
    were contaminated this way, for -Rs 5.7M.

    So selection requires evidence of a real transaction. Open interest alone is
    not enough — a position can exist without a price being struck today.
    """
    volume = opt.get("volume")
    return isinstance(volume, (int, float)) and math.isfinite(volume) and volume > 0


#: An expiry with fewer distinct settle values than this is not a price surface.
_MIN_DISTINCT_SETTLES = 2

#: Bhavcopy's expiry format, shared by every chain lookup here.
_EXPIRY_FMT = "%d-%b-%Y"


def chain_is_degenerate(
    option_chain: list[dict[str, Any]], expiry_date: date | None = None
) -> bool:
    """Does one expiry carry a single price for every contract in it?

    **The defect.** On NMDC 2022-10-25 every element of the chain — 206 rows
    across strikes 72.5 to 157.5 — carried an identical settle of 130.75 against
    a spot of 130.71. The underlying's price had been written into every
    option's premium, and every delta was 0.0 because the IV solver cannot
    converge on prices that make no sense.

    **Why the test is per expiry.** IOC 2022-06-29 is the same defect in
    partial form: 146 of 436 rows carry the spot price, and they are exactly the
    contracts of the expiring 30-Jun series, while July and August are priced
    normally. A whole-chain test sees 193 distinct settles and passes it.
    Checking the expiry actually being traded catches both shapes.

    It is rare and it is not random: **47 of 213,379 symbol-days (0.022%)**, and
    **39 of those 47 (83%) fall on an expiry date**. That pattern is the
    diagnosis — on expiry day the expiring series settles against the
    underlying, and for these rows that settlement value has been written into
    every contract's premium. The remaining 8 are scattered singletons
    (NMDC 2022-10-25, RELIANCE 2023-07-19, ITC 2025-01-03, …).

    Neither obvious per-contract filter catches it. Volume does not: affected
    strikes include ones with 2,492 lots traded. The no-arbitrage bound does not
    either, because the corrupt value sits at 99.97% of spot — inside any
    tolerance loose enough for a legitimate deep in-the-money call. What
    identifies it is the degeneracy itself.

    A month touching such an expiry is skipped rather than priced: one such
    trade lost 14x the margin posted, and a handful of fabricated catastrophes
    distorts exactly the tail statistics that decide whether a short-strangle
    book is usable.
    """
    if not option_chain:
        return False
    rows = option_chain
    if expiry_date is not None:
        target = expiry_date.strftime(_EXPIRY_FMT)
        rows = [opt for opt in option_chain if opt.get("expiry") == target]
    if len(rows) < 10:
        return False
    settles = {
        opt.get("settle")
        for opt in rows
        if isinstance(opt.get("settle"), (int, float))
    }
    return len(settles) < _MIN_DISTINCT_SETTLES


#: Tolerance on the no-arbitrage price bounds. ``settle`` is an official mark
#: rather than a transacted price, so a small overshoot is legitimate; 5% is far
#: below the scale of the defect this catches (a call marked at 100% of spot).
_PRICE_BOUND_TOLERANCE = 0.05


def _is_plausible_price(opt: dict[str, Any], spot: float | None) -> bool:
    """Does this contract's settle respect the no-arbitrage bounds?

    A call can never be worth more than the underlying, and a put never more
    than its strike. Violating either is not an aggressive quote, it is a
    corrupt row.

    **The defect this catches.** On NMDC 2022-10-25 *every* element of the
    chain carried an identical settle of 130.75 against a spot of 130.71 — the
    underlying's price written into every option's premium — with every delta
    at 0.0 because the IV solver could not converge on prices that make no
    sense. Heavily traded strikes were affected too (CE 135 had 2,492 lots
    traded), so a volume filter alone does not catch it.

    Selling a "130.75" call struck at 137.5 and marking it at the same
    placeholder later produced a loss of 16x the margin posted. Such days are
    rare — 2 of 2,964 symbol-days sampled in October 2022 — but a handful of
    fabricated catastrophes is enough to move the tail statistics that decide
    whether this strategy is usable.

    Returns ``True`` when ``spot`` is unknown: this is a corruption filter, not
    a valuation model, and it must not silently reject good data.
    """
    if spot is None or not math.isfinite(spot) or spot <= 0:
        return True
    settle = opt.get("settle")
    strike = opt.get("strike")
    if not isinstance(settle, (int, float)) or not math.isfinite(settle):
        return False
    if not isinstance(strike, (int, float)) or not math.isfinite(strike):
        return False
    ceiling = spot if opt.get("type") == "CE" else strike
    return settle <= ceiling * (1.0 + _PRICE_BOUND_TOLERANCE)


def find_option_near_price(
    option_chain: list[dict[str, Any]],
    expiry_date: date,
    target_price: float,
    option_type: str,
    *,
    require_liquidity: bool = True,
    spot: float | None = None,
) -> dict[str, Any] | None:
    """Find the option (CE or PE) whose settle price is closest to a target.

    ``require_liquidity`` keeps untraded strikes out of the search — see
    :func:`_is_tradeable` for why that is not optional in practice.
    """
    if (
        not option_chain
        or not expiry_date
        or target_price is None
        or not math.isfinite(target_price)
    ):
        return None

    target_expiry_str = expiry_date.strftime("%d-%b-%Y")
    relevant_options: list[dict[str, Any]] = []
    for opt in option_chain:
        settle = opt.get("settle")
        strike = opt.get("strike")
        if (
            opt.get("type") == option_type
            and opt.get("expiry") == target_expiry_str
            and isinstance(settle, (int, float))
            and math.isfinite(settle)
            and settle > 0
            and isinstance(strike, (int, float))
            and (not require_liquidity or _is_tradeable(opt))
            and _is_plausible_price(opt, spot)
        ):
            relevant_options.append(opt)

    if not relevant_options:
        return None

    return min(relevant_options, key=lambda opt: abs(float(opt["settle"]) - target_price))


# --- Exit day + hedge dates -------------------------------------------------

def find_exit_day(
    expiry_date: date,
    is_index_flag: bool,
    has_trading_day: Callable[[date], bool],
) -> date | None:
    """Find the last trading day before expiry on or before the target weekday.

    Target is Tuesday (1) for indices and Wednesday (2) for stocks. Walks
    backwards from the day before expiry (up to 20 days) and returns the first
    trading day whose weekday is not after the target — i.e. the last
    Tuesday/Wednesday with data, falling back to an earlier trading day of the
    same week if the target weekday itself has no data.
    """
    target_weekday = 1 if is_index_flag else 2  # 1 = Tuesday, 2 = Wednesday
    current_day = expiry_date - timedelta(days=1)
    for _ in range(14):
        if has_trading_day(current_day):
            if current_day.weekday() == target_weekday:
                return current_day
            if current_day.weekday() < target_weekday:
                return current_day
        current_day -= timedelta(days=1)
        if current_day < expiry_date - timedelta(days=20):
            break
    logger.warning("Could not find suitable exit day before expiry %s", expiry_date)
    return None


def find_weekly_hedge_dates(
    entry_date: date,
    exit_date: date,
    trading_days_map: dict[date, bool],
) -> list[date]:
    """Find the last trading day of each week between entry and exit dates.

    Weeks roll on Fridays: for each Friday in (entry, exit], the most recent
    trading day on or before it is a hedge date, as long as it falls strictly
    after the entry date. Results are unique and sorted ascending.
    """
    hedge_dates: list[date] = []
    current_date = entry_date + timedelta(days=(4 - entry_date.weekday() + 7) % 7)
    if current_date <= entry_date:  # entry on Fri/Sat/Sun -> next week's Friday
        current_date += timedelta(days=7)

    while current_date <= exit_date:
        hedge_day_candidate = current_date
        while hedge_day_candidate >= entry_date:
            if trading_days_map.get(hedge_day_candidate, False):
                if entry_date < hedge_day_candidate <= exit_date and (
                    not hedge_dates or hedge_day_candidate != hedge_dates[-1]
                ):
                    hedge_dates.append(hedge_day_candidate)
                break
            hedge_day_candidate -= timedelta(days=1)
        current_date += timedelta(days=7)

    return sorted(set(hedge_dates))


# --- Result persistence -----------------------------------------------------

def insert_backtest_result(cursor: Any, result_data: dict[str, Any]) -> None:
    """Insert or upsert a single backtest result row, handling conflicts."""
    defaults: dict[str, Any] = {
        "entry_date": None,
        "entry_credit": None,
        "exit_cost": None,
        "pnl_points": None,
        "skipped_reason": None,
        "call_entry_strike": None,
        "put_entry_strike": None,
        "call_entry_delta": None,
        "put_entry_delta": None,
        "call_entry_price": None,
        "put_entry_price": None,
        "call_exit_price": None,
        "put_exit_price": None,
        "exit_date": None,
        _HEDGE_PNL_KEY: None,
        "variant": "base",
        "stopped_out": False,
        "cost_breakdown": None,
        "put_entry_volume": None,
        "call_entry_volume": None,
        "lot_size": None,
        "n_orders": None,
        "n_adjustments": None,
        "entry_spot": None,
        "gross_pnl_rupees": None,
        "costs_rupees": None,
        "slippage_rupees": None,
        "net_pnl_rupees": None,
        "margin_rupees": None,
        "return_on_margin": None,
        "rates_effective_from": None,
        "slippage_fraction": None,
        "margin_pct": None,
    }
    for key, default_val in defaults.items():
        result_data.setdefault(key, default_val)

    numeric_fields = [
        "entry_credit",
        "exit_cost",
        "pnl_points",
        "call_entry_strike",
        "put_entry_strike",
        "call_entry_delta",
        "put_entry_delta",
        "call_entry_price",
        "put_entry_price",
        "call_exit_price",
        "put_exit_price",
        _HEDGE_PNL_KEY,
        "lot_size",
        "n_orders",
        "n_adjustments",
        "entry_spot",
        "gross_pnl_rupees",
        "costs_rupees",
        "slippage_rupees",
        "net_pnl_rupees",
        "margin_rupees",
        "return_on_margin",
        "slippage_fraction",
        "margin_pct",
        "call_entry_volume",
        "put_entry_volume",
    ]
    for field in numeric_fields:
        value = result_data.get(field)
        if isinstance(value, (int, float)) and not math.isfinite(value):
            result_data[field] = None

    query = """
    INSERT INTO backtest_results (
        symbol, entry_date, exit_date, year, month,
        entry_credit, exit_cost, pnl_points, skipped_reason,
        call_entry_strike, put_entry_strike, call_entry_delta, put_entry_delta,
        call_entry_price, put_entry_price, call_exit_price, put_exit_price,
        hedge_pnl_points, -- Stores PNL from leg adjustments
        variant, stopped_out, call_entry_volume, put_entry_volume, lot_size, n_orders, n_adjustments, entry_spot, gross_pnl_rupees, costs_rupees, slippage_rupees, net_pnl_rupees, margin_rupees, return_on_margin, rates_effective_from, slippage_fraction, margin_pct, cost_breakdown
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (variant, symbol, year, month) DO UPDATE SET
        entry_date = EXCLUDED.entry_date,
        exit_date = EXCLUDED.exit_date,
        entry_credit = EXCLUDED.entry_credit,
        exit_cost = EXCLUDED.exit_cost,
        pnl_points = EXCLUDED.pnl_points,
        skipped_reason = EXCLUDED.skipped_reason,
        call_entry_strike = EXCLUDED.call_entry_strike,
        put_entry_strike = EXCLUDED.put_entry_strike,
        call_entry_delta = EXCLUDED.call_entry_delta,
        put_entry_delta = EXCLUDED.put_entry_delta,
        call_entry_price = EXCLUDED.call_entry_price,
        put_entry_price = EXCLUDED.put_entry_price,
        call_exit_price = EXCLUDED.call_exit_price,
        put_exit_price = EXCLUDED.put_exit_price,
        hedge_pnl_points = EXCLUDED.hedge_pnl_points,
        stopped_out = EXCLUDED.stopped_out,
        call_entry_volume = EXCLUDED.call_entry_volume,
        put_entry_volume = EXCLUDED.put_entry_volume,
        lot_size = EXCLUDED.lot_size,
        n_orders = EXCLUDED.n_orders,
        n_adjustments = EXCLUDED.n_adjustments,
        entry_spot = EXCLUDED.entry_spot,
        gross_pnl_rupees = EXCLUDED.gross_pnl_rupees,
        costs_rupees = EXCLUDED.costs_rupees,
        slippage_rupees = EXCLUDED.slippage_rupees,
        net_pnl_rupees = EXCLUDED.net_pnl_rupees,
        margin_rupees = EXCLUDED.margin_rupees,
        return_on_margin = EXCLUDED.return_on_margin,
        rates_effective_from = EXCLUDED.rates_effective_from,
        slippage_fraction = EXCLUDED.slippage_fraction,
        margin_pct = EXCLUDED.margin_pct,
        cost_breakdown = EXCLUDED.cost_breakdown;
    """
    params = (
        result_data["symbol"],
        result_data["entry_date"],
        result_data["exit_date"],
        result_data["year"],
        result_data["month"],
        result_data["entry_credit"],
        result_data["exit_cost"],
        result_data["pnl_points"],
        result_data["skipped_reason"],
        result_data["call_entry_strike"],
        result_data["put_entry_strike"],
        result_data["call_entry_delta"],
        result_data["put_entry_delta"],
        result_data["call_entry_price"],
        result_data["put_entry_price"],
        result_data["call_exit_price"],
        result_data["put_exit_price"],
        result_data[_HEDGE_PNL_KEY],
        result_data["variant"],
        result_data["stopped_out"],
        result_data["call_entry_volume"],
        result_data["put_entry_volume"],
        result_data["lot_size"],
        result_data["n_orders"],
        result_data["n_adjustments"],
        result_data["entry_spot"],
        result_data["gross_pnl_rupees"],
        result_data["costs_rupees"],
        result_data["slippage_rupees"],
        result_data["net_pnl_rupees"],
        result_data["margin_rupees"],
        result_data["return_on_margin"],
        result_data["rates_effective_from"],
        result_data["slippage_fraction"],
        result_data["margin_pct"],
        Json(result_data["cost_breakdown"])
        if result_data.get("cost_breakdown") is not None
        else None,
    )
    try:
        cursor.execute(query, params)
    except Exception as exc:  # noqa: BLE001 - legacy behaviour: log, keep going
        logger.error(
            "Failed to insert/update result for %s %s-%s: %s",
            result_data.get("symbol"),
            result_data.get("year"),
            result_data.get("month"),
            exc,
        )


# --- Per-month strategy -----------------------------------------------------

def price_result(
    result: dict[str, Any],
    *,
    lot_size: int | None,
    entry_spot: float | None,
    variant: str = "base",
    slippage_fraction: float = 0.0,
    margin_model: MarginModel | None = None,
    rates: tuple[CostRates, ...] | None = None,
) -> dict[str, Any]:
    """Attach rupee economics to a completed month, in place.

    A month without a resolvable lot size or entry spot is left **unpriced**
    (economics columns stay NULL) and gains a ``pricing_skipped`` reason rather
    than a defaulted lot. Guessing a lot size silently mis-scales that trade's
    entire P&L, which is the class of failure this project keeps meeting.
    """
    result["variant"] = variant
    if result.get("pnl_points") is None:
        return result
    if lot_size is None or lot_size <= 0:
        result["pricing_skipped"] = "No Lot Size"
        return result
    if entry_spot is None or entry_spot <= 0:
        result["pricing_skipped"] = "No Entry Spot"
        return result

    econ = price_strangle_trade(
        entry_call=result["call_entry_price"],
        entry_put=result["put_entry_price"],
        exit_call=result["call_exit_price"],
        exit_put=result["put_exit_price"],
        adjustments=list(result.get("adjustment_legs") or []),
        lot_size=lot_size,
        entry_spot=entry_spot,
        entry_date=result["entry_date"],
        slippage_fraction=slippage_fraction,
        margin_model=margin_model or MarginModel(),
        rate_sets=rates or rate_sets(),
    )
    result.update(econ.as_row())
    return result


def run_backtest_month(
    symbol: str,
    year: int,
    month: int,
    index_symbols: set[str],
    cursor: Any,
    *,
    lot_schedule: Any | None = None,
    variant: str = "base",
    slippage_fraction: float = 0.0,
    margin_model: MarginModel | None = None,
    rates: tuple[CostRates, ...] | None = None,
    stop_loss_multiple: float | None = None,
) -> dict[str, Any]:
    """Run the short-strangle strategy for a single symbol/month and persist it.

    Returns the result dict (with ``skipped_reason`` set when the month could
    not be traded), mirroring the legacy flow.
    """
    logger.info("--- Running backtest for %s %s-%02d ---", symbol, year, month)
    # Set here, not in price_result: a skipped month never reaches pricing, and
    # would otherwise be written under the default variant — silently merging
    # every variant's skip taxonomy into the base run's.
    result: dict[str, Any] = {
        "symbol": symbol, "year": year, "month": month, "variant": variant,
    }

    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    # Extend the range so exit-day / hedge lookups near month end work.
    extended_end_date = month_end + timedelta(days=45)
    all_trading_days_full_range = get_trading_days_in_range(
        cursor, symbol, month_start, extended_end_date
    )
    trading_days_map = {day: True for day in all_trading_days_full_range}

    all_trading_days_in_month = [
        d for d in all_trading_days_full_range if d.year == year and d.month == month
    ]

    if not all_trading_days_in_month:
        logger.warning("No trading days found for %s %s-%s. Skipping.", symbol, year, month)
        result["skipped_reason"] = "No Trading Days"
        insert_backtest_result(cursor, result)
        return result

    entry_date = all_trading_days_in_month[0]
    result["entry_date"] = entry_date
    logger.info("Entry Date: %s", entry_date)

    entry_metrics = get_metrics_for_day(cursor, symbol, entry_date)
    if (
        not entry_metrics
        or not entry_metrics.get("option_chain")
        or not entry_metrics.get("expiry_30d")
        or entry_metrics.get("underlying_price") is None
    ):
        logger.warning(
            "Missing entry metrics/chain/expiry/price for %s on %s. Skipping.",
            symbol,
            entry_date,
        )
        result["skipped_reason"] = "Missing Entry Data"
        insert_backtest_result(cursor, result)
        return result

    expiry_30d = entry_metrics["expiry_30d"]
    upcoming_earning_date = entry_metrics.get("upcoming_earning_date")
    if upcoming_earning_date and entry_date <= upcoming_earning_date <= expiry_30d:
        logger.info(
            "Earnings conflict: Earning date %s between %s and %s. Skipping.",
            upcoming_earning_date,
            entry_date,
            expiry_30d,
        )
        result["skipped_reason"] = f"Earnings on {upcoming_earning_date}"
        insert_backtest_result(cursor, result)
        return result

    entry_option_chain = entry_metrics["option_chain"]
    if chain_is_degenerate(entry_option_chain, expiry_30d):
        logger.warning(
            "Degenerate chain for %s on %s (one settle across every contract); "
            "skipping rather than pricing a fabricated trade.",
            symbol,
            entry_date,
        )
        result["skipped_reason"] = "Degenerate Chain"
        insert_backtest_result(cursor, result)
        return result
    entry_spot = entry_metrics.get("underlying_price")
    call_option = find_options_near_delta(
        entry_option_chain, expiry_30d, 0.20, "CE", spot=entry_spot
    )
    put_option = find_options_near_delta(
        entry_option_chain, expiry_30d, -0.20, "PE", spot=entry_spot
    )

    if not call_option or not put_option:
        logger.warning(
            "Could not find suitable CE/PE options near 20 delta for %s on %s. Skipping.",
            symbol,
            entry_date,
        )
        result["skipped_reason"] = "No Suitable Options"
        insert_backtest_result(cursor, result)
        return result

    # Initial entry details
    result["call_entry_strike"] = call_option["strike"]
    result["put_entry_strike"] = put_option["strike"]
    result["call_entry_delta"] = call_option["delta"]
    result["put_entry_delta"] = put_option["delta"]
    result["call_entry_price"] = call_option["settle"]
    result["put_entry_price"] = put_option["settle"]
    result["entry_credit"] = call_option["settle"] + put_option["settle"]
    result["entry_spot"] = entry_spot
    # Gate 0 disclosure: a 20-delta strike that never traded makes the
    # settlement-price fill assumption fiction, regardless of the slippage
    # parameter. Recorded per leg so the share can be reported, not guessed.
    result["call_entry_volume"] = call_option.get("volume")
    result["put_entry_volume"] = put_option.get("volume")

    # Track current state of the strangle legs
    current_call_strike = result["call_entry_strike"]
    current_put_strike = result["put_entry_strike"]
    current_call_sold_price = result["call_entry_price"]
    current_put_sold_price = result["put_entry_price"]
    total_adjustment_pnl = 0.0
    # (bought_back_premium, resold_premium) per adjustment. Each pair is TWO
    # orders, which is what the cost model charges for.
    adjustment_legs: list[tuple[float, float]] = []
    # A stop is expressed as a multiple of the credit collected at entry — the
    # natural unit, since that is what the trade was paid to take the risk.
    initial_credit = result["entry_credit"]
    stop_threshold = (
        -stop_loss_multiple * initial_credit if stop_loss_multiple is not None else None
    )
    stopped_out = False

    logger.info(
        "Entry: Sell CE %s @ %.2f (Δ %.2f), Sell PE %s @ %.2f (Δ %.2f). Credit: %.2f",
        current_call_strike,
        current_call_sold_price,
        result["call_entry_delta"],
        current_put_strike,
        current_put_sold_price,
        result["put_entry_delta"],
        result["entry_credit"],
    )

    # Determine exit date (index -> Tuesday, stock -> Wednesday before expiry)
    exit_date = find_exit_day(expiry_30d, is_index(symbol, index_symbols), lambda d: d in trading_days_map)
    if not exit_date or exit_date <= entry_date:
        logger.warning(
            "Could not determine valid exit date before expiry %s for %s. Skipping.",
            expiry_30d,
            symbol,
        )
        result["skipped_reason"] = "No Valid Exit Date"
        insert_backtest_result(cursor, result)
        return result
    result["exit_date"] = exit_date
    logger.info("Target Exit Date: %s (Expiry: %s)", exit_date, expiry_30d)

    # Weekly adjustment dates
    hedge_dates = find_weekly_hedge_dates(entry_date, exit_date, trading_days_map)
    logger.info("Adjustment Dates: %s", hedge_dates)

    # --- Leg Adjustment Loop ---
    for hedge_date in hedge_dates:
        logger.debug("--- Adjustment Check: %s ---", hedge_date)
        hedge_metrics = get_metrics_for_day(cursor, symbol, hedge_date)
        if (
            not hedge_metrics
            or not hedge_metrics.get("option_chain")
            or hedge_metrics.get("underlying_price") is None
        ):
            logger.warning(
                "Missing metrics/chain/price for %s on adjustment date %s. Skipping adjustment.",
                symbol,
                hedge_date,
            )
            continue

        hedge_option_chain = hedge_metrics["option_chain"]
        if chain_is_degenerate(hedge_option_chain, expiry_30d):
            logger.warning(
                "Degenerate chain for %s on adjustment date %s; skipping this "
                "adjustment rather than re-selling against a fabricated price.",
                symbol,
                hedge_date,
            )
            continue

        # Current data for the open legs
        hedge_spot = hedge_metrics.get("underlying_price")
        call_data_hedge = get_option_data(
            hedge_option_chain, expiry_30d, current_call_strike, "CE", spot=hedge_spot
        )
        put_data_hedge = get_option_data(
            hedge_option_chain, expiry_30d, current_put_strike, "PE", spot=hedge_spot
        )

        if not call_data_hedge or not put_data_hedge:
            logger.warning(
                "Could not find current option data for adjustment on %s. Skipping adjustment.",
                hedge_date,
            )
            continue

        current_call_price = call_data_hedge["settle"]
        current_put_price = put_data_hedge["settle"]

        # --- Stop loss, checked BEFORE adjusting ---------------------------
        #
        # Mark to market: everything banked so far, plus what each open leg was
        # sold for minus what it would cost to buy back now.
        #
        # Checked before the adjustment because the adjustment re-sells into a
        # losing position — doubling down on exactly the move that is hurting.
        # A stop that fires only afterwards has already taken the risk it exists
        # to avoid.
        if stop_threshold is not None:
            unrealised = (
                total_adjustment_pnl
                + (current_call_sold_price - current_call_price)
                + (current_put_sold_price - current_put_price)
            )
            if unrealised <= stop_threshold:
                logger.info(
                    "Stop hit for %s on %s: unrealised %.2f <= %.2f (%.1fx the "
                    "%.2f entry credit). Closing both legs.",
                    symbol, hedge_date, unrealised, stop_threshold,
                    stop_loss_multiple, initial_credit,
                )
                result["call_exit_price"] = current_call_price
                result["put_exit_price"] = current_put_price
                result["exit_date"] = hedge_date
                result["stopped_out"] = True
                stopped_out = True
                break

        # Close the 'profitable' leg (the cheaper one to buy back)
        close_call = current_call_price < current_put_price

        if close_call:
            adjustment_pnl = current_call_sold_price - current_call_price
            total_adjustment_pnl += adjustment_pnl
            logger.info(
                "Adjustment (%s): Closing Call %s @ %.2f (Sold @ %.2f). PNL: %.2f",
                hedge_date,
                current_call_strike,
                current_call_price,
                current_call_sold_price,
                adjustment_pnl,
            )

            # Re-sell a Call near the current Put price
            target_price = current_put_price
            new_call_option = find_option_near_price(
                hedge_option_chain, expiry_30d, target_price, "CE", spot=hedge_spot
            )

            if new_call_option:
                current_call_strike = new_call_option["strike"]
                current_call_sold_price = new_call_option["settle"]
                adjustment_legs.append((current_call_price, current_call_sold_price))
                logger.info(
                    "Adjustment (%s): Selling New Call %s @ %.2f (Target Price: %.2f)",
                    hedge_date,
                    current_call_strike,
                    current_call_sold_price,
                    target_price,
                )
            else:
                logger.error(
                    "Failed to find replacement Call option on %s. Aborting month.",
                    hedge_date,
                )
                result["skipped_reason"] = f"Failed hedge adjustment on {hedge_date}"
                insert_backtest_result(cursor, result)
                return result
        else:
            adjustment_pnl = current_put_sold_price - current_put_price
            total_adjustment_pnl += adjustment_pnl
            logger.info(
                "Adjustment (%s): Closing Put %s @ %.2f (Sold @ %.2f). PNL: %.2f",
                hedge_date,
                current_put_strike,
                current_put_price,
                current_put_sold_price,
                adjustment_pnl,
            )

            # Re-sell a Put near the current Call price
            target_price = current_call_price
            new_put_option = find_option_near_price(
                hedge_option_chain, expiry_30d, target_price, "PE", spot=hedge_spot
            )

            if new_put_option:
                current_put_strike = new_put_option["strike"]
                current_put_sold_price = new_put_option["settle"]
                adjustment_legs.append((current_put_price, current_put_sold_price))
                logger.info(
                    "Adjustment (%s): Selling New Put %s @ %.2f (Target Price: %.2f)",
                    hedge_date,
                    current_put_strike,
                    current_put_sold_price,
                    target_price,
                )
            else:
                logger.error(
                    "Failed to find replacement Put option on %s. Aborting month.",
                    hedge_date,
                )
                result["skipped_reason"] = f"Failed hedge adjustment on {hedge_date}"
                insert_backtest_result(cursor, result)
                return result

    # --- End Adjustment Loop ---

    if stopped_out:
        result[_HEDGE_PNL_KEY] = total_adjustment_pnl
        result["pnl_points"] = (
            total_adjustment_pnl
            + (current_call_sold_price - result["call_exit_price"])
            + (current_put_sold_price - result["put_exit_price"])
        )
        result["exit_cost"] = result["call_exit_price"] + result["put_exit_price"]
        result["adjustment_legs"] = adjustment_legs
        price_result(
            result,
            lot_size=(
                lot_schedule.get(symbol, entry_date) if lot_schedule is not None else None
            ),
            entry_spot=result.get("entry_spot"),
            variant=variant,
            slippage_fraction=slippage_fraction,
            margin_model=margin_model,
            rates=rates,
        )
        insert_backtest_result(cursor, result)
        return result

    # Exit metrics
    exit_metrics = get_metrics_for_day(cursor, symbol, exit_date)
    if (
        not exit_metrics
        or exit_metrics.get("underlying_price") is None
        or not exit_metrics.get("option_chain")
    ):
        logger.warning(
            "Missing exit metrics/price/chain for %s on %s. Cannot calculate final PNL.",
            symbol,
            exit_date,
        )
        result["skipped_reason"] = "Missing Exit Data"
        result[_HEDGE_PNL_KEY] = total_adjustment_pnl
        insert_backtest_result(cursor, result)
        return result

    result[_HEDGE_PNL_KEY] = total_adjustment_pnl

    # Exit prices for the final state of the strangle legs
    exit_option_chain = exit_metrics["option_chain"]
    if chain_is_degenerate(exit_option_chain, expiry_30d):
        logger.warning(
            "Degenerate chain for %s on exit date %s; the month cannot be priced.",
            symbol,
            exit_date,
        )
        result["skipped_reason"] = "Degenerate Exit Chain"
        result[_HEDGE_PNL_KEY] = total_adjustment_pnl
        insert_backtest_result(cursor, result)
        return result
    exit_spot = exit_metrics.get("underlying_price")
    call_exit_data = get_option_data(
        exit_option_chain, expiry_30d, current_call_strike, "CE", spot=exit_spot
    )
    put_exit_data = get_option_data(
        exit_option_chain, expiry_30d, current_put_strike, "PE", spot=exit_spot
    )

    if not call_exit_data or not put_exit_data:
        logger.warning(
            "Could not find exit prices for final options (C:%s, P:%s) for %s on %s. "
            "Cannot calculate Strangle PNL.",
            current_call_strike,
            current_put_strike,
            symbol,
            exit_date,
        )
        result["skipped_reason"] = "Missing Exit Price"
        insert_backtest_result(cursor, result)  # keep adjustment PNL
        return result

    result["call_exit_price"] = call_exit_data["settle"]
    result["put_exit_price"] = put_exit_data["settle"]

    # PNL is computed leg by leg: every leg ever sold, minus what it cost to
    # close it.
    #
    # The previous formula was `initial_entry_credit - final_exit_cost +
    # total_adjustment_pnl`, which is wrong as soon as a leg has been replaced:
    # it credits the ORIGINAL leg's sale while debiting the REPLACEMENT leg's
    # buyback, so the replacement's own sale credit is never counted. The error
    # is exactly `(final sold price - original sold price)` per side. Because
    # the adjustment re-sells near the price of the *losing* leg, replacements
    # are typically sold higher than the original, so the old formula
    # systematically UNDERSTATED P&L on any month that adjusted.
    initial_entry_credit = result["call_entry_price"] + result["put_entry_price"]
    final_exit_cost = result["call_exit_price"] + result["put_exit_price"]
    result["pnl_points"] = (
        total_adjustment_pnl
        + (current_call_sold_price - result["call_exit_price"])
        + (current_put_sold_price - result["put_exit_price"])
    )
    result["entry_credit"] = initial_entry_credit
    result["exit_cost"] = final_exit_cost
    result["adjustment_legs"] = adjustment_legs

    logger.info(
        "Exit Strangle: Buy CE %s @ %.2f, Buy PE %s @ %.2f. Cost: %.2f",
        current_call_strike,
        result["call_exit_price"],
        current_put_strike,
        result["put_exit_price"],
        final_exit_cost,
    )
    logger.info(
        "Result: Initial Credit=%.2f, Final Cost=%.2f, Adjustment PNL=%.2f, "
        "Total PNL = %.2f points",
        initial_entry_credit,
        final_exit_cost,
        total_adjustment_pnl,
        result["pnl_points"],
    )

    price_result(
        result,
        lot_size=(
            lot_schedule.get(symbol, entry_date) if lot_schedule is not None else None
        ),
        entry_spot=result.get("entry_spot"),
        variant=variant,
        slippage_fraction=slippage_fraction,
        margin_model=margin_model,
        rates=rates,
    )

    insert_backtest_result(cursor, result)
    return result


# --- Entry points -----------------------------------------------------------

def resolve_scope(
    symbols: list[str] | None,
    years: list[int] | None,
    months: list[int] | None,
    *,
    all_symbols: list[str],
    current_year: int | None = None,
) -> tuple[list[str], list[int], list[int]]:
    """Resolve the backtest run scope from optional overrides.

    ``None`` keeps the default: every catalog symbol × the last 4 years
    (inclusive, ``[current_year - 4 .. current_year]``) × all 12 months.
    Explicit lists are used as given, with years outside the supported range
    (2010..current_year + 1) and months outside 1..12 filtered out.
    """
    if current_year is None:
        current_year = datetime.now().year
    symbols_to_run = list(all_symbols) if symbols is None else list(symbols)
    years_to_run = (
        list(range(current_year - 4, current_year + 1))
        if years is None
        else [y for y in years if 2010 <= y <= current_year + 1]
    )
    months_to_run = (
        list(range(1, 13))
        if months is None
        else [m for m in months if 1 <= m <= 12]
    )
    return symbols_to_run, years_to_run, months_to_run


def run_backtest(
    cfg: Settings | None = None,
    symbols: list[str] | None = None,
    years: list[int] | None = None,
    months: list[int] | None = None,
    *,
    variant: str = "base",
    slippage_fraction: float = 0.0,
    margin_model: MarginModel | None = None,
    brokerage_per_order: float | None = None,
    stop_loss_multiple: float | None = None,
) -> int:
    """Run the backtest scope and persist results to Postgres.

    Scope defaults to every catalog symbol × the last 4 years (inclusive) ×
    all 12 months; ``symbols``/``years``/``months`` narrow it (see
    :func:`resolve_scope`). Future/current months are always skipped.

    Returns the number of (symbol, year, month) tasks actually processed.
    """
    cfg = cfg or settings
    lot_schedule = load_lot_sizes()
    rates = rate_sets(brokerage_per_order)
    all_symbols, index_symbols = load_symbols_info()
    if not all_symbols:
        logger.error("No symbols loaded from catalog; aborting backtest.")
        return 0

    # Resolve run scope: defaults to every catalog symbol × the last 4 years
    # (inclusive) × all 12 months; explicit values narrow the run.
    symbols_to_run, years_to_run, months_to_run = resolve_scope(
        symbols,
        years,
        months,
        all_symbols=all_symbols,
        current_year=datetime.now().year,
    )

    processed_count = 0
    total_tasks = len(symbols_to_run) * len(years_to_run) * len(months_to_run)
    today = date.today()

    try:
        with _get_conn(cfg) as conn:
            logger.info("Database connection successful.")
            for symbol in symbols_to_run:
                for year in years_to_run:
                    if year < 2010 or year > today.year + 1:
                        logger.warning("Skipping invalid year %s for %s", year, symbol)
                        continue
                    for month in months_to_run:
                        if not 1 <= month <= 12:
                            logger.warning(
                                "Skipping invalid month %s for %s %s", month, symbol, year
                            )
                            continue
                        # Prevent running for future/current months
                        if year > today.year or (year == today.year and month >= today.month):
                            logger.info(
                                "Skipping future or current month: %s %s-%02d",
                                symbol,
                                year,
                                month,
                            )
                            continue
                        try:
                            with conn.cursor() as cur:
                                run_backtest_month(
                                    symbol,
                                    year,
                                    month,
                                    index_symbols,
                                    cur,
                                    lot_schedule=lot_schedule,
                                    variant=variant,
                                    slippage_fraction=slippage_fraction,
                                    margin_model=margin_model,
                                    rates=rates,
                                    stop_loss_multiple=stop_loss_multiple,
                                )
                            conn.commit()  # commit after each month
                        except Exception:  # noqa: BLE001 - one bad month must not kill the run
                            logger.exception(
                                "Error during backtest for %s %s-%s. Rolling back month.",
                                symbol,
                                year,
                                month,
                            )
                            try:
                                conn.rollback()
                            except Exception as rb_exc:  # noqa: BLE001
                                logger.error("Rollback failed: %s", rb_exc)
                        finally:
                            processed_count += 1
                            if processed_count % 10 == 0 or processed_count == total_tasks:
                                logger.info(
                                    "Progress: %s / %s (approx) months processed.",
                                    processed_count,
                                    total_tasks,
                                )
            logger.info("Backtest finished. Processed approximately %s months.", processed_count)
    except OperationalError as db_err:
        logger.error("Database connection error during backtest: %s", db_err)
    except Exception as exc:  # noqa: BLE001 - top-level guard, mirrors legacy main
        logger.exception("An unexpected error occurred during the backtesting process: %s", exc)
    finally:
        close_backtest_pool()

    return processed_count


def main() -> None:
    """CLI entry point for the strangle backtester."""
    run_backtest()


if __name__ == "__main__":
    main()
