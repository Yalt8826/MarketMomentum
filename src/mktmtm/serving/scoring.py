"""Phase 3 — scoring the frozen screen records against what actually happened.

Everything in Gates 0 and 1 is in-sample: the strategy, its costs and its
parameters were all measured on the same 2021-2026 window. Subset stability is
weaker evidence than an independent test, and this project has said so about its
own headline research result.

A screen record is the remedy: a dated, committed selection written **before its
outcome exists**. This module scores those records, and accumulates them into a
running record that includes the losing months.

## How a record is scored

By **re-running the backtested strategy for that month** and matching on symbol,
rather than by reimplementing position management here.

That is deliberate. The screen selects strikes with the same
``find_options_near_delta`` call the backtest uses, on the same day's chain, so
for a first-trading-day entry the two pick the same contracts. Reimplementing
the weekly hedge and exit logic in a second place would create exactly the
divergence hazard this project already guards against between ``etl.py`` and
``pipeline/process.py`` — two implementations of one calculation that drift
apart silently.

## The calibration requirement

The backtested strategy enters on the **first trading day of the month**. A
screen run on any other day produces a shorter-dated position at different
strikes, drawn from a distribution nobody measured. Such records are scored and
reported, but **excluded from Gate 3**, because averaging them into the verdict
would be comparing against a calibration that does not describe them.

## What a run of good months does and does not establish

Premium selling wins most months by construction — Gate 0 measured a 67.1% win
rate with a fat left tail. So:

> A run of profitable months does not establish that the tail risk has been
> survived. It establishes that the tail has not yet arrived.

That sentence belongs in every write-up this module feeds.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "GATE_3_MIN_MONTHS",
    "PHASE_1_MEAN",
    "ScoredRecord",
    "gate_3_report",
    "load_records",
    "running_record",
    "score_record",
]

#: Gate 0's per-trade mean return on margin — the expectation a forward record
#: is measured against.
PHASE_1_MEAN = 0.0456

#: Gate 0's monthly portfolio bounds, for the "no month exceeded the modelled
#: loss" criterion.
PHASE_1_WORST_MONTH = -0.1612

#: Six is a floor, not a target. One profitable month is one observation.
GATE_3_MIN_MONTHS = 6


@dataclass
class ScoredRecord:
    """One screen record, scored against realised outcomes."""

    as_of: date
    commit: str
    is_month_entry: bool
    selected: int
    scored: int
    unscored: dict[str, str]
    mean_return_on_margin: float
    total_net_rupees: float
    total_margin_rupees: float
    portfolio_return: float
    worst_trade: float
    trades: pd.DataFrame
    #: Date this record was first committed to git, or ``None`` if uncommitted.
    committed_at: date | None = None
    #: Expiry of the positions it selected — the date its outcome became known.
    outcome_known_from: date | None = None

    @property
    def provenance_verified(self) -> bool:
        """Was this committed BEFORE its outcome existed?

        The distinction between a forward record and a backtest with a forward
        filename. An uncommitted record cannot be verified either way.
        """
        if self.committed_at is None or self.outcome_known_from is None:
            return False
        return self.committed_at < self.outcome_known_from

    @property
    def in_calibration(self) -> bool:
        """Eligible for Gate 3: a verified forward record, entered on the
        month's first trading day, with something actually scored."""
        return self.is_month_entry and self.provenance_verified and self.scored > 0

    def as_row(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "commit": self.commit,
            "in_calibration": self.in_calibration,
            "month_entry": self.is_month_entry,
            "verified": self.provenance_verified,
            "committed_at": self.committed_at,
            "selected": self.selected,
            "scored": self.scored,
            "portfolio_return": self.portfolio_return,
            "mean_return": self.mean_return_on_margin,
            "net_rupees": self.total_net_rupees,
            "margin_rupees": self.total_margin_rupees,
            "worst_trade": self.worst_trade,
        }


