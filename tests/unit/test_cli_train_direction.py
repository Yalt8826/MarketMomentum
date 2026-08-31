"""CLI wiring tests for ``mktmtm train-direction`` (Phase 8, Task 8.1).

The command wraps ``mktmtm.ml.train_direction.run``; these tests pin the flag
→ argument mapping (``--train-end``, ``--valid-end``, ``--output-dir``,
``--no-normalize``) with the real training run monkeypatched out — the actual
training exit test is a run against the real parquet.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from typer.testing import CliRunner

from mktmtm.cli import app
from mktmtm.config.settings import settings

runner = CliRunner()

_REPORT = {
    "valid": {"metrics": {"auc": 0.5165, "mean_ic": 0.0284, "icir": 0.2709}},
    "split": {"train_end": "2024-12-31", "valid_end": "2025-12-31"},
    "normalize": True,
}


def test_train_direction_forwards_all_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "valid": {"metrics": {"auc": 0.5165, "mean_ic": 0.0284, "icir": 0.2709}},
            "split": {"train_end": "2024-12-31", "valid_end": "2025-12-31"},
            "normalize": kwargs["normalize"],
        }

    monkeypatch.setattr("mktmtm.ml.train_direction.run", fake_run)
    result = runner.invoke(
        app,
        [
            "train-direction",
            "--train-end",
            "2024-06-30",
            "--valid-end",
            "2025-06-30",
            "--output-dir",
            "/tmp/xout",
            "--no-normalize",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["train_end"] == pd.Timestamp("2024-06-30")
    assert captured["valid_end"] == pd.Timestamp("2025-06-30")
    assert captured["normalize"] is False
    assert captured["evaluation_dir"] == Path("/tmp/xout") / "evaluation"
    assert captured["candidates_dir"] == Path("/tmp/xout") / "candidates"
    # The summary line echoes the split config back.
    assert "normalize=False" in result.stdout


def test_train_direction_defaults_to_sealed_split(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return _REPORT

    monkeypatch.setattr("mktmtm.ml.train_direction.run", fake_run)
    result = runner.invoke(app, ["train-direction"])

    assert result.exit_code == 0, result.stdout
    assert captured["train_end"] is None
    assert captured["valid_end"] is None
    assert captured["normalize"] is True
    assert captured["evaluation_dir"] == settings.models_dir / "evaluation"
    assert captured["candidates_dir"] == settings.models_dir / "candidates"


def test_train_direction_help_lists_the_four_options() -> None:
    result = runner.invoke(app, ["train-direction", "--help"])
    assert result.exit_code == 0, result.stdout
    for option in ("--train-end", "--valid-end", "--output-dir", "--no-normalize"):
        assert option in result.stdout
