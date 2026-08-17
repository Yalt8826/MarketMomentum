# MktMtm Streamlit Control Panel — Executable Implementation Plan

> **Goal:** A Streamlit UI on the MktMtm project that (a) runs the pipeline steps — bhavcopy/yahoo download, process, ETL, backtest — from the browser with live progress and kill control, and (b) visualises the current `options_db` contents (schema, samples, JSONB, backtest analytics).
> **Architecture:** Streamlit app running **on athena** (PostgreSQL is localhost-bound there). Long pipeline steps run as **background subprocesses** with on-disk state (`<data_dir>/jobs/`), so the UI never blocks and jobs survive app restarts. DB reads use psycopg2 + the project's pydantic settings. **No new dependencies.**
> **Tech stack:** Streamlit ≥1.59, psycopg2, pandas — all already in `pyproject.toml`.
> **Executor:** any coding agent with shell access to athena. Plan is self-contained; do not improvise scope beyond the specs below.
> **Status:** ALL PHASES SHIPPED (2026-08-17) — Phases 1–4 landed in `bb9063f`, `18bfcb1`, `d308601`, `baf7f70`; Phase 5 (tailnet launcher + docs + hygiene) shipped in the phase-5 commit. Plan kept here as the living spec; see per-phase notes below.

---

## 0. Execution prerequisites (read first)

**Repo (canonical, athena):** `/mnt/storage/Study/Major Project/Code/MktMtm`
(Mirrored over NFS at `/mnt/arch/Major Project/Code/MktMtm` from olympus — same files.)

**Shell:** athena's interactive default is **fish**. Execute every command with bash:
```bash
cd "/mnt/storage/Study/Major Project/Code/MktMtm"
export PATH="$HOME/.local/bin:$PATH"     # uv lives here
uv --version                             # expect: uv 0.11.x
```

**Tests:** PostgreSQL 18 is live on athena (`localhost:5432/options_db`, creds in the project `.env`). Full suite including integration tests:
```bash
uv run pytest                             # run inside the project dir
uv run pytest -m "not integration"        # unit-only (must also pass)
```

**Hard rules for the executor:**
1. Never modify `src/mktmtm/api.py`, any `src/mktmtm/storage/sql/*.sql` DDL, `.env`, or files under `data/` (except adding `data/jobs/`).
2. One commit per task. Commit messages: `feat|fix|test|docs: <scope>: <short description>`.
3. Run the full test suite before every commit; integration tests run on athena (live PG).
4. At the end of each phase: run that phase's **Exit test**, commit, then **stop and report** — do not start the next phase without the user's go.
5. Keep the UI agent-agnostic: no agent names in code, docs, or UI copy.

---

## Phase 1 — Background job manager (`src/mktmtm/ui/jobs.py`) ✅ SHIPPED

**Objective:** A small, fully unit-tested subprocess job runner with on-disk state.

**Exit test:** `uv run pytest tests/unit/test_ui_jobs.py -v` → all green on athena. Manual: from a python REPL in the project venv, start a dummy job, confirm `status` transitions `running → done`, `tail_log` shows output, `kill_job` terminates it.
**Checkpoint:** report test output + manual result; wait for go.

### Task 1.1 — package scaffold
- Create `src/mktmtm/ui/__init__.py` (empty).
- Verify `data/` is git-ignored (`git check-ignore data/processed/processed_data.json` → prints path). If NOT ignored, add `data/` to `.gitignore` now (before it grows logs).

### Task 1.2 — write failing tests
- Create `tests/unit/test_ui_jobs.py`. Test list:
  - `start_job` with a slow dummy command (`python -c "import time;time.sleep(3)"` via `sys.executable`) → job returned, `status == "running"`, log file exists.
  - after completion: `get_job(job_id).status == "done"`, `exit_code == 0`.
  - failing command (`python -c "raise SystemExit(7)"`) → `status == "failed"`, `exit_code == 7`.
  - `tail_log(job_id, n=10)` returns the last lines (dummy prints 20 numbered lines).
  - `kill_job` on a `sleep 60` dummy → `status == "killed"` and process actually gone (check via `os.kill(pid, 0)` raising `ProcessLookupError`).
  - `job_command("fetch-bhavcopy", start_date="2026-06-01", end_date="2026-08-14", max_workers=4)` → argv contains `--start-date 2026-06-01 --end-date 2026-08-14 --max-workers 4`.
  - `prune(keep=2)` leaves at most 2 jobs on disk.

