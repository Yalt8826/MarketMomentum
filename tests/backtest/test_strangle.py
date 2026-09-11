"""Unit tests for mktmtm.backtest.strangle — no live database.

Covers the pure decision logic with synthetic option chains and trading-day
maps: delta picking, exact-strike lookup, price-based re-entry, exit-day
selection, weekly hedge-date calculation, and a full per-month run against an
in-memory fake cursor.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from mktmtm.backtest.strangle import (
    find_exit_day,
    find_option_near_price,
    find_options_near_delta,
    find_weekly_hedge_dates,
    get_option_data,
    insert_backtest_result,
    is_index,
    run_backtest_month,
)

# ─────────────────────────── synthetic fixtures ─────────────────────────────

def _opt(
    typ: str,
    expiry: str,
    strike: float,
    delta: float,
    settle: float,
    volume: float = 1_000.0,
) -> dict:
    """One chain element.

    ``volume`` defaults to a traded contract because selection now requires
    evidence of a real transaction; tests of the liquidity gate override it.
    """
    return {
        "type": typ,
        "expiry": expiry,
        "strike": strike,
        "delta": delta,
        "settle": settle,
        "volume": volume,
    }


def _synthetic_chain(expiry: date, strikes: list[float]) -> list[dict]:
    """CE+PE rows across the given strikes for one expiry.

    Deltas slope like a real chain: OTM calls ≈ +0.15..0.25 at lower strikes,
    OTM puts ≈ -0.15..-0.25 at higher strikes.
    """
    expiry_str = expiry.strftime("%d-%b-%Y")
    chain: list[dict] = []
    for i, strike in enumerate(strikes):
        call_delta = 0.15 + 0.025 * i
        put_delta = -(0.15 + 0.025 * (len(strikes) - 1 - i))
        chain.append(_opt("CE", expiry_str, strike, call_delta, 40.0 + i * 5))
        chain.append(_opt("PE", expiry_str, strike, put_delta, 40.0 + (len(strikes) - 1 - i) * 5))
    return chain


class FakeCursor:
    """In-memory cursor serving the queries run_backtest_month issues.

    Backed by a dict keyed on (symbol, date) → metrics row with the same
    fields ``get_metrics_for_day`` reads; never touches PostgreSQL.
    """

    def __init__(self, metrics: dict[tuple[str, date], dict]):
        self.metrics = metrics
        self.executed: list[tuple[str, tuple | None]] = []
        self._pending: list | None = None

    def execute(self, query: str, params: tuple | None = None) -> None:
        self.executed.append((query, params))
        self._pending = None
        assert params is not None
        if "SELECT date FROM option_metrics" in query:
            symbol, start, end = params
            self._pending = [
                (d,)
                for (sym, d) in sorted(self.metrics, key=lambda kv: kv[1])
                if sym == symbol and start <= d <= end
            ]
        elif "SELECT underlying_price" in query:
            symbol, day = params
            row = self.metrics.get((symbol, day))
            if row is None:
                self._pending = None
            else:
                self._pending = [
                    (
                        row["underlying_price"],
                        row["interest_rate"],
                        row["expiry_30d"],
                        row["upcoming_earning_date"],
                        row["option_chain"],
                    )
                ]

    def fetchone(self) -> tuple | None:
        if self._pending is None:
            return None
        return self._pending[0] if self._pending else None

    def fetchall(self) -> list:
        return self._pending or []

    def mogrify(self, query: str, params: tuple | None = None) -> bytes:
        return b""  # only used in the legacy error path

    def connection(self) -> None:
        return None


# ─────────────────────────── find_options_near_delta ────────────────────────

def test_find_options_near_delta_picks_closest_call() -> None:
    expiry = date(2025, 10, 28)
    chain = _synthetic_chain(expiry, [24000.0, 24500.0, 25000.0])

    best = find_options_near_delta(chain, expiry, 0.20, "CE")

    assert best is not None
    assert best["type"] == "CE"
    # deltas are 0.15, 0.175, 0.20 → the 25000 strike matches exactly
    assert best["strike"] == 25000.0
    assert best["delta"] == pytest.approx(0.20)


def test_find_options_near_delta_picks_closest_put() -> None:
    expiry = date(2025, 10, 28)
    chain = _synthetic_chain(expiry, [24000.0, 24500.0, 25000.0])

    best = find_options_near_delta(chain, expiry, -0.20, "PE")

    assert best is not None
    assert best["type"] == "PE"
    # put deltas are -0.20, -0.175, -0.15 → the 24000 strike matches exactly
    assert best["strike"] == 24000.0
    assert best["delta"] == pytest.approx(-0.20)


def test_find_options_near_delta_ignores_wrong_type_and_expiry() -> None:
    expiry = date(2025, 10, 28)
    other_expiry = date(2025, 11, 27)
    chain = [
        _opt("CE", expiry.strftime("%d-%b-%Y"), 24500.0, 0.50, 300.0),
        _opt("PE", expiry.strftime("%d-%b-%Y"), 24500.0, -0.50, 300.0),
        _opt("CE", other_expiry.strftime("%d-%b-%Y"), 24500.0, 0.20, 40.0),
    ]

    # the only 0.20-delta CE is on the other expiry → the correct-expiry CE wins
    best = find_options_near_delta(chain, expiry, 0.20, "CE")
    assert best is not None
    assert best["delta"] == pytest.approx(0.50)  # not the wrong-expiry 0.20
    # PE on this expiry exists but is far from -0.20
    best = find_options_near_delta(chain, expiry, -0.20, "PE")
    assert best is not None
    assert best["delta"] == pytest.approx(-0.50)


def test_find_options_near_delta_skips_invalid_rows() -> None:
    expiry = date(2025, 10, 28)
    expiry_str = expiry.strftime("%d-%b-%Y")
    chain: list[dict] = [
        _opt("CE", expiry_str, 24500.0, 0.20, 40.0),
        {"type": "CE", "expiry": expiry_str, "strike": 24600.0, "delta": 0.19, "settle": float("nan")},
        {"type": "CE", "expiry": expiry_str, "strike": 24700.0, "delta": float("inf"), "settle": 42.0},
        {"type": "CE", "expiry": expiry_str, "delta": 0.18, "settle": 42.0},  # no strike
    ]

    best = find_options_near_delta(chain, expiry, 0.20, "CE")

    assert best is not None
    assert best["strike"] == 24500.0  # only the finite-row candidate remains


def test_find_options_near_delta_returns_none_for_empty_inputs() -> None:
    expiry = date(2025, 10, 28)
    assert find_options_near_delta([], expiry, 0.20, "CE") is None
    assert find_options_near_delta(_synthetic_chain(expiry, [24500.0]), expiry, 0.20, "CE") is not None


# ─────────────────────────── get_option_data ────────────────────────────────

def test_get_option_data_exact_match() -> None:
    expiry = date(2025, 10, 28)
    chain = _synthetic_chain(expiry, [24000.0, 24500.0, 25000.0])

    found = get_option_data(chain, expiry, 24500.0, "PE")

    assert found is not None
    assert found["strike"] == 24500.0
    assert found["type"] == "PE"


def test_get_option_data_missing_combination_returns_none() -> None:
    expiry = date(2025, 10, 28)
    chain = _synthetic_chain(expiry, [24000.0, 24500.0, 25000.0])

    assert get_option_data(chain, expiry, 99999.0, "CE") is None
    assert get_option_data(chain, expiry, 24500.0, "XX") is None
    assert get_option_data(chain, date(2026, 1, 29), 24500.0, "CE") is None


def test_get_option_data_skips_non_finite_price_or_delta() -> None:
    expiry = date(2025, 10, 28)
    expiry_str = expiry.strftime("%d-%b-%Y")
    chain: list[dict] = [
        _opt("CE", expiry_str, 24500.0, 0.20, 40.0),
        {"type": "CE", "expiry": expiry_str, "strike": 24500.0, "delta": 0.20, "settle": float("nan")},
        {"type": "CE", "expiry": expiry_str, "strike": 24500.0, "delta": float("nan"), "settle": 40.0},
    ]

    found = get_option_data(chain, expiry, 24500.0, "CE")

    assert found is not None
    assert found["settle"] == 40.0
    assert found["delta"] == 0.20


# ─────────────────────────── find_option_near_price ─────────────────────────

def test_find_option_near_price_closest_settle() -> None:
    expiry = date(2025, 10, 28)
    chain = _synthetic_chain(expiry, [24000.0, 24500.0, 25000.0])

    # settles: CE 40,45,50 — closest to 47 is 45 → strike 24500
    best = find_option_near_price(chain, expiry, 47.0, "CE")

    assert best is not None
    assert best["strike"] == 24500.0
    assert best["settle"] == pytest.approx(45.0)


def test_find_option_near_price_requires_positive_price() -> None:
    expiry = date(2025, 10, 28)
    expiry_str = expiry.strftime("%d-%b-%Y")
    chain: list[dict] = [
        {"type": "CE", "expiry": expiry_str, "strike": 24000.0, "settle": 0.0, "volume": 100},
        {"type": "CE", "expiry": expiry_str, "strike": 24500.0, "settle": -5.0, "volume": 100},
        {"type": "CE", "expiry": expiry_str, "strike": 25000.0, "settle": 30.0, "volume": 100},
    ]

    best = find_option_near_price(chain, expiry, 10.0, "CE")

    assert best is not None
    assert best["strike"] == 25000.0  # zero/negative settles are ineligible


def test_find_option_near_price_none_cases() -> None:
    expiry = date(2025, 10, 28)
    chain = _synthetic_chain(expiry, [24500.0])
    assert find_option_near_price([], expiry, 10.0, "CE") is None
    assert find_option_near_price(chain, expiry, float("nan"), "CE") is None
    assert find_option_near_price(chain, expiry, float("inf"), "CE") is None


# ─────────────────────────── find_exit_day ──────────────────────────────────

def _trading_days(*days: date) -> set[date]:
    return set(days)


def test_find_exit_day_index_picks_last_tuesday() -> None:
    # 30-Oct-2025 is a Thursday; a normal Mon-Fri week before it:
    # Mon 27, Tue 28, Wed 29, Thu 30. Index target = Tuesday 28.
    expiry = date(2025, 10, 30)
    trading = _trading_days(date(2025, 10, 27), date(2025, 10, 28), date(2025, 10, 29))

    exit_day = find_exit_day(expiry, is_index_flag=True, has_trading_day=lambda d: d in trading)

    assert exit_day == date(2025, 10, 28)


def test_find_exit_day_stock_picks_last_wednesday() -> None:
    # Stock target = Wednesday 29.
    expiry = date(2025, 10, 30)
    trading = _trading_days(date(2025, 10, 27), date(2025, 10, 28), date(2025, 10, 29))

    exit_day = find_exit_day(expiry, is_index_flag=False, has_trading_day=lambda d: d in trading)

    assert exit_day == date(2025, 10, 29)


def test_find_exit_day_falls_back_to_earlier_trading_day() -> None:
    # Tuesday 28 has no data; the most recent trading day before expiry is
    # Monday 27 → fall back to it (weekday < target).
    expiry = date(2025, 10, 30)
    trading = _trading_days(date(2025, 10, 27), date(2025, 10, 29))

    exit_day = find_exit_day(expiry, is_index_flag=True, has_trading_day=lambda d: d in trading)

    assert exit_day == date(2025, 10, 27)


def test_find_exit_day_walks_back_across_weekend() -> None:
    # Expiry on Monday 03-Nov-2025. Previous week: Fri 31-Oct, Thu 30, ...
    # Index target Tuesday: the last Tue before expiry is 28-Oct.
    expiry = date(2025, 11, 3)
    trading = _trading_days(
        date(2025, 10, 28),
        date(2025, 10, 29),
        date(2025, 10, 30),
        date(2025, 10, 31),
    )

    exit_day = find_exit_day(expiry, is_index_flag=True, has_trading_day=lambda d: d in trading)

    assert exit_day == date(2025, 10, 28)


def test_find_exit_day_returns_none_when_no_trading_day() -> None:
    expiry = date(2025, 10, 30)
    assert find_exit_day(expiry, True, lambda d: False) is None


def test_find_exit_day_stops_at_20_day_window() -> None:
    # No trading days anywhere near expiry → None (window bounded at 20 days).
    expiry = date(2025, 10, 30)
    far_away = {expiry - timedelta(days=40)}
    assert find_exit_day(expiry, True, lambda d: d in far_away) is None


# ─────────────────────────── find_weekly_hedge_dates ────────────────────────

def test_find_weekly_hedge_dates_basic_weeks() -> None:
    # Entry Tue 02-Sep-2025, exit Thu 18-Sep-2025. Fridays: 05, 12, 19 (past exit).
    entry = date(2025, 9, 2)
    exit_day = date(2025, 9, 18)
    trading = _trading_days(
        date(2025, 9, 5),   # Fri — hedge
        date(2025, 9, 12),  # Fri — hedge
        date(2025, 9, 15),  # Mon
    )

    hedge_dates = find_weekly_hedge_dates(entry, exit_day, {d: True for d in trading})

    assert hedge_dates == [date(2025, 9, 5), date(2025, 9, 12)]


def test_find_weekly_hedge_dates_rolls_friday_to_last_trading_day() -> None:
    # Friday 12-Sep-2025 is a holiday; the last trading day of that week is
    # Thursday 11-Sep-2025 → still a hedge date.
    entry = date(2025, 9, 2)
    exit_day = date(2025, 9, 18)
    trading = _trading_days(
        date(2025, 9, 5),
        date(2025, 9, 11),  # Thu — replaces the missing Fri 12
    )

    hedge_dates = find_weekly_hedge_dates(entry, exit_day, {d: True for d in trading})

    assert hedge_dates == [date(2025, 9, 5), date(2025, 9, 11)]


def test_find_weekly_hedge_dates_excludes_entry_date() -> None:
    # Entry on a Friday: the same Friday must NOT become a hedge date.
    entry = date(2025, 9, 5)  # Fri
    exit_day = date(2025, 9, 18)
    trading = _trading_days(date(2025, 9, 5), date(2025, 9, 12))

    hedge_dates = find_weekly_hedge_dates(entry, exit_day, {d: True for d in trading})

    assert hedge_dates == [date(2025, 9, 12)]


def test_find_weekly_hedge_dates_short_week_no_hedges() -> None:
    # Entry on Friday 12-Sep-2025 → the first hedge Friday is the next one
    # (19-Sep), which is past exit Tue 16-Sep → no hedge dates.
    entry = date(2025, 9, 12)
    exit_day = date(2025, 9, 16)
    trading = _trading_days(
        date(2025, 9, 12), date(2025, 9, 15), date(2025, 9, 16)
    )

    hedge_dates = find_weekly_hedge_dates(entry, exit_day, {d: True for d in trading})

    assert hedge_dates == []


# ─────────────────────────── misc helpers ───────────────────────────────────

def test_is_index() -> None:
    assert is_index("NIFTY", {"NIFTY", "BANKNIFTY"})
    assert not is_index("RELIANCE", {"NIFTY", "BANKNIFTY"})


def test_insert_backtest_result_uses_hedge_pnl_column() -> None:
    cursor = FakeCursor({})
    result = {
        "symbol": "NIFTY",
        "entry_date": date(2025, 9, 2),
        "exit_date": date(2025, 9, 18),
        "year": 2025,
        "month": 9,
        "entry_credit": 85.0,
        "exit_cost": 60.0,
        "pnl_points": 25.0,
        "skipped_reason": None,
        "call_entry_strike": 25000.0,
        "put_entry_strike": 24000.0,
        "call_entry_delta": 0.2,
        "put_entry_delta": -0.2,
        "call_entry_price": 45.0,
        "put_entry_price": 40.0,
        "call_exit_price": 30.0,
        "put_exit_price": 30.0,
    }

    insert_backtest_result(cursor, result)

    query, params = cursor.executed[-1]
    assert "INSERT INTO backtest_results" in query
    # The PK gained `variant` so a parameter sweep cannot overwrite itself.
    assert "ON CONFLICT (variant, symbol, year, month)" in query
    assert "hedge_pnl_points" in query
    assert params[0] == "NIFTY"
    assert params[17] is None  # hedge_pnl_points defaulted to None
    assert params[18] == "base"  # variant defaults rather than failing


def test_insert_backtest_result_scrubs_nan_and_keeps_hedge_pnl() -> None:
    cursor = FakeCursor({})
    result = {
        "symbol": "NIFTY",
        "entry_date": date(2025, 9, 2),
        "year": 2025,
        "month": 9,
        "pnl_points": float("nan"),
        "hedge_pnl_points": 4.5,
    }

    insert_backtest_result(cursor, result)

    _, params = cursor.executed[-1]
    assert params[6] is None  # NaN pnl_points → None
    assert params[17] == 4.5  # hedge PNL preserved


# ─────────────────────────── run_backtest_month (fake cursor) ───────────────

def _month_metrics() -> dict[tuple[str, date], dict]:
    """Two weeks of NIFTY data: entry Mon 01-Sep-2025, expiry Thu 25-Sep-2025.

    Trading days: 01,02,03,04,05,08,09,10,11,12,15,16,17,18,19-Sep-2025.
    Expiry 25-Sep-2025 (Thu); index target Tuesday → 23-Sep is Tue but the
    last trading Tue before expiry with data is 16-Sep... actually the walk
    starts 24-Sep (Wed, has data) → weekday 2 > 1, continue; 23-Sep (Tue)
    has data → return 23-Sep.
    """
    week1 = [1, 2, 3, 4, 5]
    week2 = [8, 9, 10, 11, 12]
    week3 = [15, 16, 17, 18, 19]
    days = [date(2025, 9, d) for d in [*week1, *week2, *week3]]
    expiry = date(2025, 9, 25)
    chain = _synthetic_chain(expiry, [24000.0, 24500.0, 25000.0])
    metrics: dict[tuple[str, date], dict] = {}
    for day in days:
        metrics[("NIFTY", day)] = {
            "underlying_price": 24600.0,
            "interest_rate": 6.5,
            "expiry_30d": expiry,
            "upcoming_earning_date": None,
            "option_chain": chain,
        }
    return metrics


def test_run_backtest_month_full_flow_no_db() -> None:
    cursor = FakeCursor(_month_metrics())

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY", "BANKNIFTY"}, cursor)

    assert result["symbol"] == "NIFTY"
    assert result["year"] == 2025
    assert result["month"] == 9
    assert result["entry_date"] == date(2025, 9, 1)
    # Expiry 25-Sep (Thu); walk back: 24-Sep (Wed) is a trading day but weekday
    # 2 > target 1, 23-Sep (Tue) has no data, so the last trading Tue is 16-Sep.
    assert result["exit_date"] == date(2025, 9, 16)
    assert result["call_entry_strike"] == 25000.0  # 0.20 delta call
    assert result["put_entry_strike"] == 24000.0  # -0.20 delta put
    assert result["entry_credit"] == pytest.approx(100.0)  # 50 (CE) + 50 (PE)
    assert result["pnl_points"] is not None
    assert result["hedge_pnl_points"] is not None
    assert result.get("skipped_reason") is None
    # results are persisted via the fake cursor
    inserts = [q for q, _ in cursor.executed if "INSERT INTO backtest_results" in q]
    assert len(inserts) == 1


def test_run_backtest_month_skips_no_trading_days() -> None:
    cursor = FakeCursor({})

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY"}, cursor)

    assert result["skipped_reason"] == "No Trading Days"
    inserts = [q for q, _ in cursor.executed if "INSERT INTO backtest_results" in q]
    assert len(inserts) == 1


def test_run_backtest_month_skips_missing_entry_metrics() -> None:
    metrics = _month_metrics()
    # keep the day as a trading day, but strip its chain → "Missing Entry Data"
    metrics[("NIFTY", date(2025, 9, 1))]["option_chain"] = None
    cursor = FakeCursor(metrics)

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY"}, cursor)

    assert result["skipped_reason"] == "Missing Entry Data"


def test_run_backtest_month_skips_earnings_conflict() -> None:
    metrics = _month_metrics()
    # earnings between entry (01-Sep) and expiry (25-Sep)
    for row in metrics.values():
        row["upcoming_earning_date"] = date(2025, 9, 10)
    cursor = FakeCursor(metrics)

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY"}, cursor)

    assert result["skipped_reason"] == "Earnings on 2025-09-10"


def test_run_backtest_month_skips_no_suitable_options() -> None:
    metrics = _month_metrics()
    # entry-day chain has no CE at all → find_options_near_delta returns None
    expiry = date(2025, 9, 25)
    chain = [_opt("PE", expiry.strftime("%d-%b-%Y"), 24000.0, -0.20, 40.0)]
    metrics[("NIFTY", date(2025, 9, 1))]["option_chain"] = chain
    cursor = FakeCursor(metrics)

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY"}, cursor)

    assert result["skipped_reason"] == "No Suitable Options"


# ─────────────────── P&L accounting across an adjustment ────────────────────


def _adjusting_month_metrics() -> dict[tuple[str, date], dict]:
    """A month where the underlying moves enough to force a leg replacement.

    Entry Mon 01-Sep-2025 with a flat chain, then the call side collapses and
    the put side rises, so the weekly hedge buys the call back cheaply and
    re-sells a new call near the (expensive) put's price.
    """
    days = [date(2025, 9, d) for d in [1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16]]
    expiry = date(2025, 9, 25)
    expiry_str = expiry.strftime("%d-%b-%Y")

    def chain_at(call_mult: float, put_mult: float) -> list[dict]:
        rows: list[dict] = []
        for i, strike in enumerate([24000.0, 24500.0, 25000.0]):
            rows.append(
                _opt("CE", expiry_str, strike, 0.15 + 0.025 * i, (40.0 + i * 5) * call_mult)
            )
            rows.append(
                _opt("PE", expiry_str, strike, -(0.20 - 0.025 * i), (50.0 - i * 5) * put_mult)
            )
        return rows

    metrics: dict[tuple[str, date], dict] = {}
    for day in days:
        # From the first hedge date onward the call is cheap and the put dear.
        moved = day >= date(2025, 9, 5)
        metrics[("NIFTY", day)] = {
            "underlying_price": 24600.0,
            "interest_rate": 6.5,
            "expiry_30d": expiry,
            "upcoming_earning_date": None,
            "option_chain": chain_at(0.4, 1.6) if moved else chain_at(1.0, 1.0),
        }
    return metrics


def test_pnl_counts_the_replacement_legs_credit() -> None:
    """Regression: P&L must sum every leg ever sold, minus its buyback.

    The superseded formula was ``initial_entry_credit - final_exit_cost +
    total_adjustment_pnl``. Once a leg is replaced that credits the ORIGINAL
    sale while debiting the REPLACEMENT's buyback, losing the replacement's own
    credit — so it understated any month that adjusted.

    Checked against ``economics.price_strangle_trade``, which reaches the same
    figure by a different route: two independent implementations agreeing is a
    stronger statement than either matching a hand-computed constant.
    """
    from mktmtm.backtest.economics import price_strangle_trade

    cursor = FakeCursor(_adjusting_month_metrics())

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY"}, cursor)

    assert result.get("skipped_reason") is None
    legs = result["adjustment_legs"]
    assert legs, "this fixture must force at least one adjustment"

    # lot_size 1 makes gross rupees equal gross points.
    independent = price_strangle_trade(
        entry_call=result["call_entry_price"],
        entry_put=result["put_entry_price"],
        exit_call=result["call_exit_price"],
        exit_put=result["put_exit_price"],
        adjustments=legs,
        lot_size=1,
        entry_spot=24_600.0,
        entry_date=date(2025, 9, 1),
    )
    assert result["pnl_points"] == pytest.approx(independent.gross_pnl_rupees)

    # And it differs from the superseded formula by exactly the credit that
    # formula dropped.
    superseded = (
        result["entry_credit"] - result["exit_cost"] + result["hedge_pnl_points"]
    )
    assert result["pnl_points"] != pytest.approx(superseded)


def test_adjustment_legs_are_recorded_for_the_cost_model() -> None:
    """Each adjustment is two orders; the cost model needs both premiums."""
    cursor = FakeCursor(_adjusting_month_metrics())

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY"}, cursor)

    legs = result["adjustment_legs"]
    assert legs
    for bought_back, resold in legs:
        assert bought_back > 0
        assert resold > 0


# ────────────────────── liquidity gate on selection ─────────────────────────


def _chain_with_placeholder_strikes() -> list[dict]:
    """A chain reproducing the NMDC 2022-10-25 defect.

    Deep ITM calls all carry an identical placeholder settle far above what the
    strike is worth, with zero volume, zero OI and a zero delta — NSE assigns a
    settlement price to every listed strike whether or not it traded.
    """
    expiry_str = date(2025, 9, 25).strftime("%d-%b-%Y")
    chain: list[dict] = []
    for strike in (72.5, 80.0, 90.0, 100.0):  # deep ITM, never traded
        opt = _opt("CE", expiry_str, strike, 0.0, 130.75)
        opt["volume"] = 0
        opt["oi"] = 0
        chain.append(opt)
    for strike, delta, settle in ((135.0, 0.20, 2.10), (140.0, 0.12, 1.05)):
        opt = _opt("CE", expiry_str, strike, delta, settle)
        opt["volume"] = 5_000
        opt["oi"] = 40_000
        chain.append(opt)
    return chain


def test_untraded_placeholder_strikes_are_not_selectable_by_price() -> None:
    """Regression: the defect that produced losses of 16x the margin posted.

    Asking for a call near 130 must not return a never-traded strike whose
    settle is a flat placeholder; it returns the closest contract that actually
    traded instead.
    """
    chain = _chain_with_placeholder_strikes()

    picked = find_option_near_price(chain, date(2025, 9, 25), 130.0, "CE")

    assert picked is not None
    assert picked["volume"] > 0
    assert picked["settle"] != 130.75


def test_untraded_strikes_are_not_selectable_by_delta_either() -> None:
    chain = _chain_with_placeholder_strikes()

    picked = find_options_near_delta(chain, date(2025, 9, 25), 0.20, "CE")

    assert picked is not None
    assert picked["volume"] > 0
    assert picked["strike"] == 135.0


def test_liquidity_gate_can_be_disabled_for_comparison() -> None:
    """The old behaviour stays reachable, so the fix's effect is measurable."""
    chain = _chain_with_placeholder_strikes()

    picked = find_option_near_price(
        chain, date(2025, 9, 25), 130.0, "CE", require_liquidity=False
    )

    assert picked is not None
    assert picked["settle"] == 130.75  # the placeholder, as before


