# Stage 5 — Detecting institutional accumulation from the F&O footprint

**Status:** specification. The label (`shareholding`) is being fetched; the
experiment is not built yet.

**This stage reframes the project.** Stages 1–4 asked forecasting questions
inherited from the abandoned direction model. The actual goal is
**nowcasting**: given a symbol's F&O activity, detect that institutions *have
been accumulating the underlying stock*.

---

## 1. Why the earlier stages kept failing

| Stage | Question it answered | Right shape? |
|---|---|---|
| 1 — market flow | Will flow rise *tomorrow*? | ❌ forecasting |
| 2 — regime HMM | Do days cluster into states? | ❌ no target at all |
| **3 — attribution** | **Where did *today's* flow go?** | ✅ contemporaneous, per-symbol |
| 4 — flow momentum | Does today's direction *continue*? | ❌ forecasting |
| 1b — quadrants | (feature engineering) | ✅ reusable |

Stage 3 had the right shape and the wrong **label** — it attributed a
market-wide F&O number and validated against an earnings calendar that its own
placebo beat. Point the same machinery at a real per-symbol equity-accumulation
label and it becomes this stage.

## 2. The hypothesis

> An institution accumulating a large equity position in stock X leaves a
> detectable footprint in X's F&O book — hedging with puts, building synthetic
> exposure through futures and calls, or the dealer hedging their trades induce.

Falsifiable, per-symbol, and finally checkable against ground truth.

## 3. The label

`shareholding` — SEBI-mandated quarterly filings, ~20 per symbol over the
project's window, ~200 F&O symbols. See
[`deals-label.md`](deals-label.md) for why bulk/block deals could not serve
(the 0.5%-of-listed-shares threshold selects microcaps by construction).

**Target definition.** For consecutive filings `f₋₁ → f`:

```
accumulation(X, f) = (fii_shares(f) − fii_shares(f₋₁)) / total_shares(f₋₁)
```

Three deliberate choices:

- **Share counts, not percentages.** A holding percentage moves when the
  company buys back stock or issues new shares, with nobody having traded.
- **Divided by total shares**, so "FIIs bought 0.8% of the company" is
  comparable between a large cap and a mid cap. Raw Δshares would rank purely
  by size.
- **Consecutive filings, not calendar quarters.** Filing dates are usually
  quarter ends but not always — RELIANCE has filings dated 08-DEC-2021 and
  29-OCT-2024. Differencing on calendar quarters would silently mis-align
  those.

Run the same target for `dii_shares`, and separately for `mf_shares`: mutual
funds respond to retail inflows while insurers are slow, so they should *not*
behave alike. If a signal predicts both identically, suspect it is tracking
something common to neither.

## 4. Signals — and why means will not work

The window is ~90 days. If an institution accumulates over three weeks,
averaging F&O features across the whole quarter dilutes the signal by roughly
4×. **Tail and count statistics are expected to beat means here**, and the
comparison is part of the experiment:

| Family | Statistic over the window |
|---|---|
| Outlier activity | count of days with `oi_outlier_count > 0`; max `oi_outlier_share`; sum `oi_outlier_build_notional` |
| Direction | mean, max, and *fraction of days positive* for `q_fut_direction` and `opt_direction`; mean `oi_outlier_direction` |
| Writers | mean `ce_writer_frac`, `pe_writer_frac` |
| Basis | mean and max `fut_basis_excess`; its p75−p25 across the window |
| Futures flow | sum `fut_oi_chg_notional`; sum of the four quadrant notionals |
| Chain | mean `gex`, `put_call_oi_ratio`, `chain_oi_chg` |

Report the mean-based and tail-based feature sets as separate ablations. If
means win, the accumulation is slow and diffuse; if tails win, it is episodic.
Either answer is informative.

## 5. Three windows, testing three different claims

The user's framing is **detection with a short lead** — the footprint should
appear before the accumulation is publicly visible. That needs the window
varied explicitly:

| Variant | Features from | Label over | Claim tested |
|---|---|---|---|
| **Contemporaneous** | all of Q | Q | The footprint exists at all |
| **Early** | first 30 days of Q | Q | It appears *early* in the accumulation |
| **Lead** | Q−1 | Q | It precedes the accumulation entirely |

**Contemporaneous is the primary test.** Early is the one that matters for the
stated goal. Lead is forecasting, which Stages 1 and 4 already suggest will
fail — included as contrast, and if it *succeeds* where the others fail, be
suspicious of leakage before celebrating.

### The disclosure lag is an asset

