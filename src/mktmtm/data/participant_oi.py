"""NSE participant-wise F&O open interest (Client / DII / FII / Pro / TOTAL).

NSE's clearing arm publishes one small CSV per trading day giving open
interest split by *who holds it*: the four participant categories against
every instrument category (index/stock x future/option x long/short). It is
the only public ground truth on institutional positioning in the Indian F&O
market, and the only way to check the ``DEALER_SIGN`` assumption baked into
``options.dealer_exposure`` — see the "Known limitations" note there.

**This data is market-wide: one row per participant per date, not per
symbol.** Joined onto ``ml_dataset`` by date alone it is constant within a
date, so it contributes exactly zero to any cross-sectional (within-date)
rank IC. Its legitimate uses are dealer-sign validation, supplying a
market-level regime target, and validating per-symbol anonymous flow
features by aggregating them to the market level.

Unlike ``data.bhavcopy`` this module reports a *typed* outcome per day
rather than a bare bool, so a blocked or throttled run can never be
mistaken for a run of market holidays.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date as date_cls
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta

from mktmtm.config.settings import settings
from mktmtm.progress import progress_stage

logger = logging.getLogger(__name__)

__all__ = [
    "PARTICIPANT_TYPES",
    "PARTICIPANT_COLUMNS",
    "FetchOutcome",
    "ParticipantOIError",
    "get_participant_oi_url",
    "participant_oi_dir",
    "normalize_participant_columns",
    "parse_participant_oi",
    "validate_participant_frame",
    "warm_session",
    "download_day",
    "run_pipeline",
    "load_participant_frame",
    "add_net_positions",
    "write_participant_oi",
]

# The five row labels NSE emits, in file order. TOTAL is the market sum and is
# kept rather than dropped: it is what makes the integrity checks below possible.
PARTICIPANT_TYPES = ("Client", "DII", "FII", "Pro", "TOTAL")

# Source header -> our snake_case column. NSE's header carries era-dependent
# trailing whitespace (a literal tab pre-2026, spaces after) on two fields, so
# every name is matched *after* stripping — see normalize_participant_columns.
PARTICIPANT_COLUMN_MAP: dict[str, str] = {
    "Future Index Long": "future_index_long",
    "Future Index Short": "future_index_short",
    "Future Stock Long": "future_stock_long",
    "Future Stock Short": "future_stock_short",
    "Option Index Call Long": "option_index_call_long",
    "Option Index Put Long": "option_index_put_long",
    "Option Index Call Short": "option_index_call_short",
    "Option Index Put Short": "option_index_put_short",
    "Option Stock Call Long": "option_stock_call_long",
    "Option Stock Put Long": "option_stock_put_long",
    "Option Stock Call Short": "option_stock_call_short",
    "Option Stock Put Short": "option_stock_put_short",
    "Total Long Contracts": "total_long_contracts",
    "Total Short Contracts": "total_short_contracts",
}

PARTICIPANT_COLUMNS = tuple(PARTICIPANT_COLUMN_MAP.values())

# (long, short) pairs that must balance in the TOTAL row: every open long
# contract is somebody's short. A break here means a truncated or corrupt file.
_BALANCED_PAIRS = (
    ("future_index_long", "future_index_short"),
    ("future_stock_long", "future_stock_short"),
    ("option_index_call_long", "option_index_call_short"),
    ("option_index_put_long", "option_index_put_short"),
    ("option_stock_call_long", "option_stock_call_short"),
    ("option_stock_put_long", "option_stock_put_short"),
    ("total_long_contracts", "total_short_contracts"),
)

# NSE rounds each participant row to whole contracts independently of the TOTAL
# row, so the four categories need not sum to TOTAL exactly. Four rows rounded
# at <=0.5 each plus an independently rounded total bounds the honest drift at
# 3; 4 leaves a margin. Observed drift across the 2021/2024/2026 fixtures is
# exactly 1, and only ever on index-option and total columns. Any real
# truncation misses by orders of magnitude, so this stays a live check.
_SUM_TOLERANCE = 4

# "... in Equity Derivatives as on Aug 28, 2026" — the comma may or may not be
# followed by a space depending on the era, hence \s* rather than a literal.
_TITLE_DATE_RE = re.compile(r"as on\s+([A-Za-z]{3}\s+\d{1,2},\s*\d{4})", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.nseindia.com/all-reports-derivatives",
    "Connection": "keep-alive",
}

# Seconds between requests. Single-threaded by design, so unlike the bhavcopy
# fetcher (whose per-worker sleep multiplies by max_workers) this is the true
# global rate against NSE.
DEFAULT_DELAY = 0.5


class ParticipantOIError(Exception):
    """Raised when a participant-OI file is present but unusable."""


class FetchOutcome(StrEnum):
    """Why a day did or did not produce a file.

    The whole point of this enum is that ``NO_DATA`` (a genuine holiday, which
    NSE answers with 404) is distinguishable from ``HTTP_ERROR`` /
    ``NETWORK_ERROR`` (we are being blocked or throttled). Collapsing those
    into one falsy value is how a failed bhavcopy run reads as a quiet year.
    """

    OK = "ok"
    CACHED = "cached"
    NO_DATA = "no_data"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"

    @property
    def is_failure(self) -> bool:
        """True for outcomes that indicate *our* problem, not a closed market."""
        return self in (
            FetchOutcome.HTTP_ERROR,
            FetchOutcome.NETWORK_ERROR,
            FetchOutcome.PARSE_ERROR,
        )


def participant_oi_dir() -> Path:
    """Location of the cached raw participant-OI CSVs."""
    return settings.raw_data_dir / "participant_oi"


def get_participant_oi_url(day: datetime | date_cls) -> str:
    """URL for one trading day's participant-wise OI CSV.

    The path has been stable across the whole study window (verified against
    2021, 2024 and 2026), unlike the bhavcopy URL which changed at the UDiFF
    cutover.
    """
    return (
        "https://nsearchives.nseindia.com/content/nsccl/"
        f"fao_participant_oi_{day.strftime('%d%m%Y')}.csv"
    )


def normalize_participant_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip NSE's era-dependent header noise and map to snake_case.

    Two fields carry trailing whitespace that changed form over the years:
    ``Future Stock Short`` and ``Total Long Contracts`` end in a literal tab in
    the 2021 and 2024 files (quoted in 2021, bare in 2024) and in spaces by
    2026. Stripping first means one map handles every era.
    """
    df = df.rename(columns=lambda c: str(c).strip())
    df = df.rename(columns=PARTICIPANT_COLUMN_MAP)
    if "Client Type" in df.columns:
        df = df.rename(columns={"Client Type": "client_type"})
    return df


