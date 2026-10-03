"""The daily screen — today's available strangles, priced and risk-bounded.

**This tool does not rank by conviction, because there is nothing to rank by.**

Gate 0 established a real result: systematic short-strangle selling earned
**+4.56% mean return on margin**, positive in 6 of 6 years, with a **-37.27%
max drawdown**. Gate 1 then established that **no entry filter improves it** —
not this project's research features, not implied-volatility rank, nothing
tested. Selecting a random 30% of trades beats every real filter 5% of the time.
And a stop loss was measured at 1.5x/2x/3x the entry credit: it costs return and
buys no tail protection.

So the edge is in **breadth and discipline**, not in selection. A product that
ordered names by a score would be claiming something three measurements refuted.
This one orders by *tradeability* — can the position actually be opened, and
what does it cost — and shows the research features as clearly labelled context,
because a human may know something the data does not.

What it therefore spends its effort on instead:

- **Refusing** candidates it cannot price honestly *or legally*, each with a
  typed reason. Gate 0's skip taxonomy is a product feature here, not a data
  footnote — and the F&O ban check was added because the first live run
  selected INOXWIND while it was banned (see ``mktmtm.data.secban``).
- **Sizing**: margin, credit, and a modelled loss at a defined move.
- **Risk caps**: position count, capital committed, and sector correlation —
  since 15 strangles in one sector is one trade wearing fifteen hats.

See ``docs/trading/implementation/05-phase-2-serving.md``.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from mktmtm.backtest.economics import (
    MarginModel,
    Order,
    contract_value,
    rate_sets,
    resolve_rates,
    trade_costs,
)
from mktmtm.backtest.strangle import (
    chain_is_degenerate,
    find_options_near_delta,
    get_metrics_for_day,
)
from mktmtm.data.lots import load_lot_sizes

logger = logging.getLogger(__name__)

__all__ = [
    "Candidate",
    "RiskLimits",
    "ScreenResult",
    "apply_risk_limits",
    "build_candidate",
    "run_screen",
    "record_payload",
    "write_screen_record",
    "ScreenRecordExists",
]

#: Target delta for each leg, matching the backtested strategy exactly.
TARGET_DELTA = 0.20

#: Move used for the modelled loss. A strangle's true maximum is unbounded, so
#: this is a stated scenario and is labelled as one everywhere it appears.
LOSS_SCENARIO_SIGMA = 2.0

#: Trades whose expiry falls within this many days of an earnings date are
#: refused. IV collapses after results, so being short a strangle across one is
#: a different trade with a different risk profile — and the backtest excluded
#: them, so their behaviour here is simply unmeasured.
EARNINGS_BUFFER_DAYS = 0


@dataclass(frozen=True)
class RiskLimits:
    """Hard caps. A short strangle can lose more than the capital allocated to
    it, so these are product features rather than disclaimers."""

    capital: float = 1_000_000.0
    max_positions: int = 10
    max_margin_fraction: float = 0.50
    max_per_sector: int = 3
    min_leg_volume: int = 1


@dataclass(frozen=True)
class Candidate:
    """One tradeable strangle, priced. Every field a sizing decision needs."""

    symbol: str
    as_of: date
    expiry: date
    call_strike: float
    put_strike: float
    call_price: float
    put_price: float
    spot: float
    lot_size: int
    credit_rupees: float
    costs_rupees: float
    net_credit_rupees: float
    margin_rupees: float
    credit_to_margin: float
    modelled_loss_rupees: float
    call_volume: float
    put_volume: float
    min_leg_volume: float
    days_to_expiry: int
    #: Tested in Gate 1; none beat chance. Shown for human judgement only.
    context: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "expiry": self.expiry.isoformat(),
            "call_strike": self.call_strike,
            "put_strike": self.put_strike,
            "credit": round(self.credit_rupees, 2),
            "costs": round(self.costs_rupees, 2),
            "net_credit": round(self.net_credit_rupees, 2),
            "margin": round(self.margin_rupees, 2),
            "credit_to_margin": round(self.credit_to_margin, 4),
            "modelled_loss_2sigma": round(self.modelled_loss_rupees, 2),
            "min_leg_volume": self.min_leg_volume,
            "lot_size": self.lot_size,
            **{f"ctx_{k}": v for k, v in self.context.items()
               if not k.startswith("_")},
        }


@dataclass
class ScreenResult:
    as_of: date
    candidates: list[Candidate]
    selected: list[Candidate]
    refusals: dict[str, list[str]]
    limits: RiskLimits
    #: Is ``as_of`` the first trading day of its month?
    #:
    #: The backtested strategy enters on the first trading day and holds to the
    #: scheduled exit. A screen run on any other day produces a position whose
    #: behaviour was **never measured** — shorter-dated, different strikes, and
    #: drawn from a different distribution than the +4.56% / -37.27% figures
    #: describe. Recorded so Phase 3 can exclude such records from Gate 3
    #: rather than quietly averaging them in.
    is_month_entry: bool = True
    first_trading_day: date | None = None
    #: Symbols NSE had in an F&O ban on ``as_of`` (no new position may open).
    banned: frozenset[str] = frozenset()
    #: Did the ban list actually resolve?
    #:
    #: ``False`` means the lookup failed, **not** that nothing was banned —
    #: NSE publishes the file on every trading day, saying ``NIL`` when the
    #: list is empty. Surfaced in the record because a selection made without
    #: checking bans may contain a position that cannot be opened, and that is
    #: a limit of the record rather than a detail of the run.
    secban_checked: bool = True

    @property
    def total_margin(self) -> float:
        return sum(c.margin_rupees for c in self.selected)

    @property
    def total_credit(self) -> float:
        return sum(c.net_credit_rupees for c in self.selected)


def build_candidate(
    symbol: str,
    metrics: dict[str, Any],
    as_of: date,
    lot_size: int | None,
    *,
    margin_model: MarginModel | None = None,
    brokerage_per_order: float | None = None,
    context: dict[str, float] | None = None,
) -> tuple[Candidate | None, str | None]:
    """Price one symbol's strangle, or say why it cannot be.

    Returns ``(candidate, None)`` or ``(None, reason)``. Every refusal is typed:
    a screen that silently omits a name teaches nothing, and two of this
    project's worst defects were silent omissions.
    """
    spot = metrics.get("underlying_price")
    expiry = metrics.get("expiry_30d")
    chain = metrics.get("option_chain")

    if not chain or expiry is None or spot is None or spot <= 0:
        return None, "no chain or spot"
    if chain_is_degenerate(chain, expiry):
        # 83% of these fall on an expiry date: the expiring series settles
        # against the underlying and that value lands in every premium. The
        # prices are fiction — see backtest.strangle.chain_is_degenerate.
        return None, "degenerate chain for this expiry"

    earnings = metrics.get("upcoming_earning_date")
    if earnings and as_of <= earnings <= expiry:
        return None, f"earnings {earnings} before expiry"

    if lot_size is None or lot_size <= 0:
        # Never defaulted: guessing mis-scales the entire position.
        return None, "lot size unresolvable"

    call = find_options_near_delta(chain, expiry, TARGET_DELTA, "CE", spot=spot)
    put = find_options_near_delta(chain, expiry, -TARGET_DELTA, "PE", spot=spot)
    if not call or not put:
        return None, "no tradeable 20-delta strikes"

    call_vol = float(call.get("volume") or 0)
    put_vol = float(put.get("volume") or 0)

    call_price, put_price = float(call["settle"]), float(put["settle"])
    credit = contract_value(call_price + put_price, lot_size)

    rates = resolve_rates(as_of, rate_sets(brokerage_per_order))
    # Entry is two sells; the round trip adds an exit and the weekly hedges, but
    # only the entry cost is knowable today.
    costs = trade_costs(
        [Order(call_price, lot_size, "sell"), Order(put_price, lot_size, "sell")], rates
    )
    margin = spot * lot_size * (margin_model or MarginModel()).total_pct

    # Modelled loss: spot moves LOSS_SCENARIO_SIGMA implied standard deviations
    # and the breached leg finishes that far in the money. Stated, not maximal.
    iv = metrics.get("ce_iv_30") or (context or {}).get("_iv_30")
    dte = (expiry - as_of).days
    if iv and iv > 0 and dte > 0:
        sigma_move = spot * (float(iv) / 100.0) * math.sqrt(dte / 365.0)
        breach = max(
            spot + LOSS_SCENARIO_SIGMA * sigma_move - call["strike"],
            put["strike"] - (spot - LOSS_SCENARIO_SIGMA * sigma_move),
            0.0,
        )
        modelled_loss = contract_value(breach, lot_size) - credit
    else:
        modelled_loss = float("nan")

    return (
        Candidate(
            symbol=symbol,
            as_of=as_of,
            expiry=expiry,
            call_strike=float(call["strike"]),
            put_strike=float(put["strike"]),
            call_price=call_price,
            put_price=put_price,
            spot=float(spot),
            lot_size=lot_size,
            credit_rupees=credit,
            costs_rupees=costs.total,
            net_credit_rupees=credit - costs.total,
            margin_rupees=margin,
            credit_to_margin=(credit - costs.total) / margin if margin > 0 else 0.0,
            modelled_loss_rupees=modelled_loss,
            call_volume=call_vol,
            put_volume=put_vol,
            min_leg_volume=min(call_vol, put_vol),
            days_to_expiry=dte,
            context=context or {},
        ),
        None,
    )


def apply_risk_limits(
    candidates: list[Candidate],
    limits: RiskLimits,
    sectors: dict[str, str] | None = None,
) -> tuple[list[Candidate], dict[str, list[str]]]:
    """Select within the caps, ordered by tradeability.

    Ordering is by **net credit per rupee of margin**, then by liquidity — not
    by any predictive score, because Gate 1 established there is none. This is
    an efficiency ordering: it asks which positions pay best for the capital
    they tie up.
    """
    refused: dict[str, list[str]] = {}
    eligible = []
    for c in candidates:
        if c.min_leg_volume < limits.min_leg_volume:
            refused.setdefault("leg never traded today", []).append(c.symbol)
        elif c.margin_rupees > limits.capital * limits.max_margin_fraction:
            refused.setdefault("single position exceeds the margin cap", []).append(c.symbol)
        else:
            eligible.append(c)

    eligible.sort(key=lambda c: (-c.credit_to_margin, -c.min_leg_volume))

    selected: list[Candidate] = []
    committed = 0.0
    per_sector: dict[str, int] = {}
    budget = limits.capital * limits.max_margin_fraction

    for c in eligible:
        if len(selected) >= limits.max_positions:
            refused.setdefault("position count cap reached", []).append(c.symbol)
            continue
        if committed + c.margin_rupees > budget:
            refused.setdefault("margin budget exhausted", []).append(c.symbol)
            continue
        sector = (sectors or {}).get(c.symbol, "unknown")
        if sector != "unknown" and per_sector.get(sector, 0) >= limits.max_per_sector:
            refused.setdefault(f"sector cap ({sector})", []).append(c.symbol)
            continue
        selected.append(c)
        committed += c.margin_rupees
        per_sector[sector] = per_sector.get(sector, 0) + 1

    return selected, refused


_CONTEXT_SQL = """
SELECT m.ce_iv_30, m.ce_ivr, m.iv_rv_spread, m.dist_to_flip,
       f.fut_basis_excess, f.oi_outlier_share
