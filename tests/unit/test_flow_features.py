"""Unit tests for mktmtm.features.flow.

The outlier detector's job is to be *quiet*. Most of these tests therefore
check that it does not fire — on a chain that only churns, on a build too small
to matter, on an expiry too thin to have a cross-section — because a detector
that flags everything is indistinguishable from one that measures noise, and
that is exactly what the first threshold pair did (99.4% of symbol-days).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mktmtm.features.flow import (
    FLOW_COLUMNS,
    MIN_CONTRACTS_PER_EXPIRY,
    OUTLIER_MIN_SHARE,
    OUTLIER_Z,
    _robust_scale,
    detect_outlier_strikes,
    futures_basis,
    write_flow_features,
)

DAY = pd.Timestamp("2025-01-10")
NEAR = pd.Timestamp("2025-01-30")
FAR = pd.Timestamp("2025-03-27")
SPOT = 1200.0


def _chain(
    n: int = 60,
    expiry: pd.Timestamp = NEAR,
    seed: int = 0,
    symbol: str = "ACME",
) -> pd.DataFrame:
    """A quiet chain: every strike churns a little, nobody takes a position."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "SYMBOL": symbol,
            "DATE": DAY,
            "EXPIRY_DT": expiry,
            "STRIKE_PR": [900.0 + i * 10 for i in range(n)],
            "OPTION_TYP": ["CE" if i % 2 else "PE" for i in range(n)],
            "CHG_IN_OI": rng.integers(-50, 50, n).astype("float64"),
            "spot": SPOT,
        }
    )


# ── robust scale ────────────────────────────────────────────────────────────


def test_robust_scale_ignores_untraded_strikes() -> None:
    """Regression: estimating the scale over the whole chain collapses it.

    An option chain is mostly zeros. Including them drags the MAD to 0, so the
    p10-p90 fallback fires on a *normal* day and every contract that moved
    scores a huge z — measured at 22% of contracts over z=4, 99.4% of
    symbol-days flagged. The scale must be estimated from what traded.
    """
    values = np.array([0.0] * 90 + [100.0, 120.0, 90.0, 110.0, 95.0,
                                    105.0, 115.0, 85.0, 100.0, 108.0])
    scale, centre = _robust_scale(values)
    assert scale > 0
    assert 85.0 <= centre <= 115.0, "centre must be the traded median, not ~0"
    # A 130 sits among its peers; with the zeros included it would be enormous.
    assert abs((130.0 - centre) / scale) < OUTLIER_Z


def test_robust_scale_degenerates_to_zero_not_a_sentinel() -> None:
    """Too few traded contracts is 'unknown', which the caller must skip."""
    assert _robust_scale(np.zeros(50))[0] == 0.0
    assert _robust_scale(np.array([0.0, 0.0, 5.0]))[0] == 0.0
    assert _robust_scale(np.array([]))[0] == 0.0


def test_robust_scale_falls_back_when_mad_is_zero() -> None:
    """All-tied traded contracts have MAD 0; the p10-p90 spread stands in."""
    values = np.array([0.0] * 20 + [10.0] * 9 + [900.0])
    scale, _ = _robust_scale(values)
    assert scale > 0


# ── outlier detector ────────────────────────────────────────────────────────


def test_flags_a_planted_far_otm_build() -> None:
    """The signal the feature exists for: one strike loaded up in size."""
    chain = _chain()
    chain.loc[55, ["CHG_IN_OI", "OPTION_TYP"]] = [40000.0, "CE"]
    out = detect_outlier_strikes(chain)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["oi_outlier_count"] == 1
    assert row["oi_outlier_call_frac"] == 1.0
    assert row["oi_outlier_share"] > 0.9
    assert row["_build_contracts"] == 40000.0
    assert row["_unwind_contracts"] == 0.0
    # strike 1450 against a 1200 spot
    assert row["oi_outlier_moneyness"] == pytest.approx((1450 - SPOT) / SPOT)
    assert row["oi_outlier_dte"] == pytest.approx((NEAR - DAY).days)


def test_quiet_chain_flags_nothing_but_still_reports() -> None:
    """A symbol-day with no outlier is a measured zero, not a missing row."""
    out = detect_outlier_strikes(_chain())
    assert len(out) == 1
    row = out.iloc[0]
    assert row["oi_outlier_count"] == 0
    assert row["oi_outlier_share"] == 0.0
    # ...but the ratios are undefined, not 0 — the sentinel defect from
    # docs/data/iv-integrity-audit.md, in a new place.
    assert pd.isna(row["oi_outlier_call_frac"])
    assert pd.isna(row["oi_outlier_moneyness"])
    assert pd.isna(row["oi_outlier_max_absz"])


