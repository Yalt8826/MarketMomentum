from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from mktmtm.config.logging import configure_logging
from mktmtm.config.settings import settings

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
        labeled = int(df["next_day_return"].notna().sum())
        typer.echo(f"Wrote {written} rows to ml_dataset ({labeled} with targets).")
        if not no_export:
            out = export_path or (settings.processed_data_dir / "ml_dataset.parquet")
            export_parquet(df, out)
            typer.echo(f"Parquet export: {out}")
    finally:
        conn.close()


@app.command(name="train-direction")
def train_direction(
    train_end: Annotated[
        datetime | None,
        typer.Option(
            "--train-end",
            help="TRAIN window end date (YYYY-MM-DD); default: sealed 2024-12-31.",
        ),
    ] = None,
    valid_end: Annotated[
        datetime | None,
        typer.Option(
            "--valid-end",
            help="VALID window end date (YYYY-MM-DD); default: sealed 2025-12-31. "
            "TEST = everything after it, and is never fit on.",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help="Models root for artifacts; evaluation/ and candidates/ land under it "
            "(default: <repo>/models).",
        ),
    ] = None,
    no_normalize: Annotated[
        bool,
        typer.Option(
            "--no-normalize",
            help="Skip per-date rank normalization of the continuous features.",
        ),
    ] = False,
) -> None:
    """Train the XGBoost next-day direction model (Phase 6 exit test).

    Fits on TRAIN with early stopping on VALID (AUC), runs the 5-fold
    walk-forward robustness check over the TRAIN+VALID era, and writes
    xgb_direction_valid.json plus the model to <output-dir>/candidates/.
    TEST (everything after --valid-end) stays sealed.
    """
    import pandas as pd

    from mktmtm.ml.train_direction import run as run_training

    base = output_dir if output_dir is not None else settings.models_dir
    report = run_training(
        evaluation_dir=base / "evaluation",
        candidates_dir=base / "candidates",
        train_end=pd.Timestamp(train_end) if train_end is not None else None,
        valid_end=pd.Timestamp(valid_end) if valid_end is not None else None,
        normalize=not no_normalize,
    )
    valid_metrics = report["valid"]["metrics"]
    typer.echo(
        f"Trained xgb_direction: VALID AUC {valid_metrics['auc']:.4f}, "
        f"mean IC {valid_metrics['mean_ic']:.4f}, ICIR {valid_metrics['icir']:.4f} "
        f"(split train_end={report['split']['train_end']}, "
        f"valid_end={report['split']['valid_end']}, normalize={report['normalize']})"
    )


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


def main() -> None:
    app()
