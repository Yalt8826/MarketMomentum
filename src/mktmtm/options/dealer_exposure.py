"""Dealer-exposure conventions and math (GEX/DEX, the gamma-flip level).

Pure numpy over chain-element dicts — no I/O, no DB. Shared by the two
enrichment engines (``storage.etl`` and ``pipeline.process``) so they
cannot silently diverge (hard rule 1), and by ``ml.dataset`` for the SQL
aggregate sign convention.

The dealer side is an **assumption**, not a measurement: the standard GEX
convention takes dealers long calls and short puts against customer flow.
Without participant-level data it cannot be verified — see "Known
limitations" in the model card. Phase 6 flips the convention as a
robustness check; that is a one-line change to ``DEALER_SIGN`` here.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from mktmtm.options.chain import get_time_to_expiry_in_years

__all__ = ["DEALER_SIGN", "gamma_flip_spot"]

# Standard GEX convention: dealers are long calls and short puts against
# customer flow. This is a heuristic — without participant-level data it
# cannot be verified. See "Known limitations" in the model card.
DEALER_SIGN = {"CE": +1.0, "PE": -1.0}

# Hypothetical spot grid for the gamma-flip search: S · [0.90, 0.91, …, 1.10].
_GEX_GRID = np.arange(0.90, 1.101, 0.01)

# GEX units: currency P&L per 1% move in the underlying (S² · 0.01 convention).
_GEX_SCALE = 0.01


def _net_gex_at(
    strikes: np.ndarray,
    ois: np.ndarray,
    ivs: np.ndarray,
    ts: np.ndarray,
    signs: np.ndarray,
    spot: float,
    r_dec: float,
) -> float:
    """Net dealer GEX at a hypothetical spot level, vectorized over strikes.

    Black-Scholes gamma (q = 0) is re-evaluated at ``spot`` with each
    strike's stored IV held constant — the same formula
    ``options.pricing.black_scholes_greeks`` uses, so at ``spot == S`` the
    result matches the SQL ``gex`` aggregate (which reads stored gamma).
    """
    sqrt_t = np.sqrt(ts)
    d1 = (np.log(spot / strikes) + (r_dec + 0.5 * ivs**2) * ts) / (ivs * sqrt_t)
    gamma = np.exp(-0.5 * d1**2) / np.sqrt(2.0 * np.pi) / (spot * ivs * sqrt_t)
    return float(np.sum(signs * ois * gamma * spot * spot * _GEX_SCALE))


def gamma_flip_spot(
    chain_rows: list[dict[str, Any]],
    spot: float | None,
    date_key: str,
    r_dec: float,
) -> float | None:
    """Spot level at which net dealer GEX crosses zero; ``None`` when it does not.

    Evaluates net GEX at ``S · [0.90, 0.91, …, 1.10]`` (raw basis, hard
    rule 3), finds the first sign change walking up the grid, and linearly
    interpolates the crossing. ``None`` means no crossing in range — a real
    state (e.g. dealers long gamma everywhere in the ±10% band), not a
    failure.

    Elements without a converged IV (``iv`` None/≤ 0) or time to expiry
    contribute nothing — matching the SQL aggregates, which read stored
    gamma only where it exists.
    """
    if not chain_rows or spot is None or not np.isfinite(spot) or spot <= 0:
        return None
    strikes: list[float] = []
    ois: list[float] = []
    ivs: list[float] = []
    ts: list[float] = []
    signs: list[float] = []
    for c in chain_rows:
        iv = c.get("iv")
        strike = c.get("strike")
        expiry = c.get("expiry")
        if not iv or iv <= 0 or not strike or strike <= 0 or not expiry:
            continue
        t = get_time_to_expiry_in_years(date_key, str(expiry))
        if t <= 0:
            continue
        strikes.append(float(strike))
        ois.append(float(c.get("oi") or 0))
        ivs.append(float(iv) / 100.0)  # stored IV is percent-scale
        ts.append(t)
        signs.append(DEALER_SIGN.get(str(c.get("type")), 0.0))
    if not strikes:
        return None

    s_arr = np.asarray(strikes)
    o_arr = np.asarray(ois)
    i_arr = np.asarray(ivs)
    t_arr = np.asarray(ts)
    g_arr = np.asarray(signs)

    prev_spot = 0.0
    prev_gex = 0.0
    have_prev = False
    with np.errstate(divide="ignore", invalid="ignore"):
        for frac in _GEX_GRID:
            s = spot * float(frac)
            gex = _net_gex_at(s_arr, o_arr, i_arr, t_arr, g_arr, s, r_dec)
            if not np.isfinite(gex):
                have_prev = False  # never interpolate across a non-finite point
                continue
            if have_prev:
                if gex == 0.0:
                    return s
                if prev_gex == 0.0:
                    return prev_spot
                if (prev_gex < 0) != (gex < 0):
                    # linear interpolation of the root between the two grid points
                    return float(prev_spot + (s - prev_spot) * prev_gex / (prev_gex - gex))
            prev_spot, prev_gex, have_prev = s, gex, True
    return None
