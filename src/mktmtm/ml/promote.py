"""Promotion of an accepted direction-model candidate (Phase 8, Task 8.2).

Copies the accepted model from ``models/candidates/`` into ``models/promoted/``
together with its evaluation JSON. Artifacts stay local — ``models/*`` is
gitignored — so the promotion is a plain, verifiable file copy; the model card
in ``docs/models/`` is the committed record.

Public API:

- ``promote`` — copy a candidate model + its evaluation JSON into the promoted
  directory, verifying the copies byte-for-byte (sha256).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default artifact location (repo root relative; models/* is gitignored).
PROMOTED_DIR = Path("models/promoted")


def _sha256(path: Path) -> str:
    """Hex sha256 of a file, streamed (model files can be tens of MB)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def promote(
    model: Path,
    evaluation: Path,
    promoted_dir: Path = PROMOTED_DIR,
) -> dict[str, Any]:
    """Copy an accepted candidate model and its evaluation JSON into ``promoted_dir``.

    Both files are copied byte-for-byte (``shutil.copy2``, verified by sha256)
    into ``promoted_dir``, preserving their filenames. Returns a provenance
    record: sources, destinations, both sha256 digests, and the promotion
    timestamp (UTC).

    Raises
    ------
    FileNotFoundError
        If ``model`` or ``evaluation`` is not a file.
    """
    for name, path in (("model", model), ("evaluation", evaluation)):
        if not path.is_file():
            raise FileNotFoundError(f"{name} artifact not found: {path}")

    promoted_dir.mkdir(parents=True, exist_ok=True)
    model_dest = promoted_dir / model.name
    evaluation_dest = promoted_dir / evaluation.name
    shutil.copy2(model, model_dest)
    shutil.copy2(evaluation, evaluation_dest)

    record: dict[str, Any] = {
        "model_source": str(model),
        "evaluation_source": str(evaluation),
        "model_dest": str(model_dest),
        "evaluation_dest": str(evaluation_dest),
        "model_sha256": _sha256(model_dest),
        "evaluation_sha256": _sha256(evaluation_dest),
        "promoted_at": datetime.now(UTC).isoformat(),
    }
    # Integrity: the promoted copies are byte-identical to the sources.
    if record["model_sha256"] != _sha256(model):
        raise RuntimeError(f"promoted model copy failed sha256 check: {model_dest}")
    if record["evaluation_sha256"] != _sha256(evaluation):
        raise RuntimeError(
            f"promoted evaluation copy failed sha256 check: {evaluation_dest}"
        )
    logger.info("promoted %s -> %s with %s", model, model_dest, evaluation_dest)
    return record