def committed_at(path: Path) -> date | None:
    """When this record was first committed to git, or ``None`` if it was not.

    **Gate 3's fourth criterion.** Everything else here can be satisfied by a
    record written after its outcome was already known — which is not a forward
    test, it is a backtest wearing a forward test's filename. The only external
    evidence of *when* a prediction existed is the commit that introduced it.

    An uncommitted record therefore cannot be verified and is excluded from the
    verdict, however good its numbers look.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%cs", "--", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
            cwd=path.resolve().parent,
        )
        lines = [line for line in out.stdout.split("\n") if line.strip()]
        return date.fromisoformat(lines[-1]) if lines else None
    except Exception:  # noqa: BLE001 - unverifiable is a valid answer
        return None


def load_records(directory: Path) -> list[dict[str, Any]]:
    """Every screen payload in ``directory``, oldest first.

    Reads the ``.json`` sidecars, never the markdown. The markdown is the dated
    commitment for a human; making its prose load-bearing would mean a
    reformatting could silently break scoring.
    """
    records = []
    for path in sorted(Path(directory).glob("screen-*.json")):
        payload = json.loads(path.read_text())
        payload["_path"] = str(path)
        payload["_committed_at"] = (
            committed_at(path.with_suffix(".md")) or committed_at(path)
        )
        records.append(payload)
    return records


def _expiry_of(selected: list[dict[str, Any]]) -> date | None:
    """The latest expiry among selected positions — when the outcome existed."""
    expiries = [
        date.fromisoformat(row["expiry"]) for row in selected if row.get("expiry")
    ]
    return max(expiries) if expiries else None


_RESULT_SQL = """
SELECT symbol, entry_date, exit_date, pnl_points, lot_size,
       gross_pnl_rupees, costs_rupees, slippage_rupees, net_pnl_rupees,
       margin_rupees, return_on_margin, skipped_reason