### Task 1.3 — implement `src/mktmtm/ui/jobs.py`

Complete implementation (use as-is; keep the module Streamlit-free so it is unit-testable):

```python
"""Background job runner for the Streamlit control panel.

Pipeline steps run as subprocesses so the UI never blocks and jobs survive
app restarts. State lives on disk under <data_dir>/jobs: <job_id>.log
(captured output) and <job_id>.json (status).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mktmtm.config.settings import settings

JOB_DIR = settings.data_dir / "jobs"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_KILLED = "killed"

# step name -> base CLI argv (flags appended by job_command)
_COMMANDS: dict[str, list[str]] = {
    "fetch-bhavcopy": ["fetch-bhavcopy"],
    "fetch-yahoo": ["fetch-yahoo"],
    "process": ["process"],
    "etl": ["etl"],
    "backtest": ["backtest"],
}

# step -> (flag, parser) for each supported parameter
_OPTION_SPECS: dict[str, dict[str, Any]] = {
    "fetch-bhavcopy": {
        "start_date": ("--start-date", str),
        "end_date": ("--end-date", str),
        "max_workers": ("--max-workers", int),
    },
    "fetch-yahoo": {"days": ("--days", int)},
    "process": {"limit_days": ("--limit-days", int)},
    "backtest": {
        "symbols": ("--symbols", str),
        "years": ("--years", str),
        "months": ("--months", str),
    },
}


def job_command(step: str, **params: Any) -> list[str]:
    """Build the CLI argv for a pipeline step (via the mktmtm CLI)."""
    if step not in _COMMANDS:
        raise ValueError(f"unknown pipeline step: {step}")
    argv = ["uv", "run", "mktmtm", *_COMMANDS[step]]
    for key, value in params.items():
        if value is None or value == "":
            continue
        if step not in _OPTION_SPECS:
            raise ValueError(f"unknown parameter '{key}' for step '{step}'")
        flag, _parser = _OPTION_SPECS[step][key]
        argv += [flag, str(value)]
    return argv


def _env() -> dict[str, str]:
    """Inherit env but prepend ~/.local/bin (uv lives there off-PATH)."""
    env = dict(os.environ)
    home_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = f"{home_bin}:{env.get('PATH', '')}"
    return env


@dataclass
class Job:
    id: str
    name: str
    status: str
    exit_code: int | None
    started_at: float
    finished_at: float | None
    log_path: str

    @property
    def is_running(self) -> bool:
        return self.status == STATUS_RUNNING


def _job_path(job_id: str) -> tuple[Path, Path]:
    return JOB_DIR / f"{job_id}.log", JOB_DIR / f"{job_id}.json"


def _read_meta(job_id: str) -> dict[str, Any]:
    _log, meta = _job_path(job_id)
    return json.loads(meta.read_text())


def _write_meta(job_id: str, meta: dict[str, Any]) -> None:
    _log, meta_path = _job_path(job_id)
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta))
    tmp.rename(meta_path)


def start_job(name: str, argv: list[str], cwd: Path | None = None) -> Job:
    """Start a background job. argv is executed as given (no shell)."""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    job_id = f"{name}-{int(time.time())}"
    log_path, meta_path = _job_path(job_id)
    meta = {
        "id": job_id, "name": name, "status": STATUS_RUNNING,
        "exit_code": None, "started_at": time.time(), "finished_at": None,
        "log_path": str(log_path),
    }
    _write_meta(job_id, meta)
    # IMPORTANT: cwd must be the PROJECT ROOT (settings.data_dir.parent),
    # NOT data/ — settings loads .env from CWD, so etl/backtest subprocesses
    # need to run where the .env with PG creds lives.
    project_root = settings.data_dir.parent
    with log_path.open("w") as logf:
        proc = subprocess.Popen(
            argv, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, env=_env(), cwd=cwd or project_root,
        )
    meta["pid"] = proc.pid  # persist pid for liveness checks / kill
    _write_meta(job_id, meta)
    return get_job(job_id)


def _refresh(job_id: str) -> None:
    """Update status from the live process if it finished."""
    meta = _read_meta(job_id)
    if meta["status"] != STATUS_RUNNING:
        return
    pid = meta.get("pid")
    if pid is None:
        return
    try:
        waited_pid, status = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            # direct child finished — reap and record the real exit code
            code = os.waitstatus_to_exitcode(status)
            meta["status"] = STATUS_DONE if code == 0 else STATUS_FAILED
            meta["exit_code"] = code
            meta["finished_at"] = time.time()
            _write_meta(job_id, meta)
        # waited_pid == 0 → still running; leave as-is
    except ChildProcessError:
        # not our child anymore (e.g. app restarted; job reparented to init)
        try:
            os.kill(pid, 0)  # probe liveness
        except ProcessLookupError:
            meta["status"] = STATUS_FAILED
            meta["exit_code"] = -1
            meta["finished_at"] = time.time()
            _write_meta(job_id, meta)


def get_job(job_id: str) -> Job:
    _refresh(job_id)  # status must be live, not stale
    return Job(**_read_meta(job_id))


def list_jobs(limit: int = 20) -> list[Job]:
    jobs = []
    for meta_path in sorted(JOB_DIR.glob("*.json"), reverse=True):
        if meta_path.name.endswith(".tmp"):
            continue
        meta = json.loads(meta_path.read_text())
        jobs.append(Job(**meta))
        if len(jobs) >= limit:
            break
    for job in jobs:
        if job.status == STATUS_RUNNING:
            _refresh(job.id)
    return [get_job(j.id) for j in jobs]


def tail_log(job_id: str, n: int = 100) -> str:
    log_path, _ = _job_path(job_id)
    if not log_path.exists():
        return ""
    lines = log_path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:])


def kill_job(job_id: str) -> None:
    meta = _read_meta(job_id)
    if meta["status"] != STATUS_RUNNING:
        return
    pid = meta.get("pid")
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    meta["status"] = STATUS_KILLED
    meta["finished_at"] = time.time()
    _write_meta(job_id, meta)


def prune(keep: int = 20) -> None:
    metas = sorted(JOB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in metas[keep:]:
        for suffix in (".json", ".log"):
            p = stale.with_suffix(suffix)
            if p.exists():
                p.unlink()
```

