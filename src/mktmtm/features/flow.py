"""Per-symbol institutional-flow features: outlier strikes and futures basis.

Two families of candidate features, both derived from data already on disk —
no new download, and no ETL re-run, because both read the bhavcopy parquet
cache rather than ``option_metrics.option_chain``.

**Why these two.** ``participant_oi`` says what institutions did, but only
market-wide: joined on date alone it is constant within a date and contributes
exactly zero cross-sectional signal. The chain has per-symbol, per-strike
detail but nobody's name attached. These features try to recover the
*fingerprint* of institutional activity from the anonymous chain, so that the
market-wide series can then be used to check it (see ``docs/implementation/
flow-features.md`` for the aggregate-and-correlate validation design).

``oi_outlier_*`` — a strike whose open-interest change is extreme against the
other strikes of the same expiry that day. Every existing chain feature
(``chain_oi_chg``, ``oi_hhi``, ``otm_put_oi_build``) is an average or a sum
over ~300 strikes, and averaging is precisely what destroys a localised
signal: nobody accumulates 50,000 contracts of a 15%-OTM call by accident, but
that build is invisible once it is summed alongside the ATM churn. The output
here is deliberately sparse and *pointable* — a date, a symbol, a strike.

``fut_*`` — near-month futures basis measured against the cost of carry, plus
the near-to-next roll. This is the family with an external check available:
``participant_oi.net_fut_stock`` / ``net_fut_index`` is literally FII and Pro
futures positioning, the same quantity from an independent source.

Memory: the cache is 60.6M rows and has previously OOM-killed the ETL on this
host (see ``storage.etl._load_cache``). Outlier detection is entirely local to
one (symbol, date, expiry), and the cache is written one row group per trade
date, so this module streams row group by row group — peak memory is one
trading day (~48k rows), never the whole frame. The futures pass instead
push-downs an ``INSTRUMENT`` filter at read time, which selects ~752k of the
60.6M rows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from psycopg2.extras import execute_values

from mktmtm.config.settings import settings
from mktmtm.progress import progress_stage

logger = logging.getLogger(__name__)

__all__ = [
    "FLOW_COLUMNS",
    "build_flow_features",
    "detect_outlier_strikes",
    "export_flow_parquet",
    "futures_basis",
    "futures_quadrants",
    "option_quadrants",
    "run_flow_features",
    "write_flow_features",
]

# ── outlier-strike thresholds ───────────────────────────────────────────────
# A contract is flagged only when it clears BOTH gates, and both defaults were
# measured rather than assumed (docs/implementation/flow-features.md §3).
#
# The z gate alone is not enough. On a sample of 24 days spread over the five
# years the traded-contract |z| distribution runs median 0.7, p90 3.1, p99
# 18.4, p99.9 53 — so a nominal-looking z >= 4 sits near the 92nd percentile
# and fires on 95% of symbol-days. 25 is roughly the 99.4th percentile.
#
# The share gate turns "largest change of the day" into "materially
# concentrated": the flagged contract must hold at least this fraction of the
# symbol-day's entire |chg_oi|. Its distribution is far better behaved than
# the z (p95 3.8%, p99 11.1%), and it is what suppresses a quiet day.
#
# Together they flag 10.8%-16.6% of symbol-days depending on the year, with no
# trend across 2021-2026 — the check that matters, since F&O liquidity grew
# several-fold over the window and a detector that merely tracked volume would
# have shown a monotone drift instead.
OUTLIER_Z = 25.0
OUTLIER_MIN_SHARE = 0.05

# 1 / Phi^-1(0.75): scales the median absolute deviation to a normal sigma.
_MAD_TO_SIGMA = 1.4826
# (p90 - p10) / (2 * Phi^-1(0.90)): the same idea for the fallback scale below.
_P80_TO_SIGMA = 1.0 / 2.5631

# Minimum contracts that actually moved before a scale can be estimated from
# them; below this the expiry contributes no candidates.
_MIN_TRADED_FOR_SCALE = 8

# An expiry with fewer live contracts than this has no meaningful cross-section
# to compare a strike against, so it contributes no candidates.
MIN_CONTRACTS_PER_EXPIRY = 15

# Annualising a basis over a 1-day horizon multiplies it by 365. A 0.5% basis
# on expiry-minus-one becomes 182% "annualised" and would dominate every
# downstream z-score — the dataset already carries 29 features with kurtosis
# over 100. Below this horizon the annualised columns stay NULL; the
# un-annualised fut_basis_pct is well-behaved at every dte and is always set.
ANNUALISE_MIN_DTE = 5

_DAYS_PER_YEAR = 365.0

# Rows per UPDATE/INSERT statement; matches gex_features._PAGE_SIZE.
_PAGE_SIZE = 1000

_OUTLIER_COLUMNS = (
    "oi_outlier_count",
    "oi_outlier_max_absz",
    "oi_outlier_share",
    "oi_outlier_call_frac",
    "oi_outlier_moneyness",
    "oi_outlier_dte",
    "oi_outlier_build_notional",
    "oi_outlier_unwind_notional",
)

_FUTURES_COLUMNS = (
    "fut_dte",
    "fut_basis",
    "fut_basis_pct",
    "fut_basis_annual",
    "fut_basis_excess",
    "fut_oi",
    "fut_oi_chg",
    "fut_volume",
    "fut_oi_notional",
    "fut_oi_chg_notional",
    "fut_roll_ratio",
    "fut_roll_spread",
)

# ── buildup quadrants (direction) ───────────────────────────────────────────
# Open-interest change crossed with price change. An OI build alone cannot
# tell a buyer from a writer — the defect this family exists to fix, recorded
# in init_flow_features.sql's header when the outlier detector was built.
#
# For a futures contract the four cases are the standard OI reading; for an
# option the price is the premium, so a rising premium with rising OI is
# buyer-driven and a falling premium with rising OI is writer-driven. Writers
# are the institutional side far more often than the retail side, since option
# writing needs margin.
_FUT_QUADRANT_COLUMNS = (
    "q_long_buildup_notional",
    "q_short_buildup_notional",
    "q_short_cover_notional",
    "q_long_unwind_notional",
    "q_fut_direction",
)

_QUADRANT_COLUMNS = (
    "q_long_buildup_notional",
    "q_short_buildup_notional",
    "q_short_cover_notional",
    "q_long_unwind_notional",
    "q_fut_direction",
    "ce_writer_frac",
    "pe_writer_frac",
    "opt_direction",
    "oi_outlier_writer_frac",
    "oi_outlier_direction",
    "q_fut_direction_resid",
)

#: Column identity *and* order for ``flow_features``; ``init_flow_features.sql``
#: mirrors this, the same contract ``DATASET_COLUMNS`` has with
#: ``init_ml_dataset.sql``. Adding a feature means editing both.
FLOW_COLUMNS = (
    "symbol", "date", *_OUTLIER_COLUMNS, *_FUTURES_COLUMNS, *_QUADRANT_COLUMNS,
)

_INT_COLUMNS = frozenset(
    {"oi_outlier_count", "fut_dte", "fut_oi", "fut_oi_chg", "fut_volume"}
)


_EXPIRY_FORMAT = "%d-%b-%Y"


def _parse_expiry(values: pd.Series) -> pd.Series:
    """Parse bhavcopy ``EXPIRY_DT`` with an explicit format, like the ETL does.

    Letting pandas infer the format here is a live bug, not a style point:
    "May" is spelled identically under ``%b`` and ``%B``, so a chunk whose
    first expiry falls in May infers ``%d-%B-%Y`` and then dies on the next
    ``27-Jun-2024`` — which is exactly how the first full five-year build
    failed. ``storage.etl`` passes ``format="%d-%b-%Y"`` for the same reason.

    Already-parsed datetime input is passed through, since the cache's dtype
    depends on how the parquet was written.
    """
    if pd.api.types.is_datetime64_any_dtype(values):
        return pd.to_datetime(values)
    return pd.to_datetime(values, format=_EXPIRY_FORMAT, errors="coerce")


def _robust_scale(values: np.ndarray) -> tuple[float, float]:
    """Robust ``(scale, centre)`` of an expiry's OI changes, from traded contracts only.

    A plain standard deviation is inflated by exactly the large build we are
    hunting, which shrinks its own z below any threshold — the estimator has to
    be robust or the detector cannot fire.

    Estimating it over *every* contract does not work either, and this is the
    subtler failure. An option chain is heavily zero-inflated: most listed
    strikes see no OI change on a given day, so the median and the median
    absolute deviation both collapse toward zero and every contract that moved
    at all scores an enormous z. Measured, that put 22% of all contracts over
    z = 4 and flagged 99.4% of symbol-days — the detector was reporting the
    chain's noise floor. Restricting the estimate to contracts with a non-zero
    change compares a strike against the others that actually traded, which is
    the intended cross-section, and drops the same flag rate to ~13%.

    The MAD is the first choice; when even the traded contracts are tied (a
    handful of identical changes) a p10-p90 spread stands in. Returns a scale
    of 0.0 when both degenerate, which the caller reads as "no usable
    cross-section here" and skips.
    """
    traded = values[values != 0]
    if traded.size < _MIN_TRADED_FOR_SCALE:
        return 0.0, 0.0
    med = float(np.median(traded))
    mad = float(np.median(np.abs(traded - med)))
    if mad > 0:
        return _MAD_TO_SIGMA * mad, med
    lo, hi = np.percentile(traded, [10.0, 90.0])
    spread = float(hi - lo)
    return (_P80_TO_SIGMA * spread, med) if spread > 0 else (0.0, med)


def detect_outlier_strikes(
    options: pd.DataFrame,
    z_threshold: float = OUTLIER_Z,
    min_share: float = OUTLIER_MIN_SHARE,
) -> pd.DataFrame:
    """Flag strikes with an extreme OI change and summarise them per symbol-day.

    ``options`` needs ``SYMBOL, DATE, EXPIRY_DT, STRIKE_PR, OPTION_TYP,
    CHG_IN_OI, spot`` and may span any number of dates. Pure — no I/O.

    The comparison is *within* ``(symbol, date, expiry)``. Comparing a
    far-month strike against near-month ones would be meaningless: near-month
    contracts carry systematically larger OI changes, so a single pooled
    z-score would flag the front expiry every day and never see a far-dated
    build, which is the more interesting of the two.

    ``CHG_IN_OI`` keeps its sign through the z-score and the threshold is on
    ``|z|``, so a large *unwind* is flagged as readily as a large build — both
    are institutional activity — and the two are reported separately rather
    than netted.

    Returns one row per symbol-day that had at least one usable expiry, with
    the ``oi_outlier_*`` summary plus the raw flagged open-interest counts
    (``_build_contracts`` / ``_unwind_contracts``, in shares) that
    :func:`build_flow_features` turns into notional. Ratio columns are NaN, not
    0, on a day with nothing flagged: no outliers means the call share and mean
    moneyness are undefined, and writing 0 there would be a sentinel.

    When ``options`` also carries ``mark`` / ``mark_prev`` / ``CONTRACTS``, the
    flagged strikes additionally get ``oi_outlier_writer_frac`` and
    ``oi_outlier_direction`` — the buildup quadrant restricted to the outliers,
    which is what turns "somebody opened Rs 400 crore here" into "somebody took
    a bullish/bearish position here". Without those columns both stay NaN and
    the rest of the output is unchanged.
    """
    required = {
        "SYMBOL", "DATE", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "CHG_IN_OI", "spot",
    }
    missing = required - set(options.columns)
    if missing:
        raise ValueError(f"detect_outlier_strikes missing columns: {sorted(missing)}")

    work = options.loc[
        options["CHG_IN_OI"].notna()
        & options["spot"].notna()
        & (options["spot"] > 0)
    ].copy()
    if work.empty:
        return _empty_outlier_frame()

    # Positional indexing below (`groupby().indices` + `iloc`) needs a
    # contiguous RangeIndex; the filter above leaves gaps.
    work = work.reset_index(drop=True)
    work["chg_oi"] = work["CHG_IN_OI"].astype("float64")
    work["abs_chg"] = work["chg_oi"].abs()

    # Denominator for the share gate: the symbol-day's whole chain, including
    # the expiries too thin to produce candidates. "5% of everything that moved
    # today" is the claim we want to make, not "5% of what we looked at".
    day_abs = work.groupby(["SYMBOL", "DATE"])["abs_chg"].transform("sum")

    # Scored into a plain array and assigned once. Writing into the frame
    # inside the loop would re-enter pandas' indexing machinery per expiry
    # group, and there are ~3 groups per symbol-day across 1,254 days.
    chg = work["chg_oi"].to_numpy()
    absz = np.full(len(work), np.nan)
    for _, positions in work.groupby(
        ["SYMBOL", "DATE", "EXPIRY_DT"], sort=False
    ).indices.items():
        idx = np.asarray(positions)
        if idx.size < MIN_CONTRACTS_PER_EXPIRY:
            continue
        values = chg[idx]
        scale, centre = _robust_scale(values)
        if scale <= 0:
            continue
        absz[idx] = np.abs((values - centre) / scale)
    work["absz"] = absz

    share = work["abs_chg"] / day_abs.where(day_abs > 0)
    flagged = work.loc[(work["absz"] >= z_threshold) & (share >= min_share)].copy()

    # Every symbol-day that reached the detector gets a row, so a day with
    # nothing flagged is recorded as a measured zero rather than a gap.
    seen = (
        work.groupby(["SYMBOL", "DATE"], as_index=False)[["abs_chg"]]
        .sum()
        .rename(columns={"SYMBOL": "symbol", "DATE": "date", "abs_chg": "_day_abs"})
    )

    if flagged.empty:
        return _no_outliers_frame(seen)

    # Weighting is by |chg_oi| throughout: a 40,000-contract build should move
    # the reported moneyness far more than a 400-contract one that happened to
    # clear the same threshold. The weighted means are accumulated as sums here
    # and divided once below, which keeps this a vectorised groupby rather than
    # a per-group Python call.
    weight = flagged["abs_chg"]
    flagged["_w_call"] = weight * (flagged["OPTION_TYP"] == "CE").astype("float64")
    flagged["_w_money"] = weight * (
        (flagged["STRIKE_PR"] - flagged["spot"]) / flagged["spot"]
    )
    flagged["_w_dte"] = weight * (
        _parse_expiry(flagged["EXPIRY_DT"]) - pd.to_datetime(flagged["DATE"])
    ).dt.days.astype("float64")
    flagged["_build"] = flagged["chg_oi"].clip(lower=0.0)
    flagged["_unwind"] = (-flagged["chg_oi"]).clip(lower=0.0)
    _attach_outlier_quadrant(flagged)

    agg = flagged.groupby(["SYMBOL", "DATE"], as_index=False).agg(
        oi_outlier_count=("chg_oi", "size"),
        oi_outlier_max_absz=("absz", "max"),
        _flagged_abs=("abs_chg", "sum"),
        _w_call=("_w_call", "sum"),
        _w_money=("_w_money", "sum"),
        _w_dte=("_w_dte", "sum"),
        _build_contracts=("_build", "sum"),
        _unwind_contracts=("_unwind", "sum"),
        _out_writer=("_out_writer", "sum"),
        _out_buyer=("_out_buyer", "sum"),
        _out_bull=("_out_bull", "sum"),
        _out_bear=("_out_bear", "sum"),
    ).rename(columns={"SYMBOL": "symbol", "DATE": "date"})

    out = seen.merge(agg, on=["symbol", "date"], how="left")
    out["oi_outlier_count"] = out["oi_outlier_count"].fillna(0).astype("int64")
    for col in ("_flagged_abs", "_build_contracts", "_unwind_contracts"):
        out[col] = out[col].fillna(0.0)

    total = out["_flagged_abs"].where(out["_flagged_abs"] > 0)
    out["oi_outlier_call_frac"] = out["_w_call"] / total
    out["oi_outlier_moneyness"] = out["_w_money"] / total
    out["oi_outlier_dte"] = out["_w_dte"] / total
    out["oi_outlier_share"] = out["_flagged_abs"] / out["_day_abs"].where(
        out["_day_abs"] > 0
    )

    traded = out["_out_writer"] + out["_out_buyer"]
    out["oi_outlier_writer_frac"] = out["_out_writer"] / traded.where(traded > 0)
    out["oi_outlier_direction"] = _direction_score(out["_out_bull"], out["_out_bear"])
    return out.drop(
        columns=[
            "_day_abs", "_flagged_abs", "_w_call", "_w_money", "_w_dte",
            "_out_writer", "_out_buyer", "_out_bull", "_out_bear",
        ]
    )


def _attach_outlier_quadrant(flagged: pd.DataFrame) -> None:
    """Add the writer/buyer and bull/bear notional splits to the flagged frame, in place.

    Restricted to flagged strikes that actually *opened* interest on a day they
    actually traded — the same two gates :func:`option_quadrants` applies, for
    the same reasons. When the caller supplied no previous-day mark every
    bucket is 0.0, which sums to a zero denominator and so surfaces as NaN, not
    as a spurious neutral reading.
    """
    zero = pd.Series(0.0, index=flagged.index)
    have_mark = {"mark", "mark_prev", "CONTRACTS"} <= set(flagged.columns)
    if not have_mark:
        for col in ("_out_writer", "_out_buyer", "_out_bull", "_out_bear"):
            flagged[col] = zero
        return

    d_prem = _premium_residual(flagged)
    usable = (
        (flagged["CONTRACTS"].fillna(0) > 0)
        & (flagged["chg_oi"] > 0)
        & flagged["mark"].notna()
        & flagged["mark_prev"].notna()
        & d_prem.notna()
    )
    notional = flagged["chg_oi"].astype("float64") * flagged["spot"].astype("float64")
    is_call = flagged["OPTION_TYP"].astype(str) == "CE"
    writer = usable & (d_prem < 0)
    buyer = usable & (d_prem > 0)

    flagged["_out_writer"] = notional.where(writer, 0.0)
    flagged["_out_buyer"] = notional.where(buyer, 0.0)
    # Calls bought and puts written are bullish; puts bought and calls
    # written are bearish.
    flagged["_out_bull"] = notional.where(
        (is_call & buyer) | (~is_call & writer), 0.0
    )
    flagged["_out_bear"] = notional.where(
        (~is_call & buyer) | (is_call & writer), 0.0
    )


def _no_outliers_frame(seen: pd.DataFrame) -> pd.DataFrame:
    """A day where nothing cleared both gates: measured zeros, undefined ratios.

    ``count`` and ``share`` are real zeros — nothing was flagged, and none of
    the day's activity sat in an outlier. The weighted means are NaN rather
    than 0, because with no flagged contracts there is nothing to average; a 0
    there would be a sentinel indistinguishable from a genuine at-the-money
    reading (docs/data/iv-integrity-audit.md).
    """
    out = seen.drop(columns=["_day_abs"]).copy()
    out["oi_outlier_count"] = 0
    out["oi_outlier_max_absz"] = np.nan
    out["oi_outlier_share"] = 0.0
    out["oi_outlier_call_frac"] = np.nan
    out["oi_outlier_moneyness"] = np.nan
    out["oi_outlier_dte"] = np.nan
    out["_build_contracts"] = 0.0
    out["_unwind_contracts"] = 0.0
    out["oi_outlier_writer_frac"] = np.nan
    out["oi_outlier_direction"] = np.nan
    return out


def _empty_outlier_frame() -> pd.DataFrame:
    """The detector's column set with no rows.

    Notional columns are added later, once spot is merged on, so they are not
    part of this frame."""
    cols = [
        "symbol", "date",
        *[c for c in _OUTLIER_COLUMNS if not c.endswith("_notional")],
        "_build_contracts", "_unwind_contracts",
        "oi_outlier_writer_frac", "oi_outlier_direction",
    ]
    return pd.DataFrame(columns=cols)


def futures_basis(futures: pd.DataFrame) -> pd.DataFrame:
    """Near-month basis against carry, plus the near-to-next roll, per symbol-day.

    ``futures`` needs ``SYMBOL, DATE, EXPIRY_DT, SETTLE_PR, CLOSE, OPEN_INT,
    CHG_IN_OI, CONTRACTS, spot, interest_rate`` — one row per futures contract.
    Pure — no I/O.

    The mark is ``SETTLE_PR`` with a ``CLOSE`` fallback. Settlement is the
    official daily mark and is defined even on a day the contract did not
    trade, where ``CLOSE`` is a stale last-traded price that would show a
    fictitious basis.

    ``spot`` must be the **raw, unadjusted** underlying
    (``option_metrics.underlying_price``). Bhavcopy futures prices are
    unadjusted, so pairing them with an adjusted close would report a basis
    equal to the corporate-action factor — the defect recorded in
    ``docs/data/iv-integrity-audit.md``, in a new place.

    ``fut_basis_excess`` is the annualised basis minus the carry rate, both in
    percent; it is the column that actually carries a signal, since the raw
    basis is mostly just interest. Positive excess is leveraged long demand
    paying up to be long; negative is crowded shorts or borrow scarcity.
    """
    required = {
        "SYMBOL", "DATE", "EXPIRY_DT", "SETTLE_PR", "CLOSE", "OPEN_INT",
        "CHG_IN_OI", "CONTRACTS", "spot", "interest_rate",
    }
    missing = required - set(futures.columns)
    if missing:
        raise ValueError(f"futures_basis missing columns: {sorted(missing)}")

    work = futures.loc[futures["spot"].notna() & (futures["spot"] > 0)].copy()
    work["EXPIRY_DT"] = _parse_expiry(work["EXPIRY_DT"])
    work["DATE"] = pd.to_datetime(work["DATE"], errors="coerce")
    work = work.loc[work["EXPIRY_DT"].notna() & work["DATE"].notna()]
    # An already-expired contract lingering in a file is not the near month.
    work = work.loc[work["EXPIRY_DT"] >= work["DATE"]]
    work["mark"] = work["SETTLE_PR"].where(
        work["SETTLE_PR"].notna() & (work["SETTLE_PR"] > 0), work["CLOSE"]
    )
    work = work.loc[work["mark"].notna() & (work["mark"] > 0)]
    if work.empty:
        return pd.DataFrame(columns=["symbol", "date", *_FUTURES_COLUMNS])

    work = work.sort_values(["SYMBOL", "DATE", "EXPIRY_DT"], kind="stable")
    work["_rank"] = work.groupby(["SYMBOL", "DATE"]).cumcount()

    near = work.loc[work["_rank"] == 0].copy()
    nxt = work.loc[work["_rank"] == 1, ["SYMBOL", "DATE", "mark", "OPEN_INT"]].rename(
        columns={"mark": "_next_mark", "OPEN_INT": "_next_oi"}
    )
    out = near.merge(nxt, on=["SYMBOL", "DATE"], how="left")

    dte = (out["EXPIRY_DT"] - out["DATE"]).dt.days.astype("float64")
    out["fut_dte"] = dte
    out["fut_basis"] = out["mark"] - out["spot"]
    out["fut_basis_pct"] = out["mark"] / out["spot"] - 1.0

    annual = out["fut_basis_pct"] * (_DAYS_PER_YEAR / dte.where(dte > 0)) * 100.0
    out["fut_basis_annual"] = annual.where(dte >= ANNUALISE_MIN_DTE)
    out["fut_basis_excess"] = out["fut_basis_annual"] - out["interest_rate"]

    out["fut_oi"] = out["OPEN_INT"]
    out["fut_oi_chg"] = out["CHG_IN_OI"]
    out["fut_volume"] = out["CONTRACTS"]

    both_oi = out["fut_oi"].astype("float64") + out["_next_oi"].astype("float64")
    out["fut_roll_ratio"] = np.where(
        both_oi > 0, out["_next_oi"].astype("float64") / both_oi, np.nan
    )
    out["fut_roll_spread"] = (out["_next_mark"] - out["mark"]) / out["mark"]

    out = out.rename(columns={"SYMBOL": "symbol", "DATE": "date"})
    keep = ["symbol", "date", "spot", *[c for c in _FUTURES_COLUMNS if c in out.columns]]
    return out[keep]


# ═════════════════════════ buildup quadrants ═══════════════════════════════


def _mark(frame: pd.DataFrame) -> pd.Series:
    """``SETTLE_PR`` with a ``CLOSE`` fallback — the same rule as :func:`futures_basis`.

    Settlement is the official daily mark and is defined even on a day the
    contract did not trade; ``CLOSE`` is then a stale last-traded price. Both
    quadrant families use this so the two stay comparable.
    """
    settle = frame["SETTLE_PR"]
    return settle.where(settle.notna() & (settle > 0), frame["CLOSE"])


def _empty_keyed_frame(value_columns: list[str]) -> pd.DataFrame:
    """A no-row frame whose merge keys already carry the right dtypes.

    ``pd.DataFrame(columns=[...])`` types every column ``object``. Under pandas
    3.0 an empty frame's dtypes participate in ``concat`` dtype resolution, so
    a single quiet day would turn a whole concatenated ``date`` column into
    ``object`` and the downstream merge would then fail with "trying to merge
    on datetime64[us] and object columns" — 3.5 minutes into the build.
    """
    frame = pd.DataFrame(
        {
            "symbol": pd.Series(dtype="object"),
            "date": pd.Series(dtype="datetime64[us]"),
            **{c: pd.Series(dtype="float64") for c in value_columns},
        }
    )
    return frame


def _align_keys(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce ``(symbol, date)`` to the dtypes every merge in this module assumes.

    Cheap insurance at each join rather than a rule every producer has to
    remember: the frames being merged arrive from a parquet reader, a psycopg2
    cursor and several groupby paths, and they do not all agree on datetime
    resolution.
    """
    if frame.empty or "date" not in frame.columns:
        return frame
    out = frame.copy()
    out["symbol"] = out["symbol"].astype(str)
    out["date"] = pd.to_datetime(out["date"]).astype("datetime64[us]")
    return out


