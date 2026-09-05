"""Measure how much the option buildup quadrants change under delta adjustment.

A call premium rises when the underlying rises, whether or not any buyer was
aggressive. So the raw premium change used by ``option_quadrants`` is
confounded by the underlying's move, scaled by the option's delta. The
rigorous alternative is the residual

    residual = (premium_t - premium_{t-1}) - delta_{t-1} * (spot_t - spot_{t-1})

which needs a per-strike delta. That is not in the bhavcopy parquet cache, but
it *is* in ``option_metrics.option_chain`` — so this measurement is bounded to
a sample of dates rather than run over all 49.5M chain elements.

The question this answers is narrow and decides a column: on the contracts the
quadrants actually classify, how often does the delta-adjusted residual put a
contract in a different bucket than the raw premium change? If the two mostly
agree, the cheap raw pass in ``features/flow.py`` is sufficient. If they do
not, the raw column is measuring the underlying's move and must be replaced.

Usage:  uv run python scripts/measure_delta_adjustment.py [n_dates]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2

from mktmtm.storage.db import build_dsn

OUT_PATH = Path("models/evaluation/delta_adjustment.json")

# One chain row per (symbol, date, strike, type, expiry) with the fields the
# residual needs. Restricted to the sampled dates on both sides of the join.
_CHAIN_SQL = """
SELECT
    om.symbol,
    om.date,
    om.underlying_price                      AS spot,
    (e ->> 'strike')::double precision       AS strike,
     e ->> 'type'                            AS option_typ,
     e ->> 'expiry'                          AS expiry,
    (e ->> 'delta')::double precision        AS delta,
    (e ->> 'close')::double precision        AS close,
    (e ->> 'settle')::double precision       AS settle,
    (e ->> 'chg_oi')::double precision       AS chg_oi,
    (e ->> 'volume')::double precision       AS volume
FROM option_metrics om
CROSS JOIN LATERAL jsonb_array_elements(om.option_chain) AS e
WHERE om.date = ANY(%s)
  AND om.option_chain IS NOT NULL
  AND om.underlying_price IS NOT NULL
"""

KEY = ["symbol", "strike", "option_typ", "expiry"]


def _sample_dates(conn: Any, n: int) -> list[pd.Timestamp]:
    """``n`` trade dates spread evenly over the history, each with its predecessor."""
    dates = pd.read_sql("SELECT DISTINCT date FROM option_metrics ORDER BY date", conn)
    series = pd.to_datetime(dates["date"])
    # Skip index 0: the earliest date has no predecessor to difference against.
    step = max(1, (len(series) - 1) // n)
    picked = series.iloc[1::step][:n]
    prior = series.shift(1).loc[picked.index]
    return sorted({*picked.tolist(), *prior.tolist()})


def measure(conn: Any, n_dates: int = 24) -> dict[str, Any]:
    """Compare raw vs delta-adjusted quadrant assignment on sampled dates."""
    wanted = _sample_dates(conn, n_dates)
    chain = pd.read_sql(_CHAIN_SQL, conn, params=(([d.date() for d in wanted]),))
    chain["date"] = pd.to_datetime(chain["date"])
    # Same mark rule as features/flow.py: settle, falling back to close.
    chain["mark"] = chain["settle"].where(
        chain["settle"].notna() & (chain["settle"] > 0), chain["close"]
    )

    ordered = sorted(chain["date"].unique())
    pairs = [(ordered[i - 1], ordered[i]) for i in range(1, len(ordered))]

    rows: list[pd.DataFrame] = []
    for prev_day, day in pairs:
        today = chain.loc[chain["date"] == day]
        prev = chain.loc[chain["date"] == prev_day]
        if today.empty or prev.empty:
            continue
        merged = today.merge(
            prev[[*KEY, "mark", "delta", "spot"]],
            on=KEY,
            how="inner",
            suffixes=("", "_prev"),
        )
        if not merged.empty:
            rows.append(merged)
    if not rows:
        raise SystemExit("no consecutive-day contract pairs found in the sample")

    m = pd.concat(rows, ignore_index=True)
    # The quadrants only ever classify traded contracts that opened interest.
    m = m.loc[
        (m["volume"].fillna(0) > 0)
        & (m["chg_oi"].fillna(0) > 0)
        & m["mark"].notna()
        & m["mark_prev"].notna()
        & m["delta_prev"].notna()
        & m["spot_prev"].notna()
    ]
    if m.empty:
        raise SystemExit("no traded, OI-opening contracts survived the filters")

    raw = m["mark"] - m["mark_prev"]
    residual = raw - m["delta_prev"] * (m["spot"] - m["spot_prev"])
    notional = m["chg_oi"] * m["spot"]

    # "Writer" is the negative-move side in both readings; a flat move is
    # neither, so agreement is measured on strict signs.
    raw_writer = raw < 0
    adj_writer = residual < 0
    decided = (raw != 0) & (residual != 0)
    disagree = decided & (raw_writer != adj_writer)

    n = int(decided.sum())
    w_total = float(notional[decided].sum())
    result = {
        "sample_dates": len(pairs),
        "contracts_compared": n,
        "disagreement_rate": float(disagree[decided].mean()),
        "disagreement_rate_notional_weighted": (
            float(notional[disagree].sum() / w_total) if w_total > 0 else 0.0
        ),
        "raw_writer_share": float(raw_writer[decided].mean()),
        "adjusted_writer_share": float(adj_writer[decided].mean()),
        "median_abs_raw_move": float(raw[decided].abs().median()),
        "median_abs_delta_term": float(
            (m["delta_prev"] * (m["spot"] - m["spot_prev"]))[decided].abs().median()
        ),
    }
    return result


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    conn = psycopg2.connect(build_dsn())
    try:
        result = measure(conn, n)
    finally:
        conn.close()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2) + "\n")
    for key, value in result.items():
        print(f"{key:44s} {value}")


if __name__ == "__main__":
    main()