# ──────────────────── no-arbitrage bound on chain prices ────────────────────


def _corrupt_chain(spot: float = 130.71) -> list[dict]:
    """The NMDC 2022-10-25 defect: the underlying's price written into every
    option's settle, with every delta at 0.0 because IV could not converge.

    Heavily traded strikes are affected too, so a volume filter alone does not
    catch it — which is why the no-arbitrage bound exists.
    """
    expiry_str = date(2025, 9, 25).strftime("%d-%b-%Y")
    chain: list[dict] = []
    for strike in (117.5, 130.0, 137.5):
        for typ in ("CE", "PE"):
            opt = _opt(typ, expiry_str, strike, 0.0, 130.75, volume=2_492)
            chain.append(opt)
    return chain


def test_a_degenerate_chain_is_detected() -> None:
    """One settle across every contract is a corrupt day, not a price surface.

    Neither obvious per-contract filter catches it: the affected strikes traded
    heavily, and the corrupt value sits at 99.97% of spot — inside any
    no-arbitrage tolerance loose enough for a legitimate deep ITM call. The
    degeneracy itself is the signature.
    """
    from mktmtm.backtest.strangle import chain_is_degenerate

    assert chain_is_degenerate(_corrupt_chain() * 4)


def test_a_healthy_chain_is_not_flagged_as_degenerate() -> None:
    from mktmtm.backtest.strangle import chain_is_degenerate

    assert not chain_is_degenerate(_synthetic_chain(date(2025, 9, 25), [100.0, 110.0, 120.0]) * 2)


