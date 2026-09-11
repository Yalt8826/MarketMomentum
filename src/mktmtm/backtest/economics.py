"""Trade economics for the short-strangle strategy (pure math, no I/O).

``strangle.py`` reports ``pnl_points`` — points per share. That is not a return
and is not comparable across symbols: 20 points on a ₹200 stock and on a ₹4,000
stock are wholly different trades and are recorded identically. This module
supplies the four missing pieces that turn points into a return on capital:

1. **Lot size** — points per share to rupees per contract.
2. **Transaction costs** — itemised, per *order*. The strategy places
   ``4 + 2 * n_adjustments`` orders, typically 10-12, so a model that charges
   only entry and exit understates the true figure roughly threefold.
3. **Margin** — the denominator. Without it there is no return, only a P&L.
4. **Slippage** — bhavcopy publishes no bid/ask, so fills are assumed at
   ``settle``. See :func:`apply_slippage` and the breakeven-slippage note below.

Pure functions over plain dataclasses, unit-tested standalone, in the same
spirit as ``options/pricing.py``: this is where an arithmetic slip silently
becomes a financial claim.

## On the rates

**Statutory rates change**, and a backtest run under undated rates is not so
much wrong as unattributable. Every rate therefore lives in a dated
:class:`CostRates` set resolved by trade date (:func:`resolve_rates`), never
hard-coded inside a function. ``DEFAULT_RATE_SETS`` carries documented defaults
that **must be verified against your broker's contract note and the current NSE
circulars before any result is relied upon** — they are a starting point, not an
authority.

## On the margin model

Exchange margin is SPAN + exposure, computed from a daily risk array NSE does
not publish in bhavcopy. It **cannot be reproduced exactly**. :class:`MarginModel`
is an explicit, parameterised approximation; run any conclusion through
:meth:`MarginModel.scaled` at ±25% and report all three. Margin only scales the
denominator, so it cannot turn a loss into a profit — but it entirely decides
whether a profit is interesting.

## On slippage

There is no bid/ask in the data, so this module does not try to estimate a
spread. It applies a configurable fraction **always against the trader** and the
analysis layer reports the *breakeven* slippage — the level at which the edge
reaches zero. That is a claim that survives not knowing the true spread.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

__all__ = [
    "CostBreakdown",
    "CostRates",
    "TradeEconomics",
    "BROKERAGE_SCENARIOS",
    "DEFAULT_RATE_SETS",
    "MarginModel",
    "Order",
    "Side",
    "apply_slippage",
    "contract_value",
    "order_costs",
    "price_strangle_trade",
    "rate_sets",
    "resolve_rates",
    "strangle_margin",
    "strangle_orders",
    "trade_costs",
]

Side = Literal["buy", "sell"]


# ═════════════════════════════ rates ═══════════════════════════════════════


@dataclass(frozen=True)
class CostRates:
    """Broker and statutory charges, as fractions of turnover unless noted.

    ``effective_from`` and ``source`` are not decoration: a result quoted
    without the rate set that produced it cannot be reproduced or audited.

    Turnover for an option order is ``premium * lot_size`` — the premium
    turnover, not the notional value of the underlying.
    """

    effective_from: date
    source: str
    #: Flat charge per executed order. Discount brokers bill options this way.
    brokerage_per_order: float = 20.0
    #: Securities Transaction Tax — **sell side only**, on premium turnover.
    #: The single largest component for a net writer.
    stt_sell_pct: float = 0.001
    #: NSE transaction charge on premium turnover.
    exchange_txn_pct: float = 0.0003503
    #: SEBI turnover fee (₹10 per crore).
    sebi_turnover_pct: float = 0.000001
    #: Investor Protection Fund Trust levy (₹50 per crore).
    ipft_pct: float = 0.000005
    #: Stamp duty — **buy side only**.
    stamp_duty_buy_pct: float = 0.00003
    #: GST on (brokerage + exchange + SEBI + IPFT). Not on STT or stamp duty.
    gst_pct: float = 0.18

    def __post_init__(self) -> None:
        for name in (
            "brokerage_per_order",
            "stt_sell_pct",
            "exchange_txn_pct",
            "sebi_turnover_pct",
            "ipft_pct",
            "stamp_duty_buy_pct",
            "gst_pct",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0, got {getattr(self, name)}")


#: Statutory rate regimes across the project's 2021-2026 window, newest last.
#:
#: **STT tripled over this window** and is the largest statutory component for a
#: writer, so the three regimes are encoded separately rather than averaged:
#:
#:   0.05%    until 2023-03-31
#:   0.0625%  from 2023-04-01  (Finance Act 2023, NSE circular FATAX56235)
#:   0.1%     from 2024-10-01  (Finance Act 2024)
#:
#: NSE options transaction charges fell at the same October 2024 boundary, from
#: 0.0495% to 0.03503% of premium (Rs 35.03 per lakh) under the SEBI uniform-fee
#: circular of July 2024 — so that date moves two rates in opposite directions.
#:
#: ``brokerage_per_order`` here is a placeholder: it is **broker-specific and
#: dominates the total** (roughly Rs 200 of Rs 316 on a typical trade), so it is
#: overridden from ``Settings.backtest_brokerage_per_order`` and swept across
#: :data:`BROKERAGE_SCENARIOS` rather than assumed. See :func:`rate_sets`.
DEFAULT_RATE_SETS: tuple[CostRates, ...] = (
    CostRates(
        effective_from=date(2004, 10, 1),
        source="STT 0.05% (pre-Finance-Act-2023); NSE options txn 0.0495%",
        stt_sell_pct=0.0005,
        exchange_txn_pct=0.000495,
    ),
    CostRates(
        effective_from=date(2023, 4, 1),
        source="Finance Act 2023: STT 0.05% -> 0.0625% (NSE FATAX56235)",
        stt_sell_pct=0.000625,
        exchange_txn_pct=0.000495,
    ),
    CostRates(
        effective_from=date(2024, 10, 1),
        source="Finance Act 2024: STT -> 0.1%; SEBI uniform fees: NSE txn -> 0.03503%",
        stt_sell_pct=0.001,
        exchange_txn_pct=0.0003503,
    ),
)

#: Brokerage per executed order, in rupees, across realistic broker models.
#: Phase 0 reports its verdict under **every** one of these, the way it reports
#: margin at +/-25% and slippage at its breakeven: the dominant cost component
#: must not be a single unexamined assumption.
BROKERAGE_SCENARIOS: dict[str, float] = {
    "zero": 0.0,        # brokers offering free F&O
    "discount": 20.0,   # the Rs 20 flat-fee standard
    "premium": 40.0,    # a costlier discount broker, or partial fills
}


def rate_sets(
    brokerage_per_order: float | None = None,
    base: tuple[CostRates, ...] = DEFAULT_RATE_SETS,
) -> tuple[CostRates, ...]:
    """``base`` with ``brokerage_per_order`` substituted into every regime.

    Statutory rates are facts and are pinned; brokerage is a user-specific
    parameter that belongs in configuration. ``None`` reads
    ``Settings.backtest_brokerage_per_order``.
    """
    if brokerage_per_order is None:
        from mktmtm.config.settings import settings

        brokerage_per_order = settings.backtest_brokerage_per_order
    return tuple(
        replace(rates, brokerage_per_order=brokerage_per_order) for rates in base
    )


def resolve_rates(
    day: date, rate_sets: tuple[CostRates, ...] = DEFAULT_RATE_SETS
) -> CostRates:
    """The rate set in force on ``day`` (last ``effective_from`` <= day).

    Mirrors ``data.lots.LotSizeSchedule.get``: resolve by date, and raise rather
    than silently falling back when the day precedes every known set. A trade
    priced under rates that did not yet exist is a fabricated number.
    """
    if not rate_sets:
        raise ValueError("rate_sets must not be empty")
    ordered = sorted(rate_sets, key=lambda r: r.effective_from)
    if day < ordered[0].effective_from:
        raise ValueError(
            f"no cost rates effective on {day} — earliest is "
            f"{ordered[0].effective_from}"
        )
    chosen = ordered[0]
    for rates in ordered:
        if rates.effective_from <= day:
            chosen = rates
        else:
            break
    return chosen


# ═════════════════════════════ orders and costs ════════════════════════════


@dataclass(frozen=True)
class Order:
    """One executed order: a single leg, one side, one point in time."""

    premium: float
    lot_size: int
    side: Side

    def __post_init__(self) -> None:
        if self.premium < 0:
            raise ValueError(f"premium must be >= 0, got {self.premium}")
        if self.lot_size <= 0:
            raise ValueError(f"lot_size must be > 0, got {self.lot_size}")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")

    @property
    def turnover(self) -> float:
        """Premium turnover in rupees — the base for every percentage charge."""
        return self.premium * self.lot_size


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised charges. Kept itemised because when a result is marginal, the
    question "which component killed it" has to be answerable."""

    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    ipft: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    n_orders: int = 0

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange
            + self.sebi
            + self.ipft
            + self.stamp_duty
            + self.gst
        )

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange=self.exchange + other.exchange,
            sebi=self.sebi + other.sebi,
            ipft=self.ipft + other.ipft,
            stamp_duty=self.stamp_duty + other.stamp_duty,
            gst=self.gst + other.gst,
            n_orders=self.n_orders + other.n_orders,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "brokerage": self.brokerage,
            "stt": self.stt,
            "exchange": self.exchange,
            "sebi": self.sebi,
            "ipft": self.ipft,
            "stamp_duty": self.stamp_duty,
            "gst": self.gst,
            "total": self.total,
            "n_orders": float(self.n_orders),
        }