def test_unwinds_are_flagged_and_reported_separately() -> None:
    """A large exit is institutional activity too, and must not net out."""
    chain = _chain()
    chain.loc[55, "CHG_IN_OI"] = -40000.0
    out = detect_outlier_strikes(chain).iloc[0]
    assert out["oi_outlier_count"] == 1
    assert out["_unwind_contracts"] == 40000.0
    assert out["_build_contracts"] == 0.0


def test_build_and_unwind_do_not_cancel() -> None:
    """Two opposite outliers on one day are both reported, not netted to zero."""
    chain = _chain()
    chain.loc[55, "CHG_IN_OI"] = 40000.0
    chain.loc[10, "CHG_IN_OI"] = -38000.0
    out = detect_outlier_strikes(chain).iloc[0]
    assert out["oi_outlier_count"] == 2
    assert out["_build_contracts"] == 40000.0
    assert out["_unwind_contracts"] == 38000.0


def test_share_gate_rejects_a_high_z_but_immaterial_build() -> None:
    """Clearing the z alone is not enough — it is the weaker of the two gates."""
    chain = _chain()
    # Far more robust sigmas than the threshold, but a trivial slice of the day.
    chain.loc[55, "CHG_IN_OI"] = 1500.0
    lone = detect_outlier_strikes(chain).iloc[0]
    assert lone["oi_outlier_count"] == 1

    # Same build, but now the rest of the chain is busy enough that it holds
    # well under OUTLIER_MIN_SHARE of the day.
    busy = chain.copy()
    busy.loc[0:40, "CHG_IN_OI"] = 3000.0
    out = detect_outlier_strikes(busy).iloc[0]
    assert out["oi_outlier_count"] == 0


def test_z_gate_rejects_a_large_but_unremarkable_share() -> None:
    """And clearing the share alone is not enough either."""
    chain = _chain(n=20)
    # Every contract moves by the same large amount: one of them holding 5% of
    # the day is arithmetic, not information.
    chain["CHG_IN_OI"] = 5000.0
    out = detect_outlier_strikes(chain).iloc[0]
    assert out["oi_outlier_count"] == 0


def test_expiries_are_compared_separately() -> None:
    """A far-month build must not be judged against near-month activity.

    Near-month contracts carry systematically larger OI changes. If one scale
    were fitted across the whole chain, front-month churn would set it and a
    genuine far-dated position — the more interesting of the two — would never
    clear the threshold. Here the 9,000-contract far build is only ~11 sigmas
    against a pooled scale (below OUTLIER_Z) but ~240 against its own expiry.
    """
    # The busy front month must outnumber the quiet far month, or the pooled
    # median lands in the quiet group and the premise below does not hold.
    near = _chain(n=120, expiry=NEAR, seed=1)
    near["CHG_IN_OI"] = np.tile([800.0, -800.0], 60)
    far = _chain(expiry=FAR, seed=2)
    far.loc[55, "CHG_IN_OI"] = 9000.0
    chain = pd.concat([near, far], ignore_index=True)

    pooled_scale, pooled_centre = _robust_scale(chain["CHG_IN_OI"].to_numpy())
    assert abs((9000.0 - pooled_centre) / pooled_scale) < OUTLIER_Z, "premise"

    out = detect_outlier_strikes(chain).iloc[0]
    assert out["oi_outlier_count"] == 1
    assert out["oi_outlier_dte"] == (FAR - DAY).days


def test_thin_expiry_contributes_no_candidates() -> None:
    """Fewer strikes than MIN_CONTRACTS_PER_EXPIRY has no cross-section."""
    chain = _chain(n=MIN_CONTRACTS_PER_EXPIRY - 1)
    chain.loc[0, "CHG_IN_OI"] = 99999.0
    assert detect_outlier_strikes(chain).iloc[0]["oi_outlier_count"] == 0


def test_symbols_are_scored_independently() -> None:
    a = _chain(symbol="ACME", seed=3)
    a.loc[55, "CHG_IN_OI"] = 40000.0
    b = _chain(symbol="BETA", seed=4)
    out = detect_outlier_strikes(pd.concat([a, b], ignore_index=True))
    counts = out.set_index("symbol")["oi_outlier_count"]
    assert counts["ACME"] == 1
    assert counts["BETA"] == 0


