from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from mktmtm.config.logging import configure_logging
from mktmtm.config.settings import settings
from mktmtm.features.flow import OUTLIER_MIN_SHARE, OUTLIER_Z

app = typer.Typer(
    name="mktmtm",
    help="MktMtm market research and ranking system.",
)


def _parse_csv[ListItem](
    value: str | None, converter: Callable[[str], ListItem]
) -> list[ListItem] | None:
    """Split a comma-separated CLI value into items; None/empty keeps the full scope."""
    if value is None or not value.strip():
        return None
    return [converter(item.strip()) for item in value.split(",") if item.strip()]


@app.callback()
def cli() -> None:
    """MktMtm market research and ranking system."""
    configure_logging()


@app.command()
def status() -> None:
    """Show system status."""
    typer.echo("MktMtm is running.")
    typer.echo(f"Environment: {settings.environment}")
    typer.echo(f"Data dir: {settings.data_dir}")
    typer.echo(f"Raw data dir: {settings.raw_data_dir}")
    typer.echo(f"Processed data dir: {settings.processed_data_dir}")
    typer.echo(f"Reference data dir: {settings.reference_data_dir}")
    typer.echo(f"PostgreSQL: {settings.pg_host}:{settings.pg_port}/{settings.pg_db}")
    typer.echo(f"Log level: {settings.log_level}")


@app.command(name="fetch-bhavcopy")
def fetch_bhavcopy(
    start_date: Annotated[
        datetime | None,
        typer.Option("--start-date", help="Start date (YYYY-MM-DD); defaults to 5 years before --end-date."),
    ] = None,
    end_date: Annotated[
        datetime | None,
        typer.Option("--end-date", help="End date (YYYY-MM-DD); defaults to today."),
    ] = None,
    max_workers: Annotated[
        int,
        typer.Option("--max-workers", min=1, help="Number of parallel download workers."),
    ] = settings.max_workers,
) -> None:
    """Download and filter NSE F&O bhavcopy files."""
    from mktmtm.data.bhavcopy import run_pipeline

    processed = run_pipeline(start_date=start_date, end_date=end_date, max_workers=max_workers)
    typer.echo(f"Downloaded and filtered bhavcopy for {processed} trading days.")


@app.command(name="fetch-participant-oi")
def fetch_participant_oi(
    start_date: Annotated[
        datetime | None,
        typer.Option("--start-date", help="Start date (YYYY-MM-DD); defaults to 5 years before --end-date."),
    ] = None,
    end_date: Annotated[
        datetime | None,
        typer.Option("--end-date", help="End date (YYYY-MM-DD); defaults to today."),
    ] = None,
    delay: Annotated[
        float,
        typer.Option("--delay", min=0.0, help="Seconds between requests (global; the fetch is single-threaded)."),
    ] = 0.5,
    load: Annotated[
        bool,
        typer.Option("--load/--no-load", help="Upsert the cached CSVs into participant_oi after downloading."),
    ] = True,
) -> None:
    """Download NSE participant-wise F&O open interest (Client/DII/FII/Pro)."""
    import psycopg2

    from mktmtm.data.participant_oi import (
        FetchOutcome,
        load_participant_frame,
        run_pipeline,
        write_participant_oi,
    )
    from mktmtm.storage.db import build_dsn

    tally = run_pipeline(start_date=start_date, end_date=end_date, delay=delay)
    failures = sum(n for o, n in tally.items() if o.is_failure)
    typer.echo(
        f"Participant OI: {tally[FetchOutcome.OK]} fetched, "
        f"{tally[FetchOutcome.CACHED]} already cached, "
        f"{tally[FetchOutcome.NO_DATA]} holidays, {failures} failed."
    )
    if failures:
        typer.echo("Some days failed for reasons other than a closed market — re-run to retry.")

    if not load:
        return
    frame = load_participant_frame()
    conn = psycopg2.connect(build_dsn())
    try:
        written = write_participant_oi(conn, frame)
    finally:
        conn.close()
    days = frame["date"].nunique()
    typer.echo(f"Loaded {written} rows across {days} trading days into participant_oi.")


@app.command(name="preparse-bhavcopy")
def preparse_bhavcopy() -> None:
    """Build the single-pass parquet cache of all bhavcopy CSVs (speeds up `etl` ~4x)."""
    from mktmtm.data.bhavcopy import build_bhavcopy_cache

    src = settings.raw_data_dir / "bhavcopy" / "extracted"
    out = settings.processed_data_dir / "bhavcopy_cache.parquet"
    build_bhavcopy_cache(src, out)
    typer.echo(f"Cache written: {out} — `mktmtm etl` will pick it up automatically.")


