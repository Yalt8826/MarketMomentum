"""Unit tests for mktmtm.serving.scoring — Phase 3 forward validation.

The load-bearing tests are the provenance ones. Everything else in Gate 3 can be
satisfied by a record written after its outcome was known — which is a backtest
wearing a forward test's filename, and is exactly what Phase 3 exists to rule
out.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from mktmtm.serving.scoring import (
    GATE_3_MIN_MONTHS,
    ScoredRecord,
    gate_3_report,
    running_record,
)

#: Distinguishes "caller did not say" from "caller said None", which is the
#: difference between a default commit date and an uncommitted record.
_UNSET = object()


def _scored(
    as_of: date,
    portfolio: float,
    *,
    month_entry: bool = True,
    committed: date | None | object = _UNSET,
    expiry: date | None = None,
    scored: int = 5,
) -> ScoredRecord:
    expiry = expiry or date(as_of.year, as_of.month, 28)
    if committed is _UNSET:
        committed = date(as_of.year, as_of.month, 2)
    return ScoredRecord(
        as_of=as_of,
        commit="abc1234",
        is_month_entry=month_entry,
        selected=5,
        scored=scored,
        unscored={},
        mean_return_on_margin=portfolio,
        total_net_rupees=portfolio * 100_000,
        total_margin_rupees=100_000.0,
        portfolio_return=portfolio,
        worst_trade=portfolio - 0.1,
        trades=pd.DataFrame({"return_on_margin": [portfolio] * scored}),
        committed_at=committed,  # type: ignore[arg-type]
        outcome_known_from=expiry,
    )


# ───────────────────── provenance: the whole point ──────────────────────────


def test_a_record_committed_before_its_outcome_is_verified() -> None:
    r = _scored(date(2026, 3, 2), 0.05, committed=date(2026, 3, 2), expiry=date(2026, 3, 26))

    assert r.provenance_verified
    assert r.in_calibration


def test_a_record_committed_after_its_outcome_is_not() -> None:
    """A backtest wearing a forward test's filename."""
    r = _scored(date(2026, 3, 2), 0.35, committed=date(2026, 4, 15), expiry=date(2026, 3, 26))

    assert not r.provenance_verified
    assert not r.in_calibration


def test_an_uncommitted_record_cannot_be_verified() -> None:
    """However good its numbers look."""
    r = _scored(date(2026, 3, 2), 0.99, committed=None, expiry=date(2026, 3, 26))

    assert not r.provenance_verified
    assert not r.in_calibration


def test_an_unverifiable_record_is_excluded_from_the_verdict() -> None:
    good = [_scored(date(2026, m, 2), 0.04) for m in range(1, 7)]
    spectacular_but_unverified = _scored(
        date(2026, 7, 1), 0.80, committed=date(2026, 8, 1), expiry=date(2026, 7, 28)
    )

    report = gate_3_report([*good, spectacular_but_unverified])

    assert report["months"] == 6
    assert report["off_calibration_records"] == 1
    assert "not committed before its outcome" in report["excluded_because"]
    # The 80% month must not drag the mean up.
    assert report["mean_portfolio_return"] == pytest.approx(0.04)


# ───────────────────── calibration: entry day matters ───────────────────────


def test_an_off_calibration_entry_is_excluded() -> None:
    """The backtest always enters on the month's first trading day; anything
    else is a position nobody measured."""
    r = _scored(date(2026, 3, 9), 0.05, month_entry=False)

    assert not r.in_calibration

    report = gate_3_report([r])
    assert report["months"] == 0
    assert "not the month's first trading day" in report["excluded_because"]


def test_a_record_with_nothing_scored_yet_is_excluded() -> None:
    """An open month has no outcome to contribute."""
    r = _scored(date(2026, 9, 1), float("nan"), scored=0)

    assert not r.in_calibration
    assert "nothing scored yet" in gate_3_report([r])["excluded_because"]


# ───────────────────── the running record ───────────────────────────────────


def test_losing_months_are_kept() -> None:
    """A tool that publishes only its good months is not being validated."""
    records = [
        _scored(date(2026, 1, 1), 0.10),
        _scored(date(2026, 2, 2), -0.16),
        _scored(date(2026, 3, 2), 0.05),
    ]

    frame = running_record(records)

    assert len(frame) == 3
    assert (frame["portfolio_return"] < 0).sum() == 1


