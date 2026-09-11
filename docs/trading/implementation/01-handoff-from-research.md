# 01 — Handoff from the research branch

[Index](README.md) · Next: [Strategy →](02-strategy-and-mechanics.md)

What `feat/gex-dealer-positioning` established, what this branch inherits, and
what it abandons. Read this before touching code: several things that look
reusable are not, and the reasons were measured rather than assumed.

---

## 1. What the research branch actually proved

Five modelling stages, 56 commits. **Three of five returned null or negative.**

| Stage | Question | Verdict |
|---|---|---|
| 1 — market flow | Predict tomorrow's market-wide institutional flow? | ❌ Null — a calendar-only baseline wins (AUC 0.61–0.72) |
| 1b — buildup quadrants | Give OI builds a direction? | ✅ Works; delta adjustment mandatory (35.8% disagreement) |
| 2 — regime HMM | Persistent latent positioning regimes? | ❌ Negative — nothing survives ~1.6 days |
| 3 — attribution | Split market-wide flow across symbols? | ⚠️ Mixed — the placebo beats the real model |
| 4 — flow momentum | Does positioning continue? | ⚠️ A real futures→options lead, but flat across activity — microstructure |
| 5 — accumulation | Detect institutional accumulation per symbol? | ✅ **+0.1374, 18/19 quarters** |
| — GEX | Dealer gamma predicts volatility? | ❌ Fails on sign, magnitude, and against IV |

Stage 5 is the surviving positive, and it is real: momentum-controlled,
bootstrapped, counterparty-confirmed, and frozen into a forward prediction
record scoreable from ~2026-11-14.

## 2. Why that result cannot become a trading tool

Measured, not assumed:

| Link | IC | Quarters + |
|---|---|---|
| score → MF accumulation | **+0.1351** | 18/19 |
| MF accumulation → next quarter's return | +0.0502 | 12/19 |
| **score → next quarter's return** | **+0.0200** | 11/19 |

Return decile spread: **+1.84%/quarter, 95% CI [−1.34%, +5.02%], t = 1.21,
p = 0.24.** Not distinguishable from zero.

**The break is in the second row and it is not the detector's fault.** The
detected quantity does not predict returns, so no improvement in detection
quality propagates to a trading outcome. Two reasons, both measured:

- Domestic funds are **contrarian** (momentum IC −0.064) — they buy weakness.
- Quarterly price momentum in this sample is itself **negative** (−0.047).

"Institutions are accumulating X" and "X will rise" are different facts.

### The insurance hypothesis, tested late

The Stage 5 spec listed `pe_writer_frac` among the signals to test; the
implementation never wired it in. Tested afterwards:

| Put-side feature | Partial IC | Quarters + | Shuffled |
|---|---|---|---|
| `otm_put_oi_build` | +0.0401 | 16/19 | +0.0164 |
| `pe_writer_frac` — *are they buying puts?* | +0.0085 | 10/19 | +0.0152 |
| `put_call_iv_spread` | **−0.0872** | 6/19 | +0.0027 |

The direct test is indistinguishable from noise, and the skew evidence runs
**against** the insurance story: if funds bought puts to protect names they were
accumulating, that demand would bid put IV *up* in those names. It does the
opposite.

**Conclusion carried into this branch:** the footprint is real, but it is
*synthetic long exposure through futures*, not protective put buying. A fund
hedging a long equity book would **short** futures, pushing basis *down*; what
correlates with accumulation is basis pushed *up*.

## 3. What this branch inherits — and it is a lot

Everything below is built, tested and verified. None of it needs rebuilding.

| Asset | State | Used for |
|---|---|---|
| `option_metrics` | 213,379 rows, 24 GB, full chain per symbol-day | Chain lookups at entry |
| `ml_dataset` | 213,379 × 69, IV percentile/rank, Greeks, GEX family | Entry filters |
| `flow_features` | 213,379 × 20+, basis, outliers, quadrants | Entry filters |
| `backtest/strangle.py` | 917 lines, **correct mechanics, never run at scale** | ⭐ The strategy itself |
| `ml/splits.py` | Purged walk-forward, embargo, inner holdout | Filter evaluation |
| `ml/metrics.py` | Rank IC, decile spread, ICIR | Evaluation |
| `data/lots.py` | 852-point lot-size change-point schedule | ⭐ Rupee conversion |
| `pipeline_runner.py` | 17 stages, timed, progress-barred | Daily refresh |
| `verify_db_health.py` | 7-section structural gate | Keeping the data honest |
| `ui/` + `ui/jobs.py` | Streamlit panel, detached job runner | Serving surface |

**The single most valuable inheritance is `backtest/strangle.py`.** It already
implements 20-delta strike selection, the 30-day expiry pick, weekly hedge dates,
exit-day resolution and an earnings-conflict skip — all unit-tested. What it
lacks is economics, which is this branch's Phase 0.

## 4. What this branch abandons

| Abandoned | Why — measured |
|---|---|
| **Direction prediction** | Archived XGBoost model: edge thin and substantially tied to *missingness* (IV-solver failures on illiquid or violently-moving names) |
| **Accumulation as a trade signal** | score → forward return +0.0200, 11/19 |
| **GEX as a volatility forecast** | Fails on sign (1.360% vs 1.547%), on magnitude (−\|gex\| ranks better), and against IV (+0.0099 vs +0.2114) |
| **Put-hedging as the mechanism** | §2 above |
| **Quarterly cadence** | Useless for a trader; the label was quarterly, the strategy is monthly |

Stage 5 is **not deleted** — it remains the project's research result, its
frozen nowcast records stay scoreable in November, and `mktmtm accumulation`
keeps working. It simply is not the basis of the trading tool.

## 5. What carries over conceptually — the method

The research branch's real asset is its evaluation discipline, and this branch
inherits all of it:

| Practice | Its form here |
|---|---|
| **Beat the boring explanation** | Stage 1 had the expiry calendar; Stage 5 had price momentum. **Here it is IV rank.** |
| **Report the partial, never the raw** | A filter must add something *over IV*, which alone scores +0.2114 |
| **Placebo** | A random filter drawing the same number of trades |
| **Pre-registered gates** | Four, each able to stop the work — [`00-plan.md`](00-plan.md) |
| **Freeze predictions before outcomes** | `write_nowcast_record` refuses to overwrite; the monthly screen will do the same |
| **Report negatives at full resolution** | Three of five stages were negative and are documented as carefully as the positive |

## 6. The one number that governs this branch

`ce_iv_30` — plain implied volatility, no modelling — scores **+0.2114** against
next-day absolute return. Every feature this project constructed sits at
**≤0.05** after that control.

> **Implied volatility is already an excellent forecast of movement, and it is
> free.** This branch only earns its existence if it beats selling options on IV
> rank alone. That is Gate 1, and the honest prior is that it fails.

[Index](README.md) · Next: [Strategy →](02-strategy-and-mechanics.md)
