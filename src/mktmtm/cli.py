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


@app.command(name="flow-regimes")
def flow_regimes(
    states: Annotated[
        str,
        typer.Option("--states", help="Comma-separated n_states grid for the BIC search."),
    ] = "2,3,4,5,6",
    restarts: Annotated[
        int,
        typer.Option("--restarts", min=1, help="EM restarts per state count."),
    ] = 10,
    train_end: Annotated[
        datetime | None,
        typer.Option(
            "--train-end",
            formats=["%Y-%m-%d"],
            help="Seal the fit at this date (default: the ml/splits.py TRAIN_END).",
        ),
    ] = None,
    parquet: Annotated[
        bool,
        typer.Option(
            "--parquet/--no-parquet",
            help="Also export data/processed/flow_regimes.parquet.",
        ),
    ] = True,
) -> None:
    """Stage 2: fit a pooled HMM over per-symbol flow observables into flow_regimes.

    The fit is sealed at --train-end and only the Viterbi decode runs forward;
    fitting on all history and then discovering states that explain it is
    look-ahead. Reports the BIC curve and the transition matrix diagonal — a
    near-uniform diagonal means regimes are not present in these observables,
    which is a result, not a bug. See docs/implementation/model-03-regime-hmm.md.
    """
    import pandas as pd
    import psycopg2

    from mktmtm.ml.regimes import run_flow_regimes
    from mktmtm.ml.splits import TRAIN_END
    from mktmtm.storage.db import build_dsn

    grid = tuple(int(x) for x in states.split(",") if x.strip())
    conn = psycopg2.connect(build_dsn())
    try:
        result = run_flow_regimes(
            conn,
            train_end=pd.Timestamp(train_end) if train_end else TRAIN_END,
            state_grid=grid,
            n_restarts=restarts,
            parquet=parquet,
        )
    finally:
        conn.close()
    _print_regime_summary(result)


def _print_regime_summary(result: dict[str, Any]) -> None:
    """Persistence first — it decides whether anything else here is worth reading."""
    typer.echo(
        f"\nstates {result['n_states']}  "
        f"(fit sealed at {result['train_end']}, {result['train_rows']:,} train rows)"
    )
    typer.echo(
        f"  scored {result['scored_rows']:,} symbol-days "
        f"({result['retention']:.1%} retained, {result['symbols']} symbols)"
    )
    diagonal = ", ".join(f"{d:.3f}" for d in result["diagonal"])
    verdict = "PASS" if result["persistence_passes"] else "FAIL"
    typer.echo(f"  transition diagonal [{diagonal}]  persistence {verdict}")
    typer.echo("  BIC curve:")
    for k, bic in sorted(result["bic_curve"].items(), key=lambda kv: int(kv[0])):
        spread = result["restart_loglik_spread"].get(k, 0.0)
        mark = "*" if int(k) == result["n_states"] else " "
        typer.echo(f"   {mark} k={k}  bic {bic:>14,.0f}  restart spread {spread:>10,.1f}")
    validation = result.get("validation", {})
    typer.echo(
        f"  states with opposed FII/Client signs: "
        f"{validation.get('states_with_opposed_fii_client_signs', 0)}"
        f"/{validation.get('n_states', 0)}"
    )


@app.command(name="flow-attribution")
def flow_attribution(
    participant: Annotated[
        list[str] | None,
        typer.Option("--participant", help="Repeatable. Default: FII, Pro, Client."),
    ] = None,
    target: Annotated[
        str, typer.Option("--target", help="participant_oi net column to attribute.")
    ] = "d_net_opt_stock_call",
    parquet: Annotated[
        bool,
        typer.Option(
            "--parquet/--no-parquet",
            help="Also export data/processed/flow_attribution.parquet.",
        ),
    ] = True,
) -> None:
    """Stage 3: attribute the market-wide flow across symbols (Learning from Label Proportions).

    flow_score is NOT a measurement of institutional flow in a symbol — the
    per-symbol allocation is not uniquely determined by the data. Read the
    placebo and identifiability lines before the R-squared. See
    docs/implementation/model-04-attribution-bridge.md.
    """
    import psycopg2

    from mktmtm.ml.attribution import run_flow_attribution
    from mktmtm.storage.db import build_dsn

    participants = tuple(participant) if participant else ("FII", "Pro", "Client")
    conn = psycopg2.connect(build_dsn())
    try:
        summary = run_flow_attribution(
            conn, participants=participants, column=target, parquet=parquet
        )
    finally:
        conn.close()
    _print_attribution_summary(summary)


