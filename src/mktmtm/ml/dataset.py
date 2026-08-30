"""ML dataset builder: ``option_metrics`` -> ``ml_dataset`` (features + targets).

Reads the enriched fact table, derives the supervised targets (next-day
return / direction, per-symbol forward shift) plus the feature set described
in ``docs/models/data-understanding-report.md``, and writes the result to the
``ml_dataset`` table (PostgreSQL) with an optional parquet export.

Idempotent: ``write_dataset`` upserts on ``(symbol, date)``, so re-running
after fresh ETL data lands fills in targets for rows that were previously the
most recent day of their symbol.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg2  # no stubs (see pyproject mypy overrides)
from psycopg2.extras import execute_values

from mktmtm.storage.db import build_dsn

logger = logging.getLogger(__name__)

__all__ = [
    "DATASET_COLUMNS",
    "build_dataset",
    "compute_chain_aggregates",
    "export_parquet",
    "screen_corporate_actions",
    "validate_no_lookahead",
    "write_dataset",
]

# ───────────────────────────── column contract ─────────────────────────────
# Single source of truth for both the DDL (init_ml_dataset.sql) and the
# builder. Order matters: it is the INSERT column order.

IDENTITY_COLUMNS = ["symbol", "date"]

LEVEL_COLUMNS = [
    "underlying_price",
    "interest_rate",
    "rv_yz",
]

CE_PE_COLUMNS = [
    "ce_iv_30", "pe_iv_30", "ce_iv_60", "pe_iv_60", "ce_iv_90", "pe_iv_90",
    "ce_ivp", "pe_ivp", "ce_ivr", "pe_ivr",
    "ce_delta", "pe_delta", "ce_gamma", "pe_gamma", "ce_theta", "pe_theta",
    "ce_vega", "pe_vega", "ce_rho", "pe_rho",
    "ce_close", "pe_close", "ce_volume", "pe_volume", "ce_oi", "pe_oi",
]

TIER2_COLUMNS = [
    "ret_1d", "ret_5d", "ret_10d", "ret_20d",
    "iv_rv_spread", "term_slope", "put_call_iv_spread",
    "days_to_expiry_30d", "days_to_earnings",
]

TIER3_COLUMNS = [
    "atm_straddle", "skew_25d", "put_call_vol_ratio", "put_call_oi_ratio",
    "otm_put_iv_premium", "chain_volume", "chain_oi", "chain_oi_chg",
    "chain_breadth",
]

TARGET_COLUMNS = ["next_day_return", "next_day_direction"]

DATASET_COLUMNS = (
    IDENTITY_COLUMNS + LEVEL_COLUMNS + CE_PE_COLUMNS + TIER2_COLUMNS
    + TIER3_COLUMNS + TARGET_COLUMNS
)

_GREEK_KEYS = ("delta", "gamma", "theta", "vega", "rho")

# A single-day underlying move larger than this is treated as a corporate action
# (split / bonus / demerger), not a return. NSE has no daily price band on F&O
# underlyings, but a genuine >20% single-day move in a large-cap F&O name is rare
# enough that screening is worth more than the handful of real moves it discards.
_MAX_ABS_DAILY_RETURN = 0.20

_MOMENTUM_PERIODS = (1, 5, 10, 20)

# Chain aggregates are computed in SQL (jsonb is C-speed there; a python pass
# over ~320M chain elements would take minutes). Each lateral join re-parses
# the array, which is acceptable for a one-shot build.
_CHAIN_AGG_SQL = """
SELECT
    m.symbol,
    m.date,
    agg.chain_pe_vol,
    agg.chain_ce_vol,
    agg.chain_pe_oi,
    agg.chain_ce_oi,
    agg.chain_volume,
    agg.chain_oi,
    agg.chain_oi_chg,
    agg.chain_breadth,
    straddle.straddle        AS atm_straddle,
    skew.skew_25d,
    otm.otm_put_iv
