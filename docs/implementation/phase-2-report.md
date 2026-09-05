# Phase 2 report — from forecasting to detection

**Period covered:** 2026-09-05, commits `4110ea2` → `9eb59c1` (12 commits).
**Scope:** everything after the [Stage 1–3 results report](../results/README.md)
was first written.

**Deliverables:** 3,658 lines across 13 files — two new data pipelines, two new
modelling stages, 70 new unit tests (401 total). Two data defects found and
fixed. One result that survives every control applied to it.

| | |
|---|---|
| New modules | `ml/flow_momentum.py` (389), `ml/accumulation.py` (779), `data/shareholding.py` (608), `data/deals.py` (318) |
| New tables | `shareholding` (4,428 rows), `deals` (158) |
| New CLI commands | `flow-momentum`, `accumulation`, `fetch-shareholding`, `fetch-deals` |
| New tests | 17 + 23 + 19 + 11 = 70 |

---

## 1. Why the project changed direction

Phase 1 produced three nulls and one microstructure finding. The diagnosis was
not that the features were weak — it was that **every stage had been asking a
forecasting question while the project's goal is detection.**

| Stage | Question it answered | Right shape? |
|---|---|---|
| 1 — market flow | Will flow rise *tomorrow*? | ❌ forecasting |
| 2 — regime HMM | Do days cluster into states? | ❌ no target at all |
| **3 — attribution** | **Where did *today's* flow go?** | ✅ contemporaneous, per-symbol |
| 4 — flow momentum | Does today's direction *continue*? | ❌ forecasting |

Stage 3 had the right shape and the wrong **label**: it attributed a
market-wide F&O number and validated against an earnings calendar that its own
placebo beat. The corrected framing is **nowcasting** — given a symbol's F&O
activity over a window, detect that institutions *have been* accumulating the
underlying stock.

That reframing made the bottleneck obvious and singular: **the project had ~30
features of detection machinery and no per-symbol ground truth to test them
against.** Everything in §3 exists to fix that.

---

## 2. Stage 4 — flow momentum

The last stage built under the old framing, and the one whose failure clarified
it. Full detail: [`model-05-flow-momentum.md`](model-05-flow-momentum.md).

### Implementation

`ml/flow_momentum.py`. Self-supervised — signal and target both from
`flow_features`, so no external label needed.

- `forward_mean(frame, column, horizon)` — mean of a column over `t+1 … t+h`
  within symbol. Implemented as `shift(-1).rolling(h).mean().shift(-(h-1))`;
  the trailing shift moves the window's right edge onto `t`, without which the
  first `h-1` rows of every symbol return `NaN` despite having a fully
  populated forward window. Results are reindexed onto the caller's row order,
  because callers assign the series positionally.
- `momentum_table(...)` — a signal × target × horizon grid, plus a
  contemporaneous table and a raw within-symbol lead-lag table.
