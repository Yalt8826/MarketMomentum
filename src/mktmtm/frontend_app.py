"""MktMtm Streamlit control panel.

Sidebar navigation with four pages: Dashboard (DB summary), Pipeline
(runs the CLI pipeline steps as background jobs, see ``mktmtm.ui.jobs``),
Data Explorer (schema/samples/JSONB + SQL box) and Analytics (backtest
P&L, leaderboard, skip reasons, coverage). DB reads go through
``mktmtm.ui.queries``. Styling/charts come from ``mktmtm.ui.styles`` and
``mktmtm.ui.charts`` (dark trading-terminal theme, no extra deps).
"""

from __future__ import annotations

import time
from datetime import date, datetime

import pandas as pd
import psycopg2
import streamlit as st
from dateutil.relativedelta import relativedelta

from mktmtm.config.settings import settings
from mktmtm.ui import charts, queries, styles
from mktmtm.ui.jobs import job_command, kill_job, list_jobs, prune, start_job, tail_log

st.set_page_config(page_title="MktMtm", layout="wide", page_icon="📈")

# ── theme + shared helpers ───────────────────────────────────────────────

st.markdown(styles.CSS, unsafe_allow_html=True)


def _html(html: str) -> None:
    st.markdown(html, unsafe_allow_html=True)


def _hero(title: str, subtitle: str) -> None:
    _html(styles.hero(title, subtitle))


def _section(title: str) -> None:
    _html(styles.section(title))


def db_connect() -> psycopg2.extensions.connection:
    """Open a psycopg2 connection from the app settings."""
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_db,
        user=settings.pg_user,
        password=settings.pg_password,
        sslmode=settings.pg_sslmode,
    )


def _query(sql: str) -> list[tuple]:
    """Run a read-only query and return all rows."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _clean_csv(text: str) -> str | None:
    """Normalise a comma-separated text input; ``None`` when empty."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    return ",".join(parts) if parts else None


@st.cache_resource
def _cached_conn() -> psycopg2.extensions.connection:
    """One shared read connection for the Explorer/Analytics pages."""
    return queries.connect()


def _sidebar() -> None:
    """Branded sidebar block (navigation radio is rendered at module level)."""
    with st.sidebar:
        _html(
            '<div style="padding:0.2rem 0 0.9rem 0; border-bottom:1px solid '
            f'{styles.BORDER}; margin-bottom:0.9rem;">'
            '<div style="font-size:1.25rem; font-weight:800; letter-spacing:-0.02em;">'
            f'<span style="color:{styles.ACCENT};">◆</span> MktMtm</div>'
            '<div style="color:' + styles.MUTED + '; font-size:0.75rem; '
            'letter-spacing:0.08em; text-transform:uppercase;">Market Momentum</div>'
            "</div>"
        )
        st.divider()
        _html(
            f'<div style="color:{styles.MUTED}; font-size:0.72rem; line-height:1.7;">'
            f"db: <code>{settings.pg_db}</code><br/>"
            f"data: <code>{settings.data_dir.name}</code></div>"
        )


# ── Dashboard ────────────────────────────────────────────────────────────

def dashboard_page() -> None:
    _hero(
        "MktMtm Control Panel",
        f"{settings.pg_db} · {settings.pg_host}:{settings.pg_port} · snapshot {date.today().isoformat()}",
    )
    try:
        om_rows = _query("SELECT count(*) FROM option_metrics;")[0][0]
        symbols, min_date, max_date = _query(
            "SELECT count(DISTINCT symbol), min(date), max(date) FROM option_metrics;"
        )[0]
        bt_rows = _query("SELECT count(*) FROM backtest_results;")[0][0]
        traded, skipped = _query(
            "SELECT count(*) FILTER (WHERE pnl_points IS NOT NULL),"
            "       count(*) FILTER (WHERE pnl_points IS NULL)"
            " FROM backtest_results;"
        )[0]
    except Exception as exc:
        st.error(f"Could not load dashboard data: {exc}")
        return

    cards = [
        ("option_metrics rows", f"{om_rows:,}", "186k+ enriched days", "🗃️"),
        ("symbols covered", f"{symbols:,}", "F&O catalog", "📊"),
        ("data range", f"{min_date} → {max_date}", "2020 → 2026", "📅"),
        ("backtest_results rows", f"{bt_rows:,}", "symbol-months", "🧪"),
        ("traded months", f"{traded:,}", "with PNL", "✅"),
        ("skipped months", f"{skipped:,}", "recorded reasons", "⏭️"),
    ]
    cols = st.columns(3)
    for i, (label, value, sub, icon) in enumerate(cards):
        with cols[i % 3]:
            _html(styles.metric_card(label, value, sub, icon))

    yearly = pd.DataFrame(
        _query(
            "SELECT extract(year FROM date)::int AS year, count(*) AS rows"
            " FROM option_metrics GROUP BY 1 ORDER BY 1;"
        ),
        columns=["year", "rows"],
    )
    st.altair_chart(charts.yearly_rows(yearly), width="stretch")

    _section("Recent pipeline jobs")
    jobs = list_jobs(5)
    if jobs:
        for job in jobs:
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"**{job.name}** · `{job.id}`")
            _html(styles.pill(job.status))
    else:
        st.caption("No jobs yet — head to the Pipeline page to start one.")


