"""MktMtm Streamlit control panel — dashboard and pipeline job runner.

Phase 3: sidebar navigation with a live Dashboard (DB summary) and a
Pipeline page that runs the CLI pipeline steps as background jobs
(see ``mktmtm.ui.jobs``). Data Explorer and Analytics arrive in Phase 4.
"""

from __future__ import annotations

import time
from datetime import date, datetime

import pandas as pd
import psycopg2
import streamlit as st
from dateutil.relativedelta import relativedelta

from mktmtm.config.settings import settings
from mktmtm.ui.jobs import job_command, kill_job, list_jobs, prune, start_job, tail_log

st.set_page_config(page_title="MktMtm", layout="wide", page_icon="📈")


def db_connect() -> psycopg2.extensions.connection:
    """Open a psycopg2 connection from the app settings.

    Phase 4 will formalise query helpers in ``mktmtm.ui.queries``; for now
    all dashboard reads go through this local helper.
    """
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


def dashboard_page() -> None:
    st.header("Dashboard")
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

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("option_metrics rows", f"{om_rows:,}")
    m2.metric("symbols", f"{symbols:,}")
    m3.metric("date range", f"{min_date} → {max_date}")
    m4.metric("backtest_results rows", f"{bt_rows:,}")
    m5.metric("traded months", f"{traded:,}")
    m6.metric("skipped months", f"{skipped:,}")

    yearly = pd.DataFrame(
        _query(
            "SELECT extract(year FROM date)::int AS year, count(*) AS rows"
            " FROM option_metrics GROUP BY 1 ORDER BY 1;"
        ),
        columns=["year", "rows"],
    ).set_index("year")
    st.subheader("option_metrics rows per year")
    st.bar_chart(yearly)


def _start_job(step: str, **params: object) -> None:
    start_job(step, job_command(step, **params), cwd=settings.data_dir.parent)
    prune(keep=20)
    st.rerun()


def pipeline_page() -> None:
    st.header("Pipeline")

    with st.expander("fetch-bhavcopy", expanded=True):
        c1, c2 = st.columns(2)
        start_date = c1.date_input(
            "start_date", value=date.today() - relativedelta(months=3)
        )
        end_date = c2.date_input("end_date", value=date.today())
        max_workers = st.number_input("max_workers", 1, 16, 4, step=1)
        if st.button("Run fetch-bhavcopy", key="run-fetch-bhavcopy"):
            _start_job(
                "fetch-bhavcopy",
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                max_workers=int(max_workers),
            )

    with st.expander("fetch-yahoo"):
        days = st.number_input("days", min_value=1, value=200, step=1)
        if st.button("Run fetch-yahoo", key="run-fetch-yahoo"):
            _start_job("fetch-yahoo", days=int(days))

    with st.expander("process"):
        limit_days = st.number_input(
            "limit_days (0 = full run)", min_value=0, value=0, step=1
        )
        if st.button("Run process", key="run-process"):
            _start_job("process", limit_days=int(limit_days) or None)

    with st.expander("etl"):
        if st.button("Run etl", key="run-etl"):
            _start_job("etl")

    with st.expander("backtest"):
        symbols = st.text_input("symbols (comma-separated; empty = all)")
        years = st.text_input("years (e.g. 2023,2024; empty = all)")
        months = st.text_input("months (e.g. 1,2; empty = all)")
        if st.button("Run backtest", key="run-backtest"):
            _start_job(
                "backtest",
                symbols=_clean_csv(symbols),
                years=_clean_csv(years),
                months=_clean_csv(months),
            )

    st.subheader("Running jobs")

    @st.fragment(run_every=2.0)
    def running_jobs_panel() -> None:
        running = [job for job in list_jobs(20) if job.is_running]
        if not running:
            st.caption("No running jobs.")
            return
        for job in running:
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.markdown(f"**{job.name}** — `{job.id}`")
                c2.metric("elapsed", f"{int(time.time() - job.started_at)}s")
                if c3.button("Kill", key=f"kill-{job.id}"):
                    kill_job(job.id)
                    st.rerun()
                st.code(tail_log(job.id, n=50))

    running_jobs_panel()

    st.subheader("Recent jobs")
    jobs = list_jobs(20)
    if not jobs:
        st.caption("No jobs yet — start one above.")
        return
    recent = pd.DataFrame(
        [
            {
                "id": job.id,
                "name": job.name,
                "status": job.status,
                "exit_code": job.exit_code,
                "started": _fmt_ts(job.started_at),
                "finished": _fmt_ts(job.finished_at),
            }
            for job in jobs
        ]
    )
    st.dataframe(recent)
    for job in jobs:
        with st.expander(f"log: {job.id}"):
            st.code(tail_log(job.id, n=500))


def explorer_page() -> None:
    st.header("Data Explorer")
    st.info("Coming in Phase 4.")


def analytics_page() -> None:
    st.header("Analytics")
    st.info("Coming in Phase 4.")


try:
    _query("SELECT 1")
    st.sidebar.success("connected")
except Exception as exc:
    st.sidebar.error(f"DB not reachable: {exc}")

page = st.sidebar.radio("Navigation", ["Dashboard", "Pipeline", "Data Explorer", "Analytics"])
{"Dashboard": dashboard_page, "Pipeline": pipeline_page,
 "Data Explorer": explorer_page, "Analytics": analytics_page}[page]()
