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

The futures lead is not explicable this way — but the margin is smaller than a
first pass suggested, and §2.5 measures it properly rather than arguing from
the cross-sectional numbers as an earlier version of this section did.

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

### 2.5 Two follow-up checks — one confirms, one does not

**Check 1: partial out the same-day relationship.** The raw lead double-counts
a mechanical route — the signal correlates with the target *today*, and the
target predicts its own future. Partialling out today's `opt_direction` removes
exactly that path. Per-symbol, then averaged:

| Lag | Raw | **Partial** | Explained by the mechanical route | Consistency |
|---|---|---|---|---|
| 1 | +0.0297 | +0.0102 | **66%** | 65% |
| 2 | +0.0351 | **+0.0325** | 7% | **84%** |
| 3 | +0.0226 | **+0.0217** | 4% | 77% |
| 5 | +0.0143 | +0.0138 | 3% | 62% |

**The lag-1 effect is mostly mechanical; lags 2–5 survive almost intact.** That
is internally consistent: `opt_direction`'s own autocorrelation is −0.092 at
lag 1 but only −0.003 by lag 5, so the mechanical route can only operate
strongly at lag 1.

So the lead is real, but it operates at a **2–3 day horizon**, not one day. The
peak is lag 2: partial **+0.0325** with 84% of symbols agreeing on sign.

The same control annihilates the mirrored signals, exactly as §2.3 predicted:
`ce_writer_frac`'s +0.0669 at lag 1 becomes **−0.0046**.

**Check 2: condition on activity.** If the lead is institutional, it should be
stronger on days with heavy futures open-interest change and weaker on quiet
days. Within-symbol terciles of `|fut_oi_chg_notional|`, partial lead at lag 2:

| Tercile of activity on day `t` | Partial lead | Consistency |
|---|---|---|
| Low (quiet days) | +0.0306 | 69% |
| Mid | +0.0347 | 76% |
| **High (heavy OI change)** | **+0.0285** | 69% |

**Flat.** No monotone increase with activity — if anything the high tercile is
marginally the weakest. The same pattern holds at lag 3 (+0.026 / +0.020 /
+0.022).

**This fails the institutional hypothesis.** A lead driven by whales
accumulating should concentrate on the days whales are active. This one is
present just as strongly on a symbol's quietest third of days. The most likely
reading is **market microstructure** — a pervasive, always-on sequencing
between how the futures and options books absorb the same information, or
routine dealer hedging — rather than episodic institutional accumulation.

## 3. What this means

The project's title question gets a three-part answer:

1. **Positioning direction does not persist.** Futures direction is
   uncorrelated with its own future; option direction actively reverses. There
   is no momentum to ride in the positioning signal itself.
2. **There is a genuine directional lead between instruments.** Futures
   positioning leads option positioning at a **2–3 day** horizon — partial
   +0.0325 at lag 2, 84% of symbols agreeing — asymmetric, and surviving the
   same-day control that annihilates every competing signal.
3. **But it is almost certainly not institutional.** It is equally strong on a
   symbol's quietest days as on its heaviest, which is not how episodic whale
   accumulation would look.

So this is a real, reproducible finding about **derivatives market
microstructure** — the futures book leads the options book by a couple of days
— and not a whale detector. That distinction matters for how it is written up:
it is a contribution about market structure, not evidence that the project's
features detect institutional activity.

The methodological yield may be the more valuable half. Three separate signals
looked like discoveries and were killed by controls specified in advance:
`ce_writer_frac` (shared construction), the +0.298 self-persistence (a
normalisation artefact), and the institutional interpretation of the surviving
lead (flat across activity). Only the last of these would have been caught by a
reviewer rather than by the analysis itself.

---

## 4. Next steps

Checks 1 and 2 above were items 2 and 4 on the original list; both are done.
What remains:

1. **Look for whale activity in *deviations* from the lead, not in the lead
   itself.** §2.5 established the futures→options coupling as a constant
   background property. If that is the baseline, an institutional event might
   show up as an **anomaly** in the coupling — a symbol-day where the two books
   decouple, or couple unusually tightly. That reframing is the most promising
   thing to come out of this stage, and it is untested.
2. **Establish the mechanism.** Dealer hedging or information sequencing?
   `participant_oi` splits futures and options by category and can separate
   these at market level.
3. **Validate against real whale events** — the bulk/block deal path. Now with
   a sharper hypothesis than before: not "does the lead appear near block
   deals" (§2.5 suggests it appears everywhere) but "does the *coupling
   strength* change near them".