# ── Pipeline ─────────────────────────────────────────────────────────────

def _start_job(step: str, **params: object) -> None:
    start_job(step, job_command(step, **params), cwd=settings.data_dir.parent)
    prune(keep=20)
    st.rerun()


def pipeline_page() -> None:
    _hero(
        "Pipeline",
        "Run the MktMtm pipeline steps as background jobs — downloads, processing, "
        "ETL and backtest. Jobs survive UI restarts; logs are on disk.",
    )

    with st.expander("📥 fetch-bhavcopy — NSE F&O bhavcopy download", expanded=False):
        c1, c2 = st.columns(2)
        start_date = c1.date_input(
            "start_date", value=date.today() - relativedelta(months=3)
        )
        end_date = c2.date_input("end_date", value=date.today())
        max_workers = st.number_input("max_workers", 1, 16, 4, step=1)
        if st.button("Run fetch-bhavcopy", key="run-fetch-bhavcopy", type="primary"):
            _start_job(
                "fetch-bhavcopy",
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                max_workers=int(max_workers),
            )

    with st.expander("🟢 fetch-yahoo — spot closes from Yahoo Finance"):
        days = st.number_input("days", min_value=1, value=200, step=1)
        if st.button("Run fetch-yahoo", key="run-fetch-yahoo", type="primary"):
            _start_job("fetch-yahoo", days=int(days))

    with st.expander("⚙️ process — enrich bhavcopy into processed JSON"):
        limit_days = st.number_input(
            "limit_days (0 = full run)", min_value=0, value=0, step=1
        )
        if st.button("Run process", key="run-process", type="primary"):
            _start_job("process", limit_days=int(limit_days) or None)

    with st.expander("🗄️ etl — upsert processed data into PostgreSQL"):
        st.caption("Reads all bhavcopy files in data/raw/bhavcopy/extracted.")
        if st.button("Run etl", key="run-etl", type="primary"):
            _start_job("etl")

    with st.expander("🎯 backtest — short-strangle with delta hedging"):
        symbols = st.text_input("symbols (comma-separated; empty = all)")
        years = st.text_input("years (e.g. 2023,2024; empty = all)")
        months = st.text_input("months (e.g. 1,2; empty = all)")
        if st.button("Run backtest", key="run-backtest", type="primary"):
            _start_job(
                "backtest",
                symbols=_clean_csv(symbols),
                years=_clean_csv(years),
                months=_clean_csv(months),
            )

    _section("Running jobs")

    @st.fragment(run_every=2.0)
    def running_jobs_panel() -> None:
        running = [job for job in list_jobs(20) if job.is_running]
        if not running:
            st.caption("No running jobs.")
            return
        for job in running:
            with st.container(border=True):
                c1, c2, c3 = st.columns([4, 1, 1])
                c1.markdown(f"**{job.name}** — `{job.id}`")
                _html(styles.pill("running"))
                c2.metric("elapsed", f"{int(time.time() - job.started_at)}s")
                if c3.button("Kill", key=f"kill-{job.id}"):
                    kill_job(job.id)
                    st.rerun()
                st.code(tail_log(job.id, n=50))

    running_jobs_panel()

    _section("Recent jobs")
    jobs = list_jobs(20)
    if not jobs:
        st.caption("No jobs yet — start one above.")
        return
    recent = pd.DataFrame(
        [
            {
                "name": job.name,
                "status": job.status,
                "exit_code": job.exit_code,
                "started": _fmt_ts(job.started_at),
                "finished": _fmt_ts(job.finished_at),
            }
            for job in jobs
        ]
    )
    recent["exit_code"] = recent["exit_code"].astype("Int64")  # nullable int → Arrow-safe
    st.dataframe(
        recent,
        column_config={
            "name": st.column_config.TextColumn("job"),
            "status": st.column_config.TextColumn("status"),
            "exit_code": st.column_config.NumberColumn("exit", format="%d"),
            "started": st.column_config.TextColumn("started"),
            "finished": st.column_config.TextColumn("finished"),
        },
        width="stretch",
        hide_index=True,
    )
    for job in jobs:
        with st.expander(f"log: {job.id}"):
            st.code(tail_log(job.id, n=500))


