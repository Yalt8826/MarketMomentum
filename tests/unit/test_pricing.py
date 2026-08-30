"""Unit tests for mktmtm.options.pricing (BS price, greeks, implied vol)."""

import math

import pytest

from mktmtm.options.pricing import (
    black_scholes_greeks,
    black_scholes_price,
    implied_volatility_bisection,
)


def test_atm_iv_bisection_round_trips_to_known_sigma() -> None:
    """Bisection IV on an ATM option priced at a known sigma recovers it."""
    S, K, T, r = 100.0, 100.0, 1.0, 0.05
    known_sigma = 0.30
    price = black_scholes_price(S, K, T, r, known_sigma, is_call=True)

    iv = implied_volatility_bisection(price, S, K, T, r, is_call=True)

    assert iv == pytest.approx(known_sigma, abs=1e-6)


def test_iv_bisection_round_trips_for_puts() -> None:
    S, K, T, r = 100.0, 105.0, 0.5, 0.04
    known_sigma = 0.25
    price = black_scholes_price(S, K, T, r, known_sigma, is_call=False)

    iv = implied_volatility_bisection(price, S, K, T, r, is_call=False)

    assert iv == pytest.approx(known_sigma, abs=1e-6)


def test_iv_bisection_returns_none_for_intrinsic_price() -> None:
    """No time value (price == intrinsic) or non-convergence => None, not a sentinel."""
    S, K, T, r = 100.0, 90.0, 0.5, 0.05
    # Call priced exactly at intrinsic value (S - K)
    assert implied_volatility_bisection(10.0, S, K, T, r, is_call=True) is None
    # Put priced exactly at intrinsic value (max(K - S, 0) == 0)
    assert implied_volatility_bisection(0.0, S, K, T, r, is_call=False) is None
    # Non-convergence (price beyond what any IV in the bracket can produce) => None
    assert implied_volatility_bisection(1e9, S, K, T, r, is_call=True) is None


def test_greeks_finite_and_atm_delta_approx_half() -> None:
    """All greeks finite; ATM call delta close to 0.5 for small vol."""
    greeks = black_scholes_greeks(S=100.0, K=100.0, T=1.0, r=0.0, sigma=0.1)

    for name, value in greeks.items():
        assert math.isfinite(value), f"greeks[{name}] should be finite, got {value}"

    # With r=q=0 and sigma small, ATM call delta -> 0.5
    assert greeks["delta"] == pytest.approx(0.5, abs=0.05)

    # Put delta should be delta - 1 (call-put delta parity)
    put_greeks = black_scholes_greeks(S=100.0, K=100.0, T=1.0, r=0.0, sigma=0.1, is_call=False)
    assert put_greeks["delta"] == pytest.approx(greeks["delta"] - 1.0, abs=1e-9)


def test_zero_time_price_is_intrinsic_value() -> None:
    """T <= 0 returns intrinsic value without division by zero."""
    assert black_scholes_price(100.0, 90.0, 0.0, 0.05, 0.2, is_call=True) == pytest.approx(10.0)
    assert black_scholes_price(100.0, 90.0, 0.0, 0.05, 0.2, is_call=False) == pytest.approx(0.0)


def test_zero_sigma_greeks_and_price_do_not_divide_by_zero() -> None:
    """sigma == 0 (IV bisection sentinel for at/below-intrinsic rows) must not crash."""
    greeks = black_scholes_greeks(S=100.0, K=90.0, T=0.5, r=0.05, sigma=0.0)
    assert greeks == {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    # Zero-vol price degenerates to intrinsic value.
    assert black_scholes_price(100.0, 90.0, 0.5, 0.05, 0.0, is_call=True) == pytest.approx(10.0)
    assert black_scholes_price(100.0, 90.0, 0.5, 0.05, 0.0, is_call=False) == pytest.approx(0.0)


def test_put_call_parity() -> None:
    """call - put == S - K*exp(-rT) (no dividends)."""
    S, K, T, r = 100.0, 105.0, 0.25, 0.04
    call = black_scholes_price(S, K, T, r, 0.2, is_call=True)
    put = black_scholes_price(S, K, T, r, 0.2, is_call=False)
    assert (call - put) == pytest.approx(S - K * math.exp(-r * T), abs=1e-9)