> ⚠️ **Executor note (Task 1.3):** the code block above is the *final* implementation — implement it exactly (the pid persistence, `_refresh` liveness/reap logic and `get_job` refreshing are intentional and covered by the Task 1.2 tests). Keep the module free of any `import streamlit` so it stays unit-testable. If any test forces a change, update the code AND re-run the whole unit suite.

### Task 1.4 — verify + commit
- `uv run pytest tests/unit/test_ui_jobs.py -v` → PASS.
- `uv run pytest -m "not integration"` → PASS (no regressions).
- Commit: `feat: ui: background job manager with on-disk state (jobs.py)`.

---

## Phase 2 — Backtest scope parameters ✅ SHIPPED

**Objective:** Make the backtest run scope (symbols/years/months) controllable via CLI (and later the UI), without changing current default behaviour (last 4 years × all symbols × all months).

**Exit test:** `uv run pytest -m "not integration"` green; real smoke on athena:
```bash
uv run mktmtm backtest --symbols NIFTY --years 2023 --months 1,2
```
→ completes in seconds, log shows only NIFTY 2023-01/2023-02, and exactly ≤2 rows inserted for that scope.
**Checkpoint:** report; wait for go.

### Task 2.1 — failing tests first
- In `tests/backtest/` add `test_scope.py`:
  - `resolve_scope(None, None, None, all_symbols=["A", "B"], current_year=2026)` → full 5-year window `[2022..2026]`, all symbols, months 1–12.
  - `resolve_scope(["NIFTY"], [2023], [1, 2], ...)` → exactly those.
  - `resolve_scope(None, [2030], None, ...)` → empty years (invalid filtered).
  - `resolve_scope(["NIFTY"], [2023], [1, 2], ...)` with `run_backtest` monkeypatched DB → only those months processed.