def _print_attribution_summary(summary: dict[str, Any]) -> None:
    """Placebo and identifiability first; the aggregate R-squared is the least
    informative number here despite being the one the model optimised."""
    for name, result in summary.items():
        if name.startswith("_"):
            continue
        if "error" in result:
            typer.echo(f"\n{name}: {result['error']}")
            continue
        ident = result.get("identifiability", {})
        placebo = result.get("placebo", {})
        earnings = result.get("earnings", {})
        typer.echo(f"\n{name}  (alpha {result['alpha']}, {result['bags']} bags)")
        typer.echo(
            f"  identifiability   linear-vs-tree rank corr "
            f"{ident.get('per_symbol_rank_correlation', float('nan')):+.4f}"
            f"   {'IDENTIFIED' if ident.get('identified') else 'NOT IDENTIFIED'}"
        )
        typer.echo(
            f"  placebo           shuffled-label oos R2 "
            f"{placebo.get('oos_r2', float('nan')):+.4f}"
            f"   (real {result['oos_r2_walk_forward']:+.4f})"
        )
        typer.echo(
            f"  earnings          pre-event lift "
            f"{earnings.get('pre_event_lift', float('nan')):+.4f}"
            f"   post {earnings.get('post_event_lift', float('nan')):+.4f}"
            f"   (placebo pre "
            f"{placebo.get('earnings', {}).get('pre_event_lift', float('nan')):+.4f})"
        )
        typer.echo(
            f"  stability         lag-1 autocorr "
            f"{result.get('stability_lag1_autocorr', float('nan')):+.4f}"
        )
    if "_rows_written" in summary:
        typer.echo(f"\nrows written {summary['_rows_written']:,}")


@app.command(name="flow-momentum")
def flow_momentum(
    parquet: Annotated[
        bool,
        typer.Option(
            "--parquet/--no-parquet",
            help="Also export data/processed/flow_momentum.parquet.",
        ),
    ] = True,
) -> None:
    """Does whale positioning continue? Cross-sectional rank IC at 1-10 day horizons.

    Self-supervised — signal and target both come from flow_features, so this
    needs no per-symbol institutional label. A within-date rank correlation is
    neutral to the market-wide factor and to the expiry calendar (all NSE stock
    futures share one expiry, so dte is near-constant within a date). Intervals
    come from a moving-block bootstrap sized to the forward horizon, because
    overlapping windows make consecutive daily ICs correlated by construction.
    """
    import psycopg2

    from mktmtm.ml.flow_momentum import run_flow_momentum
    from mktmtm.storage.db import build_dsn

    conn = psycopg2.connect(build_dsn())
    try:
        result = run_flow_momentum(conn, parquet=parquet)
    finally:
        conn.close()
    _print_momentum_table(result)