@app.command(name="fetch-yahoo")
def fetch_yahoo(
    days: Annotated[
        int,
        typer.Option(
            "--days",
            min=1,
            help="Number of days of daily closes to fetch per symbol (default 2500 ≈ full 5-year bhavcopy window).",
        ),
    ] = 2500,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Output directory (default: <raw_data_dir>/yahoo_finance)."),
    ] = None,
) -> None:
    """Download daily closes for all F&O symbols from Yahoo Finance."""
    from mktmtm.data.yahoo import download_yahoo_prices

    processed = download_yahoo_prices(output_dir=output_dir, days=days)
    typer.echo(f"Downloaded prices for {processed} symbols.")


@app.command()
def process(
    limit_days: Annotated[
        int | None,
        typer.Option(
            "--limit-days",
            min=1,
            help="Process only the N most recent bhavcopy files (useful for smoke runs).",
        ),
    ] = None,
) -> None:
    """Run the processing engine (bhavcopy + spot + rates -> enriched timestamps JSON)."""
    from mktmtm.pipeline.process import main as run_process

    out_path = run_process(limit_days=limit_days)
    typer.echo(f"Processed data written to {out_path}")


@app.command()
def etl() -> None:
    """Run the threaded per-symbol ETL into PostgreSQL."""
    from mktmtm.storage.etl import main as run_etl

    run_etl()
    typer.echo("ETL finished.")


@app.command(name="make-dataset")
def make_dataset(
    limit_symbols: Annotated[
        str | None,
        typer.Option(
            "--limit-symbols",
            help="Comma-separated symbol subset (smoke runs).",
        ),
    ] = None,
    limit_dates: Annotated[
        int | None,
        typer.Option(
            "--limit-dates",
            min=1,
            help="Only the N most recent trading dates (smoke runs).",
        ),
    ] = None,
    export_path: Annotated[
        Path | None,
        typer.Option(
            "--export-parquet",
            help="Parquet output path (default: <processed_data_dir>/ml_dataset.parquet).",
        ),
    ] = None,
    no_export: Annotated[
        bool,
        typer.Option("--no-export", help="Skip the parquet export (PG only)."),
    ] = False,
) -> None:
    """Build the ML training dataset (features + targets) into ml_dataset (+ parquet)."""
    import psycopg2

    from mktmtm.ml.dataset import build_dataset, export_parquet, write_dataset
    from mktmtm.storage.db import build_dsn

    syms = _parse_csv(limit_symbols, str)
    conn = psycopg2.connect(build_dsn())
    try:
        df = build_dataset(conn, symbols=syms, limit_dates=limit_dates)
        if df.empty:
            typer.echo("option_metrics is empty — run fetch-bhavcopy/fetch-yahoo, process, etl first.")
            raise typer.Exit(code=1)
        written = write_dataset(conn, df)
        from mktmtm.ml.dataset import TARGET_COLUMNS

        labeled = int(df[[c for c in TARGET_COLUMNS if c in df.columns]].notna().any(axis=1).sum())
        typer.echo(f"Wrote {written} rows to ml_dataset ({labeled} with targets).")
        if not no_export:
            out = export_path or (settings.processed_data_dir / "ml_dataset.parquet")
            export_parquet(df, out)
            typer.echo(f"Parquet export: {out}")
    finally:
        conn.close()


@app.command()
def backtest(
    symbols: Annotated[
        str | None,
        typer.Option(
            "--symbols",
            help="Comma-separated symbols to backtest (default: all catalog symbols).",
        ),
    ] = None,
    years: Annotated[
        str | None,
        typer.Option(
            "--years",
            help="Comma-separated years, e.g. 2022,2023 (default: last 4 years, inclusive).",
        ),
    ] = None,
    months: Annotated[
        str | None,
        typer.Option(
            "--months",
            help="Comma-separated months 1-12, e.g. 1,2 (default: all 12 months).",
        ),
    ] = None,
) -> None:
    """Run the short-strangle backtester and persist results to PostgreSQL."""
    from mktmtm.backtest.strangle import run_backtest

    processed = run_backtest(
        symbols=_parse_csv(symbols, str),
        years=_parse_csv(years, int),
        months=_parse_csv(months, int),
    )
    typer.echo(f"Backtest finished; processed {processed} symbol-months.")