### Task 2.2 — implement
- In `src/mktmtm/backtest/strangle.py`:
  - Add pure helper:
    ```python
    def resolve_scope(
        symbols: list[str] | None,
        years: list[int] | None,
        months: list[int] | None,
        all_symbols: list[str],
        current_year: int,
    ) -> tuple[list[str], list[int], list[int]]:
        """Return (symbols, years, months) to run, applying defaults and
        filtering invalid values. Defaults: every symbol, last 4 years
        (current_year-4 .. current_year), months 1..12."""
    ```
  - Change signature: `run_backtest(cfg=None, symbols=None, years=None, months=None)`; inside, replace the hardcoded `RUN SCOPE` block with `symbols_to_run, years_to_run, months_to_run = resolve_scope(...)`.
- In `src/mktmtm/cli.py` add to the `backtest` command:
  - `--symbols` (comma-separated string → list), `--years` (e.g. `2022,2023`), `--months` (e.g. `1,2`); all optional, default `None` → full scope.
- Keep the existing future/current-month skip logic untouched.

### Task 2.3 — verify + commit
- `uv run pytest -m "not integration"` green; run the smoke command above; confirm rows.
- Commit: `feat: backtest: scope overrides (--symbols/--years/--months)`.

---

## Phase 3 — Dashboard + Pipeline pages ✅ SHIPPED

**Objective:** Rewrite `src/mktmtm/frontend_app.py` with sidebar navigation and two working pages; wire Pipeline to Phase 1 jobs.

**Exit test (manual, on athena):**
```bash
uv run streamlit run src/mktmtm/frontend_app.py --server.headless true --server.port 8501 &
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8501   # expect 200
```
Then in a browser (or `curl` the health endpoint): Dashboard shows live counts matching the DB report (§2: option_metrics 186,234 rows / 222 symbols / 2020-04-13→2026-08-14; backtest_results 7,076). Start a 3-day bhavcopy fetch from the UI (`fetch-bhavcopy` with `--start-date 2026-08-11 --end-date 2026-08-14`), confirm the job appears, log streams, and it completes as `done`. Kill a dummy job from the UI.
**Checkpoint:** report; wait for go.

### Task 3.1 — Dashboard page (stub nav)
Structure in `frontend_app.py`:
```python
import streamlit as st

st.set_page_config(page_title="MktMtm", layout="wide", page_icon="📈")

def dashboard_page(): ...
def pipeline_page(): ...
def explorer_page(): ...   # Phase 4 — placeholder "coming in Phase 4"
def analytics_page(): ...  # Phase 4 — placeholder

page = st.sidebar.radio("Navigation", ["Dashboard", "Pipeline", "Data Explorer", "Analytics"])
{"Dashboard": dashboard_page, "Pipeline": pipeline_page,
 "Data Explorer": explorer_page, "Analytics": analytics_page}[page]()
```
Dashboard page spec:
- `st.metric` row: `option_metrics rows`, `symbols`, `date range` (min→max), `backtest_results rows`, `traded months`, `skipped months`.
- One bar chart: rows per year (option_metrics) via `st.bar_chart`.
- Connection status: `st.sidebar.success("connected")` / `st.sidebar.error(...)` based on a `SELECT 1` probe.
- All DB reads via small local helper (import `psycopg2`, connect from `mktmtm.config.settings.settings` — same params as `ui/queries.py` will formalise in Phase 4; for now a `db_connect()` helper may live at the top of `frontend_app.py`).

### Task 3.2 — Pipeline page (job runner UI)
Spec (no improvisation):
- For each step in `["fetch-bhavcopy", "fetch-yahoo", "process", "etl", "backtest"]`: an `st.expander` with controls and a Run button:
  - **fetch-bhavcopy:** date inputs (`st.date_input` start/end, default start = today-3mo, end = today), `st.number_input` max_workers (1–16, default 4). Run → `start_job(name, job_command("fetch-bhavcopy", start_date=..., end_date=..., max_workers=...))` with `cwd=settings.data_dir.parent` (project root).
  - **fetch-yahoo:** `st.number_input` days (default 200). 
  - **process:** `st.number_input` limit_days (optional).
  - **etl:** no controls.
  - **backtest:** Phase 2 controls — multi-select symbols (from `ui/queries` symbol list or a text input), years, months; all optional (None = full scope).