def test_a_short_chain_is_not_judged() -> None:
    """Too few contracts to distinguish corruption from a thin listing."""
    from mktmtm.backtest.strangle import chain_is_degenerate

    assert not chain_is_degenerate(_corrupt_chain()[:4])


def test_a_put_marked_above_its_strike_is_rejected() -> None:
    """A put can never be worth more than its strike."""
    chain = _corrupt_chain()

    picked = get_option_data(chain, date(2025, 9, 25), 117.5, "PE", spot=130.71)

    assert picked is None  # settle 130.75 exceeds the 117.5 strike


def test_the_bound_is_not_applied_without_a_spot() -> None:
    """A corruption filter must not silently reject good data when it cannot
    judge — so an unknown spot means no opinion."""
    chain = _corrupt_chain()

    assert get_option_data(chain, date(2025, 9, 25), 117.5, "PE") is not None


def test_legitimate_deep_itm_prices_survive_the_bound() -> None:
    """The bound must not reject real in-the-money options."""
    expiry_str = date(2025, 9, 25).strftime("%d-%b-%Y")
    # Spot 130, strike 100: a call worth ~31 is ordinary and well under spot.
    chain = [_opt("CE", expiry_str, 100.0, 0.92, 31.20, volume=500)]

    picked = get_option_data(chain, date(2025, 9, 25), 100.0, "CE", spot=130.0)

    assert picked is not None
    assert picked["settle"] == 31.20


