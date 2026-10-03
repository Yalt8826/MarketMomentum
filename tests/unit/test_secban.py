"""Unit tests for mktmtm.data.secban — the F&O ban list.

The one thing these tests exist to pin: **"nothing is banned" and "we could not
find out" must never collapse into the same return value.** A banned symbol is
untradeable, so an empty set returned on a failed lookup would silently
re-admit the defect this module was written to fix.
"""

from __future__ import annotations

from datetime import date

import pytest

from mktmtm.data.secban import (
    SecBanError,
    fetch_secban,
    get_secban_url,
    parse_secban,
)

POPULATED = (
    "Securities in Ban For Trade Date 04-SEP-2026:\n"
    "1,INOXWIND\n2,KAYNES\n3,LICHSGFIN\n4,SAIL\n"
)
NIL = "Securities in Ban For Trade Date 03-JAN-2023: NIL"


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _Session:
    def __init__(self, response: object | Exception) -> None:
        self._response = response

    def get(self, url: str, timeout: int = 0) -> object:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_url_uses_the_archives_prefix() -> None:
    # The content/ paths that serve participant OI and bhavcopy 404 here.
    url = get_secban_url(date(2026, 9, 4))
    assert url.endswith("/archives/fo/sec_ban/fo_secban_04092026.csv")


def test_parses_the_populated_shape() -> None:
    assert parse_secban(POPULATED) == frozenset(
        {"INOXWIND", "KAYNES", "LICHSGFIN", "SAIL"}
    )


def test_nil_is_authoritatively_empty() -> None:
    # NSE publishes the file on every trading day; NIL sits on the title line.
    assert parse_secban(NIL) == frozenset()


def test_date_guard_rejects_another_days_bans() -> None:
    with pytest.raises(SecBanError, match="refusing to apply another day"):
        parse_secban(POPULATED, expect=date(2026, 9, 5))


def test_date_guard_accepts_the_requested_day() -> None:
    assert "SAIL" in parse_secban(POPULATED, expect=date(2026, 9, 4))


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "some unrelated header\n1,FOO\n",
        # A title with neither a NIL marker nor entries is malformed, and must
        # not read as "nothing banned".
        "Securities in Ban For Trade Date 04-SEP-2026:\n",
    ],
)
def test_malformed_bodies_raise_rather_than_return_empty(body: str) -> None:
    with pytest.raises(SecBanError):
        parse_secban(body)


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_non_200_raises_including_the_holiday_404(status: int) -> None:
    # 404 means a closed market or a block — never "zero bans".
    with pytest.raises(SecBanError, match="UNVERIFIED"):
        fetch_secban(_Session(_Response(status)), date(2026, 10, 2))


def test_network_error_raises() -> None:
    with pytest.raises(SecBanError, match="could not reach"):
        fetch_secban(_Session(OSError("connection reset")), date(2026, 9, 4))


def test_fetch_returns_the_parsed_set() -> None:
    banned = fetch_secban(_Session(_Response(200, POPULATED)), date(2026, 9, 4))
    assert banned == frozenset({"INOXWIND", "KAYNES", "LICHSGFIN", "SAIL"})


def test_fetch_passes_the_date_guard_through() -> None:
    with pytest.raises(SecBanError, match="refusing to apply another day"):
        fetch_secban(_Session(_Response(200, POPULATED)), date(2026, 9, 7))
