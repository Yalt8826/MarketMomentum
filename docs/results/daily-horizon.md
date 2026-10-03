# Daily horizon — is there a day-to-day strategy?

**Measured 2026-10-03** on 200,952 symbol-days, 2021-08-02 → 2026-09-04.

**Verdict: no.** Intraday is structurally impossible with this data, and the
daily short-volatility edge went **gross-negative in 2024** before any costs
were charged.

> ⚠️ **Status: single-pass measurement.** No block bootstrap, no walk-forward,
> no cost model, no pre-registered criterion. These are indicative numbers that
> close a question, not a gate. Everything in
> [`README.md`](README.md) and the trading gates was held to a higher standard
> than this document is.

---

## 1. Why the question was asked

Every strategy in the project is monthly. The question was whether a daily
version exists — and whether the data supports intraday decisions at all.

It also turned out that the daily *infrastructure* was already built and unused.
`ml_dataset` carries four look-ahead-validated daily targets:

| Target | Read by |
|---|---|
| `next_day_abs_return` | `ml/gex_hypothesis.py` only — never a strategy |
| `next_day_rv_5d` | nothing |
| `next_day_iv_chg` | nothing |
| **`next_day_vrp`** | **nothing at all** |

`next_day_vrp` = `|next-day return| − ce_iv_30/√252`: the daily short-vol P&L
proxy, persisted on 200,952 rows and never consumed by anything.

---

## 2. Three structural limits

| Limit | Evidence |
|---|---|
| **Intraday is impossible** | All seven data sources are end-of-day; bhavcopy publishes after the close. The earliest action from day-`t` data is day `t+1`. No modelling changes this — it needs a paid intraday feed. |
| **Stock options are monthly-only** | The 2026-09-04 F&O bhavcopy carries exactly **3** `OPTSTK` expiries (29-Sep, 27-Oct, 23-Nov). NSE removed weekly single-stock options, so across the 217-symbol universe there is no short-dated instrument. |
| **Weeklies exist only on indices** | 18 `OPTIDX` expiries including 08/15/22-Sep. But only NIFTY, BANKNIFTY and NIFTYNXT50 have usable spot, and the GEX work **excluded indices** because the Pro index book flipped sign monotonically across 2024–25 (net short calls on 90.5% of days in 2021, 36.7% in 2026). |

Consequence: a "daily" strategy on stocks means holding a ~30-day option and
re-hedging daily, which is what §3 measures.

---

## 3. The measurement, and why functional form decides it

### The naive reading, which is wrong

`next_day_vrp` looks encouraging — negative means the realised move was smaller
than implied, so a short-vol position won:

| | |
|---|---|
| Mean | **−0.00577** |
| Median | −0.00796 |
| Share negative | **75.7%** |

And it decays monotonically: −0.00879 (2021) → −0.00348 (2026).

### Why `|ret|` is the wrong shape

A delta-hedged short option's daily P&L is **quadratic** in the move:

```
P&L  ∝  ½ · Γ · S² · ( σ²_impl · dt  −  (dS/S)² )
```

A linear measure like `|ret| − σ/√252` therefore **understates exactly the large
moves that cause the damage**. The correct comparison is of variances.

### Scale-free, so IV's mechanical presence cancels

`1 − ret² / (iv²/252)`. Positive means IV was overpriced and short vol won.
Dividing by implied variance removes IV's mechanical contribution to the level:

| Year | Mean | Median | Days won |
|---|---|---|---|
| 2021 | **+0.237** | +0.765 | 80.5% |
| 2022 | +0.177 | +0.740 | 78.0% |
| 2023 | +0.152 | +0.760 | 78.8% |
| 2024 | **−0.004** | +0.697 | 75.0% |
| 2025 | −0.043 | +0.686 | 74.3% |
| 2026 | **−0.321** | +0.609 | 69.4% |

**The daily edge crossed zero in 2024 and is deeply negative in 2026 — gross,
before a single rupee of cost.**

The median stays positive in *every* year, and that gap is the whole finding:
**most days still win; the tail now loses more than the wins collect.**

### The tail, quantified

On the raw quadratic measure `iv²/252 − ret²`:

| | |
|---|---|
| Total won | +48.4171 over 152,065 days |
| Total lost | −39.3273 over 48,887 days |
| **Net** | **+9.0898** |
| **Worst 1% of days** | **−13.5542 — 149% of the entire net** |
| Worst 0.1% of days | −3.7803 |

**About 2,000 days out of 200,952 decide the entire outcome.** Worst days of
2026, mean across symbols: 2026-04-07 (−0.002107), 2026-03-20 (−0.001285),
2026-02-02 (−0.001012), 2026-03-18 (−0.000933).

Monthly, the recent run:

| Month | Mean | Median | Days won |
|---|---|---|---|
| 2025-10 | +8.04e-05 | +0.000149 | 79.3% |
| 2025-11 | +5.90e-05 | +0.000132 | 79.6% |
| 2025-12 | −3.67e-06 | +0.000083 | 73.0% |
| **2026-01** | **−1.117e-04** | +0.000104 | 65.7% |
| 2026-02 | −5.55e-05 | +0.000120 | 69.0% |
| **2026-03** | **−2.375e-04** | +0.000032 | **53.0%** |
| 2026-04 | −8.77e-06 | +0.000234 | 74.7% |
| 2026-05 | +2.83e-05 | +0.000193 | 72.0% |
| 2026-06 | −2.24e-05 | +0.000137 | 70.7% |
| 2026-07 | +1.03e-06 | +0.000161 | 74.1% |
| 2026-08 | +9.66e-07 | +0.000123 | 74.0% |

March 2026 is the worst month in the series, and the only one where the win rate
falls to a coin flip.

---

## 4. A circular IC, caught before it was reported

Worth recording as a near-miss, because it would have been the largest forward
number in the project's history.

Ranking symbols cross-sectionally by `ce_iv_30` against the raw
`iv²/252 − ret²` target:

> **IC +0.4524, t = +69.57, positive on 94.7% of 1,197 dates.**

**It is an artefact.** `iv` appears *inside* the target, so a higher IV
mechanically raises `var_pnl` unless `ret²` rises in exact proportion. This is
the project's own documented trap — *"direction scores share construction… always
check the contemporaneous correlation before believing a forward one"* — met at
a new horizon.

Removing the mechanical scaling, then applying the project's standing rule that
any feature must be judged on partial IC against `ce_iv_30`:

| Feature | Raw (circular) | Scale-free | **Partial vs `ce_iv_30`** |
|---|---|---|---|
| `ce_iv_30` | **+0.4524** | +0.0534 (t +14.61) | — |
| `ce_ivr` | +0.1672 | +0.0360 (t +11.91) | +0.0170 (t +5.95) |
| `gex` | +0.1821 | +0.0347 (t +11.55) | +0.0177 (t +5.92) |
| `dist_to_flip` | +0.1573 | +0.0044 (t +1.45) | **−0.0176 (t −6.24)** |

**An order of magnitude lost to the correction**, and everything collapses to
±0.018 once IV is controlled. `dist_to_flip` — the strongest dealer feature at a
monthly horizon (+0.0210) — turns *negative*.

This is the same answer Gate 1 reached at a monthly horizon, reproduced
independently at a daily one, by a different method against a different target.

---

## 5. Verdict, and why it is worth having

**No day-to-day strategy.** Three independent reasons, any one sufficient:

1. The gross edge is already negative (§3).
2. Costs at daily frequency would multiply. The monthly trade pays ~₹368 across
   10–12 orders spread over 30 days; daily rebalancing multiplies the order
   count several-fold against a per-order brokerage that is already **57.9%** of
   all costs.
3. There is no short-dated stock instrument to trade (§2).

**Intraday is a permanent no** with free end-of-day data.

### The part that is a genuine contribution

This independently corroborates Gate 0's decay finding — +8.30% (2021) →
+1.09% (2025) — from a completely different measurement, and extends it.

Gate 0 could only *speculate* about the mechanism, offering two untested
candidates: the October 2024 STT rise, and a maturing options market. This
measurement is **gross of all costs**, so it rules the STT explanation out as the
primary driver and points at the second:

> **The premium itself compressed.** Implied volatility stopped being overpriced
> relative to realised volatility somewhere in 2024, and the decay in Gate 0's
> net return is substantially that, not a cost increase.

That has a direct implication the project should state plainly: an edge that
compressed to zero gross, in a market that keeps getting more competitive, has
**no floor to stop at**. It is the more worrying of Gate 0's two candidate
explanations, and this measurement promotes it from speculation to the better
supported of the two.

---

## 6. Reproducing

No committed script — these were single-pass queries against `ml_dataset`. The
core measurement:

```sql
SELECT symbol, date, ce_iv_30, next_day_abs_return
FROM ml_dataset
WHERE next_day_abs_return IS NOT NULL AND ce_iv_30 > 0
```

```python
iv_var = (df.ce_iv_30 / 100.0) ** 2 / 252.0
df["vrp_ratio"] = 1.0 - df.next_day_abs_return ** 2 / iv_var   # >0 = short vol won
```

Then group by year, and for the cross-sectional ICs compute a Spearman
correlation **within each date** before averaging — never pooled, since pooling
across dates would let the market factor dominate.

If this is ever promoted to a real result, it needs: a monthly block bootstrap
(the daily series is strongly autocorrelated through volatility clustering), a
walk-forward split, and a cost model. Until then it stands as a measurement that
closed a question, and the caveat at the top of this page applies.
