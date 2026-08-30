"""Unit tests for the ETL cache offset map and the unadjusted-spot basis.

Both cover defects found in the 2026-08-30 five-year build, which passed the
health gate while being unusable:

* the per-symbol offset map was built with ``np.searchsorted`` over a
  categorical-backed array that was not alphabetically sorted, so 75 of 222
  symbols got empty spans (silently dropped) and 3 got other symbols' rows;
* the spot driving strike selection came from Yahoo's ``quote.close``, which is
  already split/bonus-adjusted and therefore on a different corporate-action
  basis than the bhavcopy strikes it was matched against.

Nothing here touches PostgreSQL or the network.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from mktmtm.options.chain import unadjusted_spot_from_bhavcopy
from mktmtm.storage.etl import _iter_symbol_days, _load_cache

# Deliberately NOT alphabetical: this is the category order a dictionary-encoded
# parquet produces (first appearance across row groups), and the order that made
# searchsorted return nonsense spans.
_CATEGORY_ORDER = ["TCS", "AARTIIND", "ZYDUSLIFE", "MCX"]


def _write_cache(tmp_path, *, categorical: bool):
    """Build a two-day cache for four symbols, mimicking build_bhavcopy_cache."""
    rows = []
    for day in (pd.Timestamp("2021-08-02"), pd.Timestamp("2021-08-03")):
        for sym in _CATEGORY_ORDER:
            rows.append(
                {
                    "DATE": day,
                    "INSTRUMENT": "FUTSTK",
                    "SYMBOL": sym,
                    "EXPIRY_DT": "26-Aug-2021",
                    "STRIKE_PR": 0.0,
                    "OPTION_TYP": "XX",
                    "OPEN": 1.0, "HIGH": 1.0, "LOW": 1.0, "CLOSE": 1.0,
                    "SETTLE_PR": 1.0, "CONTRACTS": 1.0,
                    "OPEN_INT": 1.0, "CHG_IN_OI": 0.0, "UNDERLYING_PRICE": 1.0,
                }
            )
    df = pd.DataFrame(rows)
    if categorical:
        df["SYMBOL"] = pd.Categorical(df["SYMBOL"], categories=_CATEGORY_ORDER)
    path = tmp_path / "bhavcopy_cache.parquet"
    df.to_parquet(path, index=False)
    return path


@pytest.mark.parametrize("categorical", [True, False])
def test_load_cache_offsets_are_correct_for_every_symbol(tmp_path, categorical) -> None:
    """Every span must cover exactly its own symbol's rows, categorical or not."""
    cache = _load_cache(_write_cache(tmp_path, categorical=categorical))
    assert cache is not None
    df, offsets = cache

    assert set(offsets) == set(_CATEGORY_ORDER)
    for sym, (start, stop) in offsets.items():
        span = df.iloc[start:stop]
        assert not span.empty, f"{sym} got an empty span — it would be silently dropped"
        assert set(span["SYMBOL"].unique()) == {sym}, f"{sym} span holds foreign rows"
        assert len(span) == 2


def test_load_cache_sorts_symbols_alphabetically(tmp_path) -> None:
    """searchsorted is only valid on an alphabetically sorted array."""
    cache = _load_cache(_write_cache(tmp_path, categorical=True))
    assert cache is not None
    sym_col = cache[0]["SYMBOL"].to_numpy()
    assert bool(np.all(sym_col[:-1] <= sym_col[1:]))


def test_iter_symbol_days_yields_only_that_symbol(tmp_path) -> None:
    cache = _load_cache(_write_cache(tmp_path, categorical=True))
    assert cache is not None
    days = list(_iter_symbol_days("TCS", [], cache))
    assert len(days) == 2
    for _, rows in days:
        assert set(rows["SYMBOL"].unique()) == {"TCS"}


def test_iter_symbol_days_rejects_a_corrupt_offset_map(tmp_path) -> None:
    """A span holding foreign rows must raise, not quietly write mixed data."""
    cache = _load_cache(_write_cache(tmp_path, categorical=True))
    assert cache is not None
    df, offsets = cache
    offsets["TCS"] = (0, len(df))  # the TATASTEEL/TATAPOWER failure mode
    with pytest.raises(ValueError, match="foreign symbols"):
        list(_iter_symbol_days("TCS", [], (df, offsets)))


# ───────────────────────── unadjusted spot ─────────────────────────────────
def _fut(close: float = 2093.40, expiry: str = "26-Aug-2021") -> pd.DataFrame:
    return pd.DataFrame([{"CLOSE": close, "EXPIRY_DT": pd.Timestamp(expiry)}])


def test_spot_prefers_nse_underlying_price_when_present() -> None:
    df_sym = pd.DataFrame({"UNDERLYING_PRICE": [np.nan, 2050.5, 2050.5]})
    spot, source = unadjusted_spot_from_bhavcopy(df_sym, _fut(), "02-Aug-2021", 0.06)
    assert source == "undrlyg"
    assert spot == pytest.approx(2050.5)


def test_spot_falls_back_to_discounted_futures_close() -> None:
    """Legacy-era files have no underlying column; S = F * exp(-rT)."""
    df_sym = pd.DataFrame({"UNDERLYING_PRICE": [np.nan, np.nan]})
    spot, source = unadjusted_spot_from_bhavcopy(df_sym, _fut(close=2093.40), "02-Aug-2021", 0.06)
    assert source == "futures"
    # discounting pulls the futures price below itself, but only by carry
    assert 0.97 * 2093.40 < spot < 2093.40
    assert spot == pytest.approx(2093.40 * math.exp(-0.06 * (24 / 365)), rel=0.02)


def test_spot_is_none_when_neither_source_available() -> None:
    df_sym = pd.DataFrame({"UNDERLYING_PRICE": [np.nan]})
    assert unadjusted_spot_from_bhavcopy(df_sym, pd.DataFrame(), "02-Aug-2021", 0.06) is None


def test_spot_ignores_non_positive_underlying_values() -> None:
    df_sym = pd.DataFrame({"UNDERLYING_PRICE": [0.0, -1.0]})
    result = unadjusted_spot_from_bhavcopy(df_sym, _fut(), "02-Aug-2021", 0.06)
    assert result is not None and result[1] == "futures"


def test_spot_is_on_the_strike_basis_not_the_yahoo_basis() -> None:
    """The regression itself: KOTAKBANK 2021 traded ~1680, Yahoo's close said ~333."""
    yahoo_split_adjusted_close = 333.02
    df_sym = pd.DataFrame({"UNDERLYING_PRICE": [np.nan]})
    spot, _ = unadjusted_spot_from_bhavcopy(df_sym, _fut(close=1680.40), "02-Aug-2021", 0.06)
    assert spot / yahoo_split_adjusted_close > 4.0
    assert abs(spot / 1680.0 - 1) < 0.02
