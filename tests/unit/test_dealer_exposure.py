"""Unit tests for Phase 3 dealer-exposure features (no DB).

Covers the sign convention (DEALER_SIGN), the gamma-flip search
(ETL-time, pure numpy), the pandas extras in ``_add_derived``, the SQL
aggregate wiring, and the column-contract mirror between
``DATASET_COLUMNS`` and ``sql/init_ml_dataset.sql`` (hard rule 2).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.dataset import (
    _CHAIN_AGG_SQL,
    DATASET_COLUMNS,
    GEX_COLUMNS,
    _add_derived,
)
from mktmtm.options.dealer_exposure import DEALER_SIGN, gamma_flip_spot
from mktmtm.options.pricing import black_scholes_greeks

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ML_DATASET_DDL = _REPO_ROOT / "src/mktmtm/storage/sql/init_ml_dataset.sql"

_DATE_KEY = "01-Jan-2026"
_EXPIRY = "31-Jan-2026"  # ~30 days, T > 0
_IV_PCT = 30.0  # stored IV is percent-scale


def _chain(rows: list[tuple[float, str, int]]) -> list[dict[str, Any]]:
    """Chain-element dicts in the shape etl.py/process.py write (keys matter)."""
    return [
        {
            "expiry": _EXPIRY,
            "strike": k,
            "type": typ,
            "settle": 5.0,
            "oi": oi,
            "chg_oi": 0,
            "iv": _IV_PCT,
            "delta": 0.5,
            "gamma": 0.001,
            "vanna": 0.0,
            "charm": 0.0,
        }
        for k, typ, oi in rows
    ]


# ───────────────────────────── sign convention ─────────────────────────────
def test_dealer_sign_convention() -> None:
    """Measured from participant OI, not the US convention: dealers are net
    short calls and long puts (Pro book, 99.9%/98.3% of 1,258 days)."""
    assert DEALER_SIGN == {"CE": -1.0, "PE": +1.0}


def test_dealer_sign_has_one_definition() -> None:
    """ml.gex_hypothesis must re-export the constant, never restate it — a
    second copy lets the hypothesis test disagree with the features it tests."""
    from mktmtm.ml import gex_hypothesis

    assert gex_hypothesis.DEALER_SIGN is DEALER_SIGN


def test_chain_agg_sql_renders_sign_from_constant() -> None:
    """The SQL CASE must be rendered from DEALER_SIGN, never inlined ±1."""
    assert f"{DEALER_SIGN['CE']!r}" in _CHAIN_AGG_SQL
    assert f"{DEALER_SIGN['PE']!r}" in _CHAIN_AGG_SQL


def test_chain_agg_sql_has_all_phase3_columns() -> None:
    for col in (
        "dex", "gex", "gex_chg", "vanna_exp", "charm_exp", "oi_hhi",
        "max_pain", "call_wall", "put_wall", "dist_to_call_wall",
        "otm_put_oi_build",
    ):
        assert f"AS {col}" in _CHAIN_AGG_SQL, f"missing aggregate {col}"


# ───────────────────────────── gamma flip ──────────────────────────────────
def test_gamma_flip_interpolates_known_crossing() -> None:
    """CE@90 vs PE@110 with equal OI: net GEX flips sign between S=99 and S=100.

    The spec's method is grid-then-linear-interpolate (S·[0.90..1.10] in 1%
    steps), so the expected value is the linear interpolation of the two
    bracketing grid GEX values — verified here against a direct recomputation
    with black_scholes_greeks.
    """
    chain = _chain([(90.0, "CE", 1000), (110.0, "PE", 1000)])
    flip = gamma_flip_spot(chain, 100.0, _DATE_KEY, 0.0)
    assert flip is not None
    assert 99.0 < flip < 100.0

    # direct recomputation at the two bracketing grid points
    def net_gex(s: float) -> float:
        net = 0.0
        for el in chain:
            g = black_scholes_greeks(
                s, float(el["strike"]), 30.0 / 365.0, 0.0, _IV_PCT / 100.0,
                is_call=(el["type"] == "CE"),
            )["gamma"]
            net += DEALER_SIGN[str(el["type"])] * float(el["oi"]) * g * s * s * 0.01
        return net

    gex_lo, gex_hi = net_gex(99.0), net_gex(100.0)
    # A sign change brackets the flip. Which side is positive depends on
    # DEALER_SIGN, so assert the crossing, not the direction.
    assert gex_lo * gex_hi < 0
    expected = 99.0 + (100.0 - 99.0) * gex_lo / (gex_lo - gex_hi)
    assert flip == pytest.approx(expected, abs=1e-9)


def test_gamma_flip_is_invariant_under_sign_convention() -> None:
    """The flip level must not move when DEALER_SIGN is negated.

    Negating both entries negates net GEX, and negating a function does not
    move its zeros. This is why the 2026-09-04 sign correction needed no ETL
    re-run: `gamma_flip` and `dist_to_flip` — the strongest dealer feature
    after an IV control — are unchanged by it, while the signed level flips.
    """
    import mktmtm.options.dealer_exposure as de

    chain = _chain([(90.0, "CE", 1000), (110.0, "PE", 1000)])
    flip = gamma_flip_spot(chain, 100.0, _DATE_KEY, 0.0)

    original = de.DEALER_SIGN
    try:
        de.DEALER_SIGN = {k: -v for k, v in original.items()}
        flipped = gamma_flip_spot(chain, 100.0, _DATE_KEY, 0.0)
    finally:
        de.DEALER_SIGN = original

    assert flip is not None and flipped is not None
    assert flipped == pytest.approx(flip, abs=1e-9)


def test_gamma_flip_none_when_no_crossing() -> None:
    # calls only -> dealers short gamma everywhere in ±10% -> no flip
    chain = _chain([(100.0, "CE", 1000)])
    assert gamma_flip_spot(chain, 100.0, _DATE_KEY, 0.0) is None


def test_gamma_flip_handles_degenerate_inputs() -> None:
    assert gamma_flip_spot([], 100.0, _DATE_KEY, 0.0) is None
    assert gamma_flip_spot(_chain([(100.0, "CE", 1000)]), None, _DATE_KEY, 0.0) is None
    no_iv = _chain([(100.0, "CE", 1000)])
    no_iv[0]["iv"] = None
    assert gamma_flip_spot(no_iv, 100.0, _DATE_KEY, 0.0) is None


# ───────────────────────────── _add_derived ────────────────────────────────
def _derived_frame() -> pd.DataFrame:
    n = 4
    df = pd.DataFrame(
        {
            "symbol": ["A", "A", "B", "B"],
            "date": pd.to_datetime(["2026-08-11", "2026-08-12", "2026-08-11", "2026-08-12"]),
            "underlying_price": [100.0, 101.0, 50.0, 51.0],
            "underlying_price_raw": [100.0, 101.0, 50.0, 51.0],
            "gamma_flip": [99.0, 100.0, 49.0, 50.0],
            "rv_yz": [0.10] * n,
            "ce_iv_30": [10.0] * n,
            "ce_iv_90": [12.0] * n,
            "pe_iv_30": [10.5] * n,
            "chain_pe_vol": [50.0] * n,
            "chain_ce_vol": [100.0] * n,
            "chain_pe_oi": [500.0] * n,
            "chain_ce_oi": [1000.0] * n,
            "otm_put_iv": [12.0] * n,
            "expiry_30d": pd.to_datetime(["2026-09-10"] * n),
            "upcoming_earning_date": pd.to_datetime([pd.NaT] * n),
            "dex": [1e6, 1.2e6, 5e5, 5.5e5],
            "oi_hhi": [0.4, 0.35, 0.9, 0.85],
        }
    )
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def test_add_derived_chg_variants_are_per_symbol_diffs() -> None:
    out = _add_derived(_derived_frame())
    a = out[out["symbol"] == "A"].reset_index(drop=True)
    b = out[out["symbol"] == "B"].reset_index(drop=True)
    assert pd.isna(a.loc[0, "dex_chg"])
    assert a.loc[1, "dex_chg"] == pytest.approx(0.2e6)
    assert pd.isna(b.loc[0, "dex_chg"])
    assert b.loc[1, "dex_chg"] == pytest.approx(0.5e5)
    assert a.loc[1, "oi_hhi_chg"] == pytest.approx(-0.05)
    assert b.loc[1, "oi_hhi_chg"] == pytest.approx(-0.05)


def test_add_derived_dist_to_flip() -> None:
    out = _add_derived(_derived_frame())
    a = out[out["symbol"] == "A"].reset_index(drop=True)
    assert a.loc[0, "dist_to_flip"] == pytest.approx((100.0 - 99.0) / 100.0)
    assert a.loc[1, "dist_to_flip"] == pytest.approx((101.0 - 100.0) / 101.0)
    # a missing flip level propagates as NULL, not a fabricated number
    df = _derived_frame()
    df.loc[2, "gamma_flip"] = np.nan
    out = _add_derived(df)
    assert pd.isna(out.loc[out["symbol"] == "B", "dist_to_flip"].iloc[0])


# ───────────────────────────── column contract ─────────────────────────────
def test_gex_columns_in_dataset_columns() -> None:
    assert len(GEX_COLUMNS) == len(set(GEX_COLUMNS))
    assert set(GEX_COLUMNS) <= set(DATASET_COLUMNS)


def test_dataset_columns_match_sql_ddl_order() -> None:
    """Hard rule 2: DATASET_COLUMNS is the source of truth for order; the DDL
    mirrors it exactly (ignoring the idempotent ALTER block)."""
    text = _ML_DATASET_DDL.read_text(encoding="utf-8")
    ddl_cols = re.findall(
        r"^\s{4}([a-z_][a-z_0-9]*)\s+(?:DOUBLE PRECISION|INTEGER|SMALLINT|TEXT|DATE)\b",
        text,
        re.M,
    )
    assert ddl_cols == list(DATASET_COLUMNS)
