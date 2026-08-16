"""Black-Scholes pricing, Greeks, and implied-volatility (pure math, no I/O).

Ported verbatim from the athena-verified `process_data.py` pipeline
(2026-08-16), with type hints added.
"""

from __future__ import annotations

import math

__all__ = [
    "black_scholes_greeks",
    "black_scholes_price",
    "implied_volatility_bisection",
]


def black_scholes_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    is_call: bool = True,
    q: float = 0.0,
) -> dict[str, float]:
    """Computes Black-Scholes Greeks for an option.

    Returns a dictionary of: delta, gamma, theta, vega, rho
    """
    if T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    def phi(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def phi_pdf(x: float) -> float:
        return (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x**2)

    e_neg_qT = math.exp(-q * T)
    e_neg_rT = math.exp(-r * T)

    # Delta
    delta = e_neg_qT * phi(d1) if is_call else -e_neg_qT * phi(-d1)

    # Gamma (same for calls and puts)
    gamma = (e_neg_qT * phi_pdf(d1)) / (S * sigma * math.sqrt(T))

    # Theta
    theta_call = (
        -(S * phi_pdf(d1) * sigma * e_neg_qT) / (2 * math.sqrt(T))
         - r * K * e_neg_rT * phi(d2)
         + q * S * e_neg_qT * phi(d1)) / 365.0
    theta_put = (
        -(S * phi_pdf(d1) * sigma * e_neg_qT) / (2 * math.sqrt(T))
         + r * K * e_neg_rT * phi(-d2)
         - q * S * e_neg_qT * phi(-d1)) / 365.0
    theta = theta_call if is_call else theta_put

    # Vega
    vega = S * e_neg_qT * phi_pdf(d1) * math.sqrt(T) / 100  # per 1% change in vol

    # Rho
    rho_call = K * T * e_neg_rT * phi(d2) / 100
    rho_put = -K * T * e_neg_rT * phi(-d2) / 100
    rho = rho_call if is_call else rho_put

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }


def black_scholes_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    is_call: bool = True,
    q: float = 0.0,
) -> float:
    """Computes the theoretical option price using the Black-Scholes-Merton formula.

    S  : Underlying price
    K  : Strike price
    T  : Time to maturity (in years)
    r  : Risk-free interest rate (annualized, decimal form)
    sigma : Volatility (annualized, decimal form)
    is_call : True for Call, False for Put
    q  : Continuous dividend yield (decimal)
    """
    if T <= 0 or sigma <= 0:
        # No time => option = intrinsic value
        return max(0.0, S - K) if is_call else max(0.0, K - S)

    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    # Standard normal CDF
    def phi(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    e_neg_qT = math.exp(-q * T)
    e_neg_rT = math.exp(-r * T)

    if is_call:
        return (S * e_neg_qT * phi(d1)) - (K * e_neg_rT * phi(d2))
    else:
        return (K * e_neg_rT * phi(-d2)) - (S * e_neg_qT * phi(-d1))


def implied_volatility_bisection(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    is_call: bool = True,
    q: float = 0.0,
    lower_bound: float = 1e-9,
    upper_bound: float = 5.0,
    tol: float = 1e-8,
    max_iter: int = 500,
) -> float:
    """Finds implied volatility using a simple bisection method on Black-Scholes price.

    market_price: observed option price (premium)
    S  : Underlying price
    K  : Strike price
    T  : Time to expiry (years)
    r  : Risk-free rate (decimal)
    is_call : True for Call, False for Put
    q  : Dividend yield (decimal)
    """
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if T <= 0 or market_price <= intrinsic:
        return 0.0

    for _ in range(max_iter):
        mid_vol = 0.5 * (lower_bound + upper_bound)
        price_mid = black_scholes_price(S, K, T, r, mid_vol, is_call, q)

        if abs(price_mid - market_price) < tol:
            return mid_vol

        if price_mid > market_price:
            upper_bound = mid_vol
        else:
            lower_bound = mid_vol

    return 0.5 * (lower_bound + upper_bound)