@app.command(name="gex-features")
def gex_features(
    limit_symbols: Annotated[
        str | None,
        typer.Option(
            "--limit-symbols",
            help="Comma-separated symbol subset (smoke runs); default: all symbols in option_metrics.",
        ),
    ] = None,
    limit_dates: Annotated[
        int | None,
        typer.Option(
            "--limit-dates",
            min=1,
            help="Only the N most recent trading dates (smoke runs).",
        ),
    ] = None,
    parquet: Annotated[
        bool,
        typer.Option(
            "--parquet/--no-parquet",
            help="Re-export data/processed/ml_dataset.parquet from the DB afterwards.",
        ),
    ] = True,
) -> None:
    """Recompute dealer-exposure aggregates into ml_dataset (no full make-dataset).

    Refreshes only the GEX_COLUMNS (dex, gex, gex_chg, vanna_exp, charm_exp,
    oi_hhi, max_pain, call_wall/put_wall, dist_to_flip, ...) from the chain,
    leaving momentum features and targets untouched. Long phases report through
    ``mktmtm.progress`` and a final summary table is printed.

    The parquet export is re-run by default: this command writes to PostgreSQL
    only, so without it the parquet keeps the GEX values it was built with and
    silently disagrees with the table.
    """
    import psycopg2

    from mktmtm.gex_features import export_ml_dataset_parquet, recompute_dealer_aggregates
    from mktmtm.gex_pipeline import print_summary_table
    from mktmtm.storage.db import build_dsn

    syms = _parse_csv(limit_symbols, str)
    conn = psycopg2.connect(build_dsn())
    try:
        summary = recompute_dealer_aggregates(conn, symbols=syms, limit_dates=limit_dates)
        print_summary_table(summary)
        if parquet:
            out, n = export_ml_dataset_parquet(conn)
            typer.echo(f"Parquet re-exported: {n:,} rows -> {out}")
        else:
            typer.echo(
                "Parquet NOT re-exported — data/processed/ml_dataset.parquet now "
                "disagrees with ml_dataset. Re-run with --parquet before training from it."
            )
    finally:
        conn.close()


@app.command(name="flow-features")
def flow_features(
    limit_dates: Annotated[
        int | None,
        typer.Option(
            "--limit-dates",
            min=1,
            help="Only the N most recent trading dates (smoke runs).",
        ),
    ] = None,
    z_threshold: Annotated[
        float,
        typer.Option(
            "--z-threshold",
            min=0.0,
            help="Robust |z| an OI change must clear to be an outlier strike.",
        ),
    ] = OUTLIER_Z,
    min_share: Annotated[
        float,
        typer.Option(
            "--min-share",
            min=0.0,
            max=1.0,
            help="Minimum share of the symbol-day's total |chg_oi| a flagged strike must hold.",
        ),
    ] = OUTLIER_MIN_SHARE,
    parquet: Annotated[
        bool,
        typer.Option(
            "--parquet/--no-parquet",
            help="Also export data/processed/flow_features.parquet.",
        ),
    ] = True,
) -> None:
    """Build per-symbol flow features (outlier strikes + futures basis) into flow_features.

    Reads the bhavcopy parquet cache and option_metrics; writes the
    flow_features table (and the matching parquet). Both thresholds gate the
    outlier detector and a strike must clear both — see
    docs/implementation/flow-features.md for how they were chosen.
    """
    import psycopg2

    from mktmtm.features.flow import run_flow_features
    from mktmtm.gex_pipeline import print_flow_summary_table
    from mktmtm.storage.db import build_dsn

    conn = psycopg2.connect(build_dsn())
    try:
        summary = run_flow_features(
            conn,
            limit_dates=limit_dates,
            z_threshold=z_threshold,
            min_share=min_share,
            parquet=parquet,
        )
    finally:
        conn.close()
    print_flow_summary_table(summary)


@app.command(name="market-flow")
def market_flow(
    participant: Annotated[
        list[str] | None,
        typer.Option(
            "--participant",
            help="Participant category to model; repeatable. Default: Pro, Client, FII.",
        ),
    ] = None,
    model: Annotated[
        list[str] | None,
        typer.Option("--model", help="ridge and/or xgboost; repeatable."),
    ] = None,
    target: Annotated[
        str,
        typer.Option("--target", help="participant_oi net column to predict."),
    ] = "d_net_opt_stock_call",
    parquet: Annotated[
        bool,
        typer.Option(
            "--parquet/--no-parquet",
            help="Also export data/processed/market_flow.parquet.",
        ),
    ] = True,
) -> None:
    """Stage 1: predict tomorrow's market-wide institutional flow from today's features.

    The headline number is the AUC margin over the calendar-only baseline, not
    the absolute AUC: the expiry cycle is known years in advance and
    mechanically explains roughly half the raw next-day correlation. Writes
    models/evaluation/market_flow_<participant>.json. See
    docs/implementation/model-01-market-flow.md.
    """
    import psycopg2

    from mktmtm.ml.market_flow import TARGET_PARTICIPANTS, run_market_flow
    from mktmtm.storage.db import build_dsn

    participants = tuple(participant) if participant else TARGET_PARTICIPANTS
    models = tuple(model) if model else ("ridge", "xgboost")

    conn = psycopg2.connect(build_dsn())
    try:
        summary = run_market_flow(
            conn,
            participants=participants,
            models=models,
            column=target,
            parquet=parquet,
        )
    finally:
        conn.close()
    _print_market_flow_summary(summary)