def contract_value(points: float, lot_size: int) -> float:
    """Points per share -> rupees for one contract.

    The conversion ``strangle.py`` never applies. Note this multiplies: a lot
    is how many shares one contract covers. (Contrast bhavcopy's ``OPEN_INT``,
    which is already in shares and therefore *divides* by the lot to give a
    contract count — the inverse trap, documented in CLAUDE.md.)
    """
    if lot_size <= 0:
        raise ValueError(f"lot_size must be > 0, got {lot_size}")
    return points * lot_size


def order_costs(order: Order, rates: CostRates) -> CostBreakdown:
    """All-in cost of one order, itemised.

    STT applies to the sell side only; stamp duty to the buy side only. GST is
    levied on the brokerage and exchange-side charges, never on STT or stamp
    duty.
    """
    turnover = order.turnover
    is_sell = order.side == "sell"

    brokerage = rates.brokerage_per_order
    stt = turnover * rates.stt_sell_pct if is_sell else 0.0
    exchange = turnover * rates.exchange_txn_pct
    sebi = turnover * rates.sebi_turnover_pct
    ipft = turnover * rates.ipft_pct
    stamp_duty = 0.0 if is_sell else turnover * rates.stamp_duty_buy_pct
    gst = (brokerage + exchange + sebi + ipft) * rates.gst_pct

    return CostBreakdown(
        brokerage=brokerage,
        stt=stt,
        exchange=exchange,
        sebi=sebi,
        ipft=ipft,
        stamp_duty=stamp_duty,
        gst=gst,
        n_orders=1,
    )


