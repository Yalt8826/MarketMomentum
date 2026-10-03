"""NSE's daily F&O ban list — the securities in which no new position may open.

When a symbol's market-wide open interest crosses 95% of its position limit,
NSE puts it in a **ban period**: existing positions may only be *reduced*, and
no new ones may be created. Selling a fresh strangle creates new positions, so
a banned symbol is simply not tradeable that day.

**Why this module exists.** The daily screen selected **INOXWIND on
2026-09-04 — its second-largest position by credit — while INOXWIND was in an
F&O ban.** The position could not have been opened. The tool had no way to know,
because nothing in the project had ever read this file.

Measured reach across all 62 month-entry dates in the study window: 17 of
10,411 persisted backtest trades entered on a banned symbol (0.16%), at a mean
of 0.6 banned symbols per month-entry day. So the Gate 0 verdict is not at risk
— but the *live* tool hit one on its first ever run, because bans land on
high-IV, high-activity names and that is exactly what the screen ranks on. The
selection mechanism and the defect are correlated, which is why a 0.16%
population rate became a 1-in-6 rate in production.

## The failure semantics, which are the whole point

NSE publishes this file on **every trading day**, including days when nothing is
banned — those read ``...: NIL`` on the title line itself. So:

=================  ===========================================================
``200`` + symbols  those symbols are banned
``200`` + ``NIL``  **authoritatively zero bans**
anything else      **unknown** — a closed market, or we are being blocked
=================  ===========================================================

Returning an empty set for that third case would be the exact silent-omission
failure this project has been bitten by twice (the parquet cache that dropped
75 symbols, the writer that discarded 3,308 rows). An unreachable ban list must
therefore *raise*, and the caller must record that bans went unverified rather
than quietly proceed as though nothing were banned.
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "SecBanError",
    "get_secban_url",
    "parse_secban",
    "fetch_secban",
]

#: Stable across the whole study window — verified at 2021, 2023, 2024, 2025
#: and 2026 dates. Note the ``archives/`` prefix: the ``content/`` paths that
#: serve participant OI and bhavcopy 404 for this report.
_URL = "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{d:%d%m%Y}.csv"

#: "Securities in Ban For Trade Date 04-SEP-2026:" — optionally followed by
#: ``NIL`` on the same line when nothing is banned.
_TITLE_RE = re.compile(
    r"Securities\s+in\s+Ban\s+For\s+Trade\s+Date\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\s*:\s*(NIL)?",
    re.IGNORECASE,
)


class SecBanError(RuntimeError):
    """Raised when the ban list cannot be retrieved or trusted.

    Never swallowed into an empty set: "nothing is banned" and "we could not
    find out" are different facts and the caller must be able to tell them
    apart.
    """


def get_secban_url(day: datetime | date_cls) -> str:
    """URL for one trading day's F&O ban list."""
    return _URL.format(d=day)


def parse_secban(text: str, *, expect: date_cls | None = None) -> frozenset[str]:
    """Symbols in an F&O ban, from the raw CSV body.

    Pure: no I/O, so the parse is unit-testable against both the populated and
    the ``NIL`` shape without touching the network.

    ``expect`` guards against NSE serving a file for the wrong date — a stale
    or misrouted response would otherwise apply yesterday's bans to today
    silently. Raises :class:`SecBanError` on a mismatch rather than returning
    the wrong day's answer.
    """
    body = (text or "").strip()
    if not body:
        raise SecBanError("empty ban-list body")

    lines = body.splitlines()
    title = _TITLE_RE.search(lines[0])
    if title is None:
        raise SecBanError(f"unrecognised ban-list header: {lines[0][:100]!r}")

    if expect is not None:
        served = datetime.strptime(title.group(1), "%d-%b-%Y").date()
        if served != expect:
            raise SecBanError(
                f"ban list is for {served}, not the requested {expect} "
                "— refusing to apply another day's bans"
            )

    if title.group(2):  # "...: NIL" — authoritatively empty
        return frozenset()

    symbols = {
        part[1].strip().upper()
        for line in lines[1:]
        if len(part := line.split(",")) >= 2 and part[1].strip()
    }
    if not symbols:
        # A title with no NIL marker and no entries is malformed, not empty.
        raise SecBanError("ban list has neither a NIL marker nor any entries")
    return frozenset(symbols)


def fetch_secban(session: Any, day: datetime | date_cls) -> frozenset[str]:
    """Symbols in an F&O ban on ``day``.

    ``session`` must already carry NSE's cookies — use
    :func:`mktmtm.data.participant_oi.warm_session`.

    Raises :class:`SecBanError` for anything but a well-formed ``200``,
    including the 404 that a non-trading day returns. An empty frozenset means
    NSE said ``NIL``, never that the lookup failed.
    """
    url = get_secban_url(day)
    try:
        response = session.get(url, timeout=30)
    except Exception as exc:  # noqa: BLE001 — network errors are all one case here
        raise SecBanError(f"could not reach {url}: {exc}") from exc

    if response.status_code != 200:
        raise SecBanError(
            f"ban list for {day:%Y-%m-%d} returned HTTP {response.status_code} "
            "(a closed market, or we are being blocked) — bans are UNVERIFIED"
        )

    banned = parse_secban(
        response.text, expect=day.date() if isinstance(day, datetime) else day
    )
    logger.info("F&O ban list %s: %d symbol(s) banned", day, len(banned))
    return banned
