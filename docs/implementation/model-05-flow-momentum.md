# Stage 4 — Flow momentum: does whale positioning continue?

**Read [model-00-overview.md](model-00-overview.md) first.** Built and run
2026-09-05. `uv run mktmtm flow-momentum` (~20s).

**Why this stage exists.** Stages 1–3 all asked *prediction* or *attribution*
questions inherited from the abandoned direction model: will institutions act
tomorrow, where did today's flow go. None asked the question the project is
named for — **is a directional build in a symbol followed by more of the
same?** This stage asks it literally.

It is **self-supervised**: signal and target both come from `flow_features`, so
it needs no per-symbol institutional label. That matters, because the absence
of one is what shaped every previous stage.

Stage 2 asked an adjacent question (do symbol-days fall into persistent
discrete *states*) and answered no. Continuation of a continuous signal is
something a discrete-state model cannot represent, so that negative result did
not settle this one.

---

## 1. Design

**Signal** at (symbol, `t`): the Stage 1b direction features plus a magnitude
contrast — `q_fut_direction`, `opt_direction`, `oi_outlier_direction`,
`ce_writer_frac`, `oi_outlier_share`.

**Target** at (symbol, `t`): forward mean of a direction score over
`t+1 … t+h`, for `h ∈ {1, 2, 3, 5, 10}`.

**Two targets, not one.** Each signal is tested against forward
`q_fut_direction` *and* forward `opt_direction`. The diagonal (a signal against
its own future) is contaminated; the off-diagonal is where a real lead shows.

### Why the primary metric is a cross-sectional rank IC

With ~170 symbols per date this is a cross-sectional task, so
`ml.metrics.daily_rank_ic` applies directly. A **within-date** rank correlation
neutralises the two confounds that would otherwise dominate:

- **The market-wide common factor.** If every symbol drifts together, a pooled
  correlation reports that drift as per-symbol momentum. Ranking within a date
  removes anything common to the cross-section.
- **The expiry calendar**, which explained roughly half of everything in
  Stage 1. All NSE stock futures share one monthly expiry, so `fut_dte` is
  near-constant within a date — measured at a **single value on 83.6% of
  dates**, median cross-sectional spread **0**. A within-date correlation
  cannot be driven by the roll cycle.

### Three traps this stage had to handle

1. **Overlapping windows.** At horizon `h`, targets at `t` and `t+1` share
   `h−1` forward days, so the daily IC series is autocorrelated *by
   construction* and a naive t-stat overstates significance by roughly
   `√h`. Every interval is a moving-block bootstrap with block ≥ `2h`.
2. **Bounded open interest.** OI is a stock, so its flow is negatively
   autocorrelated by construction — a position opened today must eventually
   close. That alone produces apparent short-horizon "reversal" in any
   self-referential cell.
3. **Shared construction.** `ce_writer_frac` and `oi_outlier_direction` are
   built from the same buckets as `opt_direction`. A signal that mirrors the
   target today will appear to lead it tomorrow purely by inheriting the
   target's own reversal.

---

## 2. Results

### 2.1 There is no momentum in positioning direction

Raw within-symbol autocorrelation — no ranking, no normalisation, no
cross-section:

| Lag | `q_fut_direction` | Symbols positive |
|---|---|---|
| 1 | −0.0093 | 35.9% |
| 2 | −0.0011 | 44.7% |
| 3 | +0.0003 | 52.1% |
| 5 | −0.0117 | 38.2% |
| 10 | +0.0065 | 56.2% |

**Futures positioning direction has essentially zero autocorrelation at every
horizon.** A symbol positioned bullishly today is no more likely than chance to
be positioned bullishly tomorrow.

`opt_direction` is different, and strongly so: lag-1 autocorrelation
**−0.0921, negative for 98.6% of symbols**. Option positioning direction
**reverses** day to day — consistent with the bounded-OI mechanism above, and
too consistent across symbols to be anything else.

**The project's title hypothesis, tested directly, is not supported.**

### 2.2 But futures positioning leads option positioning

Raw within-symbol correlation of `signal(t)` against `target(t+lag)`,
`(c)` = share of symbols agreeing with the mean's sign:

| Direction | lag 0 | lag 1 | lag 2 | lag 3 | lag 5 |
|---|---|---|---|---|---|
| **futures → options** | −0.2184 (99%) | **+0.0295 (77%)** | **+0.0349 (86%)** | **+0.0224 (79%)** | +0.0148 (63%) |
| **options → futures** | −0.2184 (99%) | +0.0031 (55%) | +0.0009 (51%) | +0.0021 (55%) | +0.0025 (53%) |