def _direction_score(bull: pd.Series, bear: pd.Series) -> pd.Series:
    """``(bull - bear) / (bull + bear)`` in [-1, 1], NaN when nothing was traded.

    NaN rather than 0.0 on a zero denominator: a symbol-day with no traded
    open-interest change is *unmeasured*, not *neutral*. The two average to
    opposite conclusions, which is exactly the sentinel defect recorded in
    docs/data/iv-integrity-audit.md.
    """
    total = bull + bear
    return ((bull - bear) / total.where(total > 0)).astype("float64")


def _premium_residual(frame: pd.DataFrame) -> pd.Series:
    """Premium change with the underlying's own move removed.

    ``(mark - mark_prev) - delta_prev * (spot - spot_prev)``. Returns NaN where
    the delta or the previous spot is unavailable, so the caller drops those
    contracts instead of silently classifying them on the raw change — which
    is wrong about a third of the time (see :func:`option_quadrants`).
    """
    raw = frame["mark"].astype("float64") - frame["mark_prev"].astype("float64")
    if "delta_prev" not in frame.columns or "spot_prev" not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype="float64")
    explained = frame["delta_prev"].astype("float64") * (
        frame["spot"].astype("float64") - frame["spot_prev"].astype("float64")
    )
    return raw - explained


