# GEX dealer-positioning — five-year results (Phase 6)

Run of 2026-09-02 against the full re-built `option_metrics` / `ml_dataset`.
Raw output: `models/evaluation/gex_h1.json`. Reproduce with `uv run mktmtm test-gex`.

> **Sign convention corrected 2026-09-04 (commit below); the numbers on this page
> predate it and are stated under the old convention.** `DEALER_SIGN` was the US
> default (dealers long calls, short puts). NSE participant-wise OI measures the
> opposite for single stocks — the Pro book is net short calls on 99.9% of 1,258
> days and net long puts on 98.3% — so the constant is now `{"CE": -1.0, "PE":
> +1.0}` and `gex` is negated throughout. See `docs/data/participant-oi-report.md`.
>
> What this changes, and what it does not:
>
> - **The H1 headline reverses.** Under the corrected sign the roles of the
>   `gex_positive` and `gex_negative` groups swap: dealers are net short gamma on
>   206,553 of 212,198 symbol-days (97.3%, structurally sensible given retail call
>   buying) and the rare *long*-gamma days are the high-volatility ones. That runs
>   opposite to dealer-hedging theory. The corrected result is already in the file
>   as the `flip_dealer_sign` check — a perfect mirror, `h1_consistent: false`.
> - **Nothing that survived the confound controls changes.** The surviving result
>   is a *magnitude* effect, and `|gex|` is invariant under negation. `gamma_flip`
>   and `dist_to_flip` are invariant too (negating a function does not move its
>   zeros), so the strongest dealer feature after an IV control is untouched. The
>   within-symbol correlation keeps its magnitude and reverses its sign label.
> - **Indices are not covered by the correction.** The Pro index book flipped sign
>   monotonically across 2024-25 (net short calls on 90.5% of days in 2021, 36.7%
>   in 2026), so no fixed constant is right for them. They are 3,086 of 212,441
>   rows and are excluded from dealer-exposure claims rather than assigned a sign.
>
> Re-run `mktmtm gex-features && mktmtm test-gex` to regenerate this page's
> numbers under the corrected convention.

**Data.** 212,441 symbol-days, 217 symbols, 1,254 trading dates, 2021-08-02 → 2026-08-28.
Per-strike `gamma`/`vanna`/`charm` present on 100% of chain elements; `gamma_flip`
resolves on 91.4% of rows (the remainder have no zero crossing in the ±10% band,
which is a real state, not a failure). `gex` is negative on 5,634 symbol-days
(2.7%), positive on 206,553, zero on 11.

Target throughout: `next_day_abs_return` (adjusted basis).

## Headline: the cross-sectional H1 result does not survive its confounds

H1 — *dealers short gamma (GEX < 0) precedes higher realized volatility* — passes
its pooled test with a large margin, and passes every sample-varying robustness
check. It fails both confound controls.

| test | result | reading |
|---|---|---|
| H1 Welch, gex<0 vs gex>0 | 1.547% vs 1.360%, t = −8.96, p = 4.3e−19 | passes |
| IC(−gex), cross-sectional | +0.0674, t = +22.97, ICIR 0.65, hit 74.1% | passes |
| flip `DEALER_SIGN` | IC −0.0674, h1_consistent False | passes (correctly reverses) |
| liquidity filter (volume > 0 strikes) | IC +0.0676 | passes (unchanged) |
| expiry week vs not | +0.0690 / +0.0664 | passes (stable) |
| **indices only** | **IC −0.0495, t = −1.75; Welch p = 0.72** | **fails** |
| **sign vs magnitude** | **IC(−gex) +0.0674 vs IC(−\|gex\|) +0.0723** | **fails** |
| **partial IC \| `ce_iv_30`** | **−0.0099, t = −3.35** | **fails** |

Two findings kill the cross-sectional claim:

1. **The sign of GEX carries no information.** Discarding it entirely and ranking
   on `−|gex|` gives a *higher* IC (+0.0723) than the signed version (+0.0674).
   H1 is a claim about the sign. GEX ∝ ΣOI · Γ · S², so it scales with a name's
   size and option liquidity; small, thinly-traded names carry small |GEX| and are
   more volatile for reasons unrelated to dealer hedging. The pooled test was
   ranking symbols by size.