# ── Data Explorer ────────────────────────────────────────────────────────

def explorer_page() -> None:
    _hero("Data Explorer", "Inspect schema, samples, JSONB payloads — or run your own SQL.")
    try:
        conn = _cached_conn()
    except Exception as exc:
        st.error(f"DB not reachable: {exc}")
        return

    table = st.selectbox("Table", list(queries.BROWSE_TABLES))
    info = queries.table_info(conn, table)
    table_status = queries.db_status(conn).get(table, {})

    _section("Schema")
    schema = pd.DataFrame(info)
    st.dataframe(
        schema,
        column_config={
            "name": st.column_config.TextColumn("column"),
            "type": st.column_config.TextColumn("type"),
            "nullable": st.column_config.CheckboxColumn("nullable"),
            "default": st.column_config.TextColumn("default"),
        },
        width="stretch",
        hide_index=True,
    )
    m1, m2, m3 = st.columns(3)
    _html(styles.metric_card("row count", f"{table_status.get('rows', 0):,}", "", "📦"))
    _html(styles.metric_card("size", table_status.get("size", "—"), "", "💾"))
    _html(styles.metric_card("columns", f"{len(info)}", "", "🧬"))

    _section("Sample rows")
    st.dataframe(queries.sample_rows(conn, table, n=50), width="stretch")

    jsonb_cols = [col["name"] for col in info if col["type"] == "jsonb"]
    if jsonb_cols:
        _section("JSONB viewer")
        json_col = st.selectbox("JSONB column", jsonb_cols, key="explorer-jsonb-col")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {json_col} FROM {table} "
                    f"WHERE {json_col} IS NOT NULL LIMIT 1"
                )
                first = cur.fetchone()
            st.json(first[0] if first else None)
        except Exception as exc:
            st.error(f"Could not load JSONB sample: {exc}")

    _section("SQL")
    st.caption("Read-only — run the report's appendix queries or your own.")
    sql = st.text_area(
        "SQL query",
        value="SELECT * FROM option_metrics LIMIT 10;",
        height=120,
        key="explorer-sql",
    )
    if st.button("Run", key="explorer-run-sql", type="primary"):
        try:
            st.session_state["explorer_sql_result"] = queries.run_sql(conn, sql)
            st.session_state["explorer_sql_error"] = None
        except ValueError as exc:
            st.session_state["explorer_sql_error"] = str(exc)
            st.session_state["explorer_sql_result"] = None
    if st.session_state.get("explorer_sql_error"):
        st.error(st.session_state["explorer_sql_error"])
    result = st.session_state.get("explorer_sql_result")
    if result is not None:
        st.dataframe(result, width="stretch")
        st.download_button(
            "Download CSV",
            result.to_csv(index=False).encode("utf-8"),
            file_name="query_result.csv",
            mime="text/csv",
        )


# ── Analytics ────────────────────────────────────────────────────────────