def option_quadrants(options: pd.DataFrame) -> pd.DataFrame:
    """Writer-vs-buyer split of newly opened option interest, per symbol-day.

    ``options`` needs ``SYMBOL, DATE, OPTION_TYP, CHG_IN_OI, CONTRACTS, spot``
    and ``mark`` / ``mark_prev`` (previous *trading* day's mark for the same
    contract). ``delta_prev`` / ``spot_prev`` are optional but strongly
    recommended — see the delta confound below. Pure — no I/O.

    Only contracts with ``CHG_IN_OI > 0`` count: the question is who opened
    the new interest, and a close-out tells us nothing about a fresh view.
    Only contracts with ``CONTRACTS > 0`` count either — on an untraded strike
    ``SETTLE_PR`` is a theoretical value, so a "premium change" between two
    consecutive theoretical settles is model noise, and giving it a quadrant
    would manufacture signal out of arithmetic. The same traded-only rule is
    what :func:`_robust_scale` already applies to the z-score.

    **The delta confound.** A call premium rises when the underlying rises,
    with or without an aggressive buyer, so the raw premium change is not a
    clean read on buyer-versus-writer pressure. When ``delta_prev`` and
    ``spot_prev`` are supplied the classification runs on the residual

        ``(mark - mark_prev) - delta_prev * (spot - spot_prev)``

    which strips the move the underlying alone explains. This is not a
    refinement: measured over 163,383 traded contracts on 47 sampled days, the
    raw and adjusted readings disagree on **35.8%** of contracts (36.9%
    notional-weighted), and the median |delta term| (3.12) is comparable to the
    median |raw move| (4.20). Classifying on the raw change gets the direction
    wrong roughly a third of the time. See
    ``scripts/measure_delta_adjustment.py`` and
    ``docs/implementation/model-02-buildup-quadrants.md``.

    Contracts with no ``delta_prev`` are dropped rather than falling back to
    the raw change — a third-wrong reading is not a usable fallback.

    Call buying and put writing are bullish; put buying and call writing are
    bearish. A sign transposition here is invisible in aggregate — every
    downstream number stays plausible while meaning the reverse — so it is
    pinned by ``test_call_writing_and_put_writing_have_opposite_signs``.
    """
    required = {
        "SYMBOL", "DATE", "OPTION_TYP", "CHG_IN_OI", "CONTRACTS", "spot",
        "mark", "mark_prev",
    }
    missing = required - set(options.columns)
    if missing:
        raise ValueError(f"option_quadrants missing columns: {sorted(missing)}")

    work = options.loc[
        (options["CONTRACTS"].fillna(0) > 0)
        & (options["CHG_IN_OI"].fillna(0) > 0)
        & options["mark_prev"].notna()
        & options["mark"].notna()
        & options["spot"].notna()
        & (options["spot"] > 0)
    ].copy()
    if work.empty:
        return _empty_keyed_frame(
            ["ce_writer_frac", "pe_writer_frac", "opt_direction"]
        )

    d_prem = _premium_residual(work)
    work = work.loc[d_prem.notna()]
    d_prem = d_prem.loc[work.index]
    if work.empty:
        return _empty_keyed_frame(
            ["ce_writer_frac", "pe_writer_frac", "opt_direction"]
        )
    notional = work["CHG_IN_OI"].astype("float64") * work["spot"].astype("float64")
    is_call = work["OPTION_TYP"].astype(str) == "CE"
    # A flat premium is neither side pressing; it contributes to no bucket.
    writer = d_prem < 0
    buyer = d_prem > 0

    work["_ce_writer"] = notional.where(is_call & writer, 0.0)
    work["_ce_buyer"] = notional.where(is_call & buyer, 0.0)
    work["_pe_writer"] = notional.where(~is_call & writer, 0.0)
    work["_pe_buyer"] = notional.where(~is_call & buyer, 0.0)

    agg = work.groupby(["SYMBOL", "DATE"], as_index=False)[
        ["_ce_writer", "_ce_buyer", "_pe_writer", "_pe_buyer"]
    ].sum().rename(columns={"SYMBOL": "symbol", "DATE": "date"})

    ce_total = agg["_ce_writer"] + agg["_ce_buyer"]
    pe_total = agg["_pe_writer"] + agg["_pe_buyer"]
    agg["ce_writer_frac"] = agg["_ce_writer"] / ce_total.where(ce_total > 0)
    agg["pe_writer_frac"] = agg["_pe_writer"] / pe_total.where(pe_total > 0)
    agg["opt_direction"] = _direction_score(
        agg["_ce_buyer"] + agg["_pe_writer"],
        agg["_pe_buyer"] + agg["_ce_writer"],
    )
    return agg[["symbol", "date", "ce_writer_frac", "pe_writer_frac", "opt_direction"]]


