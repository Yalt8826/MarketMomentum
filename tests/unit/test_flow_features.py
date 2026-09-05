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
    futures_quadrants,
    option_quadrants,
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


# ── buildup quadrants ───────────────────────────────────────────────────────


def _futures_two_days(
    price_delta: float, oi_delta: float, contracts: float = 500.0
) -> pd.DataFrame:
    """One futures contract on two consecutive days, with a chosen move."""
    return pd.DataFrame(
        {
            "SYMBOL": ["ACME", "ACME"],
            "DATE": [DAY - pd.Timedelta(days=1), DAY],
            "EXPIRY_DT": ["30-Jan-2025", "30-Jan-2025"],
            "SETTLE_PR": [1200.0, 1200.0 + price_delta],
            "CLOSE": [1200.0, 1200.0 + price_delta],
            "CHG_IN_OI": [0.0, oi_delta],
            "CONTRACTS": [contracts, contracts],
            "spot": [SPOT, SPOT],
        }
    )


@pytest.mark.parametrize(
    ("price_delta", "oi_delta", "expected"),
    [
        (+5.0, +1000.0, "q_long_buildup_notional"),
        (-5.0, +1000.0, "q_short_buildup_notional"),
        (+5.0, -1000.0, "q_short_cover_notional"),
        (-5.0, -1000.0, "q_long_unwind_notional"),
    ],
)
def test_futures_quadrants_classify_the_four_cases(
    price_delta: float, oi_delta: float, expected: str
) -> None:
    """Each price/OI sign pair lands in exactly one bucket, and only one."""
    out = futures_quadrants(_futures_two_days(price_delta, oi_delta))
    row = out.loc[out["date"] == DAY].iloc[0]

    buckets = [
        "q_long_buildup_notional",
        "q_short_buildup_notional",
        "q_short_cover_notional",
        "q_long_unwind_notional",
    ]
    assert row[expected] == pytest.approx(abs(oi_delta) * SPOT)
    assert all(row[b] == 0.0 for b in buckets if b != expected)


def test_futures_direction_signs_follow_the_bullish_buckets() -> None:
    """Long buildup and short covering are bullish; the other two are bearish."""
    bullish = futures_quadrants(_futures_two_days(+5.0, +1000.0))
    bearish = futures_quadrants(_futures_two_days(-5.0, +1000.0))
    assert bullish.loc[bullish["date"] == DAY, "q_fut_direction"].iloc[0] == 1.0
    assert bearish.loc[bearish["date"] == DAY, "q_fut_direction"].iloc[0] == -1.0


def test_untraded_contracts_get_no_quadrant() -> None:
    """CONTRACTS == 0 contributes nothing.

    On an untraded contract SETTLE_PR is a theoretical mark, so the change
    between two consecutive such marks is model output, not a transaction.
    Giving it a quadrant would manufacture signal out of arithmetic.
    """
    out = futures_quadrants(_futures_two_days(+5.0, +1000.0, contracts=0.0))
    assert out.empty or out.loc[out["date"] == DAY].empty


def test_new_listings_have_null_not_zero_price_change() -> None:
    """A contract with no previous day is excluded, not treated as unchanged."""
    frame = _futures_two_days(+5.0, +1000.0)
    frame = frame.loc[frame["DATE"] == DAY]  # drop the history
    out = futures_quadrants(frame)
    assert out.empty or out.loc[out["date"] == DAY].empty


def test_expiry_roll_drops_vanished_contracts() -> None:
    """An expired contract present only on the earlier day does not resurrect."""
    frame = _futures_two_days(+5.0, +1000.0)
    expired = frame.iloc[[0]].copy()
    expired["EXPIRY_DT"] = "26-Dec-2024"
    out = futures_quadrants(pd.concat([frame, expired], ignore_index=True))
    # Only the surviving contract's move is counted.
    row = out.loc[out["date"] == DAY].iloc[0]
    assert row["q_long_buildup_notional"] == pytest.approx(1000.0 * SPOT)


def _options_two_days(
    option_typ: str,
    premium_delta: float,
    oi_delta: float = 1000.0,
    *,
    delta_prev: float = 0.5,
    spot_move: float = 0.0,
) -> pd.DataFrame:
    """One option contract with a chosen premium move and OI build.

    ``spot_move`` defaults to 0 so ``premium_delta`` *is* the delta-adjusted
    residual and the direction tests stay about the sign mapping rather than
    the adjustment arithmetic.
    """
    return pd.DataFrame(
        {
            "SYMBOL": ["ACME"],
            "DATE": [DAY],
            "OPTION_TYP": [option_typ],
            "CHG_IN_OI": [oi_delta],
            "CONTRACTS": [500.0],
            "spot": [SPOT + spot_move],
            "spot_prev": [SPOT],
            "mark": [50.0 + premium_delta],
            "mark_prev": [50.0],
            "delta_prev": [delta_prev],
        }
    )