def trade_costs(orders: list[Order], rates: CostRates) -> CostBreakdown:
    """Total cost across every order in a trade, itemised."""
    total = CostBreakdown()
    for order in orders:
        total = total + order_costs(order, rates)
    return total


def strangle_orders(
    entry_call: float,
    entry_put: float,
    exit_call: float,
    exit_put: float,
    lot_size: int,
    adjustments: list[tuple[float, float]] | None = None,
) -> list[Order]:
    """The full order list for one strangle: ``4 + 2 * n_adjustments`` orders.

    Each adjustment is ``(closed_leg_premium, reopened_leg_premium)`` — the
    weekly hedge buys one leg back and sells a replacement, so it costs two
    orders, not zero. Charging only entry and exit understates a typical trade
    roughly threefold, and the adjustment is the strategy's most expensive
    habit.
    """
    orders = [
        Order(entry_call, lot_size, "sell"),
        Order(entry_put, lot_size, "sell"),
    ]
    for closed_premium, reopened_premium in adjustments or []:
        orders.append(Order(closed_premium, lot_size, "buy"))
        orders.append(Order(reopened_premium, lot_size, "sell"))
    orders.append(Order(exit_call, lot_size, "buy"))
    orders.append(Order(exit_put, lot_size, "buy"))
    return orders