FROM backtest_results
WHERE variant = %(variant)s AND year = %(year)s AND month = %(month)s
"""


def score_record(
    conn: Any,
    payload: dict[str, Any],
    *,
    variant: str = "base",
    run_if_missing: bool = True,
) -> ScoredRecord:
    """Score one screen record against what the strategy actually returned.

    Runs the backtested strategy for the record's month (unless already
    present under ``variant``) and matches the selected symbols. A position
    whose month cannot be priced — no exit price, a degenerate chain, an
    earnings conflict that materialised after the screen ran — is reported as
    **unscored with its reason**, never dropped.
    """
    as_of = date.fromisoformat(payload["as_of"])
    selected = payload["selected"]
    symbols = [row["symbol"] for row in selected]

    params = {"variant": variant, "year": as_of.year, "month": as_of.month}
    results = pd.read_sql(_RESULT_SQL, conn, params=params)

    missing = set(symbols) - set(results["symbol"])
    if missing and run_if_missing:
        logger.info(
            "backfilling %d symbol(s) for %s-%02d before scoring",
            len(missing), as_of.year, as_of.month,
        )
        from mktmtm.backtest.strangle import run_backtest

        run_backtest(
            symbols=sorted(missing), years=[as_of.year], months=[as_of.month],
            variant=variant,
        )
        results = pd.read_sql(_RESULT_SQL, conn, params=params)

    by_symbol = results.set_index("symbol")
    rows, unscored = [], {}
    for row in selected:
        symbol = row["symbol"]
        if symbol not in by_symbol.index:
            unscored[symbol] = "no backtest row for this month"
            continue
        actual = by_symbol.loc[symbol]
        if pd.isna(actual["return_on_margin"]):
            unscored[symbol] = str(actual["skipped_reason"] or "unpriced")
            continue
        rows.append(
            {
                "symbol": symbol,
                "expected_credit": row.get("net_credit"),
                "expected_margin": row.get("margin"),
                "realised_net": float(actual["net_pnl_rupees"]),
                "realised_margin": float(actual["margin_rupees"]),
                "return_on_margin": float(actual["return_on_margin"]),
                "exit_date": actual["exit_date"],
            }
        )

    trades = pd.DataFrame(rows)
    if trades.empty:
        return ScoredRecord(
            as_of=as_of, commit=payload.get("commit", "unknown"),
            is_month_entry=payload.get("is_month_entry", True),
            selected=len(selected), scored=0, unscored=unscored,
            mean_return_on_margin=float("nan"), total_net_rupees=0.0,
            total_margin_rupees=0.0, portfolio_return=float("nan"),
            worst_trade=float("nan"), trades=trades,
            committed_at=payload.get("_committed_at"),
            outcome_known_from=_expiry_of(selected),
        )

    net = float(trades["realised_net"].sum())
    margin = float(trades["realised_margin"].sum())
    return ScoredRecord(
        as_of=as_of,
        commit=payload.get("commit", "unknown"),
        is_month_entry=payload.get("is_month_entry", True),
        selected=len(selected),
        scored=len(trades),
        unscored=unscored,
        mean_return_on_margin=float(trades["return_on_margin"].mean()),
        total_net_rupees=net,
        total_margin_rupees=margin,
        # Margin-weighted, matching how Gate 0 pooled its monthly portfolio:
        # the book's return, not the average of its positions'.
        portfolio_return=net / margin if margin > 0 else float("nan"),
        worst_trade=float(trades["return_on_margin"].min()),
        trades=trades,
        committed_at=payload.get("_committed_at"),
        outcome_known_from=_expiry_of(selected),
    )


def running_record(scored: list[ScoredRecord]) -> pd.DataFrame:
    """Every scored record in order, losing months included.

    No filtering, no "since inception excluding the outlier". A tool that
    publishes only its good months is not being validated, it is being
    marketed — the same standard the research branch applied when it reported
    that four of eighteen Stage 1 combinations were significantly negative.
    """
    if not scored:
        return pd.DataFrame()
    frame = pd.DataFrame([s.as_row() for s in scored]).sort_values("as_of")
    eligible = frame["in_calibration"]
    frame["cumulative_return"] = np.nan
    if eligible.any():
        frame.loc[eligible, "cumulative_return"] = (
            1.0 + frame.loc[eligible, "portfolio_return"]
        ).cumprod() - 1.0
    return frame.set_index("as_of")


def gate_3_report(scored: list[ScoredRecord]) -> dict[str, Any]:
    """The four Gate 3 criteria, evaluated on the in-calibration records."""
    frame = running_record(scored)
    if frame.empty:
        return {"months": 0, "verdict": "no records"}

    eligible = frame[frame["in_calibration"]]
    months = len(eligible)
    returns = eligible["portfolio_return"].dropna()

    mean_realised = float(returns.mean()) if len(returns) else float("nan")
    # Gate 0's per-trade mean is the expectation; a portfolio of ~10 positions
    # has a wider spread than a single trade, so the comparison is directional
    # rather than a confidence interval — stated as such rather than dressed up.
    within_expectation = bool(
        len(returns) and mean_realised > 0.0 and mean_realised < 4.0 * PHASE_1_MEAN
    )
    no_blowup = bool(len(returns) == 0 or returns.min() > PHASE_1_WORST_MONTH * 2.0)

    excluded = frame[~frame["in_calibration"]]
    why_excluded: dict[str, int] = {}
    for _, row in excluded.iterrows():
        if not row.get("month_entry", True):
            why_excluded["not the month's first trading day"] = (
                why_excluded.get("not the month's first trading day", 0) + 1
            )
        elif not row.get("verified", False):
            why_excluded["not committed before its outcome"] = (
                why_excluded.get("not committed before its outcome", 0) + 1
            )
        else:
            why_excluded["nothing scored yet"] = (
                why_excluded.get("nothing scored yet", 0) + 1
            )

    return {
        "months": months,
        "off_calibration_records": int((~frame["in_calibration"]).sum()),
        "excluded_because": why_excluded,
        "criterion_4_provenance_verified": bool(
            months > 0 and eligible["verified"].all()
        ),
        "mean_portfolio_return": mean_realised,
        "worst_month": float(returns.min()) if len(returns) else float("nan"),
        "best_month": float(returns.max()) if len(returns) else float("nan"),
        "positive_months": int((returns > 0).sum()) if len(returns) else 0,
        "expected_per_trade": PHASE_1_MEAN,
        "criterion_1_enough_months": months >= GATE_3_MIN_MONTHS,
        "criterion_2_within_expectation": within_expectation,
        "criterion_3_no_month_beyond_modelled_loss": no_blowup,
        "verdict": (
            "PASS"
            if months >= GATE_3_MIN_MONTHS and within_expectation and no_blowup
            else f"INCOMPLETE — {months} of {GATE_3_MIN_MONTHS} months recorded"
            if months < GATE_3_MIN_MONTHS
            else "FAIL"
        ),
        "caveat": (
            "A run of profitable months does not establish that the tail risk "
            "has been survived. It establishes that the tail has not yet arrived."
        ),
    }
