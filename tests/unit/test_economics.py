"""Unit tests for mktmtm.backtest.economics (costs, margin, slippage).

These pin the arithmetic that turns ``pnl_points`` into a return. Every one of
them exists because getting it wrong produces a plausible number rather than an
error — the failure mode this project keeps meeting.
"""

from datetime import date

import pytest

from mktmtm.backtest.economics import (
    DEFAULT_RATE_SETS,
    CostRates,
    MarginModel,
    Order,
    apply_slippage,
    contract_value,
    order_costs,
    resolve_rates,
    strangle_margin,
    strangle_orders,
    trade_costs,
)

RATES = CostRates(effective_from=date(2024, 10, 1), source="test")


# ───────────────────────────── contract value ──────────────────────────────


def test_contract_value_multiplies_by_lot() -> None:
    """Points are per share; a contract covers lot_size shares."""
    assert contract_value(20.0, 500) == 10_000.0


def test_contract_value_carries_the_sign_of_a_loss() -> None:
    assert contract_value(-12.5, 400) == -5_000.0


def test_contract_value_rejects_a_zero_lot() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        contract_value(20.0, 0)


def test_same_points_on_different_lots_are_different_money() -> None:
    """The whole reason pnl_points is not comparable across symbols."""
    assert contract_value(20.0, 500) != contract_value(20.0, 1_200)


# ───────────────────────────── order costs ─────────────────────────────────


def test_stt_is_charged_on_sell_only() -> None:
    """STT on options applies to the sell side. A writer pays it at entry."""
    sell = order_costs(Order(50.0, 500, "sell"), RATES)
    buy = order_costs(Order(50.0, 500, "buy"), RATES)

    assert sell.stt == pytest.approx(50.0 * 500 * RATES.stt_sell_pct)
    assert buy.stt == 0.0


def test_stamp_duty_is_charged_on_buy_only() -> None:
    sell = order_costs(Order(50.0, 500, "sell"), RATES)
    buy = order_costs(Order(50.0, 500, "buy"), RATES)

    assert sell.stamp_duty == 0.0
    assert buy.stamp_duty == pytest.approx(50.0 * 500 * RATES.stamp_duty_buy_pct)


def test_gst_applies_to_broker_and_exchange_charges_not_to_stt() -> None:
    """GST is levied on brokerage + exchange + SEBI + IPFT. Not on STT."""
    cost = order_costs(Order(50.0, 500, "sell"), RATES)
    base = cost.brokerage + cost.exchange + cost.sebi + cost.ipft

    assert cost.gst == pytest.approx(base * RATES.gst_pct)
    # If STT were in the base the GST would be far larger.
    assert cost.gst < cost.stt


def test_percentage_charges_scale_with_turnover_but_brokerage_does_not() -> None:
    small = order_costs(Order(10.0, 500, "sell"), RATES)
    large = order_costs(Order(100.0, 500, "sell"), RATES)

    assert large.stt == pytest.approx(small.stt * 10)
    assert large.brokerage == small.brokerage  # flat per order


def test_breakdown_total_is_the_sum_of_its_parts() -> None:
    cost = order_costs(Order(50.0, 500, "sell"), RATES)
    parts = (
        cost.brokerage
        + cost.stt
        + cost.exchange
        + cost.sebi
        + cost.ipft
        + cost.stamp_duty
        + cost.gst
    )
    assert cost.total == pytest.approx(parts)


# ───────────────────────────── trade costs ─────────────────────────────────


def test_strangle_order_count_is_four_plus_two_per_adjustment() -> None:
    """The number the cost model exists to get right."""
    for n in range(5):
        adjustments = [(30.0, 45.0)] * n
        orders = strangle_orders(50, 45, 20, 18, 500, adjustments)
        assert len(orders) == 4 + 2 * n


def test_entry_legs_are_sells_and_exit_legs_are_buys() -> None:
    orders = strangle_orders(50, 45, 20, 18, 500)

    assert [o.side for o in orders[:2]] == ["sell", "sell"]
    assert [o.side for o in orders[-2:]] == ["buy", "buy"]


def test_each_adjustment_is_a_buy_then_a_sell() -> None:
    orders = strangle_orders(50, 45, 20, 18, 500, [(30.0, 45.0)])

    assert [o.side for o in orders] == ["sell", "sell", "buy", "sell", "buy", "buy"]


def test_adjustments_make_a_trade_materially_more_expensive() -> None:
    """The weekly hedge is the strategy's most expensive habit; charging only
    entry and exit understates a typical trade roughly threefold."""
    bare = trade_costs(strangle_orders(50, 45, 20, 18, 500), RATES)
    hedged = trade_costs(
        strangle_orders(50, 45, 20, 18, 500, [(30.0, 45.0)] * 3), RATES
    )

    assert hedged.n_orders == 10
    assert bare.n_orders == 4
    assert hedged.total > bare.total * 2