2. **Implied volatility already contains it.** `ce_iv_30` alone scores IC +0.2114 —
   three times the GEX IC. Residualize `−gex` against it and the partial IC is
   −0.0099: not merely gone, marginally the wrong sign. GEX has no incremental
   information over the option market's own volatility quote.

The index result points the same way: dealer-hedging theory should apply most
strongly to NIFTY/BANKNIFTY, where hedging flow is most concentrated. There the
effect is absent (p = 0.72). It appears only in the single-stock cross-section —
which is where the size dispersion lives.

**Consequence for the supervised model.** The Phase-6 model reports fixed-split
IC 0.2365 (ICIR 1.92) and walk-forward fold ICs 0.196–0.233. Given that `ce_iv_30`
alone scores 0.2114, essentially all of that is the IV feature. The model is not
evidence for the dealer-positioning hypothesis and must not be presented as such.

## What does survive: the within-symbol specification

Re-specifying H1 *within* each symbol removes the size confound structurally — a
symbol is only ever compared with itself. Each symbol's `gex` is z-scored against
its own past using an expanding mean/std over strictly prior rows (`shift(1)`,
min 60 observations), so the statistic is causal.

| | value |
|---|---|
| rows (after z-score warm-up) | 199,178 |
| Spearman(own gex z, own next-day \|ret\|) | **−0.0748** (H1 predicts negative) |
| own-lowest-decile gex, mean \|ret\| | 1.614% |
| own-highest-decile gex, mean \|ret\| | 1.232% |
| decile ratio / Welch | **1.31×**, t = +26.93, p = 2.8e−158 |
| partial Spearman, controlling own IV z | **−0.0436** |

Per-year: 2021 −0.060, 2022 −0.087, 2023 −0.020, 2024 −0.046, 2025 −0.083,
2026 −0.078. Correct sign in all six years; weakest in 2023.

This is the defensible version of the result: **when a symbol's own dealer gamma
is unusually low relative to its own history, its next-day move is larger — and
that holds after controlling for the symbol's own implied volatility.** Unlike
the pooled test, it cannot be explained by size, liquidity, or price level,
because those are constant within a symbol over the comparison.

The effect is modest (ρ = −0.044 after the IV control) and is a volatility
statement, not a directional one.

## Dealer features that retain incremental value over IV

Partial IC against `ce_iv_30`, cross-sectional, full sample:

| feature | partial IC | t |
|---|---|---|
| `dist_to_flip` | +0.0210 | +7.47 |
| `dex` | +0.0124 | +3.94 |
| `vanna_exp` | +0.0120 | +3.37 |
| `gex_chg` | +0.0110 | +4.06 |
| `otm_put_oi_build` | +0.0063 | +2.26 |
| `charm_exp` | −0.0156 | −4.40 |
| `−gex` (level) | −0.0099 | −3.35 |

`dist_to_flip` — distance from spot to the gamma-flip level — is the strongest,
and it is the feature that most directly encodes the dealer-gamma mechanism
rather than exposure size. The GEX *level* is the weakest. That ordering is
itself evidence: what predicts is proximity to the regime boundary, not how much
gamma is outstanding.

## Known limitations

- **The dealer side is assumed, not measured.** `DEALER_SIGN` takes dealers long
  calls and short puts. NSE publishes no participant-level per-strike data, so
  this cannot be verified. The flip check confirms the result reverses under the
  opposite convention, which establishes the sign is load-bearing — not that the
  assumed direction is correct.
- **Open interest is in contracts, not shares.** Lot sizes are applied from
  `data/reference/lot_sizes.csv`; any gap in that schedule mis-scales exposure
  for the affected symbol-periods.
- **Settlement IV contamination.** Untraded deep strikes carry settlement-derived
  IV. Measured: ATM ±5% carries 83.1% of the gamma×OI weight, and strikes beyond
  30% moneyness carry 0.0%, so the contamination sits where it has no weight. The
  liquidity-filtered robustness check (volume > 0 strikes only) is unchanged at
  IC +0.0676.
- **`interest_rate` is unusable as a feature.** The ETL carries the MIFOR rate
  forward across gaps (e.g. 11 days around 2025-04-15); it is a frozen constant
  on those dates.
- **5 catalog symbols produce no rows**: FINNIFTY and MIDCPNIFTY have no working
  Yahoo ticker (documented, expected); LTIM, PEL and TATAMOTORS are new to this
  run and not yet diagnosed.