def test_call_writing_and_put_writing_have_opposite_signs() -> None:
    """Calls bought and puts written are bullish; puts bought and calls written bearish.

    This is the test that catches the likeliest bug in this family. A sign
    transposition is invisible in aggregate — every downstream number stays
    plausible while meaning the reverse — and it would silently invert the
    regime states and the attribution built on top.
    """
    call_bought = option_quadrants(_options_two_days("CE", +5.0))
    call_written = option_quadrants(_options_two_days("CE", -5.0))
    put_bought = option_quadrants(_options_two_days("PE", +5.0))
    put_written = option_quadrants(_options_two_days("PE", -5.0))

    assert call_bought["opt_direction"].iloc[0] == 1.0
    assert put_written["opt_direction"].iloc[0] == 1.0
    assert call_written["opt_direction"].iloc[0] == -1.0
    assert put_bought["opt_direction"].iloc[0] == -1.0

    # And the writer fractions land on the right side of the chain.
    assert call_written["ce_writer_frac"].iloc[0] == 1.0
    assert call_bought["ce_writer_frac"].iloc[0] == 0.0
    assert put_written["pe_writer_frac"].iloc[0] == 1.0


def test_option_quadrants_ignore_closing_interest() -> None:
    """Only newly opened interest carries a view; a close-out is excluded."""
    out = option_quadrants(_options_two_days("CE", +5.0, oi_delta=-1000.0))
    assert out.empty or pd.isna(out["opt_direction"].iloc[0])


def test_direction_score_is_null_when_no_traded_flow() -> None:
    """A zero denominator surfaces as NaN, never as a neutral 0.0.

    A symbol-day with no traded open-interest change is *unmeasured*, not
    *neutral* — the two average to opposite conclusions.
    """
    flat = option_quadrants(_options_two_days("CE", 0.0))
    assert flat.empty or pd.isna(flat["opt_direction"].iloc[0])


def test_direction_scores_are_bounded() -> None:
    """Random builds keep both direction scores inside [-1, 1]."""
    rng = np.random.default_rng(7)
    n = 400
    frame = pd.DataFrame(
        {
            "SYMBOL": rng.choice(["ACME", "BETA"], n),
            "DATE": DAY,
            "OPTION_TYP": rng.choice(["CE", "PE"], n),
            "CHG_IN_OI": rng.integers(1, 5000, n).astype("float64"),
            "CONTRACTS": rng.integers(1, 900, n).astype("float64"),
            "spot": SPOT,
            "spot_prev": SPOT,
            "delta_prev": rng.uniform(-1, 1, n),
            "mark": 50.0 + rng.normal(0, 5, n),
            "mark_prev": 50.0,
        }
    )
    out = option_quadrants(frame)
    assert out["opt_direction"].between(-1.0, 1.0).all()
    assert out["ce_writer_frac"].between(0.0, 1.0).all()
    assert out["pe_writer_frac"].between(0.0, 1.0).all()


def test_outlier_direction_is_null_without_price_history() -> None:
    """The detector still works when no mark is supplied; direction stays NaN."""
    chain = _chain()
    chain.loc[0, "CHG_IN_OI"] = 500_000.0
    out = detect_outlier_strikes(chain)
    assert out["oi_outlier_count"].iloc[0] >= 1
    assert pd.isna(out["oi_outlier_direction"].iloc[0])
    assert pd.isna(out["oi_outlier_writer_frac"].iloc[0])


def test_outlier_direction_reads_the_flagged_strike() -> None:
    """A flagged call build on a falling premium is call writing, hence bearish."""
    chain = _chain()
    chain["CONTRACTS"] = 500.0
    chain["mark"] = 50.0
    chain["mark_prev"] = 50.0
    chain["delta_prev"] = 0.5
    chain["spot_prev"] = SPOT  # flat underlying: the residual is the raw move
    chain.loc[0, "OPTION_TYP"] = "CE"
    chain.loc[0, "CHG_IN_OI"] = 500_000.0
    chain.loc[0, "mark"] = 40.0  # premium fell while OI surged -> writers

    out = detect_outlier_strikes(chain)
    assert out["oi_outlier_count"].iloc[0] == 1
    assert out["oi_outlier_writer_frac"].iloc[0] == 1.0
    assert out["oi_outlier_direction"].iloc[0] == -1.0