def test_degeneracy_is_detected_within_the_traded_expiry() -> None:
    """IOC 2022-06-29: only the expiring series was corrupt.

    146 of 436 rows carried the spot price, all in the 30-Jun expiry, while
    July and August were priced normally. A whole-chain test sees 193 distinct
    settles and passes it — so the check must look at the expiry being traded.
    """
    from mktmtm.backtest.strangle import chain_is_degenerate

    near, far = date(2025, 9, 25), date(2025, 10, 30)
    chain = [
        _opt(t, near.strftime("%d-%b-%Y"), k, 0.0, 109.80, volume=9)
        for k in (100.0, 105.0, 111.0, 120.0, 127.0, 135.0)
        for t in ("CE", "PE")
    ]
    chain += _synthetic_chain(far, [100.0, 110.0, 120.0, 130.0])

    assert chain_is_degenerate(chain, near)
    assert not chain_is_degenerate(chain, far)
    # The whole-chain view is fooled — which is the point.
    assert not chain_is_degenerate(chain)


# ───────────────────────────── stop loss ────────────────────────────────────


def _runaway_month_metrics() -> dict[tuple[str, date], dict]:
    """A month where the call side runs away hard after the first week.

    Entry Mon 01-Sep-2025. From 08-Sep the call premium multiplies tenfold while
    the put decays — a position deep under water, which is what a stop is for.
    """
    days = [date(2025, 9, d) for d in [1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16]]
    expiry = date(2025, 9, 25)
    expiry_str = expiry.strftime("%d-%b-%Y")

    def chain_at(call_mult: float, put_mult: float) -> list[dict]:
        rows: list[dict] = []
        for i, strike in enumerate([24000.0, 24500.0, 25000.0]):
            rows.append(_opt("CE", expiry_str, strike, 0.15 + 0.025 * i,
                             (40.0 + i * 5) * call_mult))
            rows.append(_opt("PE", expiry_str, strike, -(0.20 - 0.025 * i),
                             (50.0 - i * 5) * put_mult))
        return rows

    return {
        ("NIFTY", day): {
            "underlying_price": 24600.0,
            "interest_rate": 6.5,
            "expiry_30d": expiry,
            "upcoming_earning_date": None,
            "option_chain": chain_at(10.0, 0.2) if day >= date(2025, 9, 8) else chain_at(1.0, 1.0),
        }
        for day in days
    }


