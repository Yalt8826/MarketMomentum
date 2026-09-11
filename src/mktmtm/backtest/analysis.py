"""Portfolio analysis for the short-strangle backtest — Gate 0's report.

An average is not enough here. A premium-selling book wins most months by
construction and keeps all of its risk in the left tail, so a mean return with
no drawdown beside it describes the strategy's comfortable half and hides the
half that decides whether it is usable.

Every function is a pure transformation of the ``backtest_results`` frame; the
only I/O is :func:`load_results`.

Gate 0 (``docs/trading/implementation/03-phase-0-economics.md``) is two pass/fail
criteria plus four **mandatory disclosures**, and this module produces all six:

===========================================  =========================
mean return on margin, net, > 0               :func:`summarize`
positive in >= 4 of 5 years                   :func:`by_year`
verdict stable across margin +/-25%           :func:`margin_sensitivity`
breakeven slippage stated                     :func:`breakeven_slippage`
max drawdown stated                           :func:`summarize`
skip taxonomy and zero-volume share stated    :func:`skip_taxonomy`, :func:`liquidity`
===========================================  =========================
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "breakeven_slippage",
    "by_year",
    "cost_composition",
    "equity_curve",
    "gate_0_report",
    "liquidity",
    "load_results",
    "margin_sensitivity",
    "max_drawdown",
    "monthly_portfolio",
    "skip_taxonomy",
    "summarize",
]

_RESULTS_SQL = """
SELECT variant, symbol, year, month, entry_date, exit_date,
       pnl_points, skipped_reason,
       lot_size, n_orders, n_adjustments, entry_spot,
       gross_pnl_rupees, costs_rupees, slippage_rupees, net_pnl_rupees,
       margin_rupees, return_on_margin, cost_breakdown,
       rates_effective_from, slippage_fraction, margin_pct,
       call_entry_volume, put_entry_volume
FROM backtest_results
WHERE variant = %(variant)s
ORDER BY symbol, year, month
"""


def load_results(conn: Any, variant: str = "base") -> pd.DataFrame:
    """Every row for one variant, skipped months included.

    Skips are kept deliberately: the share of months a strategy *cannot* trade
    is part of its description, and dropping them here would make the skip
    taxonomy unreportable.
    """
    frame = pd.read_sql(_RESULTS_SQL, conn, params={"variant": variant})
    if frame.empty:
        raise ValueError(f"no backtest_results rows for variant {variant!r}")
    return frame


def _traded(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows that produced a priced trade."""
    return frame.loc[frame["return_on_margin"].notna()]


# ═══════════════════════════ the disclosures ═══════════════════════════════


def skip_taxonomy(frame: pd.DataFrame) -> pd.DataFrame:
    """Why months did not trade, most common first.

    A strategy that trades 40% of the months it wants to is a different
    proposition from one that trades 90%, and the reasons say which parts of the
    pipeline are load-bearing.
    """
    total = len(frame)
    reasons = frame["skipped_reason"].fillna("(traded)")
    counts = reasons.value_counts()
    return pd.DataFrame(
        {"months": counts, "share": counts / total if total else counts}
    ).rename_axis("reason")


def liquidity(frame: pd.DataFrame) -> dict[str, float]:
    """How plausible the settlement-price fill assumption is.

    If a large share of selected 20-delta strikes never traded, the fill is
    fiction whatever the slippage parameter says — and that is a finding about
    the backtest, not a parameter to tune.
    """
    traded = _traded(frame)
    if traded.empty:
        return {}
    volumes = pd.concat(
        [traded["call_entry_volume"], traded["put_entry_volume"]]
    ).astype("float64")
    volumes = volumes.dropna()
    if volumes.empty:
        return {"legs": 0.0}
    return {
        "legs": float(len(volumes)),
        "zero_volume_share": float((volumes <= 0).mean()),
        "median_volume": float(volumes.median()),
        "p10_volume": float(volumes.quantile(0.10)),
    }


def cost_composition(frame: pd.DataFrame) -> pd.DataFrame:
    """Mean rupee cost per component. Answers "which component killed it"."""
    traded = _traded(frame)
    rows = [b for b in traded["cost_breakdown"] if isinstance(b, dict)]
    if not rows:
        return pd.DataFrame()
    costs = pd.DataFrame(rows)
    components = [c for c in costs.columns if c not in ("total", "n_orders")]
    mean = costs[components].mean().sort_values(ascending=False)
    return pd.DataFrame(
        {"mean_rupees": mean, "share_of_total": mean / mean.sum()}
    ).rename_axis("component")