def futures_quadrants(futures: pd.DataFrame) -> pd.DataFrame:
    """The four classic OI-vs-price quadrants over all futures contracts, per symbol-day.

    ``futures`` needs ``SYMBOL, DATE, EXPIRY_DT, SETTLE_PR, CLOSE, CHG_IN_OI,
    CONTRACTS, spot`` — one row per futures contract, any number of dates.
    Pure — no I/O.

    Unlike :func:`futures_basis` this spans *every* expiry, not just the near
    month: a roll shows up as an unwind in the near contract and a buildup in
    the next, and netting them at the symbol level is the point.

    The previous mark comes from a ``groupby(SYMBOL, EXPIRY_DT).shift(1)``,
    which is the previous *trading* day for that contract — several calendar
    days back across a weekend, which is the right comparison. Row order is
    load-bearing: the sort before the shift is not cosmetic.
    """
    required = {
        "SYMBOL", "DATE", "EXPIRY_DT", "SETTLE_PR", "CLOSE", "CHG_IN_OI",
        "CONTRACTS", "spot",
    }
    missing = required - set(futures.columns)
    if missing:
        raise ValueError(f"futures_quadrants missing columns: {sorted(missing)}")

    work = futures.copy()
    work["DATE"] = pd.to_datetime(work["DATE"], errors="coerce")
    work["mark"] = _mark(work)
    work = work.loc[work["DATE"].notna() & work["mark"].notna() & (work["mark"] > 0)]
    if work.empty:
        return _empty_keyed_frame(list(_FUT_QUADRANT_COLUMNS))

    work = work.sort_values(["SYMBOL", "EXPIRY_DT", "DATE"], kind="stable")
    grouped = work.groupby(["SYMBOL", "EXPIRY_DT"], observed=True, sort=False)
    work["mark_prev"] = grouped["mark"].shift(1)
    # Same grouping, so spot_prev lines up with mark_prev row for row.
    work["spot_prev"] = grouped["spot"].shift(1)

    work = work.loc[
        (work["CONTRACTS"].fillna(0) > 0)
        & work["mark_prev"].notna()
        & work["spot"].notna()
        & (work["spot"] > 0)
        & work["CHG_IN_OI"].notna()
    ]
    if work.empty:
        return _empty_keyed_frame(list(_FUT_QUADRANT_COLUMNS))

    d_price = work["mark"].astype("float64") - work["mark_prev"].astype("float64")
    chg_oi = work["CHG_IN_OI"].astype("float64")
    notional = chg_oi.abs() * work["spot"].astype("float64")
    up, down = d_price > 0, d_price < 0
    opened, closed = chg_oi > 0, chg_oi < 0

    work["q_long_buildup_notional"] = notional.where(up & opened, 0.0)
    work["q_short_buildup_notional"] = notional.where(down & opened, 0.0)
    work["q_short_cover_notional"] = notional.where(up & closed, 0.0)
    work["q_long_unwind_notional"] = notional.where(down & closed, 0.0)

    # Basis-change variant. A future's delta to its underlying is ~1, so the
    # delta-adjusted residual that `option_quadrants` applies reduces here to
    #     (F - F_prev) - (S - S_prev) = the change in basis.
    # That reclassifies the quadrant from "price rose while OI rose" — which is
    # a momentum statement, and is why the raw q_fut_direction turned out to be
    # a momentum proxy in Stage 5 — to "futures richened against spot while OI
    # rose", which is a positioning statement. Kept alongside the raw version
    # rather than replacing it, so the published results stay reproducible and
    # the two can be compared directly.
    d_basis = d_price - (
        work["spot"].astype("float64") - work["spot_prev"].astype("float64")
    )
    r_up, r_down = d_basis > 0, d_basis < 0
    # Richening on new interest, or cheapening on an unwind, both read as
    # demand for the long side; the mirror of each reads as supply.
    work["_r_bull"] = notional.where((r_up & opened) | (r_down & closed), 0.0)
    work["_r_bear"] = notional.where((r_down & opened) | (r_up & closed), 0.0)

    agg = work.groupby(["SYMBOL", "DATE"], as_index=False)[
        [*_FUT_QUADRANT_COLUMNS[:4], "_r_bull", "_r_bear"]
    ].sum().rename(columns={"SYMBOL": "symbol", "DATE": "date"})

    agg["q_fut_direction"] = _direction_score(
        agg["q_long_buildup_notional"] + agg["q_short_cover_notional"],
        agg["q_short_buildup_notional"] + agg["q_long_unwind_notional"],
    )
    agg["q_fut_direction_resid"] = _direction_score(agg["_r_bull"], agg["_r_bear"])
    return agg[
        ["symbol", "date", *_FUT_QUADRANT_COLUMNS, "q_fut_direction_resid"]
    ]