def _title_date(text: str) -> date_cls | None:
    """Trade date advertised in the file's own title row, if parseable."""
    match = _TITLE_DATE_RE.search(text)
    if match is None:
        return None
    cleaned = re.sub(r"\s+", " ", match.group(1)).replace(", ", ",")
    try:
        return datetime.strptime(cleaned, "%b %d,%Y").date()
    except ValueError:
        return None


def parse_participant_oi(text: str, day: datetime | date_cls) -> pd.DataFrame:
    """Parse one participant-OI CSV into a tidy 5-row frame.

    ``text`` is the raw file body; ``day`` is the trade date the file was
    requested for, which becomes the ``date`` column. The file's own title row
    is cross-checked against it and a mismatch raises — NSE serving yesterday's
    file under today's URL would otherwise silently duplicate a row of history.
    """
    import io

    advertised = _title_date(text)
    requested = day.date() if isinstance(day, datetime) else day
    if advertised is not None and advertised != requested:
        raise ParticipantOIError(
            f"file advertises {advertised} but was requested for {requested}"
        )

    # Row 0 is the title banner (quoted differently across eras); the real
    # header is row 1.
    frame = pd.read_csv(io.StringIO(text), skiprows=1)
    frame = normalize_participant_columns(frame)

    if "client_type" not in frame.columns:
        raise ParticipantOIError(f"no 'Client Type' column; got {list(frame.columns)[:5]}")
    missing = [c for c in PARTICIPANT_COLUMNS if c not in frame.columns]
    if missing:
        raise ParticipantOIError(f"missing columns: {missing}")

    frame["client_type"] = frame["client_type"].astype(str).str.strip()
    frame = frame[frame["client_type"].isin(PARTICIPANT_TYPES)].copy()
    for col in PARTICIPANT_COLUMNS:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").astype("Int64")

    frame["date"] = pd.Timestamp(requested)
    return frame[["date", "client_type", *PARTICIPANT_COLUMNS]].reset_index(drop=True)


