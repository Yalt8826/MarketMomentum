"""Unit tests for mktmtm.serving.screen — the daily tool.

The screen's job is not to pick winners; Gate 1 established there is nothing to
pick by. Its job is to refuse what it cannot price honestly, size what is left,
and say what it does not know. These tests pin those three things.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from mktmtm.serving.screen import (
    Candidate,
    RiskLimits,
    ScreenRecordExists,
    ScreenResult,
    apply_risk_limits,
    build_candidate,
    write_screen_record,
)

EXPIRY = date(2025, 9, 25)
AS_OF = date(2025, 9, 1)


def _opt(typ: str, strike: float, delta: float, settle: float, volume: float = 500.0) -> dict:
    return {
        "type": typ,
        "expiry": EXPIRY.strftime("%d-%b-%Y"),
        "strike": strike,
        "delta": delta,
        "settle": settle,
        "volume": volume,
    }


def _chain() -> list[dict]:
    rows = []
    for i, k in enumerate((900.0, 1000.0, 1100.0)):
        rows.append(_opt("CE", k, 0.40 - 0.10 * i, 40.0 - 10.0 * i))
        rows.append(_opt("PE", k, -(0.20 + 0.10 * i), 10.0 + 10.0 * i))
    return rows


def _metrics(**over) -> dict:
    base = {
        "underlying_price": 1000.0,
        "interest_rate": 6.5,
        "expiry_30d": EXPIRY,
        "upcoming_earning_date": None,
        "option_chain": _chain(),
    }
    base.update(over)
    return base


def _candidate(symbol: str, margin: float, credit: float = 1000.0, volume: float = 500.0) -> Candidate:
    return Candidate(
        symbol=symbol, as_of=AS_OF, expiry=EXPIRY,
        call_strike=1100.0, put_strike=900.0, call_price=20.0, put_price=10.0,
        spot=1000.0, lot_size=500, credit_rupees=credit, costs_rupees=50.0,
        net_credit_rupees=credit - 50.0, margin_rupees=margin,
        credit_to_margin=(credit - 50.0) / margin, modelled_loss_rupees=5_000.0,
        call_volume=volume, put_volume=volume, min_leg_volume=volume,
        days_to_expiry=24,
    )


# ─────────────────────────── refusals are typed ─────────────────────────────


def test_a_degenerate_chain_is_refused_with_a_reason() -> None:
    """83% of these fall on an expiry date, where the expiring series settles
    against the underlying and that value lands in every premium. A screen
    emitting them would recommend trades at fabricated prices."""
    flat = [dict(o, settle=1000.75, delta=0.0) for o in _chain()] * 3

    candidate, reason = build_candidate("ACME", _metrics(option_chain=flat), AS_OF, 500)

    assert candidate is None
    assert "degenerate" in reason


def test_an_unresolvable_lot_size_is_refused_never_defaulted() -> None:
    """Guessing a lot size mis-scales the whole position."""
    candidate, reason = build_candidate("ACME", _metrics(), AS_OF, None)

    assert candidate is None
    assert "lot size" in reason


def test_earnings_before_expiry_is_refused() -> None:
    candidate, reason = build_candidate(
        "ACME", _metrics(upcoming_earning_date=date(2025, 9, 15)), AS_OF, 500
    )

    assert candidate is None
    assert "earnings" in reason


def test_a_chain_with_only_untraded_strikes_is_refused() -> None:
    untraded = [dict(o, volume=0) for o in _chain()]

    candidate, reason = build_candidate("ACME", _metrics(option_chain=untraded), AS_OF, 500)

    assert candidate is None
    assert "20-delta" in reason


def test_a_healthy_symbol_produces_a_priced_candidate() -> None:
    candidate, reason = build_candidate("ACME", _metrics(ce_iv_30=25.0), AS_OF, 500)

    assert reason is None
    assert candidate.credit_rupees > 0
    assert candidate.margin_rupees > 0
    assert candidate.net_credit_rupees < candidate.credit_rupees  # costs charged
    assert candidate.modelled_loss_rupees == candidate.modelled_loss_rupees  # not NaN


# ─────────────────────────── sizing and caps ────────────────────────────────


def test_the_position_count_cap_binds() -> None:
    cands = [_candidate(f"S{i}", margin=10_000.0) for i in range(20)]

    selected, refused = apply_risk_limits(cands, RiskLimits(capital=10_000_000.0, max_positions=5))

    assert len(selected) == 5
    assert "position count cap reached" in refused


def test_the_margin_budget_binds() -> None:
    cands = [_candidate(f"S{i}", margin=100_000.0) for i in range(20)]

    selected, refused = apply_risk_limits(
        cands, RiskLimits(capital=1_000_000.0, max_positions=50, max_margin_fraction=0.3)
    )

    assert sum(c.margin_rupees for c in selected) <= 300_000.0
    assert "margin budget exhausted" in refused


def test_the_sector_cap_binds() -> None:
    """15 strangles in one sector is one trade wearing fifteen hats."""
    cands = [_candidate(f"BANK{i}", margin=10_000.0) for i in range(8)]
    sectors = {c.symbol: "financials" for c in cands}

    selected, refused = apply_risk_limits(
        cands, RiskLimits(capital=10_000_000.0, max_per_sector=2), sectors
    )

    assert len(selected) == 2
    assert any("sector cap" in r for r in refused)


def test_an_untraded_leg_is_refused_before_sizing() -> None:
    cands = [_candidate("THIN", margin=10_000.0, volume=0.0)]

    selected, refused = apply_risk_limits(cands, RiskLimits(min_leg_volume=1))

    assert selected == []
    assert "leg never traded today" in refused


def test_ordering_is_by_credit_per_rupee_of_margin_not_by_size() -> None:
    """Gate 1 found nothing to rank conviction by, so the ordering is an
    efficiency one: which positions pay best for the capital they tie up."""
    poor = _candidate("POOR", margin=100_000.0, credit=2_000.0)
    rich = _candidate("RICH", margin=10_000.0, credit=2_000.0)

    selected, _ = apply_risk_limits([poor, rich], RiskLimits(capital=10_000_000.0))

    assert [c.symbol for c in selected] == ["RICH", "POOR"]


# ─────────────────────────── the frozen record ──────────────────────────────


def _result() -> ScreenResult:
    return ScreenResult(
        as_of=AS_OF,
        candidates=[_candidate("ACME", 50_000.0)],
        selected=[_candidate("ACME", 50_000.0)],
        refusals={"lot size unresolvable": ["THIN"]},
        limits=RiskLimits(),
    )


def test_the_record_refuses_to_overwrite(tmp_path: Path) -> None:
    """Same reasoning as the nowcast record: a selection made before an outcome,
    silently replaced by one made after it under the same filename, is worse
    than no record at all."""
    path = tmp_path / "screen-2025-09-01.md"
    write_screen_record(_result(), path)

    with pytest.raises(ScreenRecordExists, match="refusing to overwrite"):
        write_screen_record(_result(), path)


def test_overwrite_is_available_for_a_same_day_correction(tmp_path: Path) -> None:
    path = tmp_path / "screen-2025-09-01.md"
    write_screen_record(_result(), path)

    assert write_screen_record(_result(), path, overwrite=True) == path


def test_the_record_states_its_own_limits(tmp_path: Path) -> None:
    """The tool must carry its caveats in its OUTPUT, not only in the docs."""
    path = write_screen_record(_result(), tmp_path / "screen.md")
    # Collapse the markdown wrapping; the claims matter, not the line breaks.
    text = " ".join(path.read_text().split())

    assert "-37.27% max drawdown" in text
    assert "No entry filter is validated" in text
    assert "A stop loss does not help" in text
    assert "no severe crash" in text
    assert "did not beat chance" in text


def test_the_record_lists_refusals_with_their_reasons(tmp_path: Path) -> None:
    path = write_screen_record(_result(), tmp_path / "screen.md")
    text = path.read_text()

    assert "lot size unresolvable" in text
    assert "THIN" in text


def test_context_columns_do_not_leak_private_sizing_inputs() -> None:
    c = _candidate("ACME", 50_000.0)
    object.__setattr__(c, "context", {"_iv_30": 25.0, "ce_ivr": 80.0})

    row = c.as_row()

    assert "ctx_ce_ivr" in row
    assert "ctx__iv_30" not in row
