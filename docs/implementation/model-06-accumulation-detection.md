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

**Run 2026-09-05, on corrected data — see §9.0.** `uv run mktmtm accumulation`.
~2,886 (symbol, filing) rows · 209 symbols · 19 filing periods · median window
61 trading days. `models/evaluation/accumulation.json`.

126 of 3,753 periods (3.4%) dropped as corporate actions. Left in, they put the
99th percentile of "accumulation" at FIIs acquiring **31% of a company in one
quarter**.

### 9.0 A label bug found and fixed after the first run

The first run of this stage used contaminated data, and the numbers it produced
are superseded. The defect is worth recording because nothing failed:

`fii_shares` was **zero for 100% of 2021 filings and 50% of 2022**, then 0% from
2023 — precisely the XBRL taxonomy boundary. The pre-2023 taxonomy keeps dead
categories as explicit zeros: `ForeignInstitutionsMember` is the legacy FII
bucket, emptied when SEBI folded FII into FPI in 2014, and it sits in the same
filing as `InstitutionsForeignPortfolioInvestorMember` carrying the actual
holding. The alias resolver took the first *present* member and picked the zero.

Through the domestic fallback (`institutions − foreign`), the same bug
**inflated DII holdings by the foreign amount** on those filings. Both targets
were wrong.

Fixed by preferring the first **non-zero** alias, while still preferring a
total over a subset — an existing test caught an over-correction that reordered
the aliases instead. Zeros fell to 0.9%/0.4% (genuine), and the median holdings
are now smooth across the boundary: FII 17.2% → 14.5% and DII 12.6% → 19.0%
across 2021–2026, the well-documented shift of Indian equity ownership from
foreign to domestic hands.

**Everything below is the corrected run.**

### 9.1 Foreign and domestic institutions are opposite

| Holder | Price momentum IC | `basis_excess_mean` raw | **partial (momentum controlled)** | Positive periods | IC retained |
|---|---|---|---|---|---|
| **FII** | **+0.360** | −0.057 | −0.054 | 11% of 19 | 94% |
| **DII** | −0.122 | +0.094 | +0.090 | **95%** | 96% |
| **Mutual funds** | −0.064 | **+0.140** | **+0.137** | **95%** | **98%** |
| **Insurers** | −0.098 | −0.029 | −0.035 | 42% | — |

**FII accumulation is a price-momentum phenomenon.** Momentum alone reaches
+0.360. No F&O feature survives its control: the best raw feature collapses to
the shuffled baseline. See §9.3.

**Domestic institutions are contrarian** — accumulation correlates *negatively*
with momentum (−0.122). They buy weakness, consistent with steady SIP inflows
deployed on dips. With momentum no longer the dominant confound, a real signal
becomes visible.

**The two are counterparties.** `basis_excess` carries the opposite sign for
FII (−0.054) and mutual funds (+0.137): a futures basis rich to carry goes with
domestic funds accumulating *and* foreign investors distributing. That is one
trade seen from both sides, and it is a coherence check neither number provides
alone.

### 9.2 The signal is in mutual funds, not insurers

This was the point of splitting DII, and the split is decisive:

- **Mutual funds: partial IC +0.1374, 18 of 19 quarters positive**, retaining
  **98%** of its raw value under the momentum control. Binomial p = 0.000038;
  Bonferroni over the ~120 cells examined, **p = 0.0046**. Shuffled baseline:
  +0.0014 at 47%.
- **Insurers: −0.035, 42% positive.** Nothing.

So the DII result (+0.090) was mutual funds diluted by insurers and banks.
Isolating them raises the IC by half.

**This did not confirm the hypothesis it was meant to test, and the framing was
wrong.** §10 of the previous run predicted that an effect concentrated in
insurers would indicate informed accumulation, and one in mutual funds would
indicate mechanical SIP deployment. For Indian markets that prior is backwards:
domestic equity mutual funds are predominantly **actively managed**, so while
the aggregate inflow is mechanical, *which stocks it goes into* is a fund
manager's discretionary choice. Insurers — LIC above all — are the slow,
quasi-index participant here.

The honest reading is therefore the more favourable one: the signal tracks
**active fund-manager stock selection**, which is the most discretionary
domestic institutional flow available. But the test did not cleanly separate
"informed" from "mechanical", because the instrument chosen to separate them
was mis-specified.

