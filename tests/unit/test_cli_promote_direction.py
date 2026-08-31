"""CLI wiring tests for ``mktmtm promote-direction`` (Phase 8, Task 8.2).

The command wraps ``mktmtm.ml.promote.promote``; these tests pin the flag →
argument mapping (``--model``, ``--evaluation``, ``--output-dir``) and the
default resolution (latest ``xgb_direction_test_*.json`` candidate +
``xgb_direction_test.json``).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from mktmtm.cli import app

runner = CliRunner()


def _promoted_pair(tmp_path: Path) -> dict[str, object]:
    model = tmp_path / "xgb_direction_test_2026-08-31.json"
    model.write_bytes(b'{"learner":{}}')
    evaluation = tmp_path / "xgb_direction_test.json"
    evaluation.write_text('{"model": "xgb_direction"}', encoding="utf-8")
    return {"model": model, "evaluation": evaluation}


def test_promote_direction_forwards_explicit_paths(tmp_path: Path) -> None:
    pair = _promoted_pair(tmp_path)
    promoted = tmp_path / "promoted"
    result = runner.invoke(
        app,
        [
            "promote-direction",
            "--model",
            str(pair["model"]),
            "--evaluation",
            str(pair["evaluation"]),
            "--output-dir",
            str(promoted),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (promoted / "xgb_direction_test_2026-08-31.json").is_file()
    assert (promoted / "xgb_direction_test.json").is_file()
    assert "Promoted" in result.stdout


def test_promote_direction_defaults_resolve_latest_test_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    """Defaults: latest xgb_direction_test_*.json + xgb_direction_test.json."""
    candidates = tmp_path / "candidates"
    evaluation = tmp_path / "evaluation"
    promoted = tmp_path / "promoted"
    candidates.mkdir()
    evaluation.mkdir()
    (candidates / "xgb_direction_2026-08-31.json").write_bytes(b"old")
    (candidates / "xgb_direction_test_2026-08-31.json").write_bytes(b"new")
    (evaluation / "xgb_direction_test.json").write_text("{}", encoding="utf-8")

    class _FakeSettings:
        candidates_dir = candidates
        evaluation_dir = evaluation
        promoted_dir = promoted

    monkeypatch.setattr("mktmtm.cli.settings", _FakeSettings())
    result = runner.invoke(app, ["promote-direction"])

    assert result.exit_code == 0, result.stdout
    # The latest test candidate won, not the plain Phase 6 one.
    assert (promoted / "xgb_direction_test_2026-08-31.json").read_bytes() == b"new"
    assert (promoted / "xgb_direction_test.json").is_file()


def test_promote_direction_missing_default_candidate_fails(
    tmp_path: Path, monkeypatch
) -> None:
    class _FakeSettings:
        candidates_dir = tmp_path / "empty"
        evaluation_dir = tmp_path / "evaluation"
        promoted_dir = tmp_path / "promoted"

    monkeypatch.setattr("mktmtm.cli.settings", _FakeSettings())
    result = runner.invoke(app, ["promote-direction"])
    assert result.exit_code != 0
    assert isinstance(result.exception, FileNotFoundError)
    assert "no xgb_direction_test_*.json candidates" in str(result.exception)
