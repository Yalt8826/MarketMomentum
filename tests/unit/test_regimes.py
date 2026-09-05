"""Unit tests for mktmtm.ml.regimes.

No HMM is fitted here — EM is slow and stochastic, and what actually breaks in
this stage is the data plumbing around it. The two tests that matter are
`test_lengths_reject_ungrouped_rows` (hmmlearn silently learns transitions
across symbol boundaries if the rows are not grouped) and
`test_causal_zscore_has_no_lookahead`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mktmtm.ml.regimes import (
    INDICATOR,
    MIN_SEQUENCE,
    MODEL_COLUMNS,
    OBSERVABLES,
    WINSORIZE,
    build_observations,
    canonical_order,
    causal_zscore,
    sequence_lengths,
)


def _panel(symbols: tuple[str, ...] = ("AAA", "BBB"), n: int = 200) -> pd.DataFrame:
    """A per-symbol panel long enough to clear the 60-day z warm-up."""
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2022-01-03", periods=n)
    rows = []
    for s_i, sym in enumerate(symbols):
        for d_i, day in enumerate(dates):
            rows.append(
                {
                    "symbol": sym,
                    "date": day,
                    "fut_basis_excess": float(rng.normal(s_i, 1)),
                    "fut_roll_ratio": float(rng.random()),
                    "oi_outlier_share": float(rng.random() * 0.1),
                    # Sparse by design: most days have no flagged strike.
                    "oi_outlier_direction": (
                        float(rng.uniform(-1, 1)) if d_i % 7 == 0 else np.nan
                    ),
                    "q_fut_direction": float(rng.uniform(-1, 1)),
                    "gex": float(rng.normal(1e9 * (s_i + 1), 1e8)),
                }
            )
    return pd.DataFrame(rows)


# ── sequence lengths ────────────────────────────────────────────────────────


def test_lengths_match_row_count() -> None:
    frame = build_observations(_panel())
    lengths = sequence_lengths(frame)
    assert lengths.sum() == len(frame)
    assert len(lengths) == frame["symbol"].nunique()


def test_lengths_reject_ungrouped_rows() -> None:
    """Interleaved symbols must raise, not silently produce a plausible fit.

    hmmlearn takes a concatenated array plus per-sequence lengths. If the rows
    are interleaved it raises nothing and learns transitions *across* symbol
    boundaries — treating the end of one symbol as flowing into the start of
    the next. The result looks entirely reasonable and means nothing.
    """
    frame = build_observations(_panel())
    interleaved = frame.sort_values("date", kind="stable").reset_index(drop=True)
    with pytest.raises(ValueError, match="not grouped by symbol"):
        sequence_lengths(interleaved)


def test_lengths_follow_symbol_boundaries() -> None:
    frame = build_observations(_panel(("AAA", "BBB", "CCC")))
    lengths = sequence_lengths(frame)
    expected = frame.groupby("symbol", observed=True, sort=False).size().to_numpy()
    np.testing.assert_array_equal(lengths, expected)


# ── causal normalization ────────────────────────────────────────────────────


def test_causal_zscore_has_no_lookahead() -> None:
    """Appending future rows must leave every earlier z bit-identical."""
    frame = _panel(("AAA",), n=150)[["symbol", "date", "gex"]]
    first = causal_zscore(frame, ["gex"], min_obs=20)

    extra = frame.copy()
    extra["date"] = extra["date"] + pd.Timedelta(days=400)
    extra["gex"] = extra["gex"] * 100.0  # wildly different future regime
    second = causal_zscore(
        pd.concat([frame, extra], ignore_index=True), ["gex"], min_obs=20
    )

    np.testing.assert_array_equal(
        first["gex"].to_numpy(), second["gex"].to_numpy()[: len(frame)]
    )


def test_causal_zscore_is_null_before_min_obs() -> None:
    frame = _panel(("AAA",), n=100)[["symbol", "date", "gex"]]
    out = causal_zscore(frame, ["gex"], min_obs=60)
    assert out["gex"].iloc[:60].isna().all()
    assert out["gex"].iloc[60:].notna().any()


def test_causal_zscore_normalizes_within_symbol() -> None:
    """A pooled z would discover market capitalisation rather than regimes.

    AAA and BBB have gex an order of magnitude apart; after within-symbol
    normalization their z distributions must be comparable.
    """
    out = causal_zscore(_panel(), ["gex"], min_obs=20).dropna(subset=["gex"])
    means = out.groupby("symbol")["gex"].mean().abs()
    assert (means < 1.0).all()


def test_winsorize_bounds_observables() -> None:
    frame = _panel(("AAA",), n=150)[["symbol", "date", "gex"]].copy()
    frame.loc[140, "gex"] = 1e18  # a single absurd print
    out = causal_zscore(frame, ["gex"], min_obs=20)
    assert out["gex"].abs().max() <= WINSORIZE


# ── observation matrix ──────────────────────────────────────────────────────


def test_indicator_separates_missing_from_neutral() -> None:
    """Imputing the sparse direction to 0 needs a companion flag, or "no
    unusual activity" becomes indistinguishable from "neutral direction"."""
    out = build_observations(_panel())
    assert INDICATOR in out.columns
    assert set(out[INDICATOR].unique()) <= {0.0, 1.0}
    assert 0.0 < out[INDICATOR].mean() < 1.0


def test_observations_are_complete_cases() -> None:
    out = build_observations(_panel())
    assert out[list(MODEL_COLUMNS)].notna().all().all()


def test_short_symbols_are_dropped() -> None:
    """A symbol with too little history contributes noise, not information."""
    panel = pd.concat(
        [_panel(("AAA",)), _panel(("SHORT",), n=MIN_SEQUENCE - 10)],
        ignore_index=True,
    )
    out = build_observations(panel)
    assert "SHORT" not in set(out["symbol"])
    assert "AAA" in set(out["symbol"])


def test_observations_are_sorted_for_hmmlearn() -> None:
    out = build_observations(_panel(("BBB", "AAA")))
    assert out.equals(
        out.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)
    )


def test_indicator_is_not_z_scored() -> None:
    """The flag carries no scale; z-scoring it would make it a symbol rate."""
    out = build_observations(_panel())
    assert set(out[INDICATOR].unique()) <= {0.0, 1.0}


# ── state canonicalisation ──────────────────────────────────────────────────


def test_state_canonicalisation_is_deterministic() -> None:
    """hmmlearn numbers states arbitrarily; persisted ids must not drift."""
    idx = MODEL_COLUMNS.index("q_fut_direction")
    means = np.zeros((3, len(MODEL_COLUMNS)))
    means[:, idx] = [0.5, -0.5, 0.0]
    order = canonical_order(means, idx)
    np.testing.assert_array_equal(order, [1, 2, 0])
    # Same means in a different row order produce the same ranking of values.
    shuffled = means[[2, 0, 1]]
    assert (
        shuffled[canonical_order(shuffled, idx), idx].tolist()
        == means[order, idx].tolist()
    )


def test_observable_contract_includes_the_direction_features() -> None:
    """Stage 1b's direction columns are what let a state mean accumulation
    rather than merely activity."""
    assert "oi_outlier_direction" in OBSERVABLES
    assert "q_fut_direction" in OBSERVABLES
