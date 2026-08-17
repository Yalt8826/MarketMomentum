"""Altair chart builders for the MktMtm control panel (dark theme).

Pure functions: each takes a DataFrame and returns an ``altair.Chart``.
Altair ships with Streamlit, so no new dependencies are required.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

from mktmtm.ui import styles

_GREEN = styles.GREEN
_RED = styles.RED
_ACCENT = styles.ACCENT
_MUTED = styles.MUTED
_TEXT = styles.TEXT
_GRID = styles.BORDER
_CARD_BG = styles.CARD_BG


def _theme(chart: alt.Chart) -> alt.Chart:
    """Apply the shared dark-theme look to any chart."""
    return (
        chart.configure(background="transparent")
        .configure_view(strokeWidth=0)
        .configure_axis(
            gridColor=_GRID,
            gridOpacity=0.7,
            domainColor=_GRID,
            tickColor=_GRID,
            labelColor=_MUTED,
            titleColor=_TEXT,
            labelFontSize=11,
            titleFontSize=12,
        )
        .configure_legend(
            labelColor=_MUTED, titleColor=_TEXT, strokeColor=_GRID,
            labelFontSize=11, titleFontSize=12,
        )
        .configure_title(color=_TEXT, fontSize=15, anchor="start", dy=-6)
    )


def yearly_rows(df: pd.DataFrame) -> alt.Chart:
    """option_metrics rows per year — accent bars."""
    return _theme(
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=26)
        .encode(
            x=alt.X("year:N", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("rows:Q", title="rows"),
            color=alt.condition(
                alt.datum.rows > 0, alt.value(_ACCENT), alt.value(_CARD_BG)
            ),
            tooltip=[
                alt.Tooltip("year:N", title="year"),
                alt.Tooltip("rows:Q", title="rows", format=","),
            ],
        )
        .properties(title="option_metrics rows per year", height=280)
    )


def pnl_by_month(df: pd.DataFrame) -> alt.Chart:
    """Avg PNL per year-month, green/red by sign."""
    return _theme(
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=14)
        .encode(
            x=alt.X("ym:N", title=None, axis=alt.Axis(labelAngle=45, labelLimit=60)),
            y=alt.Y("avg_pnl:Q", title="avg PNL (points)"),
            color=alt.condition(
                alt.datum.avg_pnl >= 0, alt.value(_GREEN), alt.value(_RED)
            ),
            tooltip=[
                alt.Tooltip("ym:N", title="month"),
                alt.Tooltip("avg_pnl:Q", title="avg PNL", format=".2f"),
            ],
        )
        .properties(title="Average PNL by month", height=300)
    )


def pnl_hist(df: pd.DataFrame) -> alt.Chart:
    """Distribution of per-symbol-month PNL."""
    return _theme(
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("pnl_bin:N", title="PNL bucket", axis=alt.Axis(labelAngle=45, labelLimit=70)),
            y=alt.Y("count:Q", title="months"),
            color=alt.value(styles.ACCENT_DIM),
            tooltip=[
                alt.Tooltip("pnl_bin:N", title="bucket"),
                alt.Tooltip("count:Q", title="months"),
            ],
        )
        .properties(title="PNL distribution", height=300)
    )


def leaderboard(df: pd.DataFrame, title: str, limit: int = 10) -> alt.Chart:
    """Horizontal bar of cumulative PNL per symbol, coloured by sign."""
    ordered = df.sort_values("total_pnl", ascending=True)
    return _theme(
        alt.Chart(ordered)
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, size=16)
        .encode(
            x=alt.X("total_pnl:Q", title="total PNL (points)"),
            y=alt.Y("symbol:N", title=None, sort="-x"),
            color=alt.condition(
                alt.datum.total_pnl >= 0, alt.value(_GREEN), alt.value(_RED)
            ),
            tooltip=[
                alt.Tooltip("symbol:N"),
                alt.Tooltip("months:Q", title="months", format=","),
                alt.Tooltip("total_pnl:Q", title="total PNL", format=".2f"),
            ],
        )
        .properties(title=f"{title} (top {limit})", height=320)
    )


def skip_reasons(df: pd.DataFrame) -> alt.Chart:
    """Donut of backtest skip reasons."""
    return _theme(
        alt.Chart(df)
        .mark_arc(innerRadius=52, outerRadius=95, stroke=styles.BG, strokeWidth=2)
        .encode(
            theta=alt.Theta("n:Q", stack=True),
            color=alt.Color(
                "reason:N",
                scale=alt.Scale(
                    scheme="tableau10",
                    domain=list(df["reason"]),
                ),
                legend=alt.Legend(title=None, orient="right"),
            ),
            tooltip=[
                alt.Tooltip("reason:N"),
                alt.Tooltip("n:Q", title="rows", format=","),
            ],
        )
        .properties(title="Skip reasons", height=300)
    )


def coverage_heatmap(df: pd.DataFrame) -> alt.Chart:
    """option_metrics rows per year × month heatmap."""
    return _theme(
        alt.Chart(df)
        .mark_rect()
        .encode(
            x=alt.X("month:O", title="month", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("year:O", title=None),
            color=alt.Color(
                "n:Q",
                title="rows",
                scale=alt.Scale(scheme="teals"),
                legend=alt.Legend(orient="right"),
            ),
            tooltip=[
                alt.Tooltip("year:O"),
                alt.Tooltip("month:O"),
                alt.Tooltip("n:Q", title="rows", format=","),
            ],
        )
        .properties(title="option_metrics coverage — rows per year × month", height=260)
    )
