"""Unit tests for mktmtm.data.participant_oi.

The three fixtures are unmodified NSE files from the two header eras: 2021
(title row single-quoted, ``Future Stock Short`` quoted with a literal tab),
2024 (title row double-quoted, tab unquoted) and 2026 (tab replaced by
spaces). Every parsing test runs against all three so an era-specific quirk
cannot pass by only being handled in one branch.
"""

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from mktmtm.data.participant_oi import (
    PARTICIPANT_COLUMNS,
    PARTICIPANT_TYPES,
    FetchOutcome,
    ParticipantOIError,
    add_net_positions,
    get_participant_oi_url,
    load_participant_frame,
    normalize_participant_columns,
    parse_participant_oi,
    validate_participant_frame,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

ERAS = [
    ("fao_participant_oi_02082021.csv", date(2021, 8, 2)),
    ("fao_participant_oi_15012024.csv", date(2024, 1, 15)),
    ("fao_participant_oi_28082026.csv", date(2026, 8, 28)),
]


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_url_format() -> None:
    """DDMMYYYY, and stable across the whole window (no UDiFF-style cutover)."""
    assert get_participant_oi_url(date(2026, 8, 28)).endswith(
        "/fao_participant_oi_28082026.csv"
    )
    assert get_participant_oi_url(datetime(2021, 8, 2)).endswith(
        "/fao_participant_oi_02082021.csv"
    )


@pytest.mark.parametrize(("name", "day"), ERAS)
def test_parses_every_header_era(name: str, day: date) -> None:
    """All three eras yield the same five rows and the same column set."""
    frame = parse_participant_oi(_read(name), day)
    assert list(frame["client_type"]) == list(PARTICIPANT_TYPES)
    assert set(PARTICIPANT_COLUMNS) <= set(frame.columns)
    assert frame["date"].unique().tolist() == [pd.Timestamp(day)]
    assert frame[list(PARTICIPANT_COLUMNS)].notna().all().all()


@pytest.mark.parametrize(("name", "day"), ERAS)
def test_nse_identities_hold(name: str, day: date) -> None:
    """Real files must pass the integrity gate — otherwise it is too strict."""
    assert validate_participant_frame(parse_participant_oi(_read(name), day)) == []


def test_trailing_whitespace_columns_are_normalized() -> None:
    """``Future Stock Short\\t`` (2021/2024) and with spaces (2026) both map."""
    for name, day in ERAS:
        frame = parse_participant_oi(_read(name), day)
        assert "future_stock_short" in frame.columns
        assert "total_long_contracts" in frame.columns
        assert not any(str(c) != str(c).strip() for c in frame.columns)


def test_normalize_is_idempotent() -> None:
    raw = pd.DataFrame({"Client Type": ["FII"], "Future Stock Short\t": [1]})
    once = normalize_participant_columns(raw)
    assert list(normalize_participant_columns(once).columns) == list(once.columns)
    assert "future_stock_short" in once.columns


def test_known_values_2026() -> None:
    """Spot-check against the bytes in the fixture, not against the parser."""
    frame = parse_participant_oi(_read("fao_participant_oi_28082026.csv"), date(2026, 8, 28))
    fii = frame.set_index("client_type").loc["FII"]
    assert fii["future_index_long"] == 24157
    assert fii["option_stock_call_long"] == 74429
    assert fii["total_short_contracts"] == 4566630


def test_date_mismatch_raises() -> None:
    """NSE serving a stale file under today's URL must not duplicate history."""
    with pytest.raises(ParticipantOIError, match="advertises"):
        parse_participant_oi(_read("fao_participant_oi_28082026.csv"), date(2026, 8, 27))


def test_missing_columns_raise() -> None:
    with pytest.raises(ParticipantOIError, match="Client Type|missing columns"):
        parse_participant_oi("title row\na,b,c\n1,2,3\n", date(2026, 8, 28))


def test_validator_catches_unbalanced_total() -> None:
    """Every open long is somebody's short; a broken TOTAL means corruption."""
    frame = parse_participant_oi(_read("fao_participant_oi_28082026.csv"), date(2026, 8, 28))
    frame.loc[frame["client_type"] == "TOTAL", "future_index_short"] += 1
    problems = validate_participant_frame(frame)
    assert any("future_index_long" in p for p in problems)


def test_validator_catches_participant_sum_mismatch() -> None:
    frame = parse_participant_oi(_read("fao_participant_oi_28082026.csv"), date(2026, 8, 28))
    frame.loc[frame["client_type"] == "FII", "option_stock_call_long"] += 7
    problems = validate_participant_frame(frame)
    assert any("option_stock_call_long" in p for p in problems)


def test_validator_catches_missing_row() -> None:
    frame = parse_participant_oi(_read("fao_participant_oi_28082026.csv"), date(2026, 8, 28))
    frame = frame[frame["client_type"] != "DII"]
    assert any("missing participant rows" in p for p in validate_participant_frame(frame))


def test_add_net_positions_and_flow() -> None:
    """Nets are long-short; flows are the within-participant first difference."""
    frames = [parse_participant_oi(_read(n), d) for n, d in ERAS]
    out = add_net_positions(pd.concat(frames, ignore_index=True))

    fii_2026 = out[(out["client_type"] == "FII") & (out["date"] == pd.Timestamp(2026, 8, 28))]
    row = fii_2026.iloc[0]
    assert row["net_opt_stock_call"] == 74429 - 177472
    assert row["net_fut_index"] == 24157 - 226790

    # The first observation of each participant has no prior day, so no flow.
    first = out[out["date"] == out["date"].min()]
    assert first["d_net_fut_index"].isna().all()
    # ...and a later one does.
    assert out[out["date"] == out["date"].max()]["d_net_fut_index"].notna().all()

    # TOTAL is market-neutral by construction: every net must be zero.
    total = out[out["client_type"] == "TOTAL"]
    assert (total["net_total"] == 0).all()
    assert (total["net_opt_stock_call"] == 0).all()


def test_add_net_positions_is_order_independent() -> None:
    """Flows are a groupby-diff, so a shuffled input must give the same answer."""
    frames = [parse_participant_oi(_read(n), d) for n, d in ERAS]
    joined = pd.concat(frames, ignore_index=True)
    ordered = add_net_positions(joined)
    shuffled = add_net_positions(joined.sample(frac=1.0, random_state=0))
    pd.testing.assert_frame_equal(ordered, shuffled)


def test_load_participant_frame(tmp_path: Path) -> None:
    for name, _ in ERAS:
        (tmp_path / name).write_text(_read(name), encoding="utf-8")
    frame = load_participant_frame(tmp_path)
    assert len(frame) == 15  # 3 days x 5 participants
    assert frame["date"].is_monotonic_increasing


def test_load_participant_frame_skips_corrupt(tmp_path: Path, caplog) -> None:
    """One bad file must not abort the load of the good ones."""
    for name, _ in ERAS:
        (tmp_path / name).write_text(_read(name), encoding="utf-8")
    (tmp_path / "fao_participant_oi_01012020.csv").write_text("garbage\n", encoding="utf-8")
    frame = load_participant_frame(tmp_path)
    assert len(frame) == 15


def test_load_participant_frame_empty_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ParticipantOIError, match="no usable"):
        load_participant_frame(tmp_path)