def validate_participant_frame(df: pd.DataFrame) -> list[str]:
    """Check a single day's frame against NSE's own arithmetic identities.

    Two identities hold by construction in the source and are checked here
    because they catch truncation and corruption that a byte count cannot:

    1. In the ``TOTAL`` row every long leg equals its short leg — each open
       contract is held long by one participant and short by another.
    2. ``Client + DII + FII + Pro`` equals ``TOTAL`` in every column, to within
       ``_SUM_TOLERANCE`` — NSE rounds each row independently, so this one is
       near-exact rather than exact (see that constant for the bound).

    Returns a list of human-readable problems; empty means the file is sound.
    """
    problems: list[str] = []

    present = set(df["client_type"])
    missing = [t for t in PARTICIPANT_TYPES if t not in present]
    if missing:
        problems.append(f"missing participant rows: {missing}")
        return problems  # the identities below are meaningless without all rows

    if df["client_type"].duplicated().any():
        problems.append("duplicate participant rows")
        return problems

    total = df.loc[df["client_type"] == "TOTAL"].iloc[0]
    for long_col, short_col in _BALANCED_PAIRS:
        if pd.isna(total[long_col]) or pd.isna(total[short_col]):
            problems.append(f"TOTAL.{long_col}/{short_col} is null")
        elif total[long_col] != total[short_col]:
            problems.append(
                f"TOTAL {long_col}={total[long_col]:,} != {short_col}={total[short_col]:,}"
            )

    parts = df.loc[df["client_type"] != "TOTAL", list(PARTICIPANT_COLUMNS)].sum()
    for col in PARTICIPANT_COLUMNS:
        if pd.isna(total[col]):
            continue
        drift = abs(int(parts[col]) - int(total[col]))
        if drift > _SUM_TOLERANCE:
            problems.append(
                f"{col}: participants sum to {parts[col]:,} but TOTAL says "
                f"{total[col]:,} (off by {drift:,})"
            )
    return problems


def warm_session() -> requests.Session:
    """Open a session carrying NSE's cookies.

    NSE serves the archive only to clients that first touched the main site.
    Unlike the bhavcopy fetcher — which swallows a failed warm-up and then
    reports every subsequent 403 as a holiday — this raises immediately if no
    cookie was issued, so the failure surfaces before 1,254 wasted requests.
    """
    session = requests.Session()
    session.headers.update({**_HEADERS, "Accept-Language": "en-US,en;q=0.9"})
    try:
        session.get("https://www.nseindia.com", timeout=20)
    except requests.RequestException as exc:
        raise ParticipantOIError(f"could not reach nseindia.com to obtain cookies: {exc}") from exc
    if not session.cookies:
        raise ParticipantOIError(
            "nseindia.com issued no cookies; the archive will reject every request"
        )
    return session