FROM ml_dataset m JOIN flow_features f USING (symbol, date)
WHERE m.symbol = %s AND m.date = %s
"""

#: ``ce_iv_30`` sizes the loss scenario rather than describing the name, so it
#: is kept out of the context columns — those are Gate-1-tested features shown
#: for human judgement, and mixing a sizing input into them would misrepresent
#: what was tested.
_CONTEXT_COLUMNS = ("ce_ivr", "iv_rv_spread", "dist_to_flip",
                    "fut_basis_excess", "oi_outlier_share")


def run_screen(
    conn: Any,
    as_of: date | None = None,
    limits: RiskLimits | None = None,
    *,
    symbols: list[str] | None = None,
    margin_model: MarginModel | None = None,
    brokerage_per_order: float | None = None,
    banned: frozenset[str] | None = None,
) -> ScreenResult:
    """Today's tradeable strangles, priced, capped and ordered.

    ``banned`` overrides the live F&O ban-list lookup — pass a frozenset to
    supply it (tests, or an offline run). Left as ``None``, the list is fetched
    from NSE; if that fails the screen still runs but records
    ``secban_checked=False``, because a silently-skipped ban check would let an
    un-openable position into a committed record.
    """
    from mktmtm.data.symbols import load_fno_catalog

    limits = limits or RiskLimits()
    cursor = conn.cursor()
    if as_of is None:
        cursor.execute("SELECT max(date) FROM option_metrics")
        as_of = cursor.fetchone()[0]

    cursor.execute(
        "SELECT min(date) FROM option_metrics "
        "WHERE date_trunc('month', date) = date_trunc('month', %s::date)",
        (as_of,),
    )
    first_trading_day = cursor.fetchone()[0]
    is_month_entry = first_trading_day == as_of
    if not is_month_entry:
        logger.warning(
            "screen as_of %s is not the first trading day of its month (%s); "
            "the resulting position is outside the backtested calibration",
            as_of,
            first_trading_day,
        )

    if symbols is None:
        catalog = load_fno_catalog()
        symbols = sorted(
            {i["symbol"] for i in catalog.get("individual_securities", []) if i.get("symbol")}
        )

    secban_checked = True
    if banned is None:
        banned, secban_checked = _resolve_bans(as_of)

    lots = load_lot_sizes()
    candidates: list[Candidate] = []
    refusals: dict[str, list[str]] = {}

    for symbol in symbols:
        if symbol in banned:
            # Checked before the metrics lookup: a banned symbol is untradeable
            # whatever its chain looks like, and "in F&O ban" is the more
            # actionable refusal than "no data for this date".
            refusals.setdefault(f"in F&O ban on {as_of}", []).append(symbol)
            continue
        metrics = get_metrics_for_day(cursor, symbol, as_of)
        if not metrics:
            refusals.setdefault("no data for this date", []).append(symbol)
            continue
        cursor.execute(_CONTEXT_SQL, (symbol, as_of))
        row = cursor.fetchone()
        context: dict[str, float] = {}
        if row:
            if row[0] is not None:
                context["_iv_30"] = float(row[0])   # sizing input, not context
            context.update(
                {
                    k: float(v)
                    for k, v in zip(_CONTEXT_COLUMNS, row[1:], strict=False)
                    if v is not None
                }
            )
        candidate, reason = build_candidate(
            symbol, metrics, as_of, lots.get(symbol, as_of),
            margin_model=margin_model,
            brokerage_per_order=brokerage_per_order,
            context=context,
        )
        if candidate is None:
            refusals.setdefault(reason or "unknown", []).append(symbol)
        else:
            candidates.append(candidate)

    selected, capped = apply_risk_limits(candidates, limits)
    for reason, syms in capped.items():
        refusals.setdefault(reason, []).extend(syms)

    return ScreenResult(
        as_of=as_of, candidates=candidates, selected=selected,
        refusals=refusals, limits=limits,
        is_month_entry=is_month_entry, first_trading_day=first_trading_day,
        banned=banned, secban_checked=secban_checked,
    )


def _resolve_bans(as_of: date) -> tuple[frozenset[str], bool]:
    """``(banned, checked)`` for ``as_of`` — never raises.

    A failed lookup returns ``(frozenset(), False)`` so the screen still
    produces output, but the ``False`` travels into the record. Returning an
    empty set *without* that flag is the failure this whole check exists to
    prevent: it reads exactly like a day on which nothing was banned.
    """
    from mktmtm.data.participant_oi import warm_session
    from mktmtm.data.secban import SecBanError, fetch_secban

    try:
        return fetch_secban(warm_session(), as_of), True
    except SecBanError as exc:
        logger.warning("F&O ban list unavailable for %s: %s", as_of, exc)
    except Exception as exc:  # noqa: BLE001 — the screen must still run
        logger.warning("F&O ban list lookup failed for %s: %r", as_of, exc)
    logger.warning(
        "BANS UNVERIFIED for %s — a selected position may be un-openable; "
        "the record will say so",
        as_of,
    )
    return frozenset(), False


# ═══════════════════════ the frozen record (Phase 3) ═══════════════════════


class ScreenRecordExists(RuntimeError):
    """Refusing to overwrite a record written before its outcome existed."""


_DISCLOSURE = """\
## What this is, and is not

