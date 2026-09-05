"""Ordered, timed execution of the whole pipeline — the `mktmtm run-pipeline` body.

Stages run **in dependency order and stop at the first failure**, rather than
carrying a broken table forward into hours of downstream work. Each stage is
timed, and the stages that do real work render their own progress bar
(`mktmtm.progress`), so a long unattended run is legible from the log alone.

Three orderings are load-bearing rather than incidental:

- **`preparse-bhavcopy` follows every bhavcopy fetch.** The ETL reads the
  parquet cache, not the raw files, so a stale cache silently omits the new
  days — no error, just missing data.
- **The health gate sits between the data and every model.** Both data defects
  this project has hit were silent; that is the whole argument for its
  position. A non-zero exit stops the run.
- **The nowcast goes last and writes a dated file.** It refuses to overwrite an
  existing record, so re-running never replaces a forecast made before an
  outcome with one made after it.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

__all__ = ["Stage", "StageResult", "build_stages", "run_pipeline"]

Mode = Literal["all", "no-etl", "models"]

#: Which groups each mode runs. "no-etl" keeps acquisition off too: refetching
#: without re-running the ETL would leave option_metrics behind the raw files,
#: which is a worse state than either end.
_MODE_GROUPS: dict[str, tuple[str, ...]] = {
    "all": ("schema", "fetch", "etl", "derive", "gate", "model"),
    "no-etl": ("schema", "derive", "gate", "model"),
    "models": ("schema", "model"),
}


@dataclass(frozen=True)
class Stage:
    """One pipeline step: a display name, the group gating it, and how to run it."""

    name: str
    group: str
    run: Callable[[], Any]


@dataclass
class StageResult:
    name: str
    seconds: float
    ok: bool
    error: str | None = None


# ═════════════════════════ stage bodies ════════════════════════════════════
#
# Thin wrappers rather than calls into the Typer commands: a Typer default is
# an `OptionInfo`, not a value, so the command functions cannot be invoked
# directly without passing sentinel objects into the pipeline.


def _connect() -> Any:
    import psycopg2

    from mktmtm.storage.db import build_dsn

    return psycopg2.connect(build_dsn())


def _bootstrap_schema() -> None:
    from mktmtm.storage.db import build_dsn  # noqa: F401  (import check)

    sql = Path("src/mktmtm/storage/sql/init_db.sql")
    # psql resolves the \ir includes; psycopg2 cannot.
    result = subprocess.run(
        ["psql", _dsn(), "-v", "ON_ERROR_STOP=1", "-f", str(sql)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"schema bootstrap failed: {result.stderr[-400:]}")


def _dsn() -> str:
    from mktmtm.storage.db import build_dsn

    return build_dsn()


def _fetch_bhavcopy() -> None:
    from mktmtm.config.settings import settings
    from mktmtm.data.bhavcopy import run_pipeline as fetch

    # run_pipeline defaults to max_workers=1; the CLI passes the configured
    # value and so must this, or an overnight run is needlessly serial.
    fetch(max_workers=settings.max_workers)


def _fetch_yahoo() -> None:
    from mktmtm.data.yahoo import download_yahoo_prices

    download_yahoo_prices()


def _fetch_participant_oi() -> None:
    from mktmtm.data.participant_oi import (
        load_participant_frame,
        write_participant_oi,
    )
    from mktmtm.data.participant_oi import (
        run_pipeline as fetch,
    )

    # `run_pipeline` only downloads the daily CSVs. Loading them into the table
    # is a separate step, and omitting it left the stage reporting success in
    # 58s while participant_oi stayed exactly as stale as before.
    fetch()
    frame = load_participant_frame()
    conn = _connect()
    try:
        write_participant_oi(conn, frame)
    finally:
        conn.close()


def _fetch_deals() -> None:
    from mktmtm.data.deals import default_deal_dir, run_fetch_deals

    conn = _connect()
    try:
        search = default_deal_dir()
        files = sorted(search.glob("*.csv")) if search.exists() else []
        run_fetch_deals(conn, files=files, fetch_latest=True)
    finally:
        conn.close()


def _fetch_shareholding() -> None:
    from mktmtm.data.shareholding import run_fetch_shareholding

    conn = _connect()
    try:
        run_fetch_shareholding(conn)
    finally:
        conn.close()


def _preparse() -> None:
    from mktmtm.config.settings import settings
    from mktmtm.data.bhavcopy import build_bhavcopy_cache

    # The CSVs live under extracted/, not at the top of bhavcopy/ — which also
    # holds filtered/ and partial-download directories.
    build_bhavcopy_cache(
        settings.raw_data_dir / "bhavcopy" / "extracted",
        settings.processed_data_dir / "bhavcopy_cache.parquet",
    )


def _etl() -> None:
    from mktmtm.storage.etl import main as run_etl

    run_etl()


def _make_dataset() -> None:
    from mktmtm.config.settings import settings
    from mktmtm.ml.dataset import build_dataset, export_parquet, write_dataset

    conn = _connect()
    try:
        frame = build_dataset(conn)
        write_dataset(conn, frame)
        export_parquet(frame, settings.processed_data_dir / "ml_dataset.parquet")
    finally:
        conn.close()


def _flow_features() -> None:
    from mktmtm.features.flow import run_flow_features

    conn = _connect()
    try:
        run_flow_features(conn)
    finally:
        conn.close()


def _health_gate() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_db_health.py"],
        capture_output=True, text=True, check=False,
    )
    logger.info("%s", result.stdout[-2000:])
    if result.returncode != 0:
        raise RuntimeError(
            "health gate failed — fix the data before modelling on it"
        )


def _market_flow() -> None:
    from mktmtm.ml.market_flow import run_market_flow

    conn = _connect()
    try:
        run_market_flow(conn)
    finally:
        conn.close()


def _flow_regimes() -> None:
    from mktmtm.ml.regimes import run_flow_regimes

    conn = _connect()
    try:
        run_flow_regimes(conn)
    finally:
        conn.close()


def _flow_attribution() -> None:
    from mktmtm.ml.attribution import run_flow_attribution

    conn = _connect()
    try:
        run_flow_attribution(conn)
    finally:
        conn.close()


def _flow_momentum() -> None:
    from mktmtm.ml.flow_momentum import run_flow_momentum

    conn = _connect()
    try:
        run_flow_momentum(conn)
    finally:
        conn.close()


def _accumulation() -> None:
    from mktmtm.ml.accumulation import run_accumulation

    conn = _connect()
    try:
        run_accumulation(conn)
    finally:
        conn.close()


def _nowcast(out_dir: Path) -> None:
    from mktmtm.ml.accumulation import (
        NowcastRecordExists,
        nowcast,
        write_nowcast_record,
    )

    path = out_dir / f"nowcast-{date.today().isoformat()}.md"
    if path.exists():
        logger.info("nowcast record for today exists, left frozen: %s", path)
        return
    conn = _connect()
    try:
        result = nowcast(conn)
    finally:
        conn.close()
    try:
        write_nowcast_record(result, path)
    except NowcastRecordExists:
        logger.info("nowcast record already frozen: %s", path)


# ═════════════════════════ orchestration ═══════════════════════════════════


def build_stages(*, nowcast_dir: Path = Path("docs/results")) -> list[Stage]:
    """The full ordered pipeline. Order here *is* the dependency graph."""
    return [
        Stage("schema bootstrap", "schema", _bootstrap_schema),
        Stage("fetch bhavcopy", "fetch", _fetch_bhavcopy),
        Stage("fetch yahoo", "fetch", _fetch_yahoo),
        Stage("fetch participant OI", "fetch", _fetch_participant_oi),
        Stage("fetch deals", "fetch", _fetch_deals),
        Stage("fetch shareholding", "fetch", _fetch_shareholding),
        # Must follow the bhavcopy fetch: the ETL reads this cache, not the
        # raw files, and a stale cache omits new days without erroring.
        Stage("preparse bhavcopy", "fetch", _preparse),
        Stage("ETL", "etl", _etl),
        Stage("make dataset", "derive", _make_dataset),
        Stage("flow features", "derive", _flow_features),
        Stage("health gate", "gate", _health_gate),
        Stage("Stage 1  market flow", "model", _market_flow),
        Stage("Stage 2  flow regimes", "model", _flow_regimes),
        Stage("Stage 3  attribution", "model", _flow_attribution),
        Stage("Stage 4  flow momentum", "model", _flow_momentum),
        Stage("Stage 5  accumulation", "model", _accumulation),
        Stage("nowcast", "model", lambda: _nowcast(nowcast_dir)),
    ]


def run_pipeline(
    *,
    mode: Mode = "all",
    only: Sequence[str] | None = None,
    stages: list[Stage] | None = None,
) -> list[StageResult]:
    """Run the pipeline, stopping at the first failure. Returns per-stage timings.

    ``only`` runs a named subset — the way to resume after fixing a failure
    without repeating hours of upstream work.
    """
    if mode not in _MODE_GROUPS:
        raise ValueError(f"unknown mode {mode!r}; expected one of {list(_MODE_GROUPS)}")

    all_stages = stages if stages is not None else build_stages()
    groups = _MODE_GROUPS[mode]
    selected = [s for s in all_stages if s.group in groups]
    if only:
        wanted = {name.lower() for name in only}
        selected = [
            s for s in selected
            if any(w in s.name.lower() for w in wanted)
        ]
        if not selected:
            raise ValueError(f"no stages matched {list(only)}")

    results: list[StageResult] = []
    for index, stage in enumerate(selected, start=1):
        header = f"[{index}/{len(selected)}] {stage.name}"
        logger.info("%s", "─" * 4 + " " + header)
        start = time.monotonic()
        try:
            stage.run()
        except Exception as exc:  # a failed stage must not look like success
            elapsed = time.monotonic() - start
            results.append(StageResult(stage.name, elapsed, False, str(exc)))
            logger.error("%s FAILED after %.0fs: %s", header, elapsed, exc)
            return results
        elapsed = time.monotonic() - start
        results.append(StageResult(stage.name, elapsed, True))
        logger.info("%s done in %s", header, _fmt(elapsed))
    return results


def _fmt(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