FROM option_metrics m
LEFT JOIN LATERAL (
    SELECT
        COALESCE(SUM(CASE WHEN e->>'type' = 'PE' THEN (e->>'volume')::bigint ELSE 0 END), 0) AS chain_pe_vol,
        COALESCE(SUM(CASE WHEN e->>'type' = 'CE' THEN (e->>'volume')::bigint ELSE 0 END), 0) AS chain_ce_vol,
        COALESCE(SUM(CASE WHEN e->>'type' = 'PE' THEN COALESCE((e->>'oi')::bigint, 0) ELSE 0 END), 0) AS chain_pe_oi,
        COALESCE(SUM(CASE WHEN e->>'type' = 'CE' THEN COALESCE((e->>'oi')::bigint, 0) ELSE 0 END), 0) AS chain_ce_oi,
        COALESCE(SUM(COALESCE((e->>'volume')::bigint, 0)), 0) AS chain_volume,
        COALESCE(SUM(COALESCE((e->>'oi')::bigint, 0)), 0) AS chain_oi,
        COALESCE(SUM(COALESCE((e->>'chg_oi')::bigint, 0)), 0) AS chain_oi_chg,
        AVG(CASE WHEN (e->>'volume')::bigint > 0 THEN 1.0 ELSE 0.0 END) AS chain_breadth
    FROM jsonb_array_elements(m.option_chain) e
) agg ON TRUE
LEFT JOIN LATERAL (
    -- CE+PE settle at the strike nearest the spot, on the ~30d expiry
    SELECT SUM(s.settle) AS straddle
    FROM (
        SELECT (e->>'settle')::float AS settle
        FROM jsonb_array_elements(m.option_chain) e
        WHERE e->>'expiry' = to_char(m.expiry_30d, 'DD-Mon-YYYY')
          AND e->>'type' IN ('CE', 'PE')
          AND (e->>'settle')::float > 0
        ORDER BY abs((e->>'strike')::float - m.underlying_price)
        LIMIT 2
    ) s
) straddle ON TRUE
LEFT JOIN LATERAL (
    -- 25-delta OTM put IV minus 25-delta OTM call IV, on the ~30d expiry
    SELECT (p.iv - c.iv) AS skew_25d
    FROM (
        SELECT (e->>'iv')::float AS iv
        FROM jsonb_array_elements(m.option_chain) e
        WHERE e->>'expiry' = to_char(m.expiry_30d, 'DD-Mon-YYYY')
          AND e->>'type' = 'PE' AND (e->>'iv')::float > 0
        ORDER BY abs(abs((e->>'delta')::float) - 0.25)
        LIMIT 1
    ) p
    CROSS JOIN (
        SELECT (e->>'iv')::float AS iv
        FROM jsonb_array_elements(m.option_chain) e
        WHERE e->>'expiry' = to_char(m.expiry_30d, 'DD-Mon-YYYY')
          AND e->>'type' = 'CE' AND (e->>'iv')::float > 0
        ORDER BY abs(abs((e->>'delta')::float) - 0.25)
        LIMIT 1
    ) c
) skew ON TRUE
LEFT JOIN LATERAL (
    -- IV of the ~5 pct OTM put on the ~30d expiry (ATM premium subtracted in pandas)
    SELECT (e->>'iv')::float AS otm_put_iv
    FROM jsonb_array_elements(m.option_chain) e
    WHERE e->>'expiry' = to_char(m.expiry_30d, 'DD-Mon-YYYY')
      AND e->>'type' = 'PE' AND (e->>'iv')::float > 0
    ORDER BY abs((e->>'strike')::float - 0.95 * m.underlying_price)
    LIMIT 1
) otm ON TRUE
"""


# ───────────────────────────── helpers ─────────────────────────────────────
def _connect() -> Any:
    return psycopg2.connect(build_dsn())


def _read_sql(conn: Any, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    """Minimal DBAPI2 reader (pandas 3.x read_sql warns on raw psycopg2 connections)."""
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def _get(d: Any, *keys: str) -> float | None:
    """Dig into a JSONB-derived dict; missing/non-numeric -> None."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if cur is None:
        return None
    try:
        return float(cur)
    except (TypeError, ValueError):
        return None


def _ratio(num: pd.Series[Any], den: pd.Series[Any]) -> pd.Series[Any]:
    """num/den with division-by-zero -> NaN instead of inf."""
    return num / den.replace(0, np.nan)


