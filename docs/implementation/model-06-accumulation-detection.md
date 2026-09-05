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

### 9.6 Honest limits

- **The effect is modest.** IC +0.137 ranks symbols weakly. Its consistency —
  18 of 19 quarters — is what makes it credible, not its size.
- **19 periods is not many.** One unusual quarter moves the mean, which is why
  the positive rate and a binomial test are reported rather than a t-stat.
- **~120 cells were examined.** Bonferroni holds comfortably for the
  contemporaneous mutual-fund result (p = 0.0046) and not at all for the early
  window.
- **Not pre-registered.** The feature set was chosen before seeing results but
  the holder split was not.
- **One market, one regime**, 2021–2026, a period of unprecedented Indian
  retail inflow growth. The contrarian DII behaviour may be specific to it.

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

## 10. Next steps

1. **Confirm out-of-sample on monthly mutual-fund portfolios.** ~60 observations
   per symbol instead of 19, an independent source, and it targets exactly the
   holder the effect lives in. This is now the single highest-value test: it
   would move the result from provisional to established and would give the
   early-window question the statistical power 13 quarters cannot.
2. **Pre-register `basis_excess_mean` and re-run.** The multiple-testing
   exposure is the weakest part of §9.2 and removing it is cheap.
3. **Test the counterparty structure directly.** FII and MF carry opposite signs
   on the same feature; if they are genuinely two sides of one trade, the effect
   should be strongest in symbols where both moved most.
4. **Drop `q_fut_direction` from accumulation work**, or rebuild it on the
   delta-adjusted residual the option quadrants already use, so price direction
   is not baked in.
