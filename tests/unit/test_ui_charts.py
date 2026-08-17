"""Unit tests for the control-panel styling/chart builders (no DB needed)."""

import pandas as pd
import pytest

from mktmtm.ui import charts, styles


def _mark_type(chart) -> str:
    """Altair v6 returns a string for arg-less marks, a dict otherwise."""
    mark = chart.mark
    return mark if isinstance(mark, str) else mark["type"]


# ── styles ───────────────────────────────────────────────────────────────

def test_pill_variants() -> None:
    assert "mkt-pill running" in styles.pill("running")
    assert "mkt-pill done" in styles.pill("done")
    assert "mkt-pill failed" in styles.pill("failed")
    assert "mkt-pill killed" in styles.pill("killed")
    assert "mkt-pill muted" in styles.pill("something-else")


def test_metric_card_tones() -> None:
    card = styles.metric_card("rows", "1,234", "sub text", "🗃️", "pos")
    assert "mkt-card" in card and "1,234" in card and "sub text" in card
    assert "value pos" in card
    neg = styles.metric_card("pnl", "-5.4", tone="neg")
    assert "value neg" in neg
    neutral = styles.metric_card("pnl", "0")
    assert 'class="value "' in neutral


def test_hero_and_section() -> None:
    assert "mkt-hero" in styles.hero("Title", "subtitle")
    assert "mkt-section" in styles.section("Heading")


def test_css_has_palette() -> None:
    assert styles.ACCENT in styles.CSS and styles.RED in styles.CSS
    assert "mkt-card" in styles.CSS and "mkt-pill" in styles.CSS


# ── charts ───────────────────────────────────────────────────────────────

def test_yearly_rows() -> None:
    df = pd.DataFrame({"year": [2020, 2021], "rows": [100, 200]})
    chart = charts.yearly_rows(df)
    assert _mark_type(chart) == "bar"
    assert chart.encoding.y.shorthand == "rows:Q"


def test_pnl_by_month_colors_by_sign() -> None:
    df = pd.DataFrame({"ym": ["2022-01", "2022-02"], "avg_pnl": [10.0, -5.0]})
    chart = charts.pnl_by_month(df)
    assert chart.encoding.color["condition"]["value"] == styles.GREEN
    assert chart.encoding.color["value"] == styles.RED


def test_pnl_hist() -> None:
    df = pd.DataFrame({"pnl_bin": ["a", "b"], "count": [3, 7]})
    assert _mark_type(charts.pnl_hist(df)) == "bar"


def test_leaderboard_sorted_ascending() -> None:
    df = pd.DataFrame(
        {"symbol": ["A", "B"], "months": [5, 5], "total_pnl": [10.0, -3.0]}
    )
    chart = charts.leaderboard(df, "Best symbols")
    assert chart.to_dict()["encoding"]["y"]["sort"] == "-x"


def test_skip_reasons_donut() -> None:
    df = pd.DataFrame({"reason": ["a", "b"], "n": [10, 5]})
    chart = charts.skip_reasons(df)
    assert _mark_type(chart) == "arc"
    assert chart.encoding.color["scale"]["domain"] == ["a", "b"]


def test_coverage_heatmap() -> None:
    df = pd.DataFrame({"year": [2022, 2022], "month": [1, 2], "n": [10, 20]})
    chart = charts.coverage_heatmap(df)
    assert _mark_type(chart) == "rect"


@pytest.mark.parametrize(
    "builder,df",
    [
        (charts.yearly_rows, pd.DataFrame({"year": [2020], "rows": [1]})),
        (charts.pnl_by_month, pd.DataFrame({"ym": ["2020-01"], "avg_pnl": [1.0]})),
        (charts.pnl_hist, pd.DataFrame({"pnl_bin": ["a"], "count": [1]})),
        (lambda d: charts.leaderboard(d, "Best symbols"),
         pd.DataFrame({"symbol": ["A"], "months": [1], "total_pnl": [1.0]})),
        (charts.skip_reasons, pd.DataFrame({"reason": ["a"], "n": [1]})),
        (charts.coverage_heatmap, pd.DataFrame({"year": [2020], "month": [1], "n": [1]})),
    ],
)
def test_charts_spec_serialisable(builder, df) -> None:
    """Every builder must produce a spec Altair/Streamlit can render."""
    spec = builder(df).to_dict()
    mark = spec["mark"]
    mark_type = mark if isinstance(mark, str) else mark["type"]
    assert mark_type in {"bar", "arc", "rect"}