# ───────────────────────────── dataset build ───────────────────────────────
def _load_base(conn: Any, symbols: list[str] | None, limit_dates: int | None) -> pd.DataFrame:
    where: list[str] = []
    params: list[Any] = []
    if symbols:
        # IN with one placeholder per symbol — avoids psycopg2 list->array adaptation
        where.append(f"symbol IN ({', '.join(['%s'] * len(symbols))})")
        params.extend(symbols)
    if limit_dates:
        where.append(
            "date IN (SELECT DISTINCT date FROM option_metrics ORDER BY date DESC LIMIT %s)"
        )
        params.append(limit_dates)
    # ORDER BY is load-bearing, not cosmetic: every momentum feature below is a
    # groupby().pct_change(), which trusts row order. Without it Postgres returns
    # heap order and the returns come out scrambled (silently, and differently on
    # each rebuild). See docs/data/ml-dataset-report.md §2.
    #
    # Momentum/returns/labels must be computed from the *adjusted* close
    # (corporate-action-clean); the raw close is used only for strike/IV at ETL
    # time and for the chain-aggregate nearest-strike lookups (which read
    # option_metrics.underlying_price directly in SQL). See
    # docs/data/iv-integrity-audit.md.
    sql = (
        "SELECT symbol, date, underlying_price_adj AS underlying_price, interest_rate, rv_yz, "
        "expiry_30d, expiry_60d, expiry_90d, upcoming_earning_date, ce, pe "
        "FROM option_metrics"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + " ORDER BY symbol, date"
    )
    df = _read_sql(conn, sql, params)
    for col in ("date", "expiry_30d", "expiry_60d", "expiry_90d", "upcoming_earning_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def compute_chain_aggregates(
    conn: Any, symbols: list[str] | None = None, limit_dates: int | None = None
) -> pd.DataFrame:
    """Per-(symbol, date) aggregates squeezed out of the huge option_chain array.

    One SQL pass (jsonb_array_elements + laterals); returns one row per
    symbol-date with the Tier-3 raw pieces. Callers compute the final ratios
    (put_call_vol_ratio, put_call_oi_ratio, otm_put_iv_premium) in pandas.
    """
    where: list[str] = []
    params: list[Any] = []
    if symbols:
        # IN with one placeholder per symbol — avoids psycopg2 list->array adaptation
        where.append(f"m.symbol IN ({', '.join(['%s'] * len(symbols))})")
        params.extend(symbols)
    if limit_dates:
        where.append(
            "m.date IN (SELECT DISTINCT date FROM option_metrics ORDER BY date DESC LIMIT %s)"
        )
        params.append(limit_dates)
    sql = _CHAIN_AGG_SQL + (" WHERE " + " AND ".join(where) if where else "")
    df = _read_sql(conn, sql, params)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        # psycopg2 maps SUM(bigint)/AVG() to decimal.Decimal, which lands as an
        # object column and propagates through every ratio derived from it
        # (chain_volume, chain_oi, chain_oi_chg, chain_breadth,
        # put_call_vol_ratio, put_call_oi_ratio). Estimators either reject those
        # or silently object-ify the frame, so pin them to float here.
        for col in df.columns:
            if col not in ("symbol", "date"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _lift_ce_pe(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten the ``ce``/``pe`` JSONB blocks into ``ce_*`` / ``pe_*`` columns."""
    out = df.copy()
    for side in ("ce", "pe"):
        raw = out.pop(side)  # Series[dict | None]
        prefix = f"{side}_"
        for key in ("iv_30", "iv_60", "iv_90", "ivp", "ivr", "close", "volume", "oi"):
            out[f"{prefix}{key}"] = raw.apply(lambda d, k=key: _get(d, k))
        for key in _GREEK_KEYS:
            out[f"{prefix}{key}"] = raw.apply(lambda d, k=key: _get(d, "greeks", k))
    return out


def _add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Tier-2 features: momentum returns, vol spreads, days-to-* (needs chain cols merged).

    Requires the frame to be sorted by ``(symbol, date)`` — the pct_change calls
    below read row order directly. ``build_dataset`` guarantees this.
    """
    out = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    out["underlying_price"] = pd.to_numeric(out["underlying_price"], errors="coerce")
    out["rv_yz"] = pd.to_numeric(out["rv_yz"], errors="coerce")

    g = out.groupby("symbol", sort=False)
    for n in _MOMENTUM_PERIODS:
        out[f"ret_{n}d"] = g["underlying_price"].pct_change(periods=n)

    # rv_yz is a decimal fraction (0.072 = 7.2%); iv_* are percent-scale (10.64).
    out["iv_rv_spread"] = out["ce_iv_30"] - out["rv_yz"] * 100.0
    out["term_slope"] = out["ce_iv_90"] - out["ce_iv_30"]
    out["put_call_iv_spread"] = out["pe_iv_30"] - out["ce_iv_30"]

    out["days_to_expiry_30d"] = (out["expiry_30d"] - out["date"]).dt.days
    out["days_to_earnings"] = (out["upcoming_earning_date"] - out["date"]).dt.days

    out["put_call_vol_ratio"] = _ratio(out["chain_pe_vol"], out["chain_ce_vol"])
    out["put_call_oi_ratio"] = _ratio(out["chain_pe_oi"], out["chain_ce_oi"])
    out["otm_put_iv_premium"] = out["otm_put_iv"] - out["pe_iv_30"]
    return out


def screen_corporate_actions(
    df: pd.DataFrame, threshold: float = _MAX_ABS_DAILY_RETURN
) -> pd.DataFrame:
    """NULL out momentum features and targets contaminated by corporate actions.

    ``underlying_price`` comes from Yahoo's close series, which does not fully
    account for demergers and some Indian corporate actions even when the
    adjusted series is used (e.g. VEDL 2026-04-30 shows a −64.9% "return").
    An unscreened split poisons three things at once: the momentum features that
    span it, the *label* on the preceding row, and the scale of every fitted
    model (a −65% observation is a 40-sigma event in a σ≈1.6% series).

    For each flagged jump at row ``i`` this blanks ``ret_Nd`` over ``i..i+N-1``
    (the whole contaminated lookback window) and the target on row ``i-1`` (the
    label that would otherwise "predict" the corporate action).

    Requires ``(symbol, date)`` sort order. Logs every flagged row — a silent
    filter here is how survivorship-style bias creeps in.
    """
    out = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    if "ret_1d" not in out.columns:
        return out

    jump = out["ret_1d"].abs() > threshold
    n_flagged = int(jump.sum())
    if not n_flagged:
        logger.info("Corporate-action screen: no |ret_1d| > %.0f%% rows", threshold * 100)
        return out

    for _, row in out.loc[jump, ["symbol", "date", "underlying_price", "ret_1d"]].iterrows():
        logger.warning(
            "Corporate-action screen: %s %s ret_1d=%+.1f%% (price %.2f) — "
            "blanking contaminated returns and adjacent target",
            row["symbol"], pd.Timestamp(row["date"]).date(), row["ret_1d"] * 100.0,
            row["underlying_price"],
        )

    by_symbol = jump.groupby(out["symbol"], sort=False)

    # Derive both masks from `jump` before mutating anything: reading ret_1d back
    # after blanking it would silently disarm the label screen below.
    label_spans_jump = by_symbol.shift(-1).fillna(False).astype(bool)

    for n in _MOMENTUM_PERIODS:
        col = f"ret_{n}d"
        if col not in out.columns:
            continue
        # a jump anywhere in the trailing n rows (inclusive) corrupts this window
        contaminated = by_symbol.transform(
            lambda s, w=n: s.rolling(w, min_periods=1).max()
        ).astype(bool)
        out.loc[contaminated, col] = np.nan

    # the label on the row *before* the jump spans the corporate action
    for col in ("next_day_return", "next_day_direction"):
        if col in out.columns:
            out.loc[label_spans_jump, col] = np.nan

    logger.warning(
        "Corporate-action screen: flagged %d row(s) above |%.0f%%|; "
        "momentum windows and adjacent labels set to NULL",
        n_flagged, threshold * 100,
    )
    return out


def validate_no_lookahead(df: pd.DataFrame, atol: float = 1e-9) -> None:
    """Fail the build if ``ret_1d`` is not a genuine *backward* return.

    Cheap insurance against the row-ordering bug that produced a perfectly
    inverted ``ret_1d`` (mean daily rank IC of −1.00 against the target, and a
    trivial 100%-accuracy rule). That bug was obvious at 21 days because the
    corruption was a clean reversal; at 2500 days a partial scramble would look
    like a merely excellent model. See docs/data/ml-dataset-report.md §2.
    """
    if "ret_1d" not in df.columns or df.empty:
        return
    ordered = df.sort_values(["symbol", "date"])
    truth = ordered.groupby("symbol", sort=False)["underlying_price"].pct_change(1)
    stored = ordered["ret_1d"]
    both = stored.notna() & truth.notna()
    if not both.any():
        return

    max_dev = float((stored[both] - truth[both]).abs().max())
    if max_dev > atol:
        n_bad = int(((stored[both] - truth[both]).abs() > atol).sum())
        raise ValueError(
            f"look-ahead check FAILED: ret_1d deviates from the backward "
            f"pct_change on {n_bad}/{int(both.sum())} rows (max |diff| = {max_dev:.3e}). "
            f"Rows are almost certainly not sorted by (symbol, date) before the "
            f"momentum features are computed."
        )

    # a backward return must not be a function of the forward one
    fwd = ordered.get("next_day_return")
    if fwd is not None:
        pair = pd.concat([stored, fwd], axis=1).dropna()
        if len(pair) > 30:
            leak = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
            if abs(leak) > 0.5:
                raise ValueError(
                    f"look-ahead check FAILED: corr(ret_1d, next_day_return) = "
                    f"{leak:+.4f}. A backward return cannot track the forward one; "
                    f"this indicates target leakage."
                )
    logger.info("Look-ahead check passed (max |ret_1d deviation| = %.2e)", max_dev)


def _add_targets(df: pd.DataFrame) -> pd.DataFrame:
    """Supervised targets: next-day return + binary direction (per symbol).

    The most recent day of each symbol keeps NaN targets — there is no
    "tomorrow" yet; those rows are future-inference rows, never training rows.
    """
    out = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = out.groupby("symbol", sort=False)
    ret = g["underlying_price"].shift(-1) / out["underlying_price"] - 1.0
    out["next_day_return"] = ret
    has_tomorrow = ret.notna()
    out["next_day_direction"] = np.where(has_tomorrow, (ret > 0).astype(int), np.nan)
    return out


def build_dataset(
    conn: Any,
    symbols: list[str] | None = None,
    limit_dates: int | None = None,
) -> pd.DataFrame:
    """Full training matrix: identity + level + CE/PE + Tier2 + Tier3 + targets.

    Screens corporate actions and asserts the absence of look-ahead before
    returning; a failed check raises rather than writing a corrupt table.
    """
    base = _load_base(conn, symbols, limit_dates)
    if base.empty:
        return base
    # Belt-and-braces alongside the SQL ORDER BY: the merge below can reorder,
    # and every momentum feature depends on row order.
    base = base.sort_values(["symbol", "date"]).reset_index(drop=True)
    chain = compute_chain_aggregates(conn, symbols, limit_dates)
    df = base.merge(chain, on=["symbol", "date"], how="left")
    df = _lift_ce_pe(df)
    df = _add_derived(df)
    df = _add_targets(df)
    validate_no_lookahead(df)
    df = screen_corporate_actions(df)
    return df[DATASET_COLUMNS]


# ───────────────────────────── persistence ─────────────────────────────────
def _clean_value(v: Any) -> Any:
    """NaN/Inf -> None; numpy scalars -> python scalars (psycopg2-friendly)."""
    if v is None:
        return None
    if isinstance(v, (np.integer, np.bool_)):
        return v.item()
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if not np.isfinite(f) else f
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, np.datetime64):
        return pd.Timestamp(v).date()
    return v


def _to_rows(df: pd.DataFrame) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    dir_idx = DATASET_COLUMNS.index("next_day_direction")
    for rec in df.to_dict("records"):
        out = [_clean_value(rec.get(c)) for c in DATASET_COLUMNS]
        if out[dir_idx] is not None:
            out[dir_idx] = int(out[dir_idx])  # SMALLINT column
        rows.append(tuple(out))
    return rows


def write_dataset(conn: Any, df: pd.DataFrame) -> int:
    """Upsert the matrix into ``ml_dataset``; returns the number of rows written."""
    cols = ", ".join(DATASET_COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in DATASET_COLUMNS if c not in ("symbol", "date"))
    sql = f"INSERT INTO ml_dataset ({cols}) VALUES %s ON CONFLICT (symbol, date) DO UPDATE SET {updates}"
    rows = _to_rows(df)
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)
    conn.commit()
    return len(rows)


def export_parquet(df: pd.DataFrame, path: Path) -> Path:
    """Write the matrix to parquet (default caller side: ``data/processed/ml_dataset.parquet``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path