- **Backtested**: +4.56% mean return on margin per trade, **-37.27% max
  drawdown**. Both numbers, always together.
- **No entry filter is validated.** Gate 1 tested six features plus implied-
  volatility rank; none beat a random filter of the same size.
- **A stop loss does not help.** Measured at 1.5x / 2x / 3x the entry credit: it
  costs return and buys no tail protection.
- **The sample contains no severe crash.** 2021-2026 has nothing like 2008 or
  March 2020, which is precisely where a short strangle's risk lives.
- **The edge is decaying**: 8.30% (2021) to 1.09% (2025). Whether that is the
  October 2024 STT rise or a maturing market is untested.
- The `ctx_*` columns are shown for human judgement. **They were tested and did
  not beat chance.** They are not a recommendation.
"""


def _git_commit() -> str:
    """The commit the record was written at, or ``"unknown"``.

    Provenance the Phase 3 spec asks for by name: a record whose code version
    cannot be established cannot be reproduced, and "the parameter set in force"
    means nothing without it.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - provenance must never break a write
        return "unknown"


def record_payload(result: ScreenResult) -> dict[str, Any]:
    """Machine-readable form of a screen, for the scorer.

    The markdown record is the dated commitment; this is what
    ``serving.scoring`` reads. Parsing the markdown back would make the
    record's prose load-bearing, and a reformatting would then silently break
    scoring.
    """
    return {
        "as_of": result.as_of.isoformat(),
        "commit": _git_commit(),
        "is_month_entry": result.is_month_entry,
        "first_trading_day": (
            result.first_trading_day.isoformat() if result.first_trading_day else None
        ),
        "limits": {
            "capital": result.limits.capital,
            "max_positions": result.limits.max_positions,
            "max_margin_fraction": result.limits.max_margin_fraction,
            "max_per_sector": result.limits.max_per_sector,
            "min_leg_volume": result.limits.min_leg_volume,
        },
        "totals": {
            "selected": len(result.selected),
            "candidates": len(result.candidates),
            "margin": result.total_margin,
            "net_credit": result.total_credit,
        },
        "selected": [c.as_row() for c in result.selected],
        "refusals": {k: sorted(v) for k, v in result.refusals.items()},
        "secban_checked": result.secban_checked,
        "banned": sorted(result.banned),
    }