def test_fetch_outcome_failure_classification() -> None:
    """A holiday is not a failure; being blocked is. This distinction is the
    whole reason the fetcher returns an enum rather than a bool."""
    assert not FetchOutcome.NO_DATA.is_failure
    assert not FetchOutcome.OK.is_failure
    assert not FetchOutcome.CACHED.is_failure
    assert FetchOutcome.HTTP_ERROR.is_failure
    assert FetchOutcome.NETWORK_ERROR.is_failure
    assert FetchOutcome.PARSE_ERROR.is_failure


def test_validator_tolerates_nse_rounding_but_not_corruption() -> None:
    """NSE's own files drift by 1; the gate must pass that and still catch real
    damage. Pinning both sides stops the tolerance being widened silently."""
    frame = parse_participant_oi(_read("fao_participant_oi_28082026.csv"), date(2026, 8, 28))
    frame.loc[frame["client_type"] == "Pro", "future_stock_long"] += 3
    assert validate_participant_frame(frame) == []

    frame.loc[frame["client_type"] == "Pro", "future_stock_long"] += 1000
    assert any("future_stock_long" in p for p in validate_participant_frame(frame))


def test_write_participant_oi_emits_plain_python_scalars() -> None:
    """Regression: psycopg2 cannot adapt numpy.int64, and pandas' nullable
    Int64 columns hand exactly that out of itertuples. The write path must
    hand the driver plain ints and a datetime.date."""
    import datetime as dt

    captured: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    def fake_execute_values(cur, sql, rows, page_size=100):
        captured.extend(rows)

    import mktmtm.data.participant_oi as poi

    frame = parse_participant_oi(_read("fao_participant_oi_28082026.csv"), date(2026, 8, 28))
    import psycopg2.extras

    original = psycopg2.extras.execute_values
    psycopg2.extras.execute_values = fake_execute_values
    try:
        assert poi.write_participant_oi(_Conn(), frame) == 5
    finally:
        psycopg2.extras.execute_values = original

    assert len(captured) == 5
    for row in captured:
        assert type(row[0]) is dt.date, f"date is {type(row[0])}"
        assert type(row[1]) is str
        for value in row[2:]:
            assert value is None or type(value) is int, f"count is {type(value)}"