# ═════════════════════════ cache streaming + orchestration ═════════════════

_OPTION_COLUMNS = [
    "DATE", "SYMBOL", "INSTRUMENT", "EXPIRY_DT", "STRIKE_PR",
    "OPTION_TYP", "OPEN_INT", "CHG_IN_OI", "CONTRACTS",
    "SETTLE_PR", "CLOSE",
]

# Contract identity for the day-over-day price join. EXPIRY_DT stays a
# dictionary-encoded string: joining on it as text is cheaper than parsing
# 60.6M values, and it sidesteps the %b/%B ambiguity that _parse_expiry exists
# to handle.
_CONTRACT_KEY = ["SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP"]
_FUTURES_CACHE_COLUMNS = [
    "DATE", "SYMBOL", "INSTRUMENT", "EXPIRY_DT", "SETTLE_PR",
    "CLOSE", "OPEN_INT", "CHG_IN_OI", "CONTRACTS",
]
_OPTION_INSTRUMENTS = ("OPTSTK", "OPTIDX")
_FUTURES_INSTRUMENTS = ("FUTSTK", "FUTIDX")


def load_spot_frame(conn: Any, limit_dates: int | None = None) -> pd.DataFrame:
    """``(symbol, date, spot, interest_rate)`` from ``option_metrics``.

    ``spot`` is ``underlying_price`` — the **raw, unadjusted** close. This is
    the same basis the ETL used to pick strikes and solve IV, so moneyness and
    basis computed here line up with the rest of the pipeline;
    ``underlying_price_adj`` would be wrong for both (CLAUDE.md, "Two price
    series, two purposes").

    Using ``option_metrics`` rather than the cache's own ``UNDERLYING_PRICE``
    column also pins this table's (symbol, date) universe to the pipeline's,
    so ``flow_features`` joins ``ml_dataset`` one-to-one.
    """
    sql = (
        "SELECT symbol, date, underlying_price AS spot, interest_rate "
        "FROM option_metrics WHERE underlying_price IS NOT NULL"
    )
    if limit_dates:
        sql += (
            " AND date IN (SELECT DISTINCT date FROM option_metrics "
            f"ORDER BY date DESC LIMIT {int(limit_dates)})"
        )
    frame = pd.read_sql(sql, conn)
    frame["date"] = pd.to_datetime(frame["date"])
    frame["symbol"] = frame["symbol"].astype(str)
    return frame


