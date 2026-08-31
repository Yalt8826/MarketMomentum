"""Unit tests for mktmtm.ml.promote — Phase 8, Task 8.2 promotion.

Pins the promotion contract: the accepted candidate model and its evaluation
JSON are copied byte-for-byte into the promoted directory, verified by sha256,
and missing artifacts fail loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mktmtm.ml.promote import promote


def _artifacts(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "xgb_direction_test_2026-08-31.json"
    model.write_bytes(b"{\"learner\":{\"attributes\":{}}}\n")
    evaluation = tmp_path / "xgb_direction_test.json"
    evaluation.write_text(
        json.dumps({"model": "xgb_direction", "test": {"metrics": {"auc": 0.5152}}}),
        encoding="utf-8",
    )
    return model, evaluation


def test_promote_copies_model_and_evaluation_byte_identical(tmp_path: Path) -> None:
    model, evaluation = _artifacts(tmp_path)
    promoted = tmp_path / "promoted"

    record = promote(model, evaluation, promoted_dir=promoted)

    model_dest = promoted / model.name
    evaluation_dest = promoted / evaluation.name
    assert model_dest.read_bytes() == model.read_bytes()
    assert evaluation_dest.read_text(encoding="utf-8") == evaluation.read_text(
        encoding="utf-8"
    )
    # Provenance record: sources, destinations, digests, timestamp.
    assert record["model_source"] == str(model)
    assert record["evaluation_source"] == str(evaluation)
    assert record["model_dest"] == str(model_dest)
    assert record["evaluation_dest"] == str(evaluation_dest)
    assert len(record["model_sha256"]) == 64
    assert len(record["evaluation_sha256"]) == 64
    assert record["promoted_at"]


def test_promote_creates_promoted_dir_and_preserves_names(tmp_path: Path) -> None:
    model, evaluation = _artifacts(tmp_path)
    promoted = tmp_path / "nested" / "promoted"

    promote(model, evaluation, promoted_dir=promoted)

    assert promoted.is_dir()
    assert (promoted / "xgb_direction_test_2026-08-31.json").is_file()
    assert (promoted / "xgb_direction_test.json").is_file()


def test_promote_raises_on_missing_artifacts(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="model artifact not found"):
        promote(tmp_path / "missing.json", tmp_path / "eval.json")
    model = tmp_path / "model.json"
    model.write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="evaluation artifact not found"):
        promote(model, tmp_path / "missing_eval.json")


def test_promote_is_idempotent(tmp_path: Path) -> None:
    """Re-promoting the same pair overwrites the copies without corrupting them."""
    model, evaluation = _artifacts(tmp_path)
    promoted = tmp_path / "promoted"

    first = promote(model, evaluation, promoted_dir=promoted)
    second = promote(model, evaluation, promoted_dir=promoted)

    assert first["model_sha256"] == second["model_sha256"]
    assert (promoted / model.name).read_bytes() == model.read_bytes()