# ═══════════════════════════ portfolio view ════════════════════════════════


def monthly_portfolio(frame: pd.DataFrame) -> pd.DataFrame:
    """Equal-margin-weighted portfolio return per calendar month.

    Trades are pooled by month rather than averaged across the whole sample:
    symbols sharing a month share that month's market, so the month is the
    independent unit. This is the same reasoning the research branch used when
    it bootstrapped over periods rather than rows.
    """
    traded = _traded(frame)
    if traded.empty:
        return pd.DataFrame()
    grouped = traded.groupby(["year", "month"], sort=True)
    out = pd.DataFrame(
        {
            "trades": grouped.size(),
            "net_rupees": grouped["net_pnl_rupees"].sum(),
            "margin_rupees": grouped["margin_rupees"].sum(),
        }
    )
    out["return_on_margin"] = out["net_rupees"] / out["margin_rupees"]
    out.index = pd.PeriodIndex(
        [f"{y}-{m:02d}" for y, m in out.index], freq="M"
    ).rename("period")
    return out


def equity_curve(monthly: pd.DataFrame) -> pd.Series:
    """Compounded growth of one unit of capital, month by month."""
    if monthly.empty:
        return pd.Series(dtype="float64")
    return (1.0 + monthly["return_on_margin"]).cumprod().rename("equity")


def max_drawdown(equity: pd.Series) -> float:
    """Worst peak-to-trough fall in the equity curve, as a fraction.

    The number that decides whether a strategy is usable, as distinct from
    whether its average is positive.
    """
    if equity.empty:
        return 0.0
    running_peak = equity.cummax()
    return float((equity / running_peak - 1.0).min())


def summarize(frame: pd.DataFrame) -> dict[str, float]:
    """Headline statistics, per trade and per month."""
    traded = _traded(frame)
    if traded.empty:
        return {}
    returns = traded["return_on_margin"].astype("float64")
    wins = returns[returns > 0]
    losses = returns[returns <= 0]

    monthly = monthly_portfolio(frame)
    equity = equity_curve(monthly)
    monthly_returns = monthly["return_on_margin"] if not monthly.empty else pd.Series(
        dtype="float64"
    )

    sharpe = 0.0
    if len(monthly_returns) > 1:
        std = float(monthly_returns.std(ddof=1))
        if std > 0:
            sharpe = float(monthly_returns.mean()) / std * np.sqrt(12.0)

    return {
        "trades": float(len(traded)),
        "months": float(len(monthly)),
        "mean_return_on_margin": float(returns.mean()),
        "median_return_on_margin": float(returns.median()),
        "win_rate": float((returns > 0).mean()),
        "mean_win": float(wins.mean()) if len(wins) else 0.0,
        "mean_loss": float(losses.mean()) if len(losses) else 0.0,
        "p1_return": float(returns.quantile(0.01)),
        "p5_return": float(returns.quantile(0.05)),
        "worst_trade": float(returns.min()),
        "mean_monthly_return": float(monthly_returns.mean())
        if len(monthly_returns)
        else 0.0,
        "worst_month": float(monthly_returns.min()) if len(monthly_returns) else 0.0,
        "sharpe_annualised": sharpe,
        "max_drawdown": max_drawdown(equity),
        "total_net_rupees": float(traded["net_pnl_rupees"].sum()),
        "total_costs_rupees": float(traded["costs_rupees"].sum()),
    }