def _merge_spot(frame: pd.DataFrame, spot: pd.DataFrame) -> pd.DataFrame:
    """Attach ``spot``/``interest_rate`` to cache rows keyed by SYMBOL/DATE."""
    frame = frame.copy()
    frame["SYMBOL"] = frame["SYMBOL"].astype(str)
    frame["DATE"] = pd.to_datetime(frame["DATE"], errors="coerce")
    return frame.merge(
        spot.rename(columns={"symbol": "SYMBOL", "date": "DATE"}),
        on=["SYMBOL", "DATE"],
        how="inner",
    )


def _row_group_date(reader: pq.ParquetFile, rg: int) -> pd.Timestamp | None:
    """The single trade date a row group holds, from its DATE column statistics.

    ``build_bhavcopy_cache`` writes one row group per trade date, so min == max
    and the whole group can be skipped without decoding it — which is what
    makes ``--limit-dates`` a genuinely fast smoke run instead of a full
    60.6M-row scan that throws almost everything away. Returns ``None`` when
    the statistics are absent or the group is not single-dated, and the caller
    then reads it rather than guessing.
    """
    try:
        col = reader.schema_arrow.names.index("DATE")
        stats = reader.metadata.row_group(rg).column(col).statistics
    except (ValueError, AttributeError, IndexError):
        return None
    if stats is None or not stats.has_min_max or stats.min != stats.max:
        return None
    return pd.Timestamp(stats.min)


_DAY_DELTA_SQL = """
SELECT om.symbol                            AS "SYMBOL",
        e ->> 'expiry'                      AS "EXPIRY_DT",
       (e ->> 'strike')::double precision   AS "STRIKE_PR",
        e ->> 'type'                        AS "OPTION_TYP",
       (e ->> 'delta')::double precision    AS delta
FROM option_metrics om
CROSS JOIN LATERAL jsonb_array_elements(om.option_chain) AS e
WHERE om.date = %s AND om.option_chain IS NOT NULL
"""


def _load_day_delta(conn: Any, day: pd.Timestamp) -> pd.DataFrame:
    """One day's per-strike delta from ``option_metrics.option_chain``.

    Delta is not in the bhavcopy parquet cache — it is computed by the ETL and
    persisted per chain element. The quadrants need it to strip the underlying's
    own move out of the premium change, so this is fetched one day at a time
    alongside the row-group stream rather than as a single pass over all 49.5M
    chain elements. Measured at ~0.15 s per date, so ~3 minutes over the full
    history.

    The chain's ``expiry`` strings use the same ``%d-%b-%Y`` shape as the
    cache's ``EXPIRY_DT``, so the join key needs no parsing on either side.
    """
    frame = pd.read_sql(_DAY_DELTA_SQL, conn, params=(day.date(),))
    if frame.empty:
        return frame
    frame["SYMBOL"] = frame["SYMBOL"].astype(str)
    frame["EXPIRY_DT"] = frame["EXPIRY_DT"].astype(str)
    frame["OPTION_TYP"] = frame["OPTION_TYP"].astype(str)
    return frame