def _print_momentum_table(result: dict[str, Any]) -> None:
    """One block per forward target; rows are signals, columns horizons."""
    meta = result.get("_meta", {})
    horizons = [str(h) for h in meta.get("horizons", [])]
    typer.echo(
        f"\n{meta.get('rows', 0):,} symbol-days · {meta.get('symbols', 0)} symbols · "
        f"{meta.get('dates', 0)} dates · {meta.get('period', '?')}"
    )

    width = 28

    def _row(name: str, cells: dict[str, Any]) -> None:
        parts = "".join(
            f"{cells.get(h, {}).get('mean_ic', float('nan')):+.4f}"
            f"{'*' if cells.get(h, {}).get('significant') else ' '}   "
            for h in horizons
        )
        typer.echo(f"{name:<{width}}{parts}")

    for target, signals in result.get("grid", {}).items():
        typer.echo(f"\n── forward target: {target} " + "─" * 30)
        typer.echo(f"{'signal':<{width}}" + "".join(f"h={h:<9}" for h in horizons))
        for name, cells in signals.items():
            marker = "  (self)" if name == target else ""
            _row(name + marker, cells)

    leadlag = result.get("within_symbol_leadlag", {})
    if leadlag:
        typer.echo(
            "\n── raw within-symbol lead-lag (no ranking, no normalisation) " + "─" * 3
        )
        typer.echo(
            "   raw/partial(consistency) — partial controls the same-day target,"
        )
        typer.echo(
            "   which removes the mechanical route signal_t -> target_t -> target_t+lag"
        )
        for target, signals in leadlag.items():
            typer.echo(f"  → target: {target}")
            for name, cells in signals.items():
                if name == target:
                    continue
                parts = "".join(
                    f"lag{lag}:{c['mean']:+.4f}"
                    + (
                        f"/{c['partial']:+.4f}"
                        if lag != "0" and c.get("partial") == c.get("partial")
                        else ""
                    )
                    + f"({c['consistency']:.0%})  "
                    for lag, c in sorted(cells.items(), key=lambda kv: int(kv[0]))
                )
                typer.echo(f"    {name:<24}{parts}")

    contemporaneous = result.get("contemporaneous", {})
    if contemporaneous:
        typer.echo("\n── contemporaneous IC (same day; exposes shared construction) ──")
        for target, pairs in contemporaneous.items():
            for name, value in pairs.items():
                typer.echo(f"  {name:<26} vs {target:<20} {value:+.4f}")

    typer.echo("\n── baselines " + "─" * 42)
    for name, cells in result.get("baselines", {}).items():
        _row(name, cells)

    typer.echo(
        f"\n* = 95% block-bootstrap CI excludes zero.  "
        f"{result.get('n_tests', 0)} cells tested; "
        f"~{result.get('expected_false_positives', 0):.1f} expected to clear by chance."
    )
    typer.echo(
        "Self cells (signal == target) are contaminated by bounded-OI reversal; "
        "read the off-diagonal."
    )


@app.command(name="fetch-deals")
def fetch_deals(
    files: Annotated[
        list[Path] | None,
        typer.Option(
            "--file",
            help="Manually downloaded NSE deal CSV; repeatable. Kind inferred from the name.",
        ),
    ] = None,
    directory: Annotated[
        Path | None,
        typer.Option("--dir", help="Load every *.csv in this directory."),
    ] = None,
    latest: Annotated[
        bool,
        typer.Option(
            "--latest/--no-latest",
            help="Also fetch the latest trading day from NSE's archive.",
        ),
    ] = True,
) -> None:
    """Load NSE bulk/block deals into `deals` — the per-symbol institutional label.

    NSE's archive serves only the LATEST trading day, and its historical API is
    bot-protected (HTTP 503 to any programmatic client). So history comes from
    CSVs downloaded by hand at
    https://www.nseindia.com/report-detail/display-bulk-and-block-deals
    and passed with --file/--dir; --latest builds the record forward.
    """
    import psycopg2

    from mktmtm.data.deals import default_deal_dir, run_fetch_deals
    from mktmtm.storage.db import build_dsn

    paths = list(files or [])
    search = directory or default_deal_dir()
    if search.exists():
        paths.extend(sorted(search.glob("*.csv")))
    paths = sorted({p.resolve() for p in paths})
    if not paths and not latest:
        typer.echo(f"no CSVs found in {search} and --no-latest given; nothing to do")
        raise typer.Exit(1)

    conn = psycopg2.connect(build_dsn())
    try:
        summary = run_fetch_deals(conn, files=paths, fetch_latest=latest)
    finally:
        conn.close()

    typer.echo(
        f"\ndeals: {summary['rows']:,} parsed, {summary['written']:,} written"
    )
    if summary.get("period"):
        typer.echo(
            f"  {summary['period']} · {summary.get('dates', 0)} dates · "
            f"{summary.get('symbols', 0)} symbols · {summary.get('by_kind', {})}"
        )
    for err in summary.get("errors", []):
        typer.echo(f"  ! {err}")


