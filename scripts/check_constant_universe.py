"""Is Stage 5's time gradient a sample-composition artefact?

`robustness_checks` reports the pre-registered feature's partial IC nearly
tripling between sample halves — +0.0701 (2021-10 → 2023-10) to +0.1981
(2024-01 → 2026-04), positive in 100% of the second half's ten periods. The
stage investigated that gradient against period covariates and concluded gross
mutual-fund flow was the only survivor (+0.369), with F&O market depth
collapsing to −0.067.

**Effective sample size was not among the covariates tested.** And it moves
sharply inside the window: symbols carrying a non-null `fut_basis_excess`
within the quarter sit flat at 72–76% of filers from 2021-09 to 2024-09, then
step to ~100% at 2024-12 and hold there. NSE widened F&O eligibility in late
2024, so more names clear the feature's own DTE >= 5 gate.

That matters because a wider cross-section mechanically stabilises a per-period
rank IC: more names per quarter means less noise in each quarterly
correlation. The second half could therefore measure a higher IC with no change
whatsoever in the underlying signal — and the step lands almost exactly on the
half boundary.

This script holds the universe fixed and re-measures. A symbol is in the
constant universe only if it carries the feature in **every** period, so the
cross-section is the same width throughout and composition cannot drive the
gradient.

Two readings, both worth having:

- the gradient **survives** -> it is not composition, and the stage can say so
- the gradient **collapses** -> the tripling was composition; the full-sample
  +0.1374 is unaffected, but the "effect strengthens over time" claim goes

Run:  uv run python scripts/check_constant_universe.py
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg2

from mktmtm.ml.accumulation import (
    PREREGISTERED_FEATURE,
    _period_partial_ic,
    _summarize,
    build_accumulation_frame,
)
from mktmtm.storage.db import build_dsn

logging.basicConfig(level=logging.WARNING, format="%(message)s")

HOLDER = "mf"  # the holder the signal was established on
OUT = Path("models/evaluation/constant_universe.json")


def _halves(work: pd.DataFrame, feature: str) -> dict[str, Any]:
    """Full-sample and both halves, split exactly as robustness_checks does."""
    periods = sorted(work["period"].unique())
    midpoint = periods[len(periods) // 2]
    out: dict[str, Any] = {}
    for label, subset in (
        ("full", work),
        ("first_half", work.loc[work["period"] < midpoint]),
        ("second_half", work.loc[work["period"] >= midpoint]),
    ):
        ic = _period_partial_ic(subset, feature, "_momentum")
        summary = _summarize(ic)
        if len(subset):
            summary["range"] = (
                f"{subset['period'].min():%Y-%m} → {subset['period'].max():%Y-%m}"
            )
            summary["mean_symbols"] = round(
                float(subset.groupby("period", observed=True).size().mean()), 1
            )
        out[label] = summary
    return out


def _row(label: str, s: dict[str, Any]) -> str:
    return (
        f"  {label:<14} IC {s['mean_ic']:+.4f}   positive {s['positive_rate']:>6.1%}"
        f"   periods {int(s['periods']):>3}   mean symbols {s.get('mean_symbols', 0):>6}"
        f"   {s.get('range', '')}"
    )


def main() -> None:
    conn = psycopg2.connect(build_dsn())
    frame = build_accumulation_frame(conn, holder=HOLDER, window="contemporaneous")
    feature = PREREGISTERED_FEATURE

    work = frame.copy()
    work["_momentum"] = work["_window_return"]
    work = work.dropna(subset=[feature, "_momentum", "accumulation"])

    n_periods = work["period"].nunique()
    per_symbol = work.groupby("symbol", observed=True)["period"].nunique()
    constant = set(per_symbol[per_symbol == n_periods].index)

    print(f"holder={HOLDER}  feature={feature}")
    print(f"periods={n_periods}  symbols(any)={per_symbol.size}  "
          f"symbols(all periods)={len(constant)}")
    print()
    print("coverage per period (the step this script exists to control for):")
    cov = work.groupby("period", observed=True).size()
    for period, n in cov.items():
        print(f"    {period:%Y-%m}  {n:>4}")
    print()

    full_res = _halves(work, feature)
    const_res = _halves(work.loc[work["symbol"].isin(constant)], feature)

    print("FULL UNIVERSE (what robustness_checks reports)")
    for k in ("full", "first_half", "second_half"):
        print(_row(k, full_res[k]))
    print()
    print(f"CONSTANT UNIVERSE ({len(constant)} symbols present in all {n_periods} periods)")
    for k in ("full", "first_half", "second_half"):
        print(_row(k, const_res[k]))
    print()

    def gradient(res: dict[str, Any]) -> float:
        return res["second_half"]["mean_ic"] - res["first_half"]["mean_ic"]

    # ── Threshold sweep. "Present in every period" is one arbitrary cut, and
    # it costs 147 of 209 symbols; at 62 names the per-period cross-section is
    # itself much noisier. If the conclusion only holds at 100% presence it is
    # a property of that cut rather than of the data, so the whole curve is
    # reported. ────────────────────────────────────────────────────────────
    sweep: list[dict[str, Any]] = []
    for thresh in (0.5, 0.7, 0.8, 0.9, 1.0):
        keep = set(per_symbol[per_symbol >= thresh * n_periods].index)
        res = _halves(work.loc[work["symbol"].isin(keep)], feature)
        g = gradient(res)
        sweep.append(
            {
                "min_presence": thresh,
                "symbols": len(keep),
                "mean_symbols_per_period": res["full"].get("mean_symbols"),
                "full_ic": res["full"]["mean_ic"],
                "first_half_ic": res["first_half"]["mean_ic"],
                "second_half_ic": res["second_half"]["mean_ic"],
                "gradient": g,
            }
        )
    print("THRESHOLD SWEEP — does the conclusion depend on the cut?")
    print(f"  {'presence':>9} {'symbols':>8} {'per-period':>11} {'full':>9} "
          f"{'1st half':>9} {'2nd half':>9} {'gradient':>9}")
    for s in sweep:
        print(f"  {s['min_presence']:>8.0%} {s['symbols']:>8} "
              f"{s['mean_symbols_per_period']:>11} {s['full_ic']:>+9.4f} "
              f"{s['first_half_ic']:>+9.4f} {s['second_half_ic']:>+9.4f} "
              f"{s['gradient']:>+9.4f}")
    print()

    g_full, g_const = gradient(full_res), gradient(const_res)
    print(f"gradient (second - first):  full {g_full:+.4f}   constant {g_const:+.4f}")
    retained = g_const / g_full if abs(g_full) > 1e-9 else float("nan")
    print(f"share of the gradient retained on a fixed universe: {retained:.0%}")
    print()
    if not np.isfinite(retained):
        verdict = "INCONCLUSIVE — no gradient in the full universe to explain"
    elif retained >= 0.70:
        verdict = "SURVIVES — the gradient is not sample composition"
    elif retained <= 0.30:
        verdict = "COLLAPSES — the gradient is largely sample composition"
    else:
        verdict = "PARTIAL — composition explains some of the gradient"
    print(f"VERDICT: {verdict}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "holder": HOLDER,
                "feature": feature,
                "periods": int(n_periods),
                "symbols_any": int(per_symbol.size),
                "symbols_constant": len(constant),
                "coverage_per_period": {f"{k:%Y-%m}": int(v) for k, v in cov.items()},
                "full_universe": full_res,
                "constant_universe": const_res,
                "gradient_full": g_full,
                "gradient_constant": g_const,
                "gradient_retained": retained,
                "threshold_sweep": sweep,
                "verdict": verdict,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    print(f"\nwritten to {OUT}")


if __name__ == "__main__":
    main()
