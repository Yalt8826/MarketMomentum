"""Contract tests for the pipeline stage wrappers.

`pipeline_runner` cannot invoke the Typer commands directly — a Typer default
is an `OptionInfo`, not a value — so each stage is a small wrapper that repeats
what its command does. That duplication is the bug surface, and it has already
bitten twice: `preparse` read `bhavcopy/` instead of `bhavcopy/extracted/` and
failed outright, and `fetch participant OI` downloaded the daily CSVs without
ever loading them, reporting success while the table stayed stale.

These tests pin what each wrapper calls and with which arguments, so a wrapper
drifting from its command fails here rather than at 2am on a six-hour run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mktmtm import pipeline_runner as pr


class _Recorder:
    """Collects calls so a wrapper's behaviour can be asserted without side effects."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def make(self, name: str, result: Any = None):  # type: ignore[no-untyped-def]
        def _fn(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return result
        return _fn

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def kwargs_of(self, name: str) -> dict[str, Any]:
        return next(c[2] for c in self.calls if c[0] == name)

    def args_of(self, name: str) -> tuple[Any, ...]:
        return next(c[1] for c in self.calls if c[0] == name)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    # Never touch a real database from a unit test.
    monkeypatch.setattr(pr, "_connect", lambda: _FakeConn())
    return recorder


class _FakeConn:
    def close(self) -> None: ...


def test_preparse_reads_the_extracted_subdirectory(
    rec: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CSVs live under extracted/; bhavcopy/ also holds filtered/ and partials.

    Pointing at the parent produced "no bhavcopy files parsed" and killed the
    run at stage 7 of 17.
    """
    import mktmtm.data.bhavcopy as bhav

    monkeypatch.setattr(bhav, "build_bhavcopy_cache", rec.make("build_cache"))
    pr._preparse()

    src, out = rec.args_of("build_cache")
    assert Path(src).name == "extracted", f"reads {src}, not the extracted/ dir"
    assert Path(out).name == "bhavcopy_cache.parquet"


def test_participant_oi_loads_as_well_as_fetches(
    rec: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_pipeline only downloads CSVs; loading them is a separate call.

    Omitting the load left the stage reporting success in 58s while
    participant_oi stayed exactly as stale as before — the worst kind of
    failure, because it looks like a pass.
    """
    import mktmtm.data.participant_oi as poi

    monkeypatch.setattr(poi, "run_pipeline", rec.make("fetch"))
    monkeypatch.setattr(poi, "load_participant_frame", rec.make("load", result="FRAME"))
    monkeypatch.setattr(poi, "write_participant_oi", rec.make("write"))
    pr._fetch_participant_oi()

    assert rec.names() == ["fetch", "load", "write"], (
        "fetching without loading leaves the table stale"
    )
    # The loaded frame must be what gets written, not a re-read.
    assert rec.args_of("write")[1] == "FRAME"


def test_bhavcopy_fetch_uses_the_configured_worker_count(
    rec: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_pipeline defaults to max_workers=1; the CLI passes the setting."""
    import mktmtm.data.bhavcopy as bhav

    monkeypatch.setattr(bhav, "run_pipeline", rec.make("fetch"))
    pr._fetch_bhavcopy()
    assert rec.kwargs_of("fetch")["max_workers"] >= 1
    assert "max_workers" in rec.kwargs_of("fetch"), "an overnight run would be serial"


def test_every_db_stage_closes_its_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stage that leaks a connection exhausts the pool over a long run."""
    closed: list[bool] = []

    class _Tracked:
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(pr, "_connect", lambda: _Tracked())
    import mktmtm.features.flow as flow
    import mktmtm.ml.accumulation as acc

    monkeypatch.setattr(flow, "run_flow_features", lambda *a, **k: None)
    monkeypatch.setattr(acc, "run_accumulation", lambda *a, **k: None)
    pr._flow_features()
    pr._accumulation()
    assert closed == [True, True]


def test_stage_order_is_the_dependency_graph() -> None:
    """Three orderings are load-bearing and must not be reshuffled."""
    names = [s.name for s in pr.build_stages()]
    assert names.index("preparse bhavcopy") > names.index("fetch bhavcopy"), (
        "the ETL reads the parquet cache, so a stale cache omits new days"
    )
    assert names.index("preparse bhavcopy") < names.index("ETL")
    assert names.index("health gate") < names.index("Stage 1  market flow"), (
        "the gate exists to stop bad data reaching a model"
    )
    assert names[-1] == "nowcast", "the nowcast must see finished tables"


def test_modes_select_coherent_stage_sets() -> None:
    """no-etl must skip acquisition too.

    Refetching without re-running the ETL would leave option_metrics behind the
    raw files, which is a worse state than either end.
    """
    stages = pr.build_stages()
    for mode, expected_absent in (
        ("no-etl", {"fetch", "etl"}),
        ("models", {"fetch", "etl", "derive", "gate"}),
    ):
        groups = pr._MODE_GROUPS[mode]
        assert not (expected_absent & set(groups)), mode
    assert {s.group for s in stages if s.group == "gate"}


def test_failure_stops_the_run_and_reports_the_stage() -> None:
    """A broken table must not be carried into hours of downstream work."""
    ran: list[str] = []
    stages = [
        pr.Stage("first", "model", lambda: ran.append("first")),
        pr.Stage("boom", "model", lambda: (_ for _ in ()).throw(RuntimeError("nope"))),
        pr.Stage("never", "model", lambda: ran.append("never")),
    ]
    results = pr.run_pipeline(mode="models", stages=stages)
    assert ran == ["first"]
    assert [r.name for r in results] == ["first", "boom"]
    assert results[-1].ok is False and "nope" in (results[-1].error or "")


def test_only_selects_a_subset_for_resuming() -> None:
    ran: list[str] = []
    stages = [
        pr.Stage("alpha stage", "model", lambda: ran.append("alpha")),
        pr.Stage("beta stage", "model", lambda: ran.append("beta")),
    ]
    pr.run_pipeline(mode="models", only=["beta"], stages=stages)
    assert ran == ["beta"]


def test_unknown_mode_and_empty_selection_raise() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        pr.run_pipeline(mode="nonsense")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no stages matched"):
        pr.run_pipeline(
            mode="models", only=["zzz"],
            stages=[pr.Stage("alpha", "model", lambda: None)],
        )