### 9.3 It detects accumulation in progress, not before it

The three windows answer the project's stated goal — detection with a short
lead — directly. Mutual funds, `basis_excess_mean`, momentum partialled out:

| Window | Features from | Partial IC | Positive periods |
|---|---|---|---|
| **contemporaneous** | the whole quarter | **+0.137** | 18 of 19 |
| **early** | first 30 days only | **+0.085** | 11 of 13 |
| **lead** | the *prior* quarter | — (absent from the top four) | — |

**The footprint appears early inside the accumulation window and does not
precede it.** Thirty days of futures basis rank symbols by that quarter's
mutual-fund accumulation at IC +0.085 — about 62% of the full-quarter strength
— while the prior quarter's basis says nothing at all.

That is exactly the shape asked for: *detect that they have been buying*, not
*predict that they will*. It also matters practically, because shareholding is
published roughly six weeks after the quarter closes: a detector firing 30 days
in leads the public disclosure by about four months.

⚠️ The early result is the weaker of the two. 11 of 13 is binomial p = 0.011,
which does **not** survive Bonferroni across all cells. It is a pre-specified
follow-up on a confirmed signal rather than an independent discovery, so
whole-family correction is arguably too harsh — but it should be read as
suggestive, not established.

### 9.4 Calibration

- **Leak tripwire: +1.0000 in every cell.** Harness correct.
- **Shuffled: +0.001 to +0.024**, 31–63% positive. The floor every partial must
  clear. The FII results do not; the mutual-fund result clears it by 100×.
- **Size: −0.008 to −0.039**, never material. The confound that made pooled GEX
  H1 an artefact is not operating here.
- **Persistence: +0.13 to +0.36**, 92–100% positive. Institutions do accumulate
  across consecutive quarters. Insurers are the most persistent (+0.356),
  consistent with them being the slow participant.

### 9.5 Why `q_fut_direction` fails, and what that teaches

`q_fut_direction_mean` posts a large raw IC in several cells and survives
nowhere. It is the futures **price**-versus-OI quadrant — price up with OI up is
a long buildup — so price direction is baked into its construction. Against FII
(momentum-driven) it looks like a detector and is a momentum proxy; against
mutual funds (contrarian) it takes the *opposite* sign for the same reason.

Any future feature built from a price change should be assumed to be a momentum
proxy until the partial says otherwise.

### 9.5a Robustness: the effect holds under every split tried

Out-of-sample confirmation on monthly mutual-fund portfolios was the intended
test and **was not attempted**: SEBI requires each AMC to publish on its own
site and AMFI does not aggregate them, so it is roughly forty heterogeneous
scrapes over sixty months of scheme-level spreadsheets. That is acquisition
engineering with a high failure rate, not research.

The result is a **single feature with no fitted parameters**, so there is
nothing to overfit except the *choice* of feature. Subset stability is
therefore the right substitute for a holdout — if the effect survives in both
halves of the sample and both halves of the universe, the choice was not luck
in one corner of the data. `basis_excess_mean` was pinned at module level
(`PREREGISTERED_FEATURE`) before any subset was examined; nothing below
searches.

| Subset | Partial IC | Positive periods | Range |
|---|---|---|---|
| **Full sample** | **+0.1374** | 95% of 19 | |
| First half | +0.0701 | 89% of 9 | 2021-10 → 2023-10 |
| **Second half** | **+0.1981** | **100% of 10** | 2024-01 → 2026-04 |
| Large caps (above median OI) | +0.1665 | 79% of 19 | |
| Small caps | +0.1055 | 79% of 19 | |

**Bootstrap over periods: +0.1374, 95% CI [+0.0902, +0.1801] — excludes zero.**
Resampling is over *periods*, not rows: ~150 symbols in one quarter share that
quarter's market entirely, and treating them as independent draws would produce
an interval far too tight.

The effect is present in **every subset**, which is the point. It is
**stronger in the second half** (+0.198 vs +0.070) and stronger in large caps,
neither of which is a problem for its existence — but the time gradient is
worth flagging: 2024–26 was the peak of the Indian domestic-inflow surge, so
the effect may be strengthening with the flows that drive it rather than being
a stable constant.

### 9.5b The counterparty structure is confirmed