Shareholding is published roughly **six weeks after** the period ends. So even
the purely contemporaneous variant, if it works, knows the institutional
position change well before the market is told. That is worth stating in the
write-up: a nowcaster here is not merely descriptive.

## 6. Evaluation

**Cross-sectional rank IC per filing period** — rank ~200 symbols by signal,
correlate against ranked accumulation. `ml/metrics.py`'s `daily_rank_ic`
applies directly with `date_col` as the filing period. Ranking within a period
removes anything common to the whole market, exactly as in Stage 4.

⚠️ **Only ~20 periods.** That is 20 IC observations, not 1,250. Confidence
intervals will be wide and a single quarter can move the mean. Report the full
per-quarter IC series, not just its mean, and do not lean on a t-stat with 19
degrees of freedom. `block_bootstrap_mean_ci` is the wrong tool at this n — use
the per-period series and say plainly how many periods were positive.

### Mandatory baselines

| Baseline | Why |
|---|---|
| **Size** (`total_shares`, or OI notional) | The confound that made pooled GEX H1 an artefact. Institutions concentrate in large caps, so size alone will rank accumulation. |
| **Persistence** (previous period's accumulation) | Institutions accumulate over multiple quarters; beating this is the bar. |
| **Price momentum** (return over the window) | Institutions buy what is going up. A signal that only rediscovers this has found nothing. |
| **Shuffled** | Calibrates the false-positive rate at n=20. |
| **Leak tripwire** | Include the label itself; must score ≈1.0 or the harness is broken. |

**Price momentum is the baseline that matters most here.** It is this stage's
equivalent of Stage 1's calendar: the obvious explanation that must be ruled
out before any F&O feature gets credit.

## 7. Traps specific to this stage

1. **Filing dates are not calendar quarters.** Difference consecutive filings
   per symbol, and align F&O windows to the actual filing dates.
2. **Gaps.** A symbol that skipped a filing has a two-period gap; its Δ then
   spans six months and is not comparable. Either drop it or normalise by
   elapsed days — and say which.
3. **Survivorship.** Symbols entering the F&O universe mid-window have fewer
   periods. Do not drop them silently; that is how survivorship bias enters
   (`screen_corporate_actions` blanks rather than drops for the same reason).
4. **Corporate actions.** A bonus or split changes the share count without any
   trading. `total_shares` moving sharply between filings is the flag; check it
   before trusting a large Δ.
5. **FII ≠ the whale you imagine.** The FPI category includes passive index
   funds mechanically tracking inclusion changes. An index rebalance will show
   as a large Δ with no informed trading behind it.

## 8. Acceptance criteria

- [ ] Target built from consecutive filings with elapsed-day normalisation
- [ ] Both mean-based and tail-based feature sets, reported as an ablation
- [ ] All three windows (contemporaneous / early / lead)
- [ ] All five baselines, with **price momentum** reported most prominently
- [ ] Per-period IC series reported in full, not just its mean
- [ ] Leak tripwire ≈ 1.0
- [ ] `mypy --strict`, `ruff`, unit tests including a no-look-ahead test
- [ ] Results appended here, negative results included

## 9. Results

**Run 2026-09-05.** `uv run mktmtm accumulation`, 60s.
2,888 (symbol, filing) rows · 209 symbols · 19 filing periods · median window
61 trading days. `models/evaluation/accumulation.json`.

**126 of 3,753 periods (3.4%) were dropped as corporate actions.** Left in,
they put the 99th percentile of "accumulation" at FIIs acquiring **31% of a
company in one quarter**, which no institution does organically.

### 9.1 The headline: FII and DII behave in opposite ways

| Cell | Price momentum IC | Best raw feature | **Best partial (momentum controlled)** | Positive periods |
|---|---|---|---|---|
| fii:contemporaneous | **+0.335** | +0.250 | +0.048 | 62% of 16 |
| fii:early | **+0.281** | +0.220 | −0.049 | 42% of 12 |
| fii:lead | **+0.175** | +0.133 | +0.036 | 75% of 16 |
| dii:contemporaneous | −0.038 | +0.076 | **+0.074** | **89% of 19** |
| dii:early | −0.095 | −0.097 | +0.041 | 69% of 13 |
| dii:lead | −0.131 | −0.105 | +0.033 | 67% of 18 |

Two distinct results, and they need reading separately.

### 9.2 Foreign institutions: the F&O signal is a momentum proxy

FII accumulation tracks price momentum at **IC +0.335, positive in 94% of
periods**. Foreign investors buy what is going up, emphatically.

`q_fut_direction_mean` posts a raw IC of **+0.250** — which looks like a strong
detector until momentum is partialled out, at which point it collapses to
**+0.048 with 62% of periods positive**. The shuffled baseline is +0.025 at
62%. It is at the noise floor.

The asymmetry settles it: run the control the other way and **momentum retains
+0.233 given the feature**, while the feature retains 19% of its IC given
momentum. If the two were complementary both would survive; if they measured
one thing both would collapse. Here one survives and one does not.

In hindsight this is near-tautological. `q_fut_direction` is the futures
price-versus-OI quadrant — *price up with OI up is a long buildup* — so price
direction is baked into its construction. It could hardly fail to correlate
with price momentum.

**For FII accumulation, the F&O features add nothing beyond the price.**

### 9.3 Domestic institutions are contrarian — and one signal survives

DII accumulation has a **negative** relationship with price momentum: −0.038
contemporaneous, −0.095 early, −0.131 lead. **Domestic institutions buy into
weakness.** That is economically coherent — domestic funds deploy steady SIP
inflows on dips, where foreign portfolio flows chase performance — and it is a
genuine behavioural difference measured here for the first time in this
project.

It also means momentum is no longer the confound for this half of the data,
and something else can be seen.

**`basis_excess_mean` detects DII accumulation:**

| Measure | Value |
|---|---|
| Raw IC | +0.0759 |
| **Partial IC, momentum controlled** | **+0.0735** (97% retained) |
| Periods positive | **17 of 19** |
| Binomial p (one-sided) | **0.00036** |
| Bonferroni over ~90 tested cells | **0.033** |
| Shuffled baseline | +0.0157, 63% positive |

The mean futures basis above cost of carry, over the quarter, ranks symbols by
how much domestic institutions accumulated — and unlike every FII result it
**survives the momentum control essentially intact**, losing only 3% of its IC.

Economic reading: accumulation creates financing demand. An institution
building a position, or the desks facilitating it, leaves futures trading rich
to carry — and that pressure is visible in the basis independent of which way
the price went.

`gex_mean` shows the same sign across all three DII windows (+0.058 / +0.041 /
+0.029), which is consistent but individually weak.

### 9.4 Calibration

- **Leak tripwire: +1.0000 in every cell.** Harness correct.
- **Shuffled: +0.016 to +0.025**, 56–63% positive. That is the floor every
  partial result must clear, and the FII results do not.
- **Size: −0.015 to −0.030**, never material. The confound that made pooled
  GEX H1 an artefact is not operating here.
- **Persistence: +0.19 to +0.21**, 83–93% positive, in every cell. Institutions
  do accumulate across consecutive quarters, as expected.

### 9.5 Honest limits on the one positive result

- **The effect is small.** IC +0.0735 ranks symbols weakly. Its consistency —
  17 of 19 quarters — is what makes it credible, not its size.
- **19 periods is not many.** One unusual quarter moves the mean, which is why
  the positive rate and the binomial test are reported rather than a t-stat on
  18 degrees of freedom.
- **Multiple testing is real.** Roughly 90 cells were examined and this is the
  best. Bonferroni holds at p = 0.033, but only just, and a result that needs
  the correction to survive should be treated as provisional.
- **DII is not a single actor.** It aggregates mutual funds, insurers and
  banks. Mutual-fund flow is driven by retail SIP subscriptions rather than
  conviction, so part of what is being detected may be *inflow deployment*
  rather than informed accumulation.
- **Not out-of-sample.** The feature set was not chosen in advance of seeing
  these results.

### 9.6 What this means for the project

For the first time, a signal derived from the F&O book detects per-symbol
institutional activity, against an external label, surviving the dominant
confound. That is the result the project has been trying to produce.

It is also modest, single, provisional, and about *domestic* institutions
rather than the foreign flow usually meant by "whales".

**The FII conclusion is the more robust one, and it is negative**: foreign
institutional accumulation is predicted by price momentum, and the option and
futures features tested here add nothing to it.

## 10. Next steps

1. **Confirm `basis_excess` out-of-sample.** The cleanest test available is the
   monthly mutual-fund portfolio data (§6 of `deals-label.md`): ~60 observations
   per symbol instead of 20, an independent source, and MF is the largest DII
   component. If the basis effect reproduces there it stops being provisional.
2. **Split DII.** `mf_shares` and `insurance_shares` are already in the table.
   If the effect is concentrated in insurers (slow, discretionary) rather than
   mutual funds (mechanical SIP deployment), it is far more likely to be
   informed accumulation.
3. **Pre-register a feature set** and re-run. The multiple-testing exposure is
   the weakest part of §9.3 and it is cheap to remove.
4. **Drop the FII line of enquiry**, or reframe it: without a feature that
   beats momentum there is nothing here to build on.
