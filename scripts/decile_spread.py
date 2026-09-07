"""Decile-spread view of the Stage 5 accumulation detector.

The stage reports a partial rank IC (+0.1374). That is the correct statistic and
it is unreadable to anyone who has not met a rank correlation, so this script
restates the same ranking as "how much of the company did the top decile
actually get, versus the bottom decile" — the numbers quoted in
``docs/understanding/v1/14-results.md``.

It is a *view* of the shipped result, not a second experiment: it reuses
``build_accumulation_frame`` unchanged, applies the same momentum
residualisation ``nowcast`` ships, and adds no free parameter beyond the decile
count.

    uv run python scripts/decile_spread.py
    uv run python scripts/decile_spread.py --holder fii --deciles 5
"""

from __future__ import annotations

import argparse

import pandas as pd

from mktmtm.ml.accumulation import PREREGISTERED_FEATURE, build_accumulation_frame
from mktmtm.storage.db import build_dsn

MOMENTUM = "_window_return"


def momentum_neutral_score(frame: pd.DataFrame, signal: str) -> pd.Series:
    """Residualise ``signal`` on price momentum within each period, on ranks.

    Identical in form to ``accumulation.nowcast``: the validated result is a
    partial IC, so the score that is ranked must be a partial too. Ranks rather
    than levels, because the validated metric is a rank IC and ``basis_excess``
    has heavy tails.
    """
    out = pd.Series(float("nan"), index=frame.index, dtype="float64")
    for _, group in frame.groupby("period", observed=True, sort=True):
        chunk = group[[signal, MOMENTUM]].dropna()
        if len(chunk) < 10:
            continue
        ranked = chunk.rank(method="average")
        x, z = ranked[signal], ranked[MOMENTUM]
        variance = z.var()
        beta = x.cov(z) / variance if variance > 0 else 0.0
        out.loc[chunk.index] = x - beta * (z - z.mean()) - x.mean()
    return out


def decile_table(frame: pd.DataFrame, score: pd.Series, deciles: int) -> dict[str, float]:
    work = frame.assign(_score=score).dropna(subset=["_score", "accumulation"])
    edge = 1.0 / deciles
    pct = work.groupby("period", observed=True)["_score"].rank(pct=True)
    top, bottom = work[pct >= 1.0 - edge], work[pct <= edge]

    per_period = pd.DataFrame(
        {
            "top": top.groupby("period")["accumulation"].mean(),
            "bottom": bottom.groupby("period")["accumulation"].mean(),
        }
    ).dropna()

    return {
        "periods": float(len(per_period)),
        "mean_symbols": float(work.groupby("period").size().mean()),
        "top_mean": float(top["accumulation"].mean()),
        "bottom_mean": float(bottom["accumulation"].mean()),
        "spread": float(top["accumulation"].mean() - bottom["accumulation"].mean()),
        "periods_top_beats_bottom": float((per_period["top"] > per_period["bottom"]).sum()),
        "top_positive_rate": float((top["accumulation"] > 0).mean()),
        "bottom_positive_rate": float((bottom["accumulation"] > 0).mean()),
        "base_rate": float((work["accumulation"] > 0).mean()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holder", default="mf")
    parser.add_argument("--feature", default=PREREGISTERED_FEATURE)
    parser.add_argument("--deciles", type=int, default=10)
    args = parser.parse_args()

    import psycopg2

    with psycopg2.connect(build_dsn()) as conn:
        frame = build_accumulation_frame(conn, holder=args.holder)

    score = momentum_neutral_score(frame, args.feature)
    stats = decile_table(frame, score, args.deciles)

    n = args.deciles
    print(f"\nholder={args.holder}  feature={args.feature}  {n} buckets")
    print(f"{int(stats['periods'])} periods, ~{stats['mean_symbols']:.0f} symbols each\n")
    print(f"  mean accumulation, top bucket:     {stats['top_mean'] * 100:+.3f}% of company")
    print(f"  mean accumulation, bottom bucket:  {stats['bottom_mean'] * 100:+.3f}% of company")
    print(f"  spread:                            {stats['spread'] * 100:+.3f}pp")
    print(
        f"  periods top beat bottom:           "
        f"{int(stats['periods_top_beats_bottom'])}/{int(stats['periods'])}\n"
    )
    print(f"  top bucket accumulated at all:     {stats['top_positive_rate'] * 100:.1f}%")
    print(f"  bottom bucket accumulated at all:  {stats['bottom_positive_rate'] * 100:.1f}%")
    print(f"  base rate (all symbols):           {stats['base_rate'] * 100:.1f}%\n")


if __name__ == "__main__":
    main()