@app.command(name="fetch-shareholding")
def fetch_shareholding(
    limit_symbols: Annotated[
        int | None,
        typer.Option("--limit-symbols", min=1, help="Only the first N symbols (smoke runs)."),
    ] = None,
    limit_filings: Annotated[
        int | None,
        typer.Option("--limit-filings", min=1, help="Only the N most recent filings each."),
    ] = None,
    delay: Annotated[
        float,
        typer.Option("--delay", min=0.0, help="Seconds between XBRL downloads."),
    ] = 0.35,
) -> None:
    """Fetch SEBI quarterly shareholding patterns into `shareholding`.

    The per-symbol institutional label: FII/DII holdings per symbol per
    quarter, parsed from the XBRL filings NSE links. Filings are gzip-cached
    under data/raw/shareholding/, so re-runs and parser fixes are free.

    Share counts are the load-bearing columns — a holding percentage moves on a
    buyback with nobody having traded, the count does not.
    """
    import psycopg2

    from mktmtm.data.shareholding import run_fetch_shareholding
    from mktmtm.storage.db import build_dsn

    conn = psycopg2.connect(build_dsn())
    try:
        summary = run_fetch_shareholding(
            conn, limit_symbols=limit_symbols, limit_filings=limit_filings,
            delay=delay,
        )
    finally:
        conn.close()

    typer.echo(
        f"\nshareholding: {summary['rows']:,} filings parsed, "
        f"{summary['written']:,} written"
    )
    typer.echo(
        f"  {summary['symbols_with_data']}/{summary['symbols_requested']} symbols · "
        f"median {summary['filings_per_symbol']:.0f} filings each · "
        f"{summary.get('period') or '—'}"
    )
    v = summary.get("validation", {})
    if v.get("checked"):
        typer.echo(
            f"  scale check: institutional exceeds public on "
            f"{v['institutional_exceeds_public']}/{v['checked']} rows "
            f"({v['breach_rate']:.1%}) — median institutional "
            f"{v['median_institutional_pct']:.1f}% vs public "
            f"{v['median_public_pct']:.1f}%"
        )
    if summary.get("error_count"):
        typer.echo(f"  {summary['error_count']} errors; first few:")
        for err in summary["errors"][:5]:
            typer.echo(f"    ! {err}")


@app.command(name="accumulation")
def accumulation(
    holder: Annotated[
        list[str] | None,
        typer.Option("--holder", help="fii / dii / mf; repeatable. Default: fii, dii."),
    ] = None,
    window: Annotated[
        list[str] | None,
        typer.Option(
            "--window",
            help="contemporaneous / early / lead; repeatable. Default: all three.",
        ),
    ] = None,
    keep_corporate_actions: Annotated[
        bool,
        typer.Option(
            "--keep-corporate-actions",
            help="Do NOT drop periods whose share count moved on a split or bonus.",
        ),
    ] = False,
) -> None:
    """Stage 5: detect institutional accumulation from the F&O footprint.

    Nowcasting, not forecasting — given a symbol's F&O activity over a window,
    did institutions accumulate the underlying stock during it? Evaluated as a
    cross-sectional rank IC per filing period against SEBI shareholding.

    Read the margin over PRICE MOMENTUM, not the absolute IC: institutions buy
    what is going up, so a signal that only rediscovers that has found nothing.
    """
    import psycopg2

    from mktmtm.ml.accumulation import WINDOWS, run_accumulation
    from mktmtm.storage.db import build_dsn

    holders = tuple(holder) if holder else ("fii", "dii")
    windows = tuple(window) if window else WINDOWS

    conn = psycopg2.connect(build_dsn())
    try:
        summary = run_accumulation(
            conn, holders=holders, windows=windows,  # type: ignore[arg-type]
            drop_corporate_actions=not keep_corporate_actions,
        )
    finally:
        conn.close()
    _print_accumulation(summary)