# ═════════════════════════════ margin ══════════════════════════════════════


@dataclass(frozen=True)
class MarginModel:
    """Approximation of SPAN + exposure margin for a short strangle.

    **This is an approximation and must always be reported as one.** Real SPAN
    is computed by the exchange from a daily risk array that bhavcopy does not
    carry. Defaults sit in the range commonly quoted for a two-sided short
    options position on a single stock; a strangle attracts less than two naked
    legs because both cannot finish in the money.

    Run every conclusion through :meth:`scaled` at 0.75 and 1.25 and report all
    three. If the verdict flips inside that band, the verdict is this
    assumption, not the strategy.
    """

    span_pct: float = 0.09
    exposure_pct: float = 0.03
    note: str = "approximation; not exchange SPAN. Verify against a broker margin calculator."

    def __post_init__(self) -> None:
        if self.span_pct < 0 or self.exposure_pct < 0:
            raise ValueError("margin percentages must be >= 0")

    @property
    def total_pct(self) -> float:
        return self.span_pct + self.exposure_pct

    def scaled(self, factor: float) -> MarginModel:
        """This model with both components scaled — the sensitivity hook."""
        if factor <= 0:
            raise ValueError(f"factor must be > 0, got {factor}")
        return replace(
            self,
            span_pct=self.span_pct * factor,
            exposure_pct=self.exposure_pct * factor,
        )


def strangle_margin(
    spot: float, lot_size: int, model: MarginModel = MarginModel()
) -> float:
    """Approximate margin blocked by one short strangle, in rupees.

    Charged on the underlying's contract notional (``spot * lot_size``), not on
    premium turnover: the risk being margined is the underlying's movement.
    """
    if spot <= 0:
        raise ValueError(f"spot must be > 0, got {spot}")
    if lot_size <= 0:
        raise ValueError(f"lot_size must be > 0, got {lot_size}")
    return spot * lot_size * model.total_pct


# ═════════════════════════════ slippage ════════════════════════════════════


def apply_slippage(premium: float, side: Side, fraction: float) -> float:
    """Fill price after slippage — **always against the trader**.

    A sell fills *below* the mark and a buy fills *above* it. There is no
    bid/ask in bhavcopy, so ``fraction`` is a parameter to be swept rather than
    a measurement; the analysis layer reports the breakeven value.
    """
    if premium < 0:
        raise ValueError(f"premium must be >= 0, got {premium}")
    if not 0.0 <= fraction < 1.0:
        raise ValueError(f"fraction must be in [0, 1), got {fraction}")
    if side == "sell":
        return premium * (1.0 - fraction)
    if side == "buy":
        return premium * (1.0 + fraction)
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


# ═════════════════════════════ the whole trade ═════════════════════════════