If `basis_excess` carries opposite signs against FII and mutual-fund
accumulation because they are two sides of one trade, then FII and MF
accumulation should themselves be negatively correlated across symbols.

**They are: rank correlation −0.4807, negative in 100% of 19 quarters.**

Foreign investors and domestic mutual funds are systematically on opposite
sides of the same stocks, every quarter in the sample. That corroborates the
mechanism directly rather than by analogy, and it is the strongest structural
evidence in this project — unlike Stage 1's Pro/Client check, which turned out
near-tautological because the labels were already −0.80 correlated by
construction. Here the two series are independently filed by different holders
and nothing forces them to oppose.

### 9.5b-ii The signal reads domestic buying, not a transfer

§9.5b showed FII and mutual-fund accumulation oppose each other (−0.48,
negative in every quarter). The obvious follow-up — *is the signal strongest
where they disagree most?* — is **circular as usually posed**: disagreement is
defined by the two accumulations, which are the target, so subsetting on it and
then measuring an IC against that same target conditions on the outcome. Two
non-circular substitutes were run instead.

**The spread as a target** conditions on nothing; it just changes what is
predicted. If the basis reflects ownership *transferring* from foreign to
domestic hands, it should rank `mf − fii` at least as well as `mf` alone:

| Target | Partial IC | Positive |
|---|---|---|
| **`mf` alone** | **+0.1374** | 95% of 19 |
| `fii` alone | −0.0538 | 11% |
| `mf − fii` (the transfer) | +0.1030 | 95% |

**It does not.** The spread is *weaker* than domestic accumulation alone
(+0.103 vs +0.137), even though the FII leg carries the opposite sign and
subtracting it should naively have helped. That means the foreign leg
contributes more variance than signal: **the basis is reading the domestic
side, not the handoff.**

**Prior foreign ownership as a subset** — `fii_pct` at the *previous* filing,
observable before the window opens and not a function of the target. Symbols
foreigners already own heavily have more scope to sell into domestic buying:

| Subset (median prior `fii_pct` 17.2%) | Partial IC | Positive |
|---|---|---|
| High prior foreign ownership | +0.1659 | 84% of 19 |
| Low | +0.1230 | 84% of 19 |

Higher where foreigners own more, but modestly and with an identical hit rate.
Weakly consistent with a transfer, not evidence for one.

**Conclusion: the mechanism is simpler than §9.5b suggested.** Mutual-fund
accumulation creates financing demand and that shows in the basis. Foreign
investors are on the other side — the −0.48 opposition is real — but the signal
detects the domestic leg directly rather than reading the exchange between
them. Both non-circular tests point the same way, and the earlier framing
should be read as corroborating the *economics* rather than describing what the
feature measures.

⚠️ Both substitutes are weaker tests than the circular version would have been.
That is the price of the circular version being invalid.

### 9.5c The time gradient tracks flow, not the calendar — probably

The effect nearly triples between halves (+0.070 → +0.198), which needed
explaining: a mechanism if detection scales with *how much flow there is to
detect*, a limitation if it is specific to the 2024–26 market.

**First, the boring explanation is ruled out.** The pre-2023 half is not
degraded data: `mf_shares` is 0% NULL in both eras, zero-valued on 0.67% versus
0.03%, and the early accumulation distribution is *narrower* (sd 0.038 vs
0.055), not wider. Noisier data would widen it.

Per-period IC correlated against period-level covariates, and then against the
same covariates with **calendar time partialled out** — every covariate here
trends with time, so the raw correlation cannot separate the two:

| Covariate | Raw | **Partial vs time** |
|---|---|---|
| **Gross accumulation** (total \|flow\|) | +0.691 | **+0.369** |
| Period index (the control) | +0.635 | — |
| Symbols in the cross-section | +0.634 | +0.231 |
| Net accumulation | +0.570 | +0.274 |
| Median OI notional (F&O depth) | +0.549 | **−0.067** |
| Accumulation dispersion | +0.374 | **−0.040** |

**Gross accumulation is the only covariate that survives**, at +0.369.
Detection strength tracks how much mutual-fund flow there was that quarter,
beyond the bare time trend. Two candidate explanations are eliminated: F&O
market depth collapses to −0.067, so this is not "the futures market got
bigger", and cross-sectional dispersion collapses to −0.040, so it is not
simply "more variation to rank".