def download_day(
    day: datetime | date_cls,
    session: requests.Session,
    out_dir: Path,
    delay: float = DEFAULT_DELAY,
) -> FetchOutcome:
    """Fetch and cache one day's CSV, returning a typed outcome.

    A cached file is parsed rather than trusted blindly, so a truncated file
    from an interrupted run is re-fetched instead of being skipped forever.
    """
    requested = day.date() if isinstance(day, datetime) else day
    dest = out_dir / f"fao_participant_oi_{requested:%d%m%Y}.csv"

    if dest.exists():
        try:
            parse_participant_oi(dest.read_text(encoding="utf-8", errors="replace"), requested)
            return FetchOutcome.CACHED
        except (ParticipantOIError, ValueError, pd.errors.ParserError):
            logger.warning("participant-oi: cached %s is unusable, re-fetching", dest.name)

    time.sleep(delay)
    try:
        response = session.get(get_participant_oi_url(requested), headers=_HEADERS, timeout=30)
    except requests.RequestException as exc:
        logger.warning("participant-oi %s: network error: %s", requested, exc)
        return FetchOutcome.NETWORK_ERROR

    if response.status_code == 404:
        return FetchOutcome.NO_DATA
    if response.status_code != 200:
        logger.warning("participant-oi %s: HTTP %d", requested, response.status_code)
        return FetchOutcome.HTTP_ERROR

    try:
        frame = parse_participant_oi(response.text, requested)
    except (ParticipantOIError, ValueError, pd.errors.ParserError) as exc:
        logger.warning("participant-oi %s: unparseable: %s", requested, exc)
        return FetchOutcome.PARSE_ERROR

    problems = validate_participant_frame(frame)
    if problems:
        logger.warning("participant-oi %s: %s", requested, "; ".join(problems[:3]))
        return FetchOutcome.PARSE_ERROR

    dest.write_text(response.text, encoding="utf-8")
    return FetchOutcome.OK


def run_pipeline(
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    delay: float = DEFAULT_DELAY,
) -> dict[FetchOutcome, int]:
    """Download participant-wise OI over a date range (default: last 5 years).

    Single-threaded on purpose: 1,254 files of under a kilobyte each do not
    need concurrency, and a serial loop makes ``delay`` the honest global rate.

    Returns the tally per outcome. Callers should treat any nonzero
    ``is_failure`` count as a failed run, not a quiet market.
    """
    if end_date is None:
        end_date = datetime.now()
    if start_date is None:
        start_date = end_date - relativedelta(years=5)

    days: list[datetime] = []
    curr = start_date
    while curr <= end_date:
        if curr.weekday() < 5:  # NSE is shut at weekends; holidays 404 below
            days.append(curr)
        curr += timedelta(days=1)

    out_dir = participant_oi_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    session = warm_session()

    tally: dict[FetchOutcome, int] = dict.fromkeys(FetchOutcome, 0)
    consecutive_failures = 0
    with progress_stage(
        "Downloading participant OI", total=len(days), unit="days"
    ) as bar:
        for day in days:
            outcome = download_day(day, session, out_dir, delay=delay)
            tally[outcome] += 1

            # A long unbroken run of *our* failures means NSE has cut us off;
            # continuing would produce 1,000 more identical errors and hammer
            # them for nothing.
            consecutive_failures = consecutive_failures + 1 if outcome.is_failure else 0
            if consecutive_failures >= 20:
                raise ParticipantOIError(
                    f"aborted after 20 consecutive failures (last: {outcome.value} "
                    f"on {day:%Y-%m-%d}); NSE is likely blocking this client"
                )
            bar.advance(
                label=f"{day:%d %b %Y}  ok={tally[FetchOutcome.OK] + tally[FetchOutcome.CACHED]}"
            )

    failures = sum(n for o, n in tally.items() if o.is_failure)
    logger.info(
        "participant-oi: %d fetched, %d cached, %d holidays (404), %d failures",
        tally[FetchOutcome.OK],
        tally[FetchOutcome.CACHED],
        tally[FetchOutcome.NO_DATA],
        failures,
    )
    if failures:
        logger.warning(
            "participant-oi: %d days failed for reasons other than a closed market "
            "— re-run to retry them",
            failures,
        )
    return tally


