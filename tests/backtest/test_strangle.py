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
) -> dict:
    return {
        "type": typ,
        "expiry": expiry,
        "strike": strike,
        "delta": delta,
        "settle": settle,
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
        {"type": "CE", "expiry": expiry_str, "strike": 24000.0, "settle": 0.0},
        {"type": "CE", "expiry": expiry_str, "strike": 24500.0, "settle": -5.0},
        {"type": "CE", "expiry": expiry_str, "strike": 25000.0, "settle": 30.0},
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
    assert "ON CONFLICT (symbol, year, month)" in query
    assert "hedge_pnl_points" in query
    assert params[0] == "NIFTY"
    assert params[17] is None  # hedge_pnl_points defaulted to None


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