def by_year(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-year mean return on margin — Gate 0 criterion 2."""
    traded = _traded(frame)
    if traded.empty:
        return pd.DataFrame()
    grouped = traded.groupby("year", sort=True)
    return pd.DataFrame(
        {
            "trades": grouped.size(),
            "mean_return_on_margin": grouped["return_on_margin"].mean(),
            "net_rupees": grouped["net_pnl_rupees"].sum(),
            "win_rate": grouped["return_on_margin"].apply(lambda s: (s > 0).mean()),
        }
    )


# ═══════════════════════════ sensitivities ═════════════════════════════════


def margin_sensitivity(
    frame: pd.DataFrame, factors: tuple[float, ...] = (0.75, 1.0, 1.25)
) -> pd.DataFrame:
    """Mean return under a scaled margin assumption.

    Margin only scales the denominator, so it cannot turn a loss into a profit —
    but it decides whether a profit is interesting. If the verdict flips inside
    this band, the verdict is the assumption rather than the strategy.
    """
    traded = _traded(frame)
    if traded.empty:
        return pd.DataFrame()
    rows = []
    for factor in factors:
        scaled = traded["net_pnl_rupees"] / (traded["margin_rupees"] * factor)
        rows.append(
            {
                "margin_factor": factor,
                "mean_return_on_margin": float(scaled.mean()),
                "positive": bool(scaled.mean() > 0),
            }
        )
    return pd.DataFrame(rows).set_index("margin_factor")


def breakeven_slippage(frame: pd.DataFrame) -> dict[str, float]:
    """Per-leg slippage fraction at which the edge reaches zero.

    Reported instead of a point estimate because bhavcopy publishes no bid/ask.
    "It earns X% and breaks even at S% slippage" survives not knowing the true
    spread; "it earns X%" does not.

    Slippage costs a fraction of every premium touched, so total drag is
    approximately ``fraction * sum(premiums across all orders)``. Premium
    turnover is recovered from the trade's own STT — the only per-premium charge
    on record — rather than re-deriving leg prices.
    """
    traded = _traded(frame)
    if traded.empty:
        return {}

    net_total = float(traded["net_pnl_rupees"].sum())
    if net_total <= 0:
        return {"net_rupees": net_total, "breakeven_fraction": 0.0}

    # Sell-side premium turnover, from STT actually charged.
    turnover = 0.0
    for breakdown, stt_rate in zip(
        traded["cost_breakdown"], traded["rates_effective_from"], strict=False
    ):
        if isinstance(breakdown, dict):
            turnover += float(breakdown.get("stt", 0.0))
    # STT is levied at ~0.05-0.1% depending on regime; invert at the mean rate
    # actually applied to recover sell-side turnover, then double it to cover
    # the buy side (each leg is sold once and bought back once).
    mean_stt_rate = _mean_stt_rate(traded)
    sell_turnover = turnover / mean_stt_rate if mean_stt_rate > 0 else 0.0
    total_premium_touched = sell_turnover * 2.0

    fraction = net_total / total_premium_touched if total_premium_touched > 0 else 0.0
    return {
        "net_rupees": net_total,
        "premium_turnover_rupees": total_premium_touched,
        "breakeven_fraction": fraction,
    }


def _mean_stt_rate(traded: pd.DataFrame) -> float:
    """Mean STT rate actually applied, weighted by trade count."""
    from mktmtm.backtest.economics import DEFAULT_RATE_SETS

    by_date = {r.effective_from: r.stt_sell_pct for r in DEFAULT_RATE_SETS}
    rates = traded["rates_effective_from"].map(by_date).dropna()
    return float(rates.mean()) if len(rates) else 0.0


# ═══════════════════════════ the report ════════════════════════════════════


def gate_0_report(frame: pd.DataFrame) -> dict[str, Any]:
    """Everything Gate 0 requires, assembled.

    Returns the two pass/fail criteria alongside the four mandatory
    disclosures. A verdict without the disclosures is incomplete even when the
    strategy wins.
    """
    stats = summarize(frame)
    years = by_year(frame)
    positive_years = (
        int((years["mean_return_on_margin"] > 0).sum()) if not years.empty else 0
    )
    return {
        "summary": stats,
        "by_year": years,
        "positive_years": positive_years,
        "total_years": int(len(years)),
        "margin_sensitivity": margin_sensitivity(frame),
        "breakeven_slippage": breakeven_slippage(frame),
        "skip_taxonomy": skip_taxonomy(frame),
        "liquidity": liquidity(frame),
        "cost_composition": cost_composition(frame),
        "monthly": monthly_portfolio(frame),
        # Criterion 1: mean net return on margin > 0.
        "criterion_1_pass": bool(stats.get("mean_return_on_margin", 0.0) > 0),
        # Criterion 2: positive in a majority of years.
        "criterion_2_pass": bool(
            len(years) > 0 and positive_years >= max(1, int(np.ceil(len(years) * 0.8)))
        ),
    }
