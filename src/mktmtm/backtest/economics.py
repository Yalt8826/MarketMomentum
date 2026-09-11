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
    "DEFAULT_RATE_SETS",
    "MarginModel",
    "Order",
    "Side",
    "apply_slippage",
    "contract_value",
    "order_costs",
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


#: Documented starting point, newest last. **Verify before relying on these.**
DEFAULT_RATE_SETS: tuple[CostRates, ...] = (
    CostRates(
        effective_from=date(2021, 1, 1),
        source="pre-2024 STT regime; VERIFY against contract notes",
        stt_sell_pct=0.000625,
        exchange_txn_pct=0.00053,
    ),
    CostRates(
        effective_from=date(2024, 10, 1),
        source="post-Oct-2024 STT revision; VERIFY against contract notes",
        stt_sell_pct=0.001,
        exchange_txn_pct=0.0003503,
    ),
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