def _ordered_row_groups(
    reader: pq.ParquetFile, wanted: set[Any]
) -> tuple[list[int], set[int]]:
    """Row groups to stream, **in trade-date order**, plus the subset to emit.

    The cache's row groups are one per trade date but they are *not* stored in
    chronological order — the frame is sorted by symbol, so the groups come out
    in whatever order the dates fell, with 293 backward steps over the five
    years. Streaming them in file order and calling the last one "yesterday"
    silently joins each day against an arbitrary other day. Nothing complains:
    the same contract exists on both days, so the merge succeeds and returns a
    price change spanning months.

    The outlier detector never noticed because it is purely within-day. The
    buildup quadrants are the first thing here that needs an actual yesterday.

    Returns ``(ordered_groups, emit)`` where ``ordered_groups`` also carries one
    seed group ahead of the window — so a ``--limit-dates`` run's first day
    still has a previous mark — and ``emit`` is the subset whose results count.
    """
    dated: list[tuple[pd.Timestamp, int]] = []
    for rg in range(reader.metadata.num_row_groups):
        day = _row_group_date(reader, rg)
        if day is None:
            # No usable statistics: pay for a DATE-only read rather than guess
            # an order. Does not happen on the shipped cache.
            col = reader.read_row_group(rg, columns=["DATE"]).to_pandas()["DATE"]
            if col.empty:
                continue
            day = pd.Timestamp(col.min())
        dated.append((day, rg))
    dated.sort(key=lambda pair: pair[0])

    emit = {rg for day, rg in dated if day in wanted}
    if not emit:
        return [], emit
    first = next(i for i, (_, rg) in enumerate(dated) if rg in emit)
    seed = [dated[first - 1][1]] if first > 0 else []
    return [*seed, *[rg for _, rg in dated[first:] if rg in emit]], emit


