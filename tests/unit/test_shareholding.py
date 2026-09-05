"""Unit tests for mktmtm.data.shareholding.

Parsing is pure and offline — a small synthetic XBRL fixture stands in for the
real filings. The scale test is the important one: the filings store
percentages as fractions, and a hard-coded x100 would silently corrupt every
row the day a taxonomy version changed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from mktmtm.data.shareholding import (
    SHAREHOLDING_COLUMNS,
    ShareholdingError,
    _keep_latest_filing,
    parse_shareholding,
    validate_shareholding,
)


def _xbrl(pct_scale: float = 1.0, total: int = 1_000_000) -> str:
    """A minimal filing: 20% foreign, 30% domestic (12% MF, 8% insurance)."""
    holdings = {
        "InstitutionsForeignMember": (0.20, "Foreign"),
        "InstitutionsDomesticMember": (0.30, "Domestic"),
        "MutualFundsOrUTIMember": (0.12, "MF"),
        "InsuranceCompaniesMember": (0.08, "Ins"),
    }
    contexts, facts = [], []
    for member, (share, _) in holdings.items():
        cid = f"{member}_ctx"
        contexts.append(
            f'<xbrli:context id="{cid}">'
            f"<xbrldi:explicitMember>in-bse-shp:{member}</xbrldi:explicitMember>"
            f"</xbrli:context>"
        )
        facts.append(
            f'<in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="{cid}">'
            f"{int(share * total)}</in-bse-shp:NumberOfFullyPaidUpEquityShares>"
        )
        facts.append(
            f'<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares '
            f'contextRef="{cid}">{share * pct_scale}'
            f"</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>"
        )
    return "<xbrl>" + "".join(contexts) + "".join(facts) + "</xbrl>"


def test_parses_institutional_holdings() -> None:
    out = parse_shareholding(_xbrl())
    assert out["fii_shares"] == 200_000
    assert out["dii_shares"] == 300_000
    assert out["mf_shares"] == 120_000
    assert out["insurance_shares"] == 80_000


def test_fractions_are_scaled_to_percent() -> None:
    """Filings store 0.1720 meaning 17.20%; the table reads as the filing does."""
    out = parse_shareholding(_xbrl(pct_scale=1.0))
    assert out["fii_pct"] == pytest.approx(20.0)
    assert out["dii_pct"] == pytest.approx(30.0)


def test_a_filing_already_in_percent_is_not_rescaled() -> None:
    """A taxonomy change must not silently multiply every holding by 100.

    The scale is inferred from the data — anything above 1.0 is already
    percent — rather than hard-coded.
    """
    out = parse_shareholding(_xbrl(pct_scale=100.0))
    assert out["fii_pct"] == pytest.approx(20.0)
    assert out["dii_pct"] == pytest.approx(30.0)


def test_total_shares_is_derived_and_self_checking() -> None:
    """Every category yields an estimate of the total; they must agree."""
    out = parse_shareholding(_xbrl(total=1_000_000))
    assert out["total_shares"] == pytest.approx(1_000_000, rel=1e-6)


def test_total_shares_is_null_when_categories_disagree() -> None:
    """A wrong-but-plausible total is worse than none."""
    broken = _xbrl().replace(
        '<in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="InstitutionsForeignMember_ctx">200000',
        '<in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="InstitutionsForeignMember_ctx">999999',
    )
    assert parse_shareholding(broken)["total_shares"] is None


def test_a_filing_with_no_contexts_raises() -> None:
    with pytest.raises(ShareholdingError, match="no XBRL contexts"):
        parse_shareholding("<xbrl></xbrl>")


def test_a_filing_with_no_share_facts_raises() -> None:
    xml = (
        '<xbrl><xbrli:context id="c">'
        "<xbrldi:explicitMember>in-bse-shp:InstitutionsForeignMember</xbrldi:explicitMember>"
        "</xbrli:context></xbrl>"
    )
    with pytest.raises(ShareholdingError, match="no NumberOfFullyPaidUpEquityShares"):
        parse_shareholding(xml)


def test_revised_filings_are_deduplicated_by_record_id() -> None:
    """A company can file the same period twice; both collide on the key.

    Left alone the UPSERT fails outright with a cardinality violation. NSE's
    recordId increases with submission time, so the largest wins.
    """
    frame = pd.DataFrame(
        {
            "symbol": ["ACME", "ACME", "BETA"],
            "date": [pd.Timestamp("2024-03-31")] * 2 + [pd.Timestamp("2024-03-31")],
            "record_id": ["100", "200", "50"],
            "fii_pct": [10.0, 11.0, 5.0],
        }
    )
    out = _keep_latest_filing(frame)
    assert len(out) == 2
    assert out.loc[out["symbol"] == "ACME", "fii_pct"].iloc[0] == 11.0


def test_dedupe_survives_a_missing_record_id() -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["ACME", "ACME"],
            "date": [pd.Timestamp("2024-03-31")] * 2,
            "record_id": ["", "300"],
            "fii_pct": [10.0, 12.0],
        }
    )
    out = _keep_latest_filing(frame)
    assert len(out) == 1
    assert out["fii_pct"].iloc[0] == 12.0


def test_validation_flags_a_scaling_failure() -> None:
    """Institutions are a subset of public holding; systematic breach = bad scale."""
    good = pd.DataFrame(
        {"fii_pct": [10.0], "dii_pct": [15.0], "public_pct": [40.0]}
    )
    bad = pd.DataFrame(
        {"fii_pct": [1000.0], "dii_pct": [1500.0], "public_pct": [40.0]}
    )
    assert validate_shareholding(good)["institutional_exceeds_public"] == 0
    assert validate_shareholding(bad)["institutional_exceeds_public"] == 1


def test_columns_mirror_the_ddl() -> None:
    import re
    from pathlib import Path

    ddl = (
        Path(__file__).resolve().parents[2]
        / "src/mktmtm/storage/sql/init_shareholding.sql"
    ).read_text()
    body = ddl[ddl.index("CREATE TABLE") : ddl.index("PRIMARY KEY")]
    declared = [
        m.group(1)
        for line in body.splitlines()
        if (m := re.match(r"\s{4}(\w+)\s+(?:VARCHAR|DATE|BIGINT|DOUBLE)", line))
    ]
    assert declared == list(SHAREHOLDING_COLUMNS)


def _old_taxonomy_xbrl() -> str:
    """A pre-2023 filing: different member names, different casing, no domestic total.

    Matching only the modern names left every 2018-2022 filing with a NULL FII
    holding (899 rows), and the UTI/Uti casing drift alone made mutual funds
    74.6% NULL.
    """
    members = {
        "ForeignInstitutionsMember": 200_000,      # foreign total, old name
        "InstitutionsMember": 500_000,             # ALL institutions
        "MutualFundsOrUtiMember": 120_000,         # note the casing
        "InsuranceCompaniesMember": 80_000,
    }
    contexts, facts = [], []
    for member, count in members.items():
        cid = f"{member}_ctx"
        contexts.append(
            f'<xbrli:context id="{cid}">'
            f"<xbrldi:explicitMember>in-bse-shp:{member}</xbrldi:explicitMember>"
            f"</xbrli:context>"
        )
        facts.append(
            f'<in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="{cid}">'
            f"{count}</in-bse-shp:NumberOfFullyPaidUpEquityShares>"
        )
        facts.append(
            f'<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares '
            f'contextRef="{cid}">{count / 1_000_000}'
            f"</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>"
        )
    return "<xbrl>" + "".join(contexts) + "".join(facts) + "</xbrl>"


def test_pre_2023_taxonomy_member_names_are_recognised() -> None:
    out = parse_shareholding(_old_taxonomy_xbrl())
    assert out["fii_shares"] == 200_000


def test_member_matching_is_case_insensitive() -> None:
    """MutualFundsOrUTIMember vs MutualFundsOrUtiMember across versions."""
    out = parse_shareholding(_old_taxonomy_xbrl())
    assert out["mf_shares"] == 120_000


def test_domestic_is_recovered_as_institutions_minus_foreign() -> None:
    """Old filings carry no domestic total; subtracting is exact, not approximate."""
    out = parse_shareholding(_old_taxonomy_xbrl())
    assert out["dii_shares"] == 300_000  # 500,000 total - 200,000 foreign
    assert out["dii_pct"] == pytest.approx(30.0)


def test_a_total_member_is_preferred_over_a_subset() -> None:
    """A filing carrying both must not record only its FPI slice as the whole."""
    xml = _old_taxonomy_xbrl().replace(
        "</xbrl>",
        '<xbrli:context id="sub">'
        "<xbrldi:explicitMember>in-bse-shp:ForeignPortfolioInvestorMember"
        "</xbrldi:explicitMember></xbrli:context>"
        '<in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="sub">50000'
        "</in-bse-shp:NumberOfFullyPaidUpEquityShares></xbrl>",
    )
    assert parse_shareholding(xml)["fii_shares"] == 200_000


def test_domestic_fallback_refuses_a_negative_holding() -> None:
    """A malformed filing yields NULL, never a negative holding."""
    xml = _old_taxonomy_xbrl().replace(
        '<in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="InstitutionsMember_ctx">500000',
        '<in-bse-shp:NumberOfFullyPaidUpEquityShares contextRef="InstitutionsMember_ctx">100000',
    )
    assert parse_shareholding(xml)["dii_shares"] is None


def test_direct_domestic_member_wins_over_the_fallback() -> None:
    """The subtraction only applies when the direct member is genuinely absent."""
    out = parse_shareholding(_xbrl())
    assert out["dii_shares"] == 300_000