def load_participant_frame(src_dir: Path | None = None) -> pd.DataFrame:
    """Parse every cached CSV into one frame, sorted by date then participant."""
    src_dir = src_dir or participant_oi_dir()
    frames: list[pd.DataFrame] = []
    skipped = 0
    for path in sorted(Path(src_dir).glob("fao_participant_oi_*.csv")):
        try:
            day = datetime.strptime(path.stem.split("_")[-1], "%d%m%Y").date()
        except ValueError:
            skipped += 1
            continue
        try:
            frames.append(
                parse_participant_oi(path.read_text(encoding="utf-8", errors="replace"), day)
            )
        except (ParticipantOIError, ValueError, pd.errors.ParserError) as exc:
            logger.warning("participant-oi: skipping %s: %s", path.name, exc)
            skipped += 1
    if not frames:
        raise ParticipantOIError(f"no usable participant-OI files under {src_dir}")
    if skipped:
        logger.warning("participant-oi: %d files skipped while loading", skipped)
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["date", "client_type"]).reset_index(drop=True)


def add_net_positions(df: pd.DataFrame) -> pd.DataFrame:
    """Add net (long - short) positions and their day-over-day flow.

    Net position is the level — who is holding what. The *flow* (its first
    difference within a participant) is the entry/exit signal: a large positive
    ``d_net_opt_stock_call`` is an institution putting on upside exposure that
    day, measured rather than inferred.

    Row order is load-bearing: the diff is a ``groupby(client_type).diff()``,
    so the frame is re-sorted by (client_type, date) first.
    """
    out = df.sort_values(["client_type", "date"]).copy()

    nets = {
        "net_fut_index": ("future_index_long", "future_index_short"),
        "net_fut_stock": ("future_stock_long", "future_stock_short"),
        "net_opt_index_call": ("option_index_call_long", "option_index_call_short"),
        "net_opt_index_put": ("option_index_put_long", "option_index_put_short"),
        "net_opt_stock_call": ("option_stock_call_long", "option_stock_call_short"),
        "net_opt_stock_put": ("option_stock_put_long", "option_stock_put_short"),
        "net_total": ("total_long_contracts", "total_short_contracts"),
    }
    for name, (long_col, short_col) in nets.items():
        out[name] = out[long_col].astype("Float64") - out[short_col].astype("Float64")
    for name in nets:
        out[f"d_{name}"] = out.groupby("client_type", observed=True)[name].diff()

    return out.sort_values(["date", "client_type"]).reset_index(drop=True)


_INSERT_SQL = (
    "INSERT INTO participant_oi (date, client_type, " + ", ".join(PARTICIPANT_COLUMNS) + ") "
    "VALUES %s ON CONFLICT (date, client_type) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in PARTICIPANT_COLUMNS)
)


def write_participant_oi(conn: Any, df: pd.DataFrame) -> int:
    """Upsert a participant-OI frame into ``participant_oi``.

    Only the raw NSE counts are stored; nets and flows are derived on read via
    ``add_net_positions`` so the table stays a faithful copy of the source.
    """
    from psycopg2.extras import execute_values

    if df.empty:
        return 0
    cols = ["date", "client_type", *PARTICIPANT_COLUMNS]
    # psycopg2 cannot adapt numpy scalars, and pandas' nullable Int64 yields
    # them from itertuples — so every count is cast to a plain Python int and
    # every date to datetime.date rather than pd.Timestamp.
    rows = [
        (
            pd.Timestamp(rec[0]).date(),
            str(rec[1]),
            *(None if pd.isna(v) else int(v) for v in rec[2:]),
        )
        for rec in df[cols].itertuples(index=False, name=None)
    ]
    with conn.cursor() as cur:
        execute_values(cur, _INSERT_SQL, rows, page_size=500)
    conn.commit()
    return len(rows)
