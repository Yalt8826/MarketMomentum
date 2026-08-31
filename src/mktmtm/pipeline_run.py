"""End-to-end training pipeline orchestrator.

Runs the full data + training chain as one command::

    fetch-bhavcopy → preparse-bhavcopy → etl → make-dataset → train-direction

Each stage calls the *exact same* function its standalone CLI command calls, so
behaviour is identical whether you run ``mktmtm run-pipeline`` or the five
commands in a row — this module only adds the orchestration UI on top: a
per-stage banner, a live stage counter, and a final results table.

Progress reporting is the repo's standard: rich bars on a TTY, degraded to
log lines when stderr is not a TTY (or ``MKTMTM_NO_PROGRESS=1``), so
``mktmtm run-pipeline > run.log`` stays readable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from mktmtm.config.settings import settings
from mktmtm.progress import use_rich

__all__ = ["run_pipeline"]


# ─────────────────────────────────────────────────────────────────────────────
# Stage wrappers — thin, call the same functions the standalone CLI commands do.
# Each returns a one-line human summary string.
# ─────────────────────────────────────────────────────────────────────────────


def _stage_fetch(start_date: datetime | None, end_date: datetime | None, max_workers: int) -> str:
    from mktmtm.data.bhavcopy import run_pipeline

    processed = run_pipeline(start_date=start_date, end_date=end_date, max_workers=max_workers)
    return f"downloaded {processed} trading days"


def _stage_preparse() -> str:
    from mktmtm.data.bhavcopy import build_bhavcopy_cache

    src = settings.raw_data_dir / "bhavcopy" / "extracted"
    out = settings.processed_data_dir / "bhavcopy_cache.parquet"
    build_bhavcopy_cache(src, out)
    return f"cache -> {out}"


def _stage_etl() -> str:
    from mktmtm.storage.etl import main as run_etl

    run_etl()
    return "option_metrics refreshed"


def _stage_dataset(
    limit_symbols: str | None,
    limit_dates: int | None,
    no_export: bool,
) -> str:
    import psycopg2

    from mktmtm.ml.dataset import build_dataset, export_parquet, write_dataset
    from mktmtm.storage.db import build_dsn

    syms = [s.strip() for s in limit_symbols.split(",") if s.strip()] if limit_symbols else None
    conn = psycopg2.connect(build_dsn())
    try:
        df = build_dataset(conn, symbols=syms, limit_dates=limit_dates)
        if df.empty:
            raise RuntimeError("option_metrics is empty — run fetch/preparse/etl first")
        written = write_dataset(conn, df)
    finally:
        conn.close()

    labeled = int(df["next_day_return"].notna().sum())
    summary = f"{written} rows -> ml_dataset ({labeled} with targets)"
    if not no_export:
        out = settings.processed_data_dir / "ml_dataset.parquet"
        export_parquet(df, out)
        summary += f"; parquet -> {out}"
    return summary


def _stage_train(
    train_end: datetime | None,
    valid_end: datetime | None,
    normalize: bool,
) -> str:
    import pandas as pd

    from mktmtm.ml.train_direction import run as run_training

    report = run_training(
        evaluation_dir=settings.evaluation_dir,
        candidates_dir=settings.candidates_dir,
        train_end=pd.Timestamp(train_end) if train_end is not None else None,
        valid_end=pd.Timestamp(valid_end) if valid_end is not None else None,
        normalize=normalize,
    )
    m = report["valid"]["metrics"]
    wf = report["walk_forward"]
    return (
        f"VALID AUC {m['auc']:.4f}, mean IC {m['mean_ic']:.4f}, "
        f"ICIR {m['icir']:.4f}; walk-forward ICIR {wf['icir']:.4f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration UI
# ─────────────────────────────────────────────────────────────────────────────

_Stage = tuple[str, Callable[[], str]]


def _rich_run(stages: list[_Stage]) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.rule("[bold]MktMtm training pipeline", style="blue")
    rows: list[tuple[str, str]] = []
    for i, (name, fn) in enumerate(stages, start=1):
        console.print(f"[bold blue]Stage {i}/{len(stages)}[/] — {name}")
        summary = fn()
        rows.append((name, summary))
        console.print(f"  [green]✓[/green] {summary}\n")
    console.rule("[bold]Summary", style="green")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Stage")
    table.add_column("Result")
    for name, summary in rows:
        table.add_row(name, summary)
    console.print(table)


def _plain_run(stages: list[_Stage]) -> None:
    import logging

    logger = logging.getLogger("mktmtm.pipeline")
    for i, (name, fn) in enumerate(stages, start=1):
        logger.info("stage %d/%d — %s", i, len(stages), name)
        summary = fn()
        logger.info("stage %d/%d done — %s", i, len(stages), summary)


def run_pipeline(
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    max_workers: int | None = None,
    limit_symbols: str | None = None,
    limit_dates: int | None = None,
    no_export: bool = False,
    no_normalize: bool = False,
    skip: set[str] | None = None,
) -> None:
    """Run the five-stage pipeline in order.

    ``skip`` holds the stage names to skip (e.g. ``{"etl"}``) — useful for
    smoke runs where earlier stages' outputs already exist. Named stages are
    ``fetch``, ``preparse``, ``etl``, ``dataset``, ``train``.
    """
    workers = max_workers if max_workers is not None else settings.max_workers
    skip = skip or set()
    train_end: datetime | None = None
    valid_end: datetime | None = None

    stages: list[_Stage] = []
    if "fetch" not in skip:
        stages.append(
            ("fetch-bhavcopy", lambda: _stage_fetch(start_date, end_date, workers))
        )
    if "preparse" not in skip:
        stages.append(("preparse-bhavcopy", _stage_preparse))
    if "etl" not in skip:
        stages.append(("etl", _stage_etl))
    if "dataset" not in skip:
        stages.append(
            (
                "make-dataset",
                lambda: _stage_dataset(limit_symbols, limit_dates, no_export),
            )
        )
    if "train" not in skip:
        stages.append(
            (
                "train-direction",
                lambda: _stage_train(train_end, valid_end, not no_normalize),
            )
        )

    if not stages:
        return

    if use_rich():
        _rich_run(stages)
    else:
        _plain_run(stages)