def analytics_page() -> None:
    _hero(
        "Analytics",
        "Backtest results: P&L by month, distribution, symbol leaderboard, "
        "skip reasons and data coverage.",
    )
    try:
        conn = _cached_conn()
    except Exception as exc:
        st.error(f"DB not reachable: {exc}")
        return

    symbols = queries.run_sql(
        conn, "SELECT DISTINCT symbol FROM backtest_results ORDER BY symbol"
    )["symbol"].tolist()
    if not symbols:
        st.warning("backtest_results is empty — nothing to analyse.")
        return
    f1, f2 = st.columns([1, 3])
    year_range = f1.slider("Years", 2022, 2026, (2022, 2026))
    selected_symbols = f2.multiselect("Symbols", symbols, default=symbols)
    years = range(year_range[0], year_range[1] + 1)

    pnl = queries.run_sql(
        conn,
        "SELECT symbol, year, month, pnl_points FROM backtest_results "
        "WHERE pnl_points IS NOT NULL",
    )
    pnl = pnl[pnl["year"].isin(years) & pnl["symbol"].isin(selected_symbols)]
    if pnl.empty:
        st.warning("No traded months match the current filters.")
        return

    cards = [
        ("traded months", f"{len(pnl):,}", "", "🧪"),
        ("avg PNL", f"{pnl['pnl_points'].mean():.2f}",
         "points / symbol-month", "⚖️",
         "pos" if pnl["pnl_points"].mean() >= 0 else "neg"),
        ("best month", f"{pnl['pnl_points'].max():.2f}", "", "🟢", "pos"),
        ("worst month", f"{pnl['pnl_points'].min():.2f}", "", "🔴", "neg"),
    ]
    cols = st.columns(4)
    for i, (label, value, sub, icon, *tone) in enumerate(cards):
        with cols[i]:
            _html(styles.metric_card(label, value, sub, icon, tone[0] if tone else "neutral"))

    monthly = (
        pnl.groupby(["year", "month"], as_index=False)["pnl_points"]
        .mean()
        .sort_values(["year", "month"])
    )
    monthly["ym"] = monthly.apply(
        lambda r: f"{int(r['year'])}-{int(r['month']):02d}", axis=1
    )
    monthly = monthly.rename(columns={"pnl_points": "avg_pnl"})
    st.altair_chart(charts.pnl_by_month(monthly), width="stretch")

    _section("PNL distribution")
    hist = pd.cut(pnl["pnl_points"], bins=20).value_counts().sort_index()
    hist_df = hist.rename_axis("pnl_bin").reset_index(name="count")
    hist_df["pnl_bin"] = hist_df["pnl_bin"].astype(str)
    st.altair_chart(charts.pnl_hist(hist_df), width="stretch")

    _section("Symbol leaderboard")
    top = queries.symbol_leaderboard(conn, limit=10)
    top["total_pnl"] = top["total_pnl"].astype(float)
    bottom = queries.run_sql(
        conn,
        "SELECT symbol, count(*) AS months, "
        "round(sum(pnl_points)::numeric, 2) AS total_pnl "
        "FROM backtest_results WHERE pnl_points IS NOT NULL "
        "GROUP BY symbol ORDER BY total_pnl ASC LIMIT 10",
    )
    bottom["total_pnl"] = bottom["total_pnl"].astype(float)
    l1, l2 = st.columns(2)
    with l1:
        st.altair_chart(charts.leaderboard(top, "Best symbols"), width="stretch")
        st.dataframe(top, width="stretch", hide_index=True)
    with l2:
        st.altair_chart(charts.leaderboard(bottom, "Worst symbols"), width="stretch")
        st.dataframe(bottom, width="stretch", hide_index=True)

    _section("Skip reasons")
    reasons = queries.skip_reasons(conn)
    st.altair_chart(charts.skip_reasons(reasons), width="stretch")
    st.dataframe(reasons, width="stretch", hide_index=True)

    _section("Coverage — option_metrics rows per year × month")
    cov = queries.coverage_year_month(conn)
    cov = cov[cov["year"].isin(years)]
    if cov.empty:
        st.caption("No coverage data in the selected year range.")
    else:
        st.altair_chart(charts.coverage_heatmap(cov), width="stretch")


# ── entry ────────────────────────────────────────────────────────────────

try:
    _query("SELECT 1")
    st.sidebar.success("connected")
except Exception as exc:
    st.sidebar.error(f"DB not reachable: {exc}")

_sidebar()
page = st.sidebar.radio(
    "Navigation",
    ["Dashboard", "Pipeline", "Data Explorer", "Analytics"],
    label_visibility="collapsed",
)
{
    "Dashboard": dashboard_page,
    "Pipeline": pipeline_page,
    "Data Explorer": explorer_page,
    "Analytics": analytics_page,
}[page]()