def test_empty_day_does_not_poison_the_merge_key_dtype() -> None:
    """A quiet day's empty frame must not turn a concatenated date column to object.

    Under pandas 3.0 an empty frame's dtypes take part in concat resolution, so
    one such day silently made `date` object-typed and the build then died at
    the merge — 3.5 minutes in, with a message about datetime64[us] vs object
    that pointed nowhere near the cause.
    """
    from mktmtm.features.flow import _empty_keyed_frame

    empty = option_quadrants(
        _options_two_days("CE", 0.0)  # flat premium -> nothing to classify
    )
    real = option_quadrants(_options_two_days("CE", +5.0))
    combined = pd.concat([empty, real], ignore_index=True)
    assert combined["date"].dtype.kind == "M"

    typed = _empty_keyed_frame(["a", "b"])
    assert typed["date"].dtype.kind == "M"
    assert list(typed.columns) == ["symbol", "date", "a", "b"]


def test_row_groups_stream_in_trade_date_order() -> None:
    """The cache's row groups are not stored chronologically; the pass must sort them.

    The frame is sorted by symbol, so the one-row-group-per-date layout comes
    out in date order only by accident — it does not, with 293 backward steps
    over the five years. Streaming in file order and calling the last group
    "yesterday" joins each day against an arbitrary other day, and nothing
    complains because the same contract exists on both days.
    """
    from mktmtm.features.flow import _ordered_row_groups

    scrambled = [
        pd.Timestamp("2024-03-05"),   # rg 0
        pd.Timestamp("2022-01-04"),   # rg 1
        pd.Timestamp("2023-06-12"),   # rg 2
        pd.Timestamp("2022-01-05"),   # rg 3
    ]

    class _FakeReader:
        class metadata:  # noqa: N801 - mirrors pyarrow's attribute shape
            num_row_groups = len(scrambled)

    def _fake_date(_reader: object, rg: int) -> pd.Timestamp:
        return scrambled[rg]

    import mktmtm.features.flow as flow_mod

    original = flow_mod._row_group_date
    flow_mod._row_group_date = _fake_date  # type: ignore[assignment]
    try:
        wanted = {pd.Timestamp("2022-01-05"), pd.Timestamp("2023-06-12")}
        groups, emit = _ordered_row_groups(_FakeReader(), wanted)  # type: ignore[arg-type]
    finally:
        flow_mod._row_group_date = original  # type: ignore[assignment]

    # Chronological, and seeded with the group immediately before the window.
    assert groups == [1, 3, 2]
    assert emit == {3, 2}
    assert [scrambled[g] for g in groups] == sorted(scrambled[g] for g in groups)


def test_delta_adjustment_removes_a_pure_spot_move() -> None:
    """A premium move exactly explained by the underlying classifies as neither side.

    This is the confound the adjustment exists for: a call premium rises when
    the stock rises, with no aggressive buyer involved. Measured over 163,383
    traded contracts, the raw and adjusted readings disagree on 35.8% of them,
    which is why the raw change is not used as a fallback.
    """
    # delta 0.5, spot +10 -> a +5.0 premium move is fully explained.
    explained = option_quadrants(
        _options_two_days("CE", +5.0, delta_prev=0.5, spot_move=+10.0)
    )
    assert explained.empty or pd.isna(explained["opt_direction"].iloc[0])

    # The same premium move with a flat underlying is genuine buying.
    genuine = option_quadrants(
        _options_two_days("CE", +5.0, delta_prev=0.5, spot_move=0.0)
    )
    assert genuine["opt_direction"].iloc[0] == 1.0


def test_delta_adjustment_can_flip_the_reading() -> None:
    """A rising premium can still be writer-driven once the spot move is removed."""
    # Premium +2 but the underlying alone explains +5: net, writers pressed.
    flipped = option_quadrants(
        _options_two_days("CE", +2.0, delta_prev=0.5, spot_move=+10.0)
    )
    assert flipped["ce_writer_frac"].iloc[0] == 1.0
    assert flipped["opt_direction"].iloc[0] == -1.0


def test_contracts_without_delta_are_dropped_not_guessed() -> None:
    """No delta means no classification — the raw change is not a usable fallback."""
    frame = _options_two_days("CE", +5.0).drop(columns=["delta_prev"])
    out = option_quadrants(frame)
    assert out.empty or pd.isna(out["opt_direction"].iloc[0])
