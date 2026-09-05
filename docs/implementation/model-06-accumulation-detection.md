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

*To be filled in. Lead with the margin over price momentum, not the absolute
IC — and state how many of the ~20 periods were positive before quoting any
mean.*