def test_trade_costs_sum_their_orders() -> None:
    orders = strangle_orders(50, 45, 20, 18, 500, [(30.0, 45.0)])
    total = trade_costs(orders, RATES)
    by_hand = sum(order_costs(o, RATES).total for o in orders)

    assert total.total == pytest.approx(by_hand)
    assert total.n_orders == len(orders)


# ───────────────────────────── rate resolution ─────────────────────────────


def test_rates_resolve_to_the_set_in_force_not_the_latest() -> None:
    """Same trap as lot sizes: use the rate in force on the trade date."""
    early = resolve_rates(date(2022, 6, 1), DEFAULT_RATE_SETS)
    late = resolve_rates(date(2025, 6, 1), DEFAULT_RATE_SETS)

    assert early.effective_from == date(2021, 1, 1)
    assert late.effective_from == date(2024, 10, 1)
    assert early.stt_sell_pct != late.stt_sell_pct


def test_rates_resolve_exactly_on_the_effective_date() -> None:
    rates = resolve_rates(date(2024, 10, 1), DEFAULT_RATE_SETS)
    assert rates.effective_from == date(2024, 10, 1)


def test_a_day_before_every_known_rate_set_raises() -> None:
    """A trade priced under rates that did not exist is a fabricated number."""
    with pytest.raises(ValueError, match="no cost rates effective"):
        resolve_rates(date(2019, 1, 1), DEFAULT_RATE_SETS)


def test_negative_rates_are_rejected() -> None:
    with pytest.raises(ValueError, match="stt_sell_pct"):
        CostRates(effective_from=date(2024, 1, 1), source="bad", stt_sell_pct=-0.1)


# ───────────────────────────── margin ──────────────────────────────────────


def test_margin_is_charged_on_underlying_notional() -> None:
    model = MarginModel(span_pct=0.09, exposure_pct=0.03)
    assert strangle_margin(1_000.0, 500, model) == pytest.approx(
        1_000.0 * 500 * 0.12
    )


def test_margin_is_monotone_in_notional() -> None:
    model = MarginModel()
    assert strangle_margin(2_000.0, 500, model) > strangle_margin(1_000.0, 500, model)
    assert strangle_margin(1_000.0, 1_000, model) > strangle_margin(1_000.0, 500, model)


def test_margin_sensitivity_band_is_reachable_by_scaling() -> None:
    """Phase 0 must report the verdict at +/-25% on this assumption."""
    base = MarginModel()
    assert base.scaled(1.25).total_pct == pytest.approx(base.total_pct * 1.25)
    assert base.scaled(0.75).total_pct == pytest.approx(base.total_pct * 0.75)


def test_scaling_margin_does_not_mutate_the_original() -> None:
    base = MarginModel()
    before = base.total_pct
    base.scaled(2.0)
    assert base.total_pct == before


def test_margin_rejects_a_nonpositive_scale() -> None:
    with pytest.raises(ValueError, match="factor"):
        MarginModel().scaled(0.0)


# ───────────────────────────── slippage ────────────────────────────────────


def test_slippage_always_runs_against_the_trader() -> None:
    """A sell fills below the mark; a buy fills above it. Never the reverse."""
    assert apply_slippage(100.0, "sell", 0.02) == pytest.approx(98.0)
    assert apply_slippage(100.0, "buy", 0.02) == pytest.approx(102.0)


def test_zero_slippage_is_the_mark() -> None:
    assert apply_slippage(100.0, "sell", 0.0) == 100.0
    assert apply_slippage(100.0, "buy", 0.0) == 100.0


def test_slippage_hurts_a_strangle_at_both_ends() -> None:
    """Sold legs come in lower and bought-back legs cost more, so the credit
    shrinks from both sides — the reason breakeven slippage is small."""
    credit = apply_slippage(50.0, "sell", 0.03) + apply_slippage(45.0, "sell", 0.03)
    cost = apply_slippage(20.0, "buy", 0.03) + apply_slippage(18.0, "buy", 0.03)
    clean = (50.0 + 45.0) - (20.0 + 18.0)

    assert credit - cost < clean


def test_slippage_fraction_must_be_a_fraction() -> None:
    with pytest.raises(ValueError, match="fraction"):
        apply_slippage(100.0, "sell", 1.0)
    with pytest.raises(ValueError, match="fraction"):
        apply_slippage(100.0, "sell", -0.01)


# ───────────────────────────── validation ──────────────────────────────────


def test_order_rejects_a_bad_side() -> None:
    with pytest.raises(ValueError, match="side"):
        Order(50.0, 500, "hold")  # type: ignore[arg-type]


def test_order_rejects_a_nonpositive_lot() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        Order(50.0, 0, "sell")


def test_order_turnover_is_premium_times_lot() -> None:
    assert Order(50.0, 500, "sell").turnover == 25_000.0


# ───────────────────────────── whole-trade pricing ─────────────────────────


