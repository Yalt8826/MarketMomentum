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
    assert greeks == {
        "delta": 0.0,
        "gamma": 0.0,
        "theta": 0.0,
        "vega": 0.0,
        "rho": 0.0,
        "vanna": 0.0,
        "charm": 0.0,
    }

    # Zero-vol price degenerates to intrinsic value.
    assert black_scholes_price(100.0, 90.0, 0.5, 0.05, 0.0, is_call=True) == pytest.approx(10.0)
    assert black_scholes_price(100.0, 90.0, 0.5, 0.05, 0.0, is_call=False) == pytest.approx(0.0)


def test_put_call_parity() -> None:
    """call - put == S - K*exp(-rT) (no dividends)."""
    S, K, T, r = 100.0, 105.0, 0.25, 0.04
    call = black_scholes_price(S, K, T, r, 0.2, is_call=True)
    put = black_scholes_price(S, K, T, r, 0.2, is_call=False)
    assert (call - put) == pytest.approx(S - K * math.exp(-r * T), abs=1e-9)


def test_vanna_put_call_parity() -> None:
    """Vanna is identical for calls and puts at the same (S, K, T, r, sigma)."""
    S, K, T, r, sigma = 100.0, 105.0, 0.5, 0.04, 0.2
    call = black_scholes_greeks(S, K, T, r, sigma, is_call=True)["vanna"]
    put = black_scholes_greeks(S, K, T, r, sigma, is_call=False)["vanna"]
    assert call == pytest.approx(put, abs=1e-12)


def test_vanna_finite_difference_agrees_with_delta() -> None:
    """vanna == d(delta)/dsigma: central difference (delta(sigma+h) - delta(sigma-h)) / 2h."""
    S, K, T, r, sigma = 100.0, 105.0, 0.5, 0.04, 0.2
    h = 1e-5
    for is_call in (True, False):
        greeks = black_scholes_greeks(S, K, T, r, sigma, is_call=is_call)
        delta_hi = black_scholes_greeks(S, K, T, r, sigma + h, is_call=is_call)["delta"]
        delta_lo = black_scholes_greeks(S, K, T, r, sigma - h, is_call=is_call)["delta"]
        fd_vanna = (delta_hi - delta_lo) / (2 * h)
        assert fd_vanna == pytest.approx(greeks["vanna"], abs=1e-4)


def test_charm_finite_difference_agrees_with_delta() -> None:
    """charm == -d(delta)/dT: charm = (delta(T-h) - delta(T)) / h.

    Charm is defined as -∂delta/∂T (delta decay), and delta(T-h) - delta(T)
    approximates -h·∂delta/∂T, so the backward difference carries no extra
    minus sign.
    """
    S, K, T, r, sigma = 100.0, 105.0, 0.5, 0.04, 0.2
    h = 1e-5
    for is_call in (True, False):
        greeks = black_scholes_greeks(S, K, T, r, sigma, is_call=is_call)
        delta_earlier = black_scholes_greeks(S, K, T - h, r, sigma, is_call=is_call)["delta"]
        fd_charm = (delta_earlier - greeks["delta"]) / h
        assert fd_charm == pytest.approx(greeks["charm"], abs=1e-4)


def test_vanna_charm_degenerate_inputs_are_zero_not_nan() -> None:
    """T=0 or sigma=0 → vanna and charm are 0.0, never NaN."""
    for T_val, sigma_val in ((0.0, 0.2), (0.5, 0.0)):
        greeks = black_scholes_greeks(S=100.0, K=90.0, T=T_val, r=0.05, sigma=sigma_val)
        assert greeks["vanna"] == 0.0
        assert greeks["charm"] == 0.0
        assert math.isfinite(greeks["vanna"])
        assert math.isfinite(greeks["charm"])


def test_vanna_atm_sanity() -> None:
    """Vanna ≈ 0 at-the-money (short-dated; zero crossing is at S = K·e^{sigma²T/2} ≈ K),
    and |vanna| peaks on either side of the money."""
    K, T, r, sigma = 100.0, 1.0 / 252.0, 0.0, 0.20
    atm = abs(black_scholes_greeks(S=K, K=K, T=T, r=r, sigma=sigma)["vanna"])
    below = abs(black_scholes_greeks(S=0.99 * K, K=K, T=T, r=r, sigma=sigma)["vanna"])
    above = abs(black_scholes_greeks(S=1.01 * K, K=K, T=T, r=r, sigma=sigma)["vanna"])
    assert atm < 0.05
    assert below > atm
    assert above > atm
