"""Background job runner for the Streamlit control panel.

Pipeline steps run as subprocesses so the UI never blocks and jobs survive
app restarts. State lives on disk under <data_dir>/jobs: <job_id>.log
(captured output) and <job_id>.json (status).
"""
from __future__ import annotations

import contextlib
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
    pid: int | None = None

    @property
    def is_running(self) -> bool:
        return self.status == STATUS_RUNNING


def _job_path(job_id: str) -> tuple[Path, Path]:
    return JOB_DIR / f"{job_id}.log", JOB_DIR / f"{job_id}.json"


def _read_meta(job_id: str) -> dict[str, Any]:
    _log, meta = _job_path(job_id)
    data = json.loads(meta.read_text())
    assert isinstance(data, dict)
    return data


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
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
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