def _print_market_flow_summary(summary: dict[str, Any]) -> None:
    """Margin over the calendar first; absolute AUC is the less informative number."""
    for name, result in summary.items():
        if name.startswith("_"):
            continue
        meta = result.get("_meta", {})
        typer.echo(
            f"\n{name}  ({meta.get('period', '?')}, "
            f"{meta.get('rows_scored', meta.get('rows', 0))} of "
            f"{meta.get('rows', 0)} rows scored)"
        )
        typer.echo(f"  base rate {meta.get('label_base_rate', float('nan')):.3f}")
        for key, margin in sorted(result.get("calendar_margin", {}).items()):
            mark = "*" if margin.get("significant") else " "
            typer.echo(
                f"  {mark} {key:34s} margin {margin['auc_margin_over_calendar']:+.4f}"
                f"  [{margin['ci_low']:+.4f}, {margin['ci_high']:+.4f}]"
            )
        for key, metrics in sorted(result.get("baselines", {}).items()):
            typer.echo(f"    baseline {key:24s} auc {metrics['auc']:.4f}")
    check = summary.get("_structural_check")
    if check:
        verdict = "PASS" if check["passes"] else "FAIL"
        typer.echo(
            f"\nstructural check (Pro vs Client prediction corr): "
            f"{check['pro_client_prediction_corr']:+.4f}  {verdict}"
        )


@app.command(name="test-gex")
def test_gex(
    limit_symbols: Annotated[
        str | None,
        typer.Option(
            "--limit-symbols",
            help="Comma-separated symbol subset (smoke runs); default: all symbols in ml_dataset.",
        ),
    ] = None,
    limit_dates: Annotated[
        int | None,
        typer.Option(
            "--limit-dates",
            min=1,
            help="Only the N most recent trading dates (smoke runs).",
        ),
    ] = None,
    out_path: Annotated[
        Path | None,
        typer.Option("--out", help="JSON output path (default: models/evaluation/gex_h1.json)."),
    ] = None,
    no_model: Annotated[
        bool,
        typer.Option("--no-model", help="Skip Task 6.3 even if H1/H2 shows signal."),
    ] = False,
) -> None:
    """Run Phase 6's hypothesis tests (H1/H2/robustness), write the JSON, print a results table."""
    import psycopg2

    from mktmtm.gex_pipeline import print_results_table
    from mktmtm.ml.gex_hypothesis import DEFAULT_OUT_PATH, MissingDataError, run_exit_test
    from mktmtm.storage.db import build_dsn

    syms = _parse_csv(limit_symbols, str)
    path = out_path if out_path is not None else DEFAULT_OUT_PATH
    conn = psycopg2.connect(build_dsn())
    try:
        try:
            result = run_exit_test(
                conn,
                out_path=path,
                symbols=syms,
                limit_dates=limit_dates,
                run_model_if_signal=not no_model,
            )
        except MissingDataError as exc:
            typer.echo(f"test-gex: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(f"Wrote {path}")
    print_results_table(result)


@app.command(name="gex-pipeline")
def gex_pipeline(
    limit_symbols: Annotated[
        str | None,
        typer.Option(
            "--limit-symbols",
            help="Comma-separated symbol subset for gex-features/test-gex (smoke runs).",
        ),
    ] = None,
    limit_dates: Annotated[
        int | None,
        typer.Option(
            "--limit-dates",
            min=1,
            help="Only the N most recent trading dates (smoke runs).",
        ),
    ] = None,
    out_path: Annotated[
        Path | None,
        typer.Option("--out", help="test-gex JSON output path (default: models/evaluation/gex_h1.json)."),
    ] = None,
    no_model: Annotated[
        bool,
        typer.Option("--no-model", help="Skip Task 6.3 even if H1/H2 shows signal."),
    ] = False,
    skip: Annotated[
        str | None,
        typer.Option(
            "--skip",
            help="Comma-separated stages to skip: preparse,etl,make-dataset,test-gex.",
        ),
    ] = None,
) -> None:
    """Run preparse → etl → make-dataset → test-gex with staged progress UI.

    Each stage calls the same function its standalone command calls; this adds
    per-stage banners and a final results table (rich on a TTY, log lines
    otherwise).
    """
    from mktmtm.gex_pipeline import run_gex_pipeline

    run_gex_pipeline(
        limit_symbols=limit_symbols,
        limit_dates=limit_dates,
        out_path=out_path,
        run_model=not no_model,
        skip=set(skip.split(",")) if skip and skip.strip() else None,
    )


def main() -> None:
    app()
