"""Unit tests for mktmtm.data.symbols (load-only catalog helper)."""

from mktmtm.data.symbols import (
    extract_symbol,
    load_fno_catalog,
    load_fno_symbols,
    load_target_symbols,
)


def test_catalog_loads_222_symbols() -> None:
    """Real catalog: 5 index futures + 217 individual securities = 222."""
    catalog = load_fno_catalog()
    assert len(catalog["index_futures"]) == 5
    assert len(catalog["individual_securities"]) == 217


def test_load_fno_symbols_full_and_ordered() -> None:
    symbols = load_fno_symbols()
    assert len(symbols) == 222
    # index futures first, then securities
    assert symbols[:5] == ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]
    assert "PIIND" in symbols
    assert all(isinstance(s, str) and s for s in symbols)


def test_load_target_symbols_matches_catalog() -> None:
    targets = load_target_symbols()
    assert targets is not None
    assert len(targets) == 222
    assert targets == set(load_fno_symbols())


def test_extract_symbol_handles_dict_and_str() -> None:
    assert extract_symbol({"symbol": "NIFTY", "name": "Nifty 50"}) == "NIFTY"
    assert extract_symbol({"name": "Nifty 50"}) == "Nifty 50"
    assert extract_symbol("AARTIIND") == "AARTIIND"
    assert extract_symbol({"symbol": ""}) is None