@dataclass(frozen=True)
class TradeEconomics:
    """One strangle priced end to end: points in, rupees and a return out."""

    lot_size: int
    n_orders: int
    n_adjustments: int
    entry_spot: float
    gross_pnl_rupees: float
    costs_rupees: float
    slippage_rupees: float
    net_pnl_rupees: float
    margin_rupees: float
    return_on_margin: float
    cost_breakdown: CostBreakdown
    rates_effective_from: date
    slippage_fraction: float
    margin_pct: float

    def as_row(self) -> dict[str, object]:
        """Flat mapping for ``backtest_results``."""
        return {
            "lot_size": self.lot_size,
            "n_orders": self.n_orders,
            "n_adjustments": self.n_adjustments,
            "entry_spot": self.entry_spot,
            "gross_pnl_rupees": self.gross_pnl_rupees,
            "costs_rupees": self.costs_rupees,
            "slippage_rupees": self.slippage_rupees,
            "net_pnl_rupees": self.net_pnl_rupees,
            "margin_rupees": self.margin_rupees,
            "return_on_margin": self.return_on_margin,
            "cost_breakdown": self.cost_breakdown.as_dict(),
            "rates_effective_from": self.rates_effective_from,
            "slippage_fraction": self.slippage_fraction,
            "margin_pct": self.margin_pct,
        }


def price_strangle_trade(
    *,
    entry_call: float,
    entry_put: float,
    exit_call: float,
    exit_put: float,
    adjustments: list[tuple[float, float]],
    lot_size: int,
    entry_spot: float,
    entry_date: date,
    slippage_fraction: float = 0.0,
    margin_model: MarginModel = MarginModel(),
    rate_sets: tuple[CostRates, ...] = DEFAULT_RATE_SETS,
) -> TradeEconomics:
    """Price one strangle: slippage, then costs, then a return on margin. Pure.

    Slippage is applied to every leg **first**, because it changes the premiums
    that costs are then charged on — a sold leg fills lower, so its STT is lower
    too. Applying costs to the unslipped marks would overstate them slightly and,
    more importantly, would not be what a contract note shows.

    ``slippage_rupees`` is reported as the *difference* between the clean and
    slipped gross P&L, so the three money columns decompose exactly:
    ``net = gross − costs − slippage``, with ``gross`` measured at the marks.
    """
    rates = resolve_rates(entry_date, rate_sets)

    clean_gross_points = (entry_call + entry_put) - (exit_call + exit_put)
    for closed, reopened in adjustments:
        # Each adjustment banks (sold price − buyback price) on the closed leg
        # and re-establishes a new one; the reopened premium is credit received.
        clean_gross_points += reopened - closed

    f = slippage_fraction
    slipped_entry_call = apply_slippage(entry_call, "sell", f)
    slipped_entry_put = apply_slippage(entry_put, "sell", f)
    slipped_exit_call = apply_slippage(exit_call, "buy", f)
    slipped_exit_put = apply_slippage(exit_put, "buy", f)
    slipped_adjustments = [
        (apply_slippage(closed, "buy", f), apply_slippage(reopened, "sell", f))
        for closed, reopened in adjustments
    ]

    slipped_gross_points = (slipped_entry_call + slipped_entry_put) - (
        slipped_exit_call + slipped_exit_put
    )
    for closed, reopened in slipped_adjustments:
        slipped_gross_points += reopened - closed

    orders = strangle_orders(
        slipped_entry_call,
        slipped_entry_put,
        slipped_exit_call,
        slipped_exit_put,
        lot_size,
        slipped_adjustments,
    )
    breakdown = trade_costs(orders, rates)

    gross = contract_value(clean_gross_points, lot_size)
    slippage_cost = gross - contract_value(slipped_gross_points, lot_size)
    net = gross - breakdown.total - slippage_cost
    margin = strangle_margin(entry_spot, lot_size, margin_model)

    return TradeEconomics(
        lot_size=lot_size,
        n_orders=breakdown.n_orders,
        n_adjustments=len(adjustments),
        entry_spot=entry_spot,
        gross_pnl_rupees=gross,
        costs_rupees=breakdown.total,
        slippage_rupees=slippage_cost,
        net_pnl_rupees=net,
        margin_rupees=margin,
        return_on_margin=net / margin if margin > 0 else 0.0,
        cost_breakdown=breakdown,
        rates_effective_from=rates.effective_from,
        slippage_fraction=f,
        margin_pct=margin_model.total_pct,
    )
