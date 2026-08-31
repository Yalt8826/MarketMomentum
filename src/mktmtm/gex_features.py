"""``mktmtm gex-features`` — recompute the dealer-exposure columns in ``ml_dataset``.

After a Phase-2 ETL re-run lands new per-strike Greeks, the dealer-positioning
aggregates in ``ml_dataset`` are stale. ``make-dataset`` rebuilds the whole
matrix (momentum features + targets), which is overkill when only the chain
aggregates changed. This module recomputes *only* the ``GEX_COLUMNS`` — dex,
gex, gex_chg, vanna_exp, charm_exp, oi_hhi, max_pain, call_wall, put_wall,
dist_to_call_wall, otm_put_oi_build, dex_chg, oi_hhi_chg, dist_to_flip — and
updates them in place.

Progress contract (see docs/implementation/gex-dealer-positioning.md §2): one
``progress_stage`` over symbols, labelled with the current symbol; degrades to
log lines when stderr is not a TTY or ``MKTMTM_NO_PROGRESS=1`` is set.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from psycopg2.extras import execute_values

from mktmtm.ml.dataset import (
    GEX_COLUMNS,
    _add_derived,
    _clean_value,
    _lift_ce_pe,
    _load_base,
    compute_chain_aggregates,
)
from mktmtm.progress import progress_stage

logger = logging.getLogger(__name__)

__all__ = ["GEX_COLUMNS", "recompute_dealer_aggregates"]


def _all_symbols(conn: Any) -> list[str]:
    """Distinct symbols present in ``option_metrics``, sorted."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM option_metrics ORDER BY symbol")
        return [str(row[0]) for row in cur.fetchall()]


def _recompute_symbol(conn: Any, symbol: str, limit_dates: int | None) -> pd.DataFrame:
    """Dealer-exposure frame for one symbol: identity + ``GEX_COLUMNS``.

    Runs the same aggregate path as ``build_dataset`` (base + chain SQL +
    derived), but returns only the dealer columns — momentum features and
    targets are deliberately not recomputed here.
    """
    base = _load_base(conn, [symbol], limit_dates)
    if base.empty:
        return base
    chain = compute_chain_aggregates(conn, [symbol], limit_dates)
    df = base.merge(chain, on=["symbol", "date"], how="left")
    df = _lift_ce_pe(df)
    df = _add_derived(df)
    cols = [c for c in GEX_COLUMNS if c in df.columns]
    return df[["symbol", "date", *cols]]


def _update_gex_columns(conn: Any, df: pd.DataFrame) -> int:
    """UPDATE ``ml_dataset``'s GEX columns for rows already present.

    A strict UPDATE (not an upsert): rows missing from ``ml_dataset`` are left
    untouched, so a recompute can never invent a partial row. Returns the
    number of rows written.
    """
    cols = [c for c in GEX_COLUMNS if c in df.columns]
    if not cols or df.empty:
        return 0
    rows = [
        (
            rec["symbol"],
            _clean_value(rec["date"]),
            *(_clean_value(rec.get(c)) for c in cols),
        )
        for rec in df.to_dict("records")
    ]
    assignments = ", ".join(f"m.{c} = v.{c}" for c in cols)
    sql = (
        f"UPDATE ml_dataset m SET {assignments} "
        f"FROM (VALUES %s) AS v(symbol, date, {', '.join(cols)}) "
        "WHERE m.symbol = v.symbol AND m.date = v.date"
    )
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)
    conn.commit()
    return len(rows)


def recompute_dealer_aggregates(
    conn: Any,
    symbols: list[str] | None = None,
    limit_dates: int | None = None,
) -> dict[str, Any]:
    """Recompute the dealer-exposure columns into ``ml_dataset``, per symbol.

    Unlike ``build_dataset`` (full matrix + targets), only the ``GEX_COLUMNS``
    are recomputed and written — the fast refresh path after a new ETL pass.

    ``progress_stage`` advances once per symbol, labelled with the current
    symbol. Returns a summary dict for the CLI table::

        {"symbols": int, "rows_updated": int, "skipped": list[str], "columns": list[str]}

    Symbols with no ``option_metrics`` rows land in ``skipped``, never in a
    partial write.
    """
    syms = list(symbols) if symbols else _all_symbols(conn)
    skipped: list[str] = []
    rows_updated = 0
    columns: list[str] = list(GEX_COLUMNS)
    with progress_stage(
        "Recomputing dealer aggregates", total=len(syms), unit="symbols"
    ) as bar:
        for sym in syms:
            try:
                frame = _recompute_symbol(conn, sym, limit_dates)
            except Exception as exc:  # a broken symbol must not kill the run
                logger.warning("gex-features: %s failed: %s", sym, exc)
                skipped.append(f"{sym} ({type(exc).__name__})")
                bar.advance(label=sym)
                continue
            if frame.empty:
                skipped.append(sym)
            else:
                rows_updated += _update_gex_columns(conn, frame)
            bar.advance(label=sym)
    return {
        "symbols": len(syms),
        "rows_updated": rows_updated,
        "skipped": skipped,
        "columns": columns,
    }