def test_detector_rejects_a_frame_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        detect_outlier_strikes(_chain().drop(columns=["spot"]))


def test_detector_output_shape_is_stable_across_both_branches() -> None:
    """The flagged and unflagged paths must emit identical column sets, or a
    concat across days would produce ragged rows."""
    quiet = detect_outlier_strikes(_chain())
    loud = _chain()
    loud.loc[55, "CHG_IN_OI"] = 40000.0
    assert set(quiet.columns) == set(detect_outlier_strikes(loud).columns)


# ── futures basis ───────────────────────────────────────────────────────────


def _futures(**over: object) -> pd.DataFrame:
    base = dict(
        SYMBOL="ACME", DATE=DAY, EXPIRY_DT=NEAR, SETTLE_PR=1210.0, CLOSE=1209.0,
        OPEN_INT=100000, CHG_IN_OI=5000, CONTRACTS=8000, spot=SPOT,
        interest_rate=6.5,
    )
    nxt = dict(base, EXPIRY_DT=FAR, SETTLE_PR=1218.0, CLOSE=1217.0,
               OPEN_INT=25000, CHG_IN_OI=3000, CONTRACTS=900)
    return pd.DataFrame([dict(base, **over), nxt])


def test_basis_and_roll_known_values() -> None:
    out = futures_basis(_futures()).iloc[0]
    assert out["fut_basis"] == pytest.approx(10.0)
    assert out["fut_basis_pct"] == pytest.approx(10.0 / SPOT)
    dte = (NEAR - DAY).days
    assert out["fut_dte"] == dte
    assert out["fut_basis_annual"] == pytest.approx(10.0 / SPOT * 365 / dte * 100)
    assert out["fut_basis_excess"] == pytest.approx(out["fut_basis_annual"] - 6.5)
    assert out["fut_roll_ratio"] == pytest.approx(25000 / 125000)
    assert out["fut_roll_spread"] == pytest.approx((1218.0 - 1210.0) / 1210.0)


def test_settlement_is_the_mark_with_a_close_fallback() -> None:
    """SETTLE_PR is the official daily mark and exists even when the contract
    did not trade; CLOSE would then be a stale price showing a fake basis."""
    assert futures_basis(_futures()).iloc[0]["fut_basis"] == pytest.approx(10.0)
    fallback = futures_basis(_futures(SETTLE_PR=np.nan)).iloc[0]
    assert fallback["fut_basis"] == pytest.approx(9.0)


def test_annualisation_is_suppressed_near_expiry() -> None:
    """Dividing by a 1-day horizon turns a 0.5% basis into 182% 'annualised',
    which would dominate every downstream z-score. The un-annualised column
    stays populated so the day is not lost."""
    near_expiry = _futures(EXPIRY_DT=DAY + pd.Timedelta(days=1))
    out = futures_basis(near_expiry).iloc[0]
    assert pd.isna(out["fut_basis_annual"])
    assert pd.isna(out["fut_basis_excess"])
    assert out["fut_basis_pct"] == pytest.approx(10.0 / SPOT)


def test_roll_columns_are_null_without_a_next_month() -> None:
    single = _futures().iloc[[0]]
    out = futures_basis(single).iloc[0]
    assert pd.isna(out["fut_roll_ratio"])
    assert pd.isna(out["fut_roll_spread"])
    assert out["fut_basis"] == pytest.approx(10.0)


def test_expired_contracts_are_not_mistaken_for_the_near_month() -> None:
    """A stale row dated before the trade date would otherwise sort first and
    become 'near month', reporting a nonsense negative dte."""
    stale = _futures().iloc[[0]].copy()
    stale["EXPIRY_DT"] = DAY - pd.Timedelta(days=5)
    stale["SETTLE_PR"] = 1100.0
    out = futures_basis(pd.concat([stale, _futures()], ignore_index=True)).iloc[0]
    assert out["fut_dte"] == (NEAR - DAY).days
    assert out["fut_basis"] == pytest.approx(10.0)


def test_futures_rejects_a_frame_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        futures_basis(_futures().drop(columns=["interest_rate"]))


# ── open-interest units ─────────────────────────────────────────────────────