def test_trade_economics_decomposes_exactly() -> None:
    """net = gross - costs - slippage, to the rupee. The columns must add up."""
    from mktmtm.backtest.economics import price_strangle_trade

    econ = price_strangle_trade(
        entry_call=22.0,
        entry_put=20.0,
        exit_call=8.0,
        exit_put=7.0,
        adjustments=[(10.0, 19.0)] * 3,
        lot_size=500,
        entry_spot=1_000.0,
        entry_date=date(2025, 6, 1),
        slippage_fraction=0.02,
    )

    assert econ.net_pnl_rupees == pytest.approx(
        econ.gross_pnl_rupees - econ.costs_rupees - econ.slippage_rupees
    )
    assert econ.n_orders == 10
    assert econ.n_adjustments == 3


def test_trade_pnl_counts_every_leg_ever_sold() -> None:
    """The accounting bug this fixed, pinned.

    Entry CE@50/PE@45; one adjustment buys the call back at 30 and re-sells at
    58; exit CE@20/PE@25. Leg by leg: (50-30) + (58-20) + (45-25) = 78.

    The superseded formula (initial credit - final exit cost + adjustment PnL)
    gives 70: it credits the ORIGINAL call sale but debits the REPLACEMENT's
    buyback, losing the replacement's own 58 of credit.
    """
    from mktmtm.backtest.economics import price_strangle_trade

    econ = price_strangle_trade(
        entry_call=50.0,
        entry_put=45.0,
        exit_call=20.0,
        exit_put=25.0,
        adjustments=[(30.0, 58.0)],
        lot_size=1,
        entry_spot=1_000.0,
        entry_date=date(2025, 6, 1),
    )

    assert econ.gross_pnl_rupees == pytest.approx(78.0)
    assert econ.gross_pnl_rupees != pytest.approx(70.0)


def test_slippage_only_ever_reduces_the_result() -> None:
    from mktmtm.backtest.economics import price_strangle_trade

    kwargs = dict(
        entry_call=22.0,
        entry_put=20.0,
        exit_call=8.0,
        exit_put=7.0,
        adjustments=[(10.0, 19.0)],
        lot_size=500,
        entry_spot=1_000.0,
        entry_date=date(2025, 6, 1),
    )
    clean = price_strangle_trade(**kwargs, slippage_fraction=0.0)  # type: ignore[arg-type]
    slipped = price_strangle_trade(**kwargs, slippage_fraction=0.03)  # type: ignore[arg-type]

    assert clean.slippage_rupees == pytest.approx(0.0)
    assert slipped.slippage_rupees > 0.0
    assert slipped.net_pnl_rupees < clean.net_pnl_rupees
    # Gross is measured at the marks, so it is unchanged by a fill assumption.
    assert slipped.gross_pnl_rupees == pytest.approx(clean.gross_pnl_rupees)


def test_margin_sensitivity_changes_the_return_not_the_pnl() -> None:
    """Margin scales the denominator only — it cannot turn a loss into a gain."""
    from mktmtm.backtest.economics import MarginModel, price_strangle_trade

    kwargs = dict(
        entry_call=22.0,
        entry_put=20.0,
        exit_call=8.0,
        exit_put=7.0,
        adjustments=[],
        lot_size=500,
        entry_spot=1_000.0,
        entry_date=date(2025, 6, 1),
    )
    base = price_strangle_trade(**kwargs, margin_model=MarginModel())  # type: ignore[arg-type]
    heavy = price_strangle_trade(**kwargs, margin_model=MarginModel().scaled(1.25))  # type: ignore[arg-type]

    assert heavy.net_pnl_rupees == pytest.approx(base.net_pnl_rupees)
    assert heavy.return_on_margin == pytest.approx(base.return_on_margin / 1.25)


def test_trade_uses_the_rate_set_in_force_on_the_entry_date() -> None:
    from mktmtm.backtest.economics import price_strangle_trade

    kwargs = dict(
        entry_call=22.0,
        entry_put=20.0,
        exit_call=8.0,
        exit_put=7.0,
        adjustments=[],
        lot_size=500,
        entry_spot=1_000.0,
    )
    early = price_strangle_trade(**kwargs, entry_date=date(2022, 6, 1))  # type: ignore[arg-type]
    late = price_strangle_trade(**kwargs, entry_date=date(2025, 6, 1))  # type: ignore[arg-type]

    assert early.rates_effective_from == date(2021, 1, 1)
    assert late.rates_effective_from == date(2024, 10, 1)


def test_a_losing_trade_reports_a_negative_return() -> None:
    from mktmtm.backtest.economics import price_strangle_trade

    econ = price_strangle_trade(
        entry_call=22.0,
        entry_put=20.0,
        exit_call=80.0,  # the underlying ran through the call strike
        exit_put=2.0,
        adjustments=[],
        lot_size=500,
        entry_spot=1_000.0,
        entry_date=date(2025, 6, 1),
    )

    assert econ.net_pnl_rupees < 0
    assert econ.return_on_margin < 0