def test_the_cumulative_return_compounds_in_date_order() -> None:
    records = [_scored(date(2026, 1, 1), 0.10), _scored(date(2026, 2, 2), 0.10)]

    frame = running_record(records)

    assert frame["cumulative_return"].iloc[-1] == pytest.approx(0.21)


def test_only_eligible_records_compound() -> None:
    records = [
        _scored(date(2026, 1, 1), 0.10),
        _scored(date(2026, 2, 2), 5.00, month_entry=False),  # excluded
    ]

    frame = running_record(records)

    assert frame["cumulative_return"].iloc[0] == pytest.approx(0.10)
    assert pd.isna(frame["cumulative_return"].iloc[1])


# ───────────────────── the gate ─────────────────────────────────────────────


def test_the_gate_is_incomplete_below_six_months() -> None:
    report = gate_3_report([_scored(date(2026, m, 2), 0.04) for m in range(1, 4)])

    assert report["verdict"].startswith("INCOMPLETE")
    assert not report["criterion_1_enough_months"]


def test_six_healthy_months_pass() -> None:
    report = gate_3_report([_scored(date(2026, m, 2), 0.04) for m in range(1, 7)])

    assert report["verdict"] == "PASS"
    assert report["criterion_4_provenance_verified"]
    assert report["positive_months"] == 6


def test_a_catastrophic_month_fails_the_gate() -> None:
    """Worse than twice Gate 0's worst month is a blow-up, not a bad month."""
    records = [_scored(date(2026, m, 2), 0.04) for m in range(1, 6)]
    records.append(_scored(date(2026, 6, 1), -0.60))

    report = gate_3_report(records)

    assert not report["criterion_3_no_month_beyond_modelled_loss"]
    assert report["verdict"] == "FAIL"


def test_the_caveat_is_always_present() -> None:
    """It is the sentence the whole phase turns on."""
    report = gate_3_report([_scored(date(2026, m, 2), 0.04) for m in range(1, 7)])

    assert "tail has not yet arrived" in report["caveat"]


def test_an_empty_directory_reports_no_records() -> None:
    assert gate_3_report([])["verdict"] == "no records"


def test_six_months_is_a_floor_not_a_target() -> None:
    assert GATE_3_MIN_MONTHS == 6


# ═══════════════ committed_at: Gate 3's provenance check ═══════════════════
#
# This had never worked. The pathspec was passed through as given while cwd was
# set to the file's own parent, so a repo-root-relative path resolved *under*
# the screens directory and matched nothing. Every record came back None, so
# every record was excluded as "not committed before its outcome" and Gate 3
# could not be passed. These tests exercise a real git repo so the next
# regression is caught rather than reasoned about.


def _repo(tmp_path, relative_subdir: str = "docs/trading/results/screens"):
    import subprocess

    def git(*args, cwd=tmp_path):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    d = tmp_path / relative_subdir
    d.mkdir(parents=True)
    record = d / "screen-2026-10-01.md"
    record.write_text("# Screen\n")
    git("add", "-A")
    git("commit", "-q", "-m", "screen")
    return record


def test_committed_at_resolves_a_repo_root_relative_path(tmp_path, monkeypatch) -> None:
    from mktmtm.serving.scoring import committed_at

    _repo(tmp_path)
    # The shape that used to fail: cwd at the repo root, path relative to it.
    monkeypatch.chdir(tmp_path)
    rel = Path("docs/trading/results/screens/screen-2026-10-01.md")

    assert committed_at(rel) is not None


def test_committed_at_resolves_an_absolute_path(tmp_path) -> None:
    from mktmtm.serving.scoring import committed_at

    record = _repo(tmp_path)

    assert committed_at(record) is not None


def test_committed_at_returns_none_for_an_uncommitted_file(tmp_path) -> None:
    from mktmtm.serving.scoring import committed_at

    record = _repo(tmp_path)
    loose = record.parent / "screen-2026-11-02.md"
    loose.write_text("# Screen\n")

    assert committed_at(loose) is None


def test_committed_at_returns_none_outside_a_repo(tmp_path) -> None:
    from mktmtm.serving.scoring import committed_at

    loose = tmp_path / "screen-2026-10-01.md"
    loose.write_text("# Screen\n")

    assert committed_at(loose) is None
