"""Unit tests for mktmtm.data.deals.

Parsing is pure and offline; the network paths are not exercised here. The
date-format test is the one that matters — inferring the format rather than
pinning it has already destroyed one full build in this repo.
"""

from __future__ import annotations

import pandas as pd
import pytest

from mktmtm.data.deals import (
    DEAL_COLUMNS,
    DealsError,
    load_deal_file,
    normalize_deals,
)

# NSE's exact header spelling, taken from a live archive file.
_RAW = {
    "Date": ["04-SEP-2026", "04-SEP-2026"],
    "Symbol": ["ACME", "BETA"],
    "Security Name": ["Acme Ltd", "Beta Ltd"],
    "Client Name": ["BRIDGE INDIA FUND", "SOME PERSON"],
    "Buy/Sell": ["BUY", "SELL"],
    "Quantity Traded": [231000, 129600],
    "Trade Price / Wght. Avg. Price": [25.75, 80.70],
    "Remarks": ["-", "-"],
}


def test_normalize_maps_nse_headers_onto_the_table() -> None:
    out = normalize_deals(pd.DataFrame(_RAW), "bulk")
    assert list(out.columns) == list(DEAL_COLUMNS)
    assert set(out["kind"]) == {"bulk"}


def test_value_is_quantity_times_price() -> None:
    """Rupee value is what makes a microcap and a large cap comparable."""
    out = normalize_deals(pd.DataFrame(_RAW), "bulk")
    row = out.loc[out["symbol"] == "ACME"].iloc[0]
    assert row["value"] == pytest.approx(231000 * 25.75)


def test_dates_are_parsed_with_an_explicit_format() -> None:
    """Inferring the format is how a May-only chunk breaks on the next June row.

    `%b` and `%B` are identical for May, so pandas infers `%B` and then raises.
    That bug killed a full flow-features build once.
    """
    frame = pd.DataFrame({**_RAW, "Date": ["01-MAY-2026", "02-JUN-2026"]})
    out = normalize_deals(frame, "bulk")
    assert out["date"].dt.month.tolist() == [5, 6]


def test_unparseable_dates_raise_rather_than_silently_drop() -> None:
    frame = pd.DataFrame({**_RAW, "Date": ["not-a-date", "also-not"]})
    with pytest.raises(DealsError, match="could not parse deal dates"):
        normalize_deals(frame, "bulk")


def test_missing_required_columns_raise() -> None:
    frame = pd.DataFrame(_RAW).drop(columns=["Client Name"])
    with pytest.raises(DealsError, match="missing required columns"):
        normalize_deals(frame, "bulk")


def test_duplicate_client_rows_are_summed_not_overwritten() -> None:
    """One client can trade the same side through several brokers in a day.

    The table's primary key would reject the second row, so they are summed
    here rather than letting the UPSERT keep whichever arrived last.
    """
    frame = pd.DataFrame(
        {
            "Date": ["04-SEP-2026"] * 2,
            "Symbol": ["ACME"] * 2,
            "Security Name": ["Acme Ltd"] * 2,
            "Client Name": ["BRIDGE INDIA FUND"] * 2,
            "Buy/Sell": ["BUY"] * 2,
            "Quantity Traded": [1000, 3000],
            "Trade Price / Wght. Avg. Price": [100.0, 200.0],
        }
    )
    out = normalize_deals(frame, "bulk")
    assert len(out) == 1
    assert out["quantity"].iloc[0] == 4000
    assert out["value"].iloc[0] == pytest.approx(1000 * 100.0 + 3000 * 200.0)
    # Price becomes the value-weighted average, not the last row's price.
    assert out["price"].iloc[0] == pytest.approx(700_000 / 4000)


def test_rows_with_an_unexpected_side_are_dropped() -> None:
    frame = pd.DataFrame({**_RAW, "Buy/Sell": ["BUY", "MAYBE"]})
    out = normalize_deals(frame, "bulk")
    assert set(out["side"]) == {"BUY"}


def test_thousands_separators_are_handled() -> None:
    frame = pd.DataFrame(
        {**_RAW, "Quantity Traded": ["2,31,000", "1,29,600"]}
    )
    out = normalize_deals(frame, "bulk")
    assert out["quantity"].sum() == 231000 + 129600


def test_kind_is_inferred_from_the_filename(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "block-deals-2024.csv"
    pd.DataFrame(_RAW).to_csv(path, index=False)
    assert set(load_deal_file(path)["kind"]) == {"block"}


def test_unnameable_file_raises_rather_than_guessing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "deals.csv"
    pd.DataFrame(_RAW).to_csv(path, index=False)
    with pytest.raises(DealsError, match="cannot infer deal kind"):
        load_deal_file(path)


def test_columns_mirror_the_ddl() -> None:
    """DEAL_COLUMNS is the source of truth; init_deals.sql mirrors it."""
    import re
    from pathlib import Path

    ddl = (
        Path(__file__).resolve().parents[2]
        / "src/mktmtm/storage/sql/init_deals.sql"
    ).read_text()
    body = ddl[ddl.index("CREATE TABLE") : ddl.index("PRIMARY KEY")]
    declared = [
        m.group(1)
        for line in body.splitlines()
        if (m := re.match(r"\s{4}(\w+)\s+(?:VARCHAR|DATE|BIGINT|DOUBLE)", line))
    ]
    assert declared == list(DEAL_COLUMNS)
