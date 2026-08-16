"""Unit tests for mktmtm.data.rates (MIFOR CSV load + fallback)."""

from datetime import date
from pathlib import Path

import pytest

from mktmtm.data.rates import get_rate_with_fallback, load_interest_rates

# Mirrors the tracked reference CSV's preamble + header + a few rows.
_RATE_CSV = """Financial Benchmarks India Pvt Ltd
ADJUSTED MIFOR
Date,Time,Rate Record Date,Tenor,SETTLEMENT DATE,FBIL ADJUSTED MIFOR(%),Comments
04 Apr 2025,7:45:00 PM,04 Apr 2025,O/N,07 Apr 2025,7.7556,
04 Apr 2025,7:45:00 PM,04 Mar 2025,1M,07 Apr 2025,7.6142,
05 Apr 2025,7:45:00 PM,05 Apr 2025,O/N,08 Apr 2025,7.76,
"""


def _write_rate_csv(tmp_path: Path, content: str = _RATE_CSV) -> Path:
    path = tmp_path / "ADJUSTED_MIFOR.csv"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_interest_rates_real_reference_csv() -> None:
    """Real tracked reference CSV loads into a non-empty {date: rate} map."""
    rates = load_interest_rates()
    assert isinstance(rates, dict)
    assert len(rates) > 100
    # A known anchor from the real file: 04 Apr 2025 -> 7.7556 (first row that date)
    assert rates.get(date(2025, 4, 4)) == pytest.approx(7.7556)


def test_load_interest_rates_uses_first_row_per_date(tmp_path: Path) -> None:
    """Multiple rows on the same date -> first row wins (7.7556, not 7.6142)."""
    rates = load_interest_rates(_write_rate_csv(tmp_path))
    assert rates[date(2025, 4, 4)] == pytest.approx(7.7556)
    assert rates[date(2025, 4, 5)] == pytest.approx(7.76)


def test_get_rate_with_fallback_exact_match() -> None:
    m = {date(2025, 4, 4): 7.7556, date(2025, 4, 10): 7.8}
    assert get_rate_with_fallback(date(2025, 4, 4), m) == pytest.approx(7.7556)


def test_get_rate_with_fallback_nearest_earlier() -> None:
    m = {date(2025, 4, 4): 7.7556, date(2025, 4, 10): 7.8}
    # 2025-04-07 is between 04 and 10 -> nearest earlier = 04
    assert get_rate_with_fallback(date(2025, 4, 7), m) == pytest.approx(7.7556)


def test_get_rate_with_fallback_nearest_later() -> None:
    m = {date(2025, 4, 4): 7.7556, date(2025, 4, 10): 7.8}
    # 2025-04-01 predates every entry -> nearest later = 04
    assert get_rate_with_fallback(date(2025, 4, 1), m) == pytest.approx(7.7556)


def test_get_rate_with_fallback_empty_map() -> None:
    assert get_rate_with_fallback(date(2025, 4, 4), {}) == 0.0