**The asymmetry is the finding.** Futures positioning direction predicts option
positioning direction 1–3 days ahead, with 77–86% of the 217 symbols agreeing
on the sign. The reverse is a coin flip — ~0.003 with 50–55% consistency.

Symmetric confounds cannot produce an asymmetric result. A shared factor, a
common construction error, or a market-wide driver would show up in both
directions.

The **same-day** relationship is strongly *negative* (−0.218, 99% of symbols):
on a day when futures show long buildup, option flow is writer-dominated —
somebody takes the other side. Then over the next one to three days option
positioning turns bullish. That sign flip from same-day to next-day is what
makes this a lead rather than a correlation.

Cross-sectionally the same effect measures **+0.047 to +0.055, significant at
every horizon from 1 to 10 days** — the largest and most consistent signal
anywhere in this project. For comparison, Stage 1's best margin was +0.002.

**Interpretation.** Institutions establish directional exposure in futures
first — cheaper, more liquid, no premium decay, no vega — and option
positioning follows over the subsequent days. Effect size is small (~0.03
correlation) but broad and directionally clean.

### 2.3 Two spurious findings caught

Both would have been reported as discoveries without the diagnostics.

**Mirrored leads.** `ce_writer_frac` appeared to lead `opt_direction` at
**+0.0714** (cross-sectional, h=1) — larger than the futures lead. But its
*same-day* correlation with `opt_direction` is **−0.75**: they share the CE
buckets by construction. Combined with `opt_direction`'s own reversal of
−0.075, the mechanical route predicts ≈ +0.056 — essentially all of it. Its
consistency also collapses from 97% at lag 1 to 65% at lag 2, where the genuine
futures lead *strengthens*. `oi_outlier_direction` is the same story with a
same-day correlation of **+0.84**.

The futures lead is not explicable this way: its same-day correlation is only
−0.153, and −0.153 × −0.075 ≈ +0.012, roughly a quarter of the observed +0.047.

**A normalisation artefact.** An intermediate version applied causal
within-symbol z-scoring to remove symbol fixed effects, and reported
`q_fut_direction` self-persistence of **+0.2979** with a smooth monotone decay
— by far the largest number the project had produced, and a sign flip from the
raw −0.019.

It was an artefact. The causal z subtracts a *slowly updating* expanding mean,
so consecutive z-scores share a common baseline error and appear strongly
persistent even on a series with zero autocorrelation. §2.1's raw measurement
(−0.0093, 36% of symbols positive) is the truth.

The lesson generalises: **the standard repair for symbol fixed effects
introduced a worse confound than the one it removed.** The raw per-symbol
correlation in §2.2 is the only table here free of every confound, which is why
it is the one the conclusions rest on.

### 2.4 Calibration

- **Shuffled-within-date baseline:** −0.005 to +0.002, clearing the interval
  once in five horizons — consistent with the stated false-positive rate.
- **Size baseline** (`fut_oi_notional`, the confound that made pooled GEX H1 a
  size artefact): +0.005 to +0.016, never significant at short horizons.
- **50 cells tested, ~3 expected to clear a 95% interval by chance.** Reported
  in the output so single starred cells are not over-read. The futures lead is
  not one of these: it is significant at all five horizons in the
  cross-section, and independently confirmed by the raw per-symbol table.

---

## 3. What this means

The project's title question gets a two-part answer:

1. **Positioning direction does not persist.** Futures direction is
   uncorrelated with its own future; option direction actively reverses. There
   is no momentum to ride in the positioning signal itself.
2. **But there is a directional lead between instruments.** Futures
   positioning leads option positioning by 1–3 days, asymmetrically, broadly
   across symbols, and this survives every control applied.

This is the first positive result in the project that withstands scrutiny. It
is small, and it is about the *internal structure of the derivatives market*
rather than about price — which is what the project set out to study.

---

## 4. Next steps

1. **Establish the mechanism.** Is the lead driven by the same institutions
   layering into options after futures, or by dealers hedging into options
   against futures flow? `participant_oi` splits futures and options by
   category and can distinguish these at market level.
2. **Condition on magnitude.** The lead is measured on all symbol-days. If it
   is genuinely institutional it should be stronger on days with large
   `fut_oi_chg_notional`, and absent on quiet days.
3. **Validate against real whale events** — the bulk/block deal path. If the
   futures→options lead is institutional, it should be strongest around
   externally dated large trades.
4. **Test the reverse conditioning.** Does the lead survive when the same-day
   futures/option relationship (−0.218) is partialled out explicitly, rather
   than argued around as in §2.3?
