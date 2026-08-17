"""Unit tests for mktmtm.ui.jobs — the background subprocess job runner.

No live database. Every test gets its own empty jobs directory (autouse
fixture monkeypatches the module-level ``JOB_DIR``), so job state never
touches the real ``data/jobs``.

Covers: start (running + log), completion (done / failed + exit code),
tail_log, kill (status + process actually gone), job_command argv building,
and prune.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from mktmtm.ui import jobs


@pytest.fixture(autouse=True)
def _isolated_job_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test its own fresh jobs directory."""
    monkeypatch.setattr(jobs, "JOB_DIR", tmp_path / "jobs")


def _wait_done(job_id: str, timeout: float = 20.0) -> jobs.Job:
    """Poll get_job until the job leaves the running state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get_job(job_id)
        if not job.is_running:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id!r} still running after {timeout:.0f}s")


# ─────────────────────────── start / completion ────────────────────────────
def test_start_job_returns_running_job_with_log_file() -> None:
    argv = [sys.executable, "-c", "import time; time.sleep(3)"]
    job = jobs.start_job("slow", argv)

    assert job.status == jobs.STATUS_RUNNING
    assert job.is_running
    assert job.exit_code is None
    assert Path(job.log_path).exists()


def test_job_completes_done_with_exit_zero() -> None:
    argv = [sys.executable, "-c", "import time; time.sleep(3)"]
    job = jobs.start_job("slow-done", argv)

    finished = _wait_done(job.id)

    assert finished.status == jobs.STATUS_DONE
    assert finished.exit_code == 0
    assert finished.finished_at is not None


def test_failing_command_is_failed_with_exit_code() -> None:
    argv = [sys.executable, "-c", "raise SystemExit(7)"]
    job = jobs.start_job("boom", argv)

    finished = _wait_done(job.id)

    assert finished.status == jobs.STATUS_FAILED
    assert finished.exit_code == 7


# ─────────────────────────────── tail_log ──────────────────────────────────
def test_tail_log_returns_last_lines() -> None:
    argv = [sys.executable, "-c", "for i in range(20): print(i)"]
    job = jobs.start_job("printer", argv)
    _wait_done(job.id)

    assert jobs.tail_log(job.id, n=10) == "\n".join(str(i) for i in range(10, 20))


def test_tail_log_missing_job_returns_empty() -> None:
    assert jobs.tail_log("no-such-job", n=10) == ""


# ─────────────────────────────── kill_job ──────────────────────────────────
def test_kill_job_marks_killed_and_process_gone() -> None:
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    job = jobs.start_job("sleepy", argv)
    assert job.status == jobs.STATUS_RUNNING
    assert job.pid is not None

    jobs.kill_job(job.id)
    killed = jobs.get_job(job.id)

    assert killed.status == jobs.STATUS_KILLED
    assert killed.finished_at is not None

    # The child was started as a session leader; SIGTERM hits the whole
    # group. Reap the (now dead) child so the liveness probe is meaningful —
    # a zombie would still answer os.kill(pid, 0).
    deadline = time.monotonic() + 10.0
    reaped = False
    while time.monotonic() < deadline:
        try:
            waited, _ = os.waitpid(job.pid, os.WNOHANG)
            if waited == job.pid:
                reaped = True
                break
        except ChildProcessError:
            # Not our child anymore (reparented) — nothing to reap.
            reaped = True
            break
        time.sleep(0.05)
    assert reaped, "killed process was never reaped"

    with pytest.raises(ProcessLookupError):
        os.kill(job.pid, 0)  # process is actually gone


# ───────────────────────────── job_command ─────────────────────────────────
def test_job_command_builds_full_argv() -> None:
    argv = jobs.job_command(
        "fetch-bhavcopy",
        start_date="2026-06-01",
        end_date="2026-08-14",
        max_workers=4,
    )
    assert argv == [
        "uv",
        "run",
        "mktmtm",
        "fetch-bhavcopy",
        "--start-date",
        "2026-06-01",
        "--end-date",
        "2026-08-14",
        "--max-workers",
        "4",
    ]


def test_job_command_skips_empty_params() -> None:
    argv = jobs.job_command("fetch-yahoo", days=None)
    assert argv == ["uv", "run", "mktmtm", "fetch-yahoo"]


def test_job_command_unknown_step_raises() -> None:
    with pytest.raises(ValueError, match="unknown pipeline step"):
        jobs.job_command("nope")


def test_job_command_unknown_param_raises() -> None:
    with pytest.raises(ValueError, match="unknown parameter"):
        jobs.job_command("etl", bogus=1)


# ──────────────────────────────── prune ────────────────────────────────────
def test_prune_keeps_at_most_two_jobs() -> None:
    for name in ("p0", "p1", "p2"):
        jobs.start_job(name, [sys.executable, "-c", "print('x')"])
        time.sleep(0.05)  # separate mtimes so prune ordering is deterministic

    jobs.prune(keep=2)

    metas = sorted(p.name for p in jobs.JOB_DIR.glob("*.json"))
    logs = sorted(p.name for p in jobs.JOB_DIR.glob("*.log"))
    assert len(metas) <= 2
    assert len(logs) <= 2
    assert not any("p0" in name for name in metas)  # oldest pruned