- `_lead_lag(...)` — plain per-symbol Pearson correlation with no ranking or
  normalisation, reporting `consistency` (the share of symbols agreeing with
  the mean's sign) alongside the mean.

**Metric choice.** A within-date cross-sectional rank IC is neutral to two
confounds at once: the market-wide common factor cancels, and since all NSE
stock futures share one monthly expiry, `fut_dte` takes a single value on
**83.6% of dates** (median cross-sectional spread 0) — so the expiry cycle that
dominated Stage 1 cannot drive it.

**Overlapping windows.** At horizon `h` the targets at `t` and `t+1` share
`h−1` forward days, so the daily IC series is autocorrelated by construction
and a naive t-stat overstates significance by roughly `√h`. Every interval uses
a moving-block bootstrap with block ≥ `2h` (`block_bootstrap_mean_ci`).

### Results

**No momentum.** Raw within-symbol autocorrelation of `q_fut_direction` is ~0
at every lag (−0.009 to +0.007, positive for only 36–56% of symbols).
`opt_direction` actively *reverses* (−0.092 at lag 1, negative for **98.6%** of
symbols) — open interest is a bounded stock, so a position opened today must
eventually close.

**But futures lead options**, raw within-symbol, `(c)` = share of symbols
agreeing on sign:

| Direction | lag 0 | lag 1 | lag 2 | lag 3 |
|---|---|---|---|---|
| futures → options | −0.218 (99%) | **+0.030 (77%)** | **+0.035 (86%)** | +0.022 (79%) |
| options → futures | −0.218 (99%) | +0.003 (55%) | +0.001 (51%) | +0.002 (55%) |

The asymmetry is the finding — symmetric confounds cannot produce a one-way
result. Partialling out the same-day relationship shows lag 1 is 66% mechanical
while lags 2–5 survive nearly intact (peak: lag 2, partial +0.0325, 84%).

**But it is not institutional.** Conditioning on within-symbol terciles of
`|fut_oi_chg_notional|`, the lag-2 partial lead is +0.031 / +0.035 / +0.029
across quiet / mid / heavy days — **flat**. A lead driven by whales should
concentrate where whales are active. This is pervasive microstructure.

### Two spurious findings caught

`ce_writer_frac` appeared to lead `opt_direction` at **+0.071** — larger than
the futures lead — but its *same-day* correlation is **−0.75**: it shares the CE
buckets by construction, so it was a mirror of the target's own reversal. The
partial control takes it to **−0.005**.

An intermediate version used causal within-symbol z-scoring to strip symbol
fixed effects and reported self-persistence of **+0.2979** with smooth monotone
decay — by far the project's largest number. It was an artefact: the causal z
subtracts a *slowly updating* expanding mean, so consecutive z-scores share a
baseline error and look persistent on a series with zero autocorrelation. The
raw check settled it at −0.009. **The standard repair for symbol fixed effects
introduced a worse confound than the one it removed.**

---

## 3. Acquiring a per-symbol institutional label

The central problem of the whole project. Two sources were investigated.

### 3.1 NSE bulk and block deals — built, but structurally unsuitable

`data/deals.py`, `deals` table. Full detail:
[`deals-label.md`](deals-label.md).

**Acquisition is blocked.** Measured directly:

| Route | Result |
|---|---|
| `bulk.csv` / `block.csv` | ✅ 200 — **latest trading day only** (156 + 2 rows) |
| `/api/historical/{bulk,block}-deals` | ❌ **HTTP 503** |
| Three dated archive filename patterns | ❌ 404 |
| BSE `api.bseindia.com/.../BulkDeals/w` | ❌ 200 but HTML, not JSON |

The 503 persists with a fully warmed session — cookies obtained, referring page
returning 200, browser `User-Agent`, `Sec-Fetch-*` and `X-Requested-With` set.
The body is NSE's bot-detection page; the same report downloads fine from a
browser. So history requires manual download, and `fetch-deals` takes
`--file` / `--dir` for that alongside `--latest` for forward accumulation.

**And bulk deals structurally exclude the F&O universe.** On the day measured,
**0 of 158 rows and ₹0 of ₹6,525 crore** fell in the 217 F&O symbols. That is
definitional: a bulk deal requires one client trading **>0.5% of listed shares
in a day**, which for a large cap is billions of rupees and for a microcap is
routine. The sample was ABH Healthcare, Ahimsa Industries, Antelopus — exactly
what the threshold selects. Block deals reach large caps but there were 2 in
the whole day.

The table records that this is a **one-sided label**: a disclosed deal implies a
whale, but its absence does not imply none, since institutions slice orders
below the threshold precisely to avoid disclosure. Recall is measurable against
it; precision is not.

### 3.2 SEBI quarterly shareholding — the label that worked

`data/shareholding.py`, `shareholding` table. **4,428 filings, 213 of 217
symbols, ~20 per symbol, 2021–2026.** The four missing are three indices
(correctly) plus MCX.

`/api/corporate-share-holdings-master` answers programmatically, unlike the
deals endpoint. The master record carries only promoter/public percentages, so
the FII/DII breakdown is parsed from the linked XBRL filings.

**Implementation decisions that matter:**

- **Gzip cache** at `data/raw/shareholding/<SYMBOL>/<date>.xml.gz`. This paid
  for itself twice: the first run died on a cardinality violation *after*
  downloading everything, and the taxonomy fix in §5 required re-parsing all
  4,428 filings. Both cost zero downloads.
- **Share counts are the load-bearing columns.** A holding percentage moves
  when the company buys back stock or issues shares, with nobody having traded.
  The count only moves when somebody buys or sells.
- **Percentage scale is inferred, not hard-coded.** The XBRL stores fractions
  (0.1720 = 17.20%). If every institutional percentage is below 1.0 the file is
  using fractions; otherwise it is passed through. A hard-coded `× 100` would
  silently corrupt every row the day a taxonomy version changed. Validated
  against the master API's own `public_val`.
- **`total_shares` is derived, not parsed.** No reliable tag exists —
  `TotalMember` carries no count and the undimensioned share facts are
  individual shareholder rows. It is recovered as `shares / pct` from every
  category reporting both, which makes it self-checking: categories must agree
  within 1% or it returns `NULL` rather than a plausible wrong number.
- **Revised filings are deduplicated** on `recordId`. A company can file the
  same period twice; left alone they collide on the primary key and the UPSERT
  fails outright — which is how the bug was found.
- Symbols containing `&` are URL-encoded (M&M, M&MFIN were silently dropped
  otherwise), and dates parse with explicit formats since `%b` and `%B` are
  identical for May.

---

## 4. Stage 5 — accumulation detection

`ml/accumulation.py`. Full detail:
[`model-06-accumulation-detection.md`](model-06-accumulation-detection.md).

### Implementation

**Target.** For consecutive filings `f₋₁ → f`:
`(fii_shares(f) − fii_shares(f₋₁)) / total_shares(f₋₁)`. Consecutive filings
rather than calendar quarters, because filing dates are usually quarter ends
but not always (RELIANCE filed 08-DEC-2021 and 29-OCT-2024) and differencing on
quarters would misalign those.

**Corporate-action filtering.** 126 of 3,753 periods (3.4%) dropped. Two
independent detectors, because neither covers everything: the share-count ratio
between filings (direct, but `total_shares` is NULL on ~11%), and
raw-versus-adjusted price divergence inside the window (covers the rest —
dividends move it ~1%, splits 50%+). Left in, they put the 99th percentile of
"accumulation" at FIIs acquiring **31% of a company in one quarter**.

**Feature aggregation.** `aggregate_window` collapses a symbol's daily rows to
a vector, in two families tested as an explicit ablation: `MEAN_FEATURES`
(diffuse accumulation) and `TAIL_FEATURES` (episodic — outlier day rate, max
share, fraction of days positive). Notionals are divided by the window's mean
futures OI notional; a raw rupee flow ranks by company size, which is the
confound that made pooled GEX H1 an artefact.

**Three windows** test three claims: `contemporaneous` (the footprint exists),
`early` (first 30 days — it appears before the period closes), `lead` (prior
quarter — pure forecasting, included as contrast).

**Evaluation.** Cross-sectional rank IC per filing period. With only ~19
periods, `_summarize` deliberately reports **no t-statistic** — the positive
rate and a binomial test are the honest summaries at that sample size.

### The control that decides everything

`_period_partial_ic(frame, signal, control)` — rank IC of a signal against
accumulation with price momentum partialled out.

Comparing two |IC| magnitudes says which is larger, **not** whether either adds
anything to the other. Price momentum reaches IC +0.360 against FII
accumulation, so an F&O feature only earns credit if it survives that control.
This is Stage 5's equivalent of Stage 1's calendar baseline.

### Results

| Holder | Momentum IC | `basis_excess` raw | **Partial** | Positive |
|---|---|---|---|---|
| **FII** | **+0.360** | −0.057 | −0.054 | 11% of 19 |
| DII | −0.122 | +0.094 | +0.090 | 95% |
| **Mutual funds** | −0.064 | **+0.140** | **+0.137** | **18 of 19** |
| Insurers | −0.098 | −0.029 | −0.035 | 42% |

**FII accumulation is a price-momentum phenomenon.** The best raw feature,
`q_fut_direction_mean` at +0.250, collapses to +0.048 under the control —
against a shuffled baseline of +0.025 at the same 62% positive rate. Run the
control the other way and momentum retains +0.233 given the feature. In
hindsight near-tautological: `q_fut_direction` is the price-versus-OI quadrant,
so price direction is built into it. **This is the more robustly established of
the two findings, and it is negative.**

**Domestic institutions are contrarian** (−0.122): they buy weakness. With
momentum no longer dominant, a real signal appears.

**`basis_excess_mean` detects mutual-fund accumulation**: partial IC
**+0.1374**, retaining **98%** of its raw value, positive in **18 of 19
quarters**. Binomial p = 0.000038; Bonferroni over ~120 cells, **p = 0.0046**.
Shuffled baseline: +0.0014 at 47%. Isolating mutual funds from insurers raises
the IC by half — insurers show nothing.

**It detects accumulation in progress, not before it.** Using only the first 30
days of a quarter the signal reaches +0.085 (11 of 13 quarters); the *prior*
quarter's basis says nothing. Since shareholding publishes ~6 weeks after
quarter end, a detector firing 30 days in leads public disclosure by ~4 months.

---

## 5. Two data defects, both silent

Neither was caught by a test. Both were caught by looking at the data.

### 5.1 The pre-2023 taxonomy uses different member names

Filings before ~2023 share almost no XBRL member names with the current
taxonomy: the foreign total is `ForeignInstitutionsMember`, there is no
domestic total, and mutual funds are `MutualFundsOrUti` rather than
`MutualFundsOrUTI`. Matching only modern names left **every 2018–2022 filing
with a NULL FII holding** (899 rows), and the casing drift alone put mutual
funds at **74.6% NULL**.

Fixed with ordered aliases matched case-insensitively, plus a domestic fallback
(`InstitutionsMember − foreign`), which is exact rather than approximate.
Result: `fii_shares`, `dii_shares` and `mf_shares` all went from 19.5% / 19.5% /
74.6% NULL to **0.0%**, and computable targets rose from 2,988 to 3,704.

### 5.2 Dead categories held as explicit zeros

The more dangerous of the two, because the first fix appeared to work.

`fii_shares` read **zero for 100% of 2021 filings and 50% of 2022**, then 0%
from 2023 — precisely the taxonomy boundary. The pre-2023 taxonomy keeps dead
categories as explicit zeros: `ForeignInstitutionsMember` is the legacy FII
bucket, emptied when SEBI folded FII into FPI in 2014, and it sits in the same
filing as `InstitutionsForeignPortfolioInvestorMember` carrying the actual
277M shares. The alias resolver took the first *present* member and got the
zero. Through the domestic fallback it inflated DII by the same amount.

**Both Stage 5 targets were wrong and nothing failed.** Fixed by preferring the
first **non-zero** alias — and an existing test caught an over-correction that
reordered the aliases instead of adding the rule, which would have broken
"prefer a total over a subset".

Zeros fell to 0.9% / 0.4% and holdings became smooth across the boundary: FII
17.2% → 14.5%, DII 12.6% → 19.0% over 2021–2026, the documented shift of Indian
equity ownership from foreign to domestic hands.

**The detection method generalises:** a quantity that changes discontinuously at
a schema boundary is a defect until proven otherwise. It is now in `CLAUDE.md`.

---

## 6. Robustness

Out-of-sample confirmation on monthly mutual-fund portfolios was the intended
test and **was not attempted**: SEBI requires each AMC to publish on its own
site and AMFI does not aggregate them, so it is ~40 heterogeneous scrapes over
60 months of scheme-level spreadsheets.

The result is a **single feature with no fitted parameters**, so there is
nothing to overfit except the *choice* of feature. Subset stability is the
right substitute, with `basis_excess_mean` pinned as `PREREGISTERED_FEATURE`
before any subset was examined.

| Subset | Partial IC | Positive | Range |
|---|---|---|---|
| **Full sample** | **+0.1374** | 95% of 19 | |
| First half | +0.0701 | 89% of 9 | 2021-10 → 2023-10 |
| **Second half** | **+0.1981** | **100% of 10** | 2024-01 → 2026-04 |
| Large caps | +0.1665 | 79% of 19 | |
| Small caps | +0.1055 | 79% of 19 | |

**Bootstrap: +0.1374, 95% CI [+0.0902, +0.1801] — excludes zero.** Resampled
over *periods*, not rows: ~150 symbols in one quarter share that quarter's
market entirely, so resampling rows would treat them as independent draws and
give a falsely tight interval.

### The counterparty structure

If `basis_excess` opposes between FII and mutual funds because they are two
sides of one trade, the two accumulation series should themselves oppose.

**They do: rank correlation −0.4807, negative in 100% of 19 quarters.**

Unlike Stage 1's Pro/Client check — which proved near-tautological because
those labels were already −0.80 correlated by construction — these two series
are filed independently by different holders, with nothing forcing them to
oppose.

### The time gradient

The effect nearly triples between halves, which needed explaining.

The boring explanation is ruled out: the pre-2023 half is not degraded data
(same NULL rates, and a *narrower* accumulation distribution — noisier data
would be wider).

Every period covariate trends with time, so raw correlations cannot separate
"more flow" from "later date". With calendar time partialled out:

| Covariate | Raw | **Partial vs time** |
|---|---|---|
| **Gross mutual-fund flow** | +0.691 | **+0.369** |
| F&O market depth | +0.549 | −0.067 |
| Cross-sectional dispersion | +0.374 | −0.040 |

**Gross accumulation is the only survivor.** Detection strength tracks how much
flow there was to detect — not the futures market growing, not more variation
to rank. **But at 19 periods, +0.369 gives t = 1.59, p = 0.13.** The best
available explanation, not an established one.

---

## 7. Methodological patterns worth carrying forward

Four recurred often enough to be rules.

1. **A raw correlation is never the result; the partial is.** Three separate
   signals looked like discoveries and died under a control: `ce_writer_frac`
   (shared construction with its target), `q_fut_direction` (price baked into
   its construction), and the +0.298 self-persistence (a normalisation
   artefact). Assume any price-derived feature is a momentum proxy until the
   partial says otherwise.
2. **Check the contemporaneous relationship before believing a forward one.**
   A signal that mirrors its target today will appear to lead it tomorrow
   purely by inheriting the target's own reversal.
3. **Check the correlation between labels before citing an opposition result.**
   Client is −0.76 to −0.84 correlated with every institutional category by
   construction, so "X moves opposite for FII vs Client" is nearly a tautology.
   Two structural checks were written up as strong evidence and had to be
   withdrawn.
4. **Cache raw downloads, parse separately.** Both shareholding fixes required
   re-parsing 4,428 filings; both cost zero downloads.

---

## 8. Where the project stands

**One result survives everything applied to it:** the mean futures basis above
cost of carry ranks symbols by how much actively managed domestic mutual funds
accumulated them that quarter — visible within the first 30 days, independent
of price direction, robust across time and size splits, with a bootstrap CI
clear of zero and foreign investors demonstrably on the other side.

**And one robust negative:** foreign institutional accumulation is explained by
price momentum, and nothing in the F&O book tested here adds to it.

### Open, in priority order

1. **Test whether detection sharpens where FII and MF disagree most.** §6
   establishes they oppose on average; if the signal is strongest in exactly
   those symbols, the mechanism tightens considerably. Free.
2. **Rebuild `q_fut_direction` on the delta-adjusted residual** the option
   quadrants already use, so price direction is not baked in. It may become a
   real feature. Free.
3. **Monthly mutual-fund portfolios.** ~60 periods instead of 19. The *only*
   thing standing between "robust across subsets" and "confirmed
   out-of-sample", and it would simultaneously power the two underpowered
   results — the early-window lead (p = 0.011) and the flow-scaling mechanism
   (p = 0.13). Expensive.

### Honest limits on the positive result

The effect is modest — consistency, not size, makes it credible. Nineteen
periods is few. Roughly 120 cells were examined and the original feature
selection was not pre-registered. There is no true out-of-sample test. And
2021–2026 was a period of unprecedented Indian retail inflow growth, so the
contrarian domestic behaviour the result rests on may be regime-specific.