- **Running jobs panel:** `st.fragment(run_every=2.0)` (verify availability in the installed Streamlit; fallback: a "Refresh" button + `st.rerun()`) rendering, per running job: name, elapsed seconds, a `st.code` with `tail_log(job_id, n=50)`, and a "Kill" `st.button` → `kill_job`.
- **Recent jobs table:** `st.dataframe` of `list_jobs(20)` (id, name, status, exit_code, started, finished) + per-job log expander (`tail_log(n=500)`).
- On run: `st.rerun()` immediately after starting so the job appears.
- `prune(keep=20)` after each start.

### Task 3.3 — verify + commit
- Manual exit test above. Commit: `feat: ui: dashboard and pipeline pages with live job runner`.

---

## Phase 4 — Data Explorer + Analytics ✅ SHIPPED

**Objective:** `src/mktmtm/ui/queries.py` (testable DB helpers) + Explorer and Analytics pages.

**Exit test:** `uv run pytest tests/integration/test_ui_queries.py -v` green on athena; each view renders real data; the SQL box runs the appendix queries from `docs/data/database-report.md`.
**Checkpoint:** report; wait for go.

### Task 4.1 — `src/mktmtm/ui/queries.py` (pure, Streamlit-free)
Functions (all take a `psycopg2` connection; tests in `tests/integration/test_ui_queries.py` marked `integration`):
- `connect() -> conn` — psycopg2.connect from `settings` (host/port/db/user/password/sslmode, `connect_timeout=10`).
- `db_status(conn) -> dict` — `{"ok": bool, "tables": [{"name", "rows", "size_bytes"}], "option_metrics": {...}, "backtest_results": {...}}` using `pg_stat_user_tables`.
- `table_info(conn, table) -> list[dict]` — columns/type/nullable/default via `information_schema`.
- `sample_rows(conn, table, n=20) -> pd.DataFrame` — `SELECT * FROM <table> ORDER BY 1 LIMIT n` (table name whitelisted against `{option_metrics, backtest_results}`).
- `run_sql(conn, sql: str) -> pd.DataFrame` — pandas `read_sql`; raise clean error on failure (return message, don't crash).
- `pnl_by_month(conn) -> pd.DataFrame`, `symbol_leaderboard(conn, limit=10) -> pd.DataFrame`, `skip_reasons(conn) -> pd.DataFrame`, `coverage_year_month(conn) -> pd.DataFrame` — implement exactly the SQL from the appendix of `docs/data/database-report.md`.
- Integration tests assert: `db_status` table names match; `pnl_by_month` has columns (year, month, avg_pnl, traded) and ≥1 row for 2022–2026; `skip_reasons` sums to 3,872; `coverage_year_month` covers 2020-04→2026-08.

### Task 4.2 — Explorer page
- `st.selectbox` table (`option_metrics` / `backtest_results`) → schema `st.dataframe` (column/type/nullable), row count metric, sample `st.dataframe` (rows limited 50), and for JSONB columns a `st.json` viewer fed by the first non-null cell of the selected column.
- SQL box: `st.text_area` + Run → `run_sql` → `st.dataframe` + `st.download_button` (CSV).

### Task 4.3 — Analytics page
- Filters: year range slider (2022–2026), symbol multi-select (default all).
- Charts (built-in `st.bar_chart`/`st.dataframe`): PNL by year/month (bar), PNL distribution (`st.bar_chart` of a 20-bin histogram computed in pandas), symbol leaderboard (bar + top/bottom 10 table), skip-reasons pie via `st.plotly_chart`? — **no: use `st.bar_chart`** (no new deps); coverage heatmap year×month (option_metrics) via `st.dataframe` with color styling only if trivial, else plain table.

### Task 4.4 — verify + commit
- Integration tests green; manual render of all four pages. Commit: `feat: ui: data explorer + analytics pages`.

---

## Phase 5 — Launcher, docs, hygiene ✅ SHIPPED

**Objective:** sane default launcher that binds the UI to **athena's tailnet IP** so it's reachable from any tailnet device (verified: custom ports on athena are reachable from olympus over the tailnet; magicDNS name `athena` resolves), plus README and repo hygiene.

**Exit test (from olympus):**
```bash
# after starting the UI on athena:  uv run mktmtm-ui
curl -s -o /dev/null -w "%{http_code}\n" http://100.82.96.116:8501   # expect 200
curl -s -o /dev/null -w "%{http_code}\n" http://athena:8501          # expect 200 (magicDNS)
```
**Checkpoint:** final report; wait for go.

- **Task 5.1:** `src/mktmtm/frontend.py` — accept `--host` and `--port` (defaults below), pass to `streamlit run --server.address <host> --server.port <port> --server.headless true`. Keep `mktmtm-ui` script name.
  - **Default host = athena's tailnet IP, resolved at launch** (not hardcoded): try `tailscale ip -4` (first IP) via subprocess; on any failure fall back to `127.0.0.1`. This keeps the launcher correct if the tailnet IP ever changes and falls back gracefully when tailscale is absent.
  - Explicit `--host 127.0.0.1` gives the local-only mode (SSH-tunnel access path, see Task 5.2).
  - Default port `8501`.
- **Task 5.2:** README — add "UI" section:
  - Run: `uv run mktmtm-ui` (binds athena tailnet IP:8501 by default; `--host 127.0.0.1` for local-only).
  - Access from any tailnet device: `http://athena:8501` (magicDNS) or `http://100.82.96.116:8501`.
  - **Security note:** the app has no auth; it is reachable only by devices on the tailnet (Tailscale ACLs). Do NOT bind `0.0.0.0` — that would expose it on every interface. SSH tunnel (`ssh -N -L 8501:localhost:8501 athena`) is the fallback access pattern if local-only binding is preferred.
  - Note that jobs run on athena and survive UI restarts; scope defaults match the CLI.
- **Task 5.3:** `.gitignore` — ensure `data/jobs/` is ignored (covers executor environments where `data/` is partially tracked).
- **Task 5.4:** docs — add a short "Control panel" note to `docs/data/database-report.md` pointing at the UI and its access URL; keep `docs/frontend/implementation-plan.md` as the living spec (mark phases done as they ship).

---

## Files touched (summary)

- **Create:** `src/mktmtm/ui/{__init__,jobs,queries}.py`, `tests/unit/test_ui_jobs.py`, `tests/backtest/test_scope.py`, `tests/integration/test_ui_queries.py`
- **Modify:** `src/mktmtm/frontend_app.py`, `src/mktmtm/frontend.py`, `src/mktmtm/backtest/strangle.py`, `src/mktmtm/cli.py`, `.gitignore`, `README.md`, `docs/data/database-report.md`
- **Untouched:** `src/mktmtm/api.py`, `storage/sql/*.sql`, `.env`, `data/*` (except `data/jobs/`).

## Deferred (explicitly out of scope for now)

- **Auth/token gate** for the UI — not needed while it's tailnet-only; revisit if it ever gets exposed beyond the tailnet (then ALSO re-consider the `0.0.0.0` bind).
- Multi-session job isolation (single-user tool).
- FastAPI-driven job endpoints (the `api.py` layer stays health-only).

## Appendix — reference facts (verified 2026-08-17)

- **Tailnet:** athena = `100.82.96.116` (magicDNS `athena`), olympus = `100.99.246.26` (`olympus`), user's phone `iris` also on the tailnet. Custom ports on athena ARE reachable over the tailnet (verified: HTTP 200 from olympus to `100.82.96.116:18501` and `athena:18501`). Tailscale0 interface present on both hosts. MagicDNS resolution works.
- PG: `localhost:5432/options_db`, user `yashas`, creds in project `.env` (and `data_preprocessing/nse-options-last-5-years/store_in_db/.env`). `option_metrics`: 186,234 rows / 222 symbols / 2020-04-13→2026-08-14. `backtest_results`: 7,076 rows / 3,204 traded / 3,872 skipped (Missing Exit Price 2,088 · Earnings 1,305 · No Suitable Options 380 · No Valid Exit Date 99).
- CLI signatures: `mktmtm fetch-bhavcopy --start-date YYYY-MM-DD --end-date YYYY-MM-DD --max-workers N` · `fetch-yahoo --days N` · `process --limit-days N` · `etl` · `backtest [--symbols a,b --years 2022,2023 --months 1,2]` (Phase 2).
- Settings load `.env` from CWD → subprocess jobs must run with `cwd` = project root (jobs.py does this).