That is the favourable reading — the signal is not specific to 2024–26, it
scales with the flow it detects. **But it is underpowered.** At 19 periods with
one control, +0.369 gives t = 1.59, **p = 0.13**; roughly +0.48 would be needed
for significance. This is the best available explanation of the gradient, not
an established one, and it will stay that way until there are more periods —
which is precisely what monthly mutual-fund data would provide.

### 9.6 Honest limits

- **The effect is modest.** IC +0.137 ranks symbols weakly. Its consistency —
  18 of 19 quarters — is what makes it credible, not its size.
- **19 periods is not many.** One unusual quarter moves the mean, which is why
  the positive rate and a binomial test are reported rather than a t-stat.
- **~120 cells were examined.** Bonferroni holds comfortably for the
  contemporaneous mutual-fund result (p = 0.0046) and not at all for the early
  window.
- **Not pre-registered.** The feature set was chosen before seeing results but
  the holder split was not. The robustness battery in §9.5a *is* pre-registered
  on the single surviving feature, which removes the exposure for those splits
  but not for the original selection.
- **No true out-of-sample test.** §9.5a substitutes subset stability, which is
  weaker than an independent dataset. Monthly mutual-fund portfolios remain the
  test that would settle it.
- **One market, one regime**, 2021–2026, a period of unprecedented Indian
  retail inflow growth. The contrarian DII behaviour may be specific to it.
  §9.5c gives some reassurance — the gradient tracks flow rather than the
  calendar — but underpowered reassurance.

### 9.7 What this means for the project

**This is the project's first result that detects per-symbol institutional
activity, against an external label, surviving the dominant confound, with a
coherent economic mechanism and a counterparty check.**

Mean futures basis above cost of carry ranks symbols by how much actively
managed domestic mutual funds accumulated them that quarter — visible within
the first 30 days, independent of price direction, and with foreign investors
on the other side of the same signal.

The reading: accumulation creates financing demand. A fund building a position,
or the desks facilitating it, leaves futures trading rich to carry.

**The FII conclusion is separate and negative**, and is the more robustly
established of the two: foreign institutional accumulation is explained by
price momentum, and nothing in the F&O book tested here adds to it.

## 9.8 A live nowcast

The detector is run forward against the quarter SEBI has **not yet disclosed**,
producing a prediction that can be scored later and can fail.

**First run 2026-08-28**: 177 symbols over a 59-day window, calibrated IC
**+0.1145** (positive in 17 of 19 historical quarters), open quarter ending
2026-09-30, **checkable from ~2026-11-14**.

- Prediction record (committed, so its timestamp is verifiable):
  [`../results/nowcast-2026-08-28.md`](../results/nowcast-2026-08-28.md)
- Implementation and full results: [`nowcast.md`](nowcast.md)

Two design points worth carrying here: the IC is **calibrated at the live
window's own length** rather than interpolated between the 30-day and
full-quarter figures (the resulting curve is monotone in window length, which
is a consistency check in itself), and the shipped score is
**momentum-residualised**, because the validated result is a partial IC and a
raw ranking would largely be a momentum ranking.

## 10. Next steps

Items 2 and 3 of the previous list are done (§9.5a, §9.5b). What remains:

1. **Monthly mutual-fund portfolios**, if the acquisition cost is ever
   acceptable. ~60 observations per symbol instead of 19, an independent
   source, targeting exactly the holder the effect lives in. It is the only
   test that would move this from "robust across subsets" to "confirmed
   out-of-sample", and it would give the early-window question the power 13
   quarters cannot. Roughly forty AMC scrapes; see §9.5a.
2. **Confirm the flow-scaling story with more periods.** §9.5c finds gross
   accumulation is the only covariate surviving a time control (+0.369), but
   19 periods gives p = 0.13. This is the same bottleneck as item 1 and the
   same fix.
3. **Drop `q_fut_direction` from accumulation work**, or rebuild it on the
   delta-adjusted residual the option quadrants already use, so price direction
   is not baked into it (§9.5).
4. **Test whether the basis signal sharpens where FII and MF disagree most.**
   §9.5b establishes they oppose on average; the natural follow-up is whether
   the detection is strongest in exactly those symbols.