def test_notionals_do_not_multiply_by_the_lot_size() -> None:
    """Regression: bhavcopy OPEN_INT is in SHARES, not contracts.

    The project's own lot-size reference asserted the opposite, and applying a
    lot factor here inflated every notional by 2-3 orders of magnitude —
    RELIANCE's futures book came out at Rs 8.5 lakh crore instead of Rs 17,102
    crore. Settled against participant_oi, which reports contracts: summing
    OPEN_INT/lot over all stock futures matches its TOTAL to within 0.02%,
    while the raw sum is off by ~2,158x. So notional is oi * spot, full stop.
    """
    from mktmtm.features.flow import _attach_notionals

    frame = pd.DataFrame(
        [
            {
                "symbol": "ACME", "date": DAY, "spot": SPOT,
                "_build_contracts": 1000.0, "_unwind_contracts": 250.0,
                "fut_oi": 100000.0, "fut_oi_chg": -5000.0,
            }
        ]
    )
    out = _attach_notionals(frame).iloc[0]
    assert out["oi_outlier_build_notional"] == pytest.approx(1000.0 * SPOT)
    assert out["oi_outlier_unwind_notional"] == pytest.approx(250.0 * SPOT)
    assert out["fut_oi_notional"] == pytest.approx(100000.0 * SPOT)
    assert out["fut_oi_chg_notional"] == pytest.approx(-5000.0 * SPOT)


# ── schema and persistence ──────────────────────────────────────────────────


def test_flow_columns_mirror_the_ddl() -> None:
    """FLOW_COLUMNS is the source of truth for identity *and* order, and
    init_flow_features.sql mirrors it — the same contract DATASET_COLUMNS has
    with init_ml_dataset.sql. Adding a feature means editing both."""
    ddl = (
        Path(__file__).resolve().parents[2]
        / "src/mktmtm/storage/sql/init_flow_features.sql"
    ).read_text()
    body = ddl[ddl.index("CREATE TABLE") : ddl.index("PRIMARY KEY")]
    declared = [
        m.group(1)
        for line in body.splitlines()
        if (m := re.match(r"\s{4}(\w+)\s+(?:VARCHAR|DATE|INTEGER|BIGINT|DOUBLE)", line))
    ]
    assert declared == list(FLOW_COLUMNS)


def test_write_emits_plain_python_scalars() -> None:
    """Regression (shared with participant_oi): psycopg2 cannot adapt
    numpy.int64/float64, and pandas hands exactly those out of itertuples."""
    captured: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    frame = pd.DataFrame(
        [
            {
                **{c: np.float64(1.5) for c in FLOW_COLUMNS},
                "symbol": "ACME",
                "date": DAY,
                "oi_outlier_count": np.int64(3),
                "fut_dte": np.int64(20),
                "fut_oi": np.int64(100000),
                "fut_oi_chg": np.int64(5000),
                "fut_volume": np.int64(8000),
                "fut_roll_ratio": np.nan,
            }
        ]
    )

    from mktmtm.features import flow as flow_mod

    original = flow_mod.execute_values
    flow_mod.execute_values = lambda cur, sql, rows, page_size=100: captured.extend(rows)
    try:
        assert write_flow_features(_Conn(), frame) == 1
    finally:
        flow_mod.execute_values = original

    (row,) = captured
    assert type(row[0]) is str
    assert type(row[1]) is dt.date
    for name, value in zip(FLOW_COLUMNS[2:], row[2:], strict=True):
        assert not isinstance(value, np.generic), f"{name} leaked a numpy scalar"
        assert value is None or isinstance(value, int | float), name
    assert row[FLOW_COLUMNS.index("fut_roll_ratio")] is None, "NaN must become NULL"


def test_thresholds_are_the_measured_defaults() -> None:
    """Pins the operating point chosen in docs/implementation/flow-features.md.
    Loosening these silently is how the detector goes back to flagging 99% of
    symbol-days."""
    assert (OUTLIER_Z, OUTLIER_MIN_SHARE) == (25.0, 0.05)


def test_expiry_parsing_survives_a_may_then_june_chunk() -> None:
    """Regression: 'May' is identical under %b and %B, so pandas infers
    '%d-%B-%Y' from a May expiry and then raises on '27-Jun-2024'. This killed
    the first full five-year build. The format must always be explicit."""
    from mktmtm.features.flow import _parse_expiry

    parsed = _parse_expiry(pd.Series(["30-May-2024", "27-Jun-2024", "26-Sep-2024"]))
    assert list(parsed) == [
        pd.Timestamp("2024-05-30"),
        pd.Timestamp("2024-06-27"),
        pd.Timestamp("2024-09-26"),
    ]
    already = pd.Series(pd.to_datetime(["2024-05-30"]))
    assert list(_parse_expiry(already)) == [pd.Timestamp("2024-05-30")]