def test_a_runaway_loss_triggers_the_stop() -> None:
    cursor = FakeCursor(_runaway_month_metrics())

    result = run_backtest_month(
        "NIFTY", 2025, 9, {"NIFTY"}, cursor, stop_loss_multiple=2.0
    )

    assert result["stopped_out"] is True
    assert result["exit_date"] < date(2025, 9, 16)  # before the scheduled exit
    assert result["pnl_points"] is not None


def test_without_a_stop_the_same_month_runs_to_expiry() -> None:
    """The control: the stop is what changed the outcome, not the fixture."""
    cursor = FakeCursor(_runaway_month_metrics())

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY"}, cursor)

    assert result.get("stopped_out") is not True


def test_the_stop_caps_the_loss_near_its_threshold() -> None:
    """A 2x stop should not lose dramatically more than 2x the entry credit."""
    cursor = FakeCursor(_runaway_month_metrics())

    result = run_backtest_month(
        "NIFTY", 2025, 9, {"NIFTY"}, cursor, stop_loss_multiple=2.0
    )

    # Some overshoot is expected — the stop fires on a weekly mark, not
    # continuously, so a gap between checks is not caught.
    assert result["pnl_points"] > -6.0 * result["entry_credit"]


def test_a_quiet_month_never_trips_the_stop() -> None:
    cursor = FakeCursor(_month_metrics())

    result = run_backtest_month(
        "NIFTY", 2025, 9, {"NIFTY", "BANKNIFTY"}, cursor, stop_loss_multiple=2.0
    )

    assert result.get("stopped_out") is not True
    assert result["exit_date"] == date(2025, 9, 16)


def test_the_stop_is_checked_before_the_adjustment() -> None:
    """The adjustment re-sells into a losing position — doubling down on the
    move that is hurting. A stop firing only afterwards has already taken the
    risk it exists to avoid, so a stopped month must record no adjustment on
    the date it stopped."""
    cursor = FakeCursor(_runaway_month_metrics())

    result = run_backtest_month(
        "NIFTY", 2025, 9, {"NIFTY"}, cursor, stop_loss_multiple=2.0
    )

    assert result["stopped_out"] is True
    # The first hedge date is 05-Sep (still calm); the runaway starts 08-Sep, so
    # at most that one adjustment can have happened before the stop.
    assert len(result["adjustment_legs"]) <= 1


def test_a_skipped_month_carries_its_variant() -> None:
    """Regression: variant was set only during pricing, so skipped months — which
    never reach pricing — were written under the default. Every variant's skip
    taxonomy silently merged into the base run's."""
    cursor = FakeCursor({})

    result = run_backtest_month("NIFTY", 2025, 9, {"NIFTY"}, cursor, variant="stop2")

    assert result["skipped_reason"] == "No Trading Days"
    assert result["variant"] == "stop2"
    _, params = cursor.executed[-1]
    assert params[18] == "stop2"