def _print_accumulation(summary: dict[str, Any]) -> None:
    """Baselines first — price momentum is the bar, not chance."""
    for key, result in summary.items():
        if "error" in result:
            typer.echo(f"\n{key}: {result['error']}")
            continue
        meta = result.get("_meta", {})
        typer.echo(
            f"\n── {key} ── {meta.get('rows', 0):,} rows · "
            f"{meta.get('symbols', 0)} symbols · {meta.get('periods', 0)} periods · "
            f"median window {meta.get('median_window_days', 0):.0f}d"
        )
        typer.echo(f"   {'':<26}{'mean IC':>9}{'median':>9}{'pos rate':>10}{'n':>5}")
        for name, cell in sorted(
            result.get("baselines", {}).items(),
            key=lambda kv: -abs(kv[1]["mean_ic"]),
        ):
            typer.echo(
                f"   [base] {name:<19}{cell['mean_ic']:+9.4f}{cell['median_ic']:+9.4f}"
                f"{cell['positive_rate']:>9.0%}{int(cell['periods']):>5}"
            )
        top = sorted(
            result.get("features", {}).items(),
            key=lambda kv: -abs(kv[1]["mean_ic"]),
        )[:5]
        for name, cell in top:
            typer.echo(
                f"   {name:<26}{cell['mean_ic']:+9.4f}{cell['median_ic']:+9.4f}"
                f"{cell['positive_rate']:>9.0%}{int(cell['periods']):>5}"
            )
        partial = result.get("partial_vs_momentum", {})
        if partial:
            typer.echo("   --- with price momentum partialled out ---")
            for name, cell in sorted(
                partial.items(), key=lambda kv: -abs(kv[1]["mean_ic"])
            )[:4]:
                typer.echo(
                    f"   {name:<26}{cell['mean_ic']:+9.4f}{cell['median_ic']:+9.4f}"
                    f"{cell['positive_rate']:>9.0%}{int(cell['periods']):>5}"
                )
        reverse = result.get("momentum_partial_vs_best") or {}
        best_partial = result.get("best_partial_ic")
        if best_partial is not None and reverse:
            typer.echo(
                f"   best surviving = {result.get('best_partial_feature')} "
                f"({best_partial:+.4f}); momentum given it "
                f"{reverse.get('mean_ic', float('nan')):+.4f}"
            )

    rob = summary.get("_robustness")
    if rob and "error" not in rob:
        typer.echo(f"\n── robustness: {rob['feature']} → mutual-fund accumulation ──")
        for label in ("full_sample", "first_half", "second_half", "large_caps", "small_caps"):
            cell = rob.get(label, {})
            typer.echo(
                f"   {label:<16}{cell.get('mean_ic', float('nan')):+9.4f}"
                f"{cell.get('positive_rate', 0):>9.0%}{int(cell.get('periods', 0)):>5}"
                f"  {cell.get('period_range', '')}"
            )
        b = rob.get("bootstrap", {})
        if b:
            typer.echo(
                f"   bootstrap over {b['periods']} periods: {b['mean']:+.4f} "
                f"95% CI [{b['ci_low']:+.4f}, {b['ci_high']:+.4f}]"
                f"  {'excludes zero' if b['excludes_zero'] else 'INCLUDES ZERO'}"
            )
    cp = summary.get("_counterparty")
    if cp and "error" not in cp:
        typer.echo(
            f"\n   counterparty: FII vs MF accumulation corr "
            f"{cp['fii_vs_mf_accumulation_corr']:+.4f}, negative in "
            f"{cp['negative_periods']:.0%} of {cp['periods']} periods"
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