def _outlier_pass(
    conn: Any,
    cache_path: Path,
    spot: pd.DataFrame,
    emit_dates: set[Any],
    z_threshold: float,
    min_share: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stream the cache one row group (= one trade date) at a time.

    Outlier detection never looks outside a single (symbol, date, expiry), so
    a whole-frame load would buy nothing and cost ~9 GB. Each row group holds
    one trade date (~48k rows).

    The buildup quadrants do need one day of history, so exactly **one**
    previous frame is retained — the contract key plus its mark, nothing else.
    Retaining more (a dict of per-symbol frames) is what OOM-killed the ETL at
    ~12.5 GB once already, see ``storage.etl._load_cache``.

    Returns ``(outliers, option_quadrants)``, both one row per symbol-day.
    """
    reader = pq.ParquetFile(cache_path)
    wanted = set(spot["DATE" if "DATE" in spot.columns else "date"])
    groups, _ = _ordered_row_groups(reader, wanted)

    parts: list[pd.DataFrame] = []
    quads: list[pd.DataFrame] = []
    prev: pd.DataFrame | None = None
    with progress_stage(
        "Scanning chain for outlier strikes", total=len(groups), unit="days",
    ) as bar:
        for rg in groups:
            day = reader.read_row_group(rg, columns=_OPTION_COLUMNS).to_pandas()
            day = day.loc[day["INSTRUMENT"].astype(str).isin(_OPTION_INSTRUMENTS)]
            label = None
            if not day.empty:
                day = day.copy()
                day["mark"] = _mark(day)
                stamp = pd.Timestamp(day["DATE"].iloc[0])
                label = str(stamp.date())
                # Spot and delta are attached before the emit check: this frame
                # becomes the next iteration's `prev`, and the residual needs
                # both from the *previous* day.
                day = _merge_spot(day, spot)
                if not day.empty:
                    delta = _load_day_delta(conn, stamp)
                    day = (
                        day.merge(delta, on=_CONTRACT_KEY, how="left")
                        if not delta.empty
                        else day.assign(delta=np.nan)
                    )
                    if stamp in emit_dates:
                        merged = _attach_prev_mark(day, prev)
                        parts.append(
                            detect_outlier_strikes(merged, z_threshold, min_share)
                        )
                        quads.append(option_quadrants(merged))
                    prev = day[[*_CONTRACT_KEY, "mark", "delta", "spot"]]
            bar.advance(label=label)

    parts = [f for f in parts if not f.empty]
    quads = [f for f in quads if not f.empty]
    outliers = (
        pd.concat(parts, ignore_index=True) if parts else _empty_outlier_frame()
    )
    quadrants = (
        pd.concat(quads, ignore_index=True)
        if quads
        else _empty_keyed_frame(["ce_writer_frac", "pe_writer_frac", "opt_direction"])
    )
    return outliers, quadrants


def _attach_prev_mark(day: pd.DataFrame, prev: pd.DataFrame | None) -> pd.DataFrame:
    """Left-join the previous trading day's mark onto today's contracts.

    A left join is deliberate: a contract listed today has no previous row and
    must end up with ``NaN``, not a zero price change. Contracts that expired
    simply do not appear in ``day`` and drop out on their own.
    """
    if prev is None or prev.empty:
        out = day.copy()
        for col in ("mark_prev", "delta_prev", "spot_prev"):
            out[col] = np.nan
        return out
    return day.merge(
        prev.rename(
            columns={"mark": "mark_prev", "delta": "delta_prev", "spot": "spot_prev"}
        ),
        on=_CONTRACT_KEY,
        how="left",
    )


def _futures_pass(
    cache_path: Path, spot: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read only the futures rows (~752k of 60.6M) via a predicate push-down.

    Returns ``(basis, quadrants)``. The basis is near-month only; the quadrants
    span every expiry, because a roll is an unwind in the near contract and a
    buildup in the next and netting them at the symbol level is the point.
    """
    frame = pd.read_parquet(
        cache_path,
        columns=_FUTURES_CACHE_COLUMNS,
        filters=[("INSTRUMENT", "in", list(_FUTURES_INSTRUMENTS))],
    )
    if frame.empty:
        return (
            pd.DataFrame(columns=["symbol", "date", "spot", *_FUTURES_COLUMNS]),
            _empty_keyed_frame([*_FUT_QUADRANT_COLUMNS, "q_fut_direction_resid"]),
        )
    merged = _merge_spot(frame, spot)
    return futures_basis(merged), futures_quadrants(merged)


def _attach_notionals(frame: pd.DataFrame) -> pd.DataFrame:
    """Turn open-interest counts into rupees.

    Bhavcopy's ``OPEN_INT``/``CHG_IN_OI`` are in **shares, not contracts**, so
    the notional is ``oi * spot`` with no lot-size factor. This is worth
    stating because the obvious reading is the wrong one, and multiplying by
    the lot inflates every notional by 2-3 orders of magnitude (it put
    RELIANCE's futures book at Rs 8.5 lakh crore instead of Rs 17,102 crore).

    Measured two ways, since the project's own reference file asserted the
    opposite. ``OPEN_INT`` divides exactly by the lot size on 98% of 2026 rows
    (the residue is early years where lot_sizes.csv rows are ``source=derived``
    and approximate); and summing ``OPEN_INT / lot`` across all FUTSTK symbols
    reproduces ``participant_oi``'s TOTAL stock-futures contract count to
    within 0.02%, where the un-divided sum is off by a factor of ~2,158.
    """
    out = frame.copy()
    exposure = out["spot"].astype("float64")
    out["oi_outlier_build_notional"] = out["_build_contracts"] * exposure
    out["oi_outlier_unwind_notional"] = out["_unwind_contracts"] * exposure
    out["fut_oi_notional"] = out["fut_oi"].astype("float64") * exposure
    out["fut_oi_chg_notional"] = out["fut_oi_chg"].astype("float64") * exposure
    return out.drop(columns=["_build_contracts", "_unwind_contracts"])


def build_flow_features(
    conn: Any,
    cache_path: Path | None = None,
    limit_dates: int | None = None,
    z_threshold: float = OUTLIER_Z,
    min_share: float = OUTLIER_MIN_SHARE,
) -> pd.DataFrame:
    """Build the whole ``flow_features`` matrix from the cache + ``option_metrics``.

    The two families are computed independently and joined outer on
    (symbol, date): a symbol can have a chain but no futures contract on a
    given day, and the reverse happens around listing and delisting. An inner
    join would silently drop those days rather than record what is known.

    Returns a frame with exactly :data:`FLOW_COLUMNS`, sorted by
    ``(symbol, date)``.
    """
    cache_path = cache_path or settings.processed_data_dir / "bhavcopy_cache.parquet"
    if not cache_path.exists():
        raise FileNotFoundError(
            f"bhavcopy cache not found at {cache_path} — run `mktmtm preparse-bhavcopy`"
        )
    spot = load_spot_frame(conn, limit_dates)
    if spot.empty:
        raise ValueError("option_metrics has no rows with an underlying_price")

    # The quadrants need the day *before* the window as a price/delta seed, so
    # the streaming pass gets one extra date of spot while the emitted set stays
    # exactly the window. Without this a --limit-dates run's first day would
    # silently lose its direction columns.
    emit_dates = set(spot["date"])
    spot_seeded = (
        load_spot_frame(conn, limit_dates + 1) if limit_dates else spot
    )
    outliers, opt_quads = _outlier_pass(
        conn, cache_path, spot_seeded, emit_dates, z_threshold, min_share
    )
    futures, fut_quads = _futures_pass(cache_path, spot)

    if outliers.empty and futures.empty:
        return pd.DataFrame(columns=list(FLOW_COLUMNS))

    # `spot` rides along on the futures side; the outlier side needs it too for
    # notional, so pull it back from option_metrics rather than from whichever
    # side happened to survive the join.
    merged = _align_keys(outliers).merge(
        _align_keys(futures.drop(columns=["spot"])),
        on=["symbol", "date"],
        how="outer",
    )
    # Both quadrant families are left joins onto the union above: a symbol-day
    # with no traded open-interest change gets NULL, never a neutral 0.
    for extra in (opt_quads, fut_quads):
        if not extra.empty:
            merged = merged.merge(
                _align_keys(extra), on=["symbol", "date"], how="left"
            )
    merged = merged.merge(
        _align_keys(spot[["symbol", "date", "spot"]]),
        on=["symbol", "date"],
        how="left",
    )
    merged = _attach_notionals(merged)

    for col in FLOW_COLUMNS:
        if col not in merged.columns:
            merged[col] = np.nan
    out = merged[list(FLOW_COLUMNS)].sort_values(["symbol", "date"], kind="stable")
    return out.reset_index(drop=True)


# ═════════════════════════ persistence ═════════════════════════════════════


def _clean(value: Any, as_int: bool) -> Any:
    """Adapt one cell for psycopg2: NaN/NaT -> None, numpy scalars -> Python.

    psycopg2 cannot adapt ``numpy.int64``/``numpy.float64``, and pandas hands
    exactly those out of ``itertuples`` — the defect that broke the first
    ``participant_oi`` write (tests/unit/test_participant_oi.py).
    """
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    if isinstance(value, pd.Timestamp | np.datetime64):
        return pd.Timestamp(value).date()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if as_int:
        return int(value)
    if isinstance(value, np.generic):
        return value.item()
    return value


def write_flow_features(conn: Any, frame: pd.DataFrame) -> int:
    """UPSERT ``frame`` into ``flow_features`` on (symbol, date). Returns rows written."""
    if frame.empty:
        return 0
    cols = list(FLOW_COLUMNS)
    rows = [
        tuple(
            _clean(rec[i], cols[i] in _INT_COLUMNS)
            for i in range(len(cols))
        )
        for rec in frame[cols].itertuples(index=False, name=None)
    ]
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c not in ("symbol", "date")
    )
    sql = (
        f"INSERT INTO flow_features ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (symbol, date) DO UPDATE SET {updates}"
    )
    written = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), _PAGE_SIZE):
            page = rows[i : i + _PAGE_SIZE]
            execute_values(cur, sql, page, page_size=len(page))
            written += len(page)
    conn.commit()
    return written


def export_flow_parquet(frame: pd.DataFrame, path: Path | None = None) -> Path:
    """Write the frame beside ml_dataset.parquet, in ``FLOW_COLUMNS`` order.

    Exported at build time from the same frame that is written to PostgreSQL,
    so the two cannot drift the way ``ml_dataset.parquet`` did when
    ``gex-features`` wrote only to the database.
    """
    path = path or settings.processed_data_dir / "flow_features.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame[list(FLOW_COLUMNS)].to_parquet(path, index=False)
    return path


def run_flow_features(
    conn: Any,
    cache_path: Path | None = None,
    limit_dates: int | None = None,
    z_threshold: float = OUTLIER_Z,
    min_share: float = OUTLIER_MIN_SHARE,
    parquet: bool = True,
) -> dict[str, Any]:
    """Build, persist and (optionally) export the flow features. CLI entry point."""
    frame = build_flow_features(
        conn, cache_path=cache_path, limit_dates=limit_dates,
        z_threshold=z_threshold, min_share=min_share,
    )
    written = write_flow_features(conn, frame)
    out_path = export_flow_parquet(frame) if parquet else None
    flagged = frame["oi_outlier_count"].fillna(0)
    return {
        "rows": len(frame),
        "symbols": int(frame["symbol"].nunique()) if not frame.empty else 0,
        "dates": int(frame["date"].nunique()) if not frame.empty else 0,
        "rows_written": written,
        "symbol_days_with_outlier": int((flagged > 0).sum()),
        "outliers_flagged": int(flagged.sum()),
        "with_futures_basis": int(frame["fut_basis"].notna().sum()),
        "parquet": str(out_path) if out_path else None,
    }