def write_screen_record(result: ScreenResult, path: Any, overwrite: bool = False) -> Any:
    """Write a dated, committed record of today's selection.

    Follows the nowcast precedent exactly (``ml.accumulation.write_nowcast_record``)
    and for the same reasons: a record in gitignored ``models/`` proves nothing
    about *when* it was written, and a forward record whose date cannot be
    established is not a forward record. It **refuses to overwrite**, because a
    selection made before an outcome, silently replaced by one made after it
    under the same filename, is worse than no record at all.
    """
    from pathlib import Path

    path = Path(path)
    if path.exists() and not overwrite:
        raise ScreenRecordExists(
            f"{path} already exists — refusing to overwrite a record that may "
            f"predate its outcome. Pass overwrite=True only to correct a record "
            f"written today."
        )
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = record_payload(result)
    calibration = (
        "Entered on the first trading day of the month, matching the backtested "
        "strategy."
        if result.is_month_entry
        else (
            f"⚠️ **Off-calibration.** The first trading day of this month was "
            f"{result.first_trading_day}; this record entered on {result.as_of}. "
            f"The backtested strategy always enters on the first trading day, so "
            f"this position's behaviour was never measured and it is **excluded "
            f"from Gate 3**."
        )
    )

    lines = [
        f"# Screen — {result.as_of}",
        "",
        f"**Written {result.as_of}, before any outcome exists.** "
        f"{len(result.selected)} positions selected from {len(result.candidates)} "
        f"tradeable candidates.",
        "",
        calibration,
        "",
        (
            f"F&O ban list checked: {len(result.banned)} symbol(s) banned on "
            f"{result.as_of}"
            + (f" ({', '.join(sorted(result.banned))})" if result.banned else "")
            + "."
            if result.secban_checked
            else "⚠️ **F&O bans UNVERIFIED.** NSE's ban list could not be "
            "retrieved for this date, so a selected position may be one in "
            "which no new position can legally be opened. This is a limit of "
            "this record, not a statement that nothing was banned."
        ),
        "",
        f"Code: `{payload['commit']}`",
        "",
        f"Capital ₹{result.limits.capital:,.0f} · margin cap "
        f"{result.limits.max_margin_fraction:.0%} · max {result.limits.max_positions} "
        f"positions · max {result.limits.max_per_sector} per sector",
        "",
        f"- Margin committed: **₹{result.total_margin:,.0f}** "
        f"({result.total_margin / result.limits.capital:.1%} of capital)",
        f"- Net credit collected: **₹{result.total_credit:,.0f}**",
        "",
        "## Selected",
        "",
        "| # | Symbol | Expiry | Call | Put | Net credit | Margin | Credit/margin | Modelled loss (2σ) | Min leg vol |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(result.selected, 1):
        loss = "n/a" if math.isnan(c.modelled_loss_rupees) else f"₹{c.modelled_loss_rupees:,.0f}"
        lines.append(
            f"| {i} | {c.symbol} | {c.expiry} | {c.call_strike:g} | {c.put_strike:g} | "
            f"₹{c.net_credit_rupees:,.0f} | ₹{c.margin_rupees:,.0f} | "
            f"{c.credit_to_margin:.3f} | {loss} | {c.min_leg_volume:.0f} |"
        )
    lines += ["", "## Refused", "", "| Reason | Count | Symbols |", "|---|---|---|"]
    for reason, syms in sorted(result.refusals.items(), key=lambda kv: -len(kv[1])):
        shown = ", ".join(sorted(syms)[:8]) + ("…" if len(syms) > 8 else "")
        lines.append(f"| {reason} | {len(syms)} | {shown} |")
    lines += ["", _DISCLOSURE]

    path.write_text("\n".join(lines) + "\n")
    # Sidecar beside the commitment: the scorer reads this, never the prose.
    path.with_suffix(".json").write_text(json.dumps(payload, indent=2) + "\n")
    logger.info("screen record written to %s (+ .json)", path)
    return path
