"""End-to-end GEX pipeline orchestrator.

Runs the GEX data + test chain as one command::

    preparse-bhavcopy → etl → gex-features → test-gex

Each stage calls the *exact same* function its standalone CLI command calls,
so behaviour is identical whether you run ``mktmtm gex-pipeline`` or the four
commands in a row — this module only adds the orchestration UI on top: a
per-stage banner, a live stage counter, and a final results table.

This is a **port of the UI pattern** from the direction project's
``pipeline_run.py`` (branch ``model/xgboost-direction``), not a copy of the
file — that module imports ``ml/train_direction.py``, which does not exist on
this branch. Progress reporting is the repo's standard: rich bars on a TTY,
degraded to log lines when stderr is not a TTY (or ``MKTMTM_NO_PROGRESS=1``),
so ``mktmtm gex-pipeline > run.log`` stays readable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mktmtm.config.settings import settings
from mktmtm.progress import use_rich

logger = logging.getLogger(__name__)

__all__ = ["print_results_table", "print_summary_table", "run_gex_pipeline"]


# ─────────────────────────────────────────────────────────────────────────────
# Stage wrappers — thin, call the same functions the standalone CLI commands do.
# Each returns a one-line human summary string.
# ─────────────────────────────────────────────────────────────────────────────


def _parse_symbols(limit_symbols: str | None) -> list[str] | None:
    return [s.strip() for s in limit_symbols.split(",") if s.strip()] if limit_symbols else None


def _stage_preparse() -> str:
    from mktmtm.data.bhavcopy import build_bhavcopy_cache

    src = settings.raw_data_dir / "bhavcopy" / "extracted"
    out = settings.processed_data_dir / "bhavcopy_cache.parquet"
    build_bhavcopy_cache(src, out)
    return f"cache -> {out}"


def _stage_etl() -> str:
    from mktmtm.storage.etl import main as run_etl

    run_etl()
    return "option_metrics refreshed (gamma/vanna/charm baked into every chain element)"


def _stage_make_dataset(limit_symbols: str | None, limit_dates: int | None) -> str:
    """Rebuild ``ml_dataset`` — features, dealer aggregates *and* targets.

    This is the stage that writes ``TARGET_COLUMNS``; ``gex-features`` refreshes
    the dealer aggregates only, so a pipeline without this one leaves
    ``next_day_abs_return`` NULL and ``test-gex`` blocks with "no complete
    (gex, next_day_abs_return) rows".
    """
    import psycopg2

    from mktmtm.ml.dataset import (
        TARGET_COLUMNS,
        build_dataset,
        export_parquet,
        write_dataset,
    )
    from mktmtm.storage.db import build_dsn

    conn = psycopg2.connect(build_dsn())
    try:
        df = build_dataset(
            conn, symbols=_parse_symbols(limit_symbols), limit_dates=limit_dates
        )
        if df.empty:
            return "no rows — option_metrics is empty"
        written = write_dataset(conn, df)
    finally:
        conn.close()
    targets = [c for c in TARGET_COLUMNS if c in df.columns]
    labeled = int(df[targets].notna().any(axis=1).sum())
    out = export_parquet(df, settings.processed_data_dir / "ml_dataset.parquet")
    return f"{written} rows -> ml_dataset ({labeled} with targets); parquet -> {out}"


def _stage_gex_features(limit_symbols: str | None, limit_dates: int | None) -> str:
    import psycopg2

    from mktmtm.gex_features import recompute_dealer_aggregates
    from mktmtm.storage.db import build_dsn

    conn = psycopg2.connect(build_dsn())
    try:
        summary = recompute_dealer_aggregates(
            conn, symbols=_parse_symbols(limit_symbols), limit_dates=limit_dates
        )
    finally:
        conn.close()
    skipped = f", skipped {summary['skipped']}" if summary["skipped"] else ""
    return (
        f"{summary['rows_updated']} rows -> ml_dataset "
        f"({summary['symbols']} symbols){skipped}"
    )


def _stage_test_gex(
    limit_symbols: str | None,
    limit_dates: int | None,
    out_path: str | Path | None,
    run_model: bool,
) -> str:
    import psycopg2

    from mktmtm.ml.gex_hypothesis import DEFAULT_OUT_PATH, MissingDataError, run_exit_test
    from mktmtm.storage.db import build_dsn

    path = out_path if out_path is not None else DEFAULT_OUT_PATH
    conn = psycopg2.connect(build_dsn())
    try:
        try:
            result = run_exit_test(
                conn,
                out_path=path,
                symbols=_parse_symbols(limit_symbols),
                limit_dates=limit_dates,
                run_model_if_signal=run_model,
            )
        except MissingDataError as exc:
            # The designed outcome when Phases 2-5 data hasn't landed yet —
            # report it as a blocked stage, never a hard crash.
            return f"blocked — {exc}"
    finally:
        conn.close()
    h1 = result["h1_sign_test"]
    ic = h1["ic_neg_gex"]
    return (
        f"H1 Welch p={h1['welch_p_value']:.3g}, IC(-gex) {ic['mean_ic']:+.4f} "
        f"(t={ic['ic_t_stat']:+.2f}); JSON -> {path}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration UI — ported from pipeline_run.py (model/xgboost-direction).
# ─────────────────────────────────────────────────────────────────────────────

_Stage = tuple[str, Callable[[], str]]


def _rich_run(stages: list[_Stage], title: str = "MktMtm GEX pipeline") -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.rule(f"[bold]{title}", style="blue")
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
    for i, (name, fn) in enumerate(stages, start=1):
        logger.info("stage %d/%d — %s", i, len(stages), name)
        summary = fn()
        logger.info("stage %d/%d done — %s", i, len(stages), summary)


def run_gex_pipeline(
    *,
    limit_symbols: str | None = None,
    limit_dates: int | None = None,
    out_path: str | Path | None = None,
    run_model: bool = True,
    skip: set[str] | None = None,
    include: set[str] | None = None,
) -> None:
    """Run the GEX pipeline in order: preparse -> etl -> make-dataset -> test-gex.

    ``skip`` holds the stage names to skip (``preparse``, ``etl``,
    ``make-dataset``, ``test-gex``) — useful for smoke runs where earlier
    stages' outputs already exist. ``include`` opts extra stages in;
    ``gex-features`` is the only one, and only useful when ``make-dataset`` is
    skipped.
    """
    skip = skip or set()
    stages: list[_Stage] = []
    if "preparse" not in skip:
        stages.append(("preparse-bhavcopy", _stage_preparse))
    if "etl" not in skip:
        stages.append(("etl", _stage_etl))
    if "make-dataset" not in skip:
        stages.append(
            ("make-dataset", lambda: _stage_make_dataset(limit_symbols, limit_dates))
        )
    # gex-features is redundant after make-dataset (build_dataset writes the
    # GEX_COLUMNS too), so it is opt-in here; it stays the fast standalone
    # refresh when only the aggregate SQL changed and targets are already good.
    if "gex-features" in (include or set()):
        stages.append(("gex-features", lambda: _stage_gex_features(limit_symbols, limit_dates)))
    if "test-gex" not in skip:
        stages.append(
            ("test-gex", lambda: _stage_test_gex(limit_symbols, limit_dates, out_path, run_model))
        )

    if not stages:
        return

    if use_rich():
        _rich_run(stages)
    else:
        _plain_run(stages)


# ─────────────────────────────────────────────────────────────────────────────
# Summary tables shared by the standalone commands and the orchestrator.
# ─────────────────────────────────────────────────────────────────────────────


def _table_or_log(title: str, rows: list[tuple[str, str]]) -> None:
    """Render ``rows`` as a rich ``Table`` on a TTY, log lines otherwise.

    This is the §2 final-summary contract: the rich path must never be the
    only thing that works — ``mktmtm ... > run.log`` and
    ``MKTMTM_NO_PROGRESS=1`` both degrade to readable log lines.
    """
    if use_rich():
        from rich.console import Console
        from rich.table import Table

        table = Table(title=title, show_header=True, header_style="bold")
        table.add_column("Metric")
        table.add_column("Value")
        for metric, value in rows:
            table.add_row(metric, value)
        Console().print(table)
    else:
        logger.info("%s:", title)
        for metric, value in rows:
            logger.info("  %-24s %s", metric, value)


def print_summary_table(summary: dict[str, Any]) -> None:
    """Final ``gex-features`` summary: rows written, symbols covered, skipped."""
    _table_or_log(
        "gex-features summary",
        [
            ("symbols", str(summary["symbols"])),
            ("rows updated", str(summary["rows_updated"])),
            ("skipped", ", ".join(summary["skipped"]) or "—"),
            ("columns", ", ".join(summary["columns"])),
        ],
    )


def _robustness_line(name: str, check: dict[str, Any]) -> tuple[str, str]:
    """One table row per robustness check (sign-test-shaped or blocked)."""
    if "status" in check:  # blocked entry: {"status": ..., "reason": ...}
        return (f"robustness: {name}", f"blocked — {check.get('reason', '')}")
    # The confound controls are not sign-test-shaped; each gets its own line.
    if name == "sign_vs_magnitude":
        signed = check["ic_signed"]["mean_ic"]
        mag = check["ic_magnitude_only"]["mean_ic"]
        verdict = "sign adds info" if check["sign_adds_information"] else "SIGN ADDS NOTHING"
        return (
            f"robustness: {name}",
            f"IC(-gex)={signed:+.4f} vs IC(-|gex|)={mag:+.4f} — {verdict}",
        )
    if name == "iv_control":
        base = check["ic_control_alone"]
        gex = check.get("-gex", {})
        return (
            f"robustness: {name}",
            f"IC({check['control']})={base['mean_ic']:+.4f} alone; "
            f"IC(-gex | {check['control']})={gex.get('mean_ic', float('nan')):+.4f} "
            f"(t={gex.get('ic_t_stat', float('nan')):+.2f})",
        )
    if name == "within_symbol":
        partial = check.get("partial_spearman_controlling_own_iv")
        tail = f", | own IV {partial:+.4f}" if partial is not None else ""
        return (
            f"robustness: {name}",
            f"h1_consistent={check['h1_consistent']}, "
            f"rho={check['spearman_gex_z_vs_abs_return']:+.4f}{tail}, "
            f"decile ratio {check['decile_ratio']:.2f}x",
        )
    h1 = check.get("h1_consistent", "—")
    ic = check.get("ic_neg_gex", {})
    mean_ic = ic.get("mean_ic")
    return (
        f"robustness: {name}",
        f"h1_consistent={h1}, IC(-gex)={mean_ic:+.4f}" if mean_ic is not None else f"h1_consistent={h1}",
    )


def print_results_table(result: dict[str, Any]) -> None:
    """Phase-6 results as a table: H1, H2, model verdict, robustness checks."""
    data = result.get("data", {})
    h1 = result.get("h1_sign_test", {})
    ic = h1.get("ic_neg_gex", {})
    rows: list[tuple[str, str]] = [
        ("phase", str(result.get("phase", ""))),
        ("rows / symbols / dates", f"{data.get('rows')} / {data.get('symbols')} / {data.get('dates')}"),
        ("period", str(data.get("period"))),
    ]
    pos, neg = h1.get("gex_positive", {}), h1.get("gex_negative", {})
    rows.append(("H1 gex>0 n / mean abs ret", f"{pos.get('n')} / {pos.get('mean_abs_return')}"))
    rows.append(("H1 gex<0 n / mean abs ret", f"{neg.get('n')} / {neg.get('mean_abs_return')}"))
    rows.append(("H1 Welch t / p", f"{h1.get('welch_t_stat')} / {h1.get('welch_p_value')}"))
    rows.append(("H1 consistent", str(h1.get("h1_consistent"))))
    rows.append(("IC(-gex) mean / t", f"{ic.get('mean_ic')} / {ic.get('ic_t_stat')}"))
    for feature, entry in (result.get("h2_positioning_change") or {}).items():
        pic = entry.get("partial_ic_controlling_gex", {})
        rows.append((f"H2 {feature} partial IC", f"{pic.get('mean_ic')} (t={pic.get('ic_t_stat')})"))
    model = result.get("model", {})
    rows.append(("model", f"ran={model.get('ran')} — {model.get('reason')}"))
    for name, check in (result.get("robustness") or {}).items():
        rows.append(_robustness_line(name, check))
    _table_or_log("test-gex results", rows)
