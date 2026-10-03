# Pre-submission audit — October 2026

**Run 2026-10-03.** Commits `e429788`, `47014fc`, `3de432c`, `4d2b7fe`.
Data refreshed to **2026-10-01**; health gate **PASS**.

A single working session, undertaken four weeks before submission, whose
purpose was to stop adding and start checking. It produced two silent defects,
one closed threat to the project's headline result, one new negative result,
a reusable data survey, and Gate 3's first eligible record.

This document is the complete record: what was done, how, and what it found.
Each section links to the standing document that now carries the finding.

---

## Contents

| § | What | Outcome |
|---|---|---|
| [1](#1-can-this-be-traded-day-to-day) | Day-to-day tradeability | **New negative result** — the daily edge went gross-negative in 2024 |
| [2](#2-what-else-does-nse-publish) | NSE archive survey | 8 new usable sources, full 5-year history |
| [3](#3-defect-7--the-screen-selected-a-banned-symbol) | **Defect 7** — banned symbol selected | Fixed; 1-in-6 live hit rate |
| [4](#4-verifying-the-pipeline-refresh) | Pipeline verification | PASS; 4 suspicious signatures all documented behaviour |
| [5](#5-the-symbol-catalog-has-drifted) | Catalog drift | 177 of 213 live names; **not** a price-source problem |
| [6](#6-gate-3s-first-eligible-record) | Gate 3 record | 1 pending record, honest provenance |
| [7](#7-defect-8--gate-3s-provenance-check-never-worked) | **Defect 8** — provenance check | Fixed; the gate had been unpassable |
| [8](#8-is-the-time-gradient-sample-composition) | Constant-universe control | **Gradient survives**, 73% retained |

---

## 1. Can this be traded day to day?

**Question asked:** the project's strategy is monthly. Is there a daily version,
and does the data support intraday decisions at all?

### What was found in the code

Every strategy in the repository is monthly. `backtest/strangle.py` enters on
the first trading day; `serving/screen.py` warns and marks the record
off-calibration if it is not. But four daily-horizon targets are already built,
look-ahead-validated by `validate_no_lookahead`, and persisted:

| Target | Read by |
|---|---|
| `next_day_abs_return` | the GEX hypothesis tests only — never a strategy |
| `next_day_rv_5d` | nothing |
| `next_day_iv_chg` | nothing |
| **`next_day_vrp`** | **nothing at all** |

`next_day_vrp` is `|next-day return| − ce_iv_30/√252` — the daily short-vol P&L
proxy. Built, validated, stored on 200,952 rows, and never consumed.

### Three structural limits, measured not assumed

| Limit | Evidence |
|---|---|
| **Intraday is impossible** | All seven sources are EOD; bhavcopy publishes after the close. Earliest action from day-`t` data is `t+1`. |
| **Stock options are monthly-only** | The 2026-09-04 F&O bhavcopy carries exactly **3** `OPTSTK` expiries (29-Sep, 27-Oct, 23-Nov). NSE removed weekly single-stock options. |
| **Weeklies exist only on indices** | 18 `OPTIDX` expiries incl. 08/15/22-Sep. But only NIFTY, BANKNIFTY and NIFTYNXT50 have usable spot, and the GEX work excluded indices because the Pro index book flipped sign mid-sample. |

So a "daily" strategy on stocks means holding a ~30-day option and re-hedging
daily — which is what the measurement below approximates.

### The measurement, and the functional form that matters

The naive reading of `next_day_vrp` is encouraging: mean **−0.577%** per day
(negative = short vol wins), **75.7%** of days negative, and monotonically
decaying 2021→2026.

**But `|ret|` is the wrong shape.** A delta-hedged short option's daily P&L is
quadratic in the move, proportional to `σ²_impl·dt − (dS/S)²`, so a linear
measure understates exactly the large moves that do the damage. Re-measured
correctly, and scale-free so that implied volatility's mechanical presence in
the target cancels:

**`1 − ret² / (iv²/252)`** — positive means IV was overpriced, short vol won:

| Year | Mean | Median | Days won |
|---|---|---|---|
| 2021 | **+0.237** | +0.765 | 80.5% |
| 2022 | +0.177 | +0.740 | 78.0% |
| 2023 | +0.152 | +0.760 | 78.8% |
| 2024 | **−0.004** | +0.697 | 75.0% |
| 2025 | −0.043 | +0.686 | 74.3% |
| 2026 | **−0.321** | +0.609 | 69.4% |

**The daily edge went gross-negative in 2024 — before any costs.** The median
stays positive in every single year, which is the whole story: most days still
win, but the tail now loses more than the wins collect.

On the raw quadratic measure the concentration is extreme:

| | |
|---|---|
| Total won | +48.4171 over 152,065 days |
| Total lost | −39.3273 over 48,887 days |
| **Net** | **+9.0898** |
| **Worst 1% of days** | **−13.5542 — 149% of the entire net** |
| Worst 0.1% | −3.7803 |

**The whole edge is decided by about 2,000 days out of 200,952.** The worst
single day of 2026 is 2026-04-07 at −0.002107 mean across symbols.

### A circular IC, caught before it was reported

Ranking symbols by `ce_iv_30` against the raw `iv²/252 − ret²` target gives
**IC +0.4524, t = +69.57** — which would have been the largest forward number in
the project's history. It is an artefact: `iv` appears *inside* the target, so
higher IV mechanically raises it.

This is the project's own documented trap — *"direction scores share
construction… always check the contemporaneous correlation before believing a
forward one."* Removing the mechanical scaling:

| Feature | Raw (circular) | Scale-free | **Partial vs `ce_iv_30`** |
|---|---|---|---|
| `ce_iv_30` | +0.4524 | +0.0534 | — |
| `ce_ivr` | +0.1672 | +0.0360 | +0.0170 |
| `gex` | +0.1821 | +0.0347 | +0.0177 |
| `dist_to_flip` | +0.1573 | +0.0044 | **−0.0176** |

An order of magnitude lost to the correction, and everything collapses to
±0.018 once IV is controlled — the same answer Gate 1 reached at a monthly
horizon, reproduced independently at a daily one.

### Verdict

**No day-to-day strategy, and intraday is permanently out of reach with this
data.** The gross edge is already negative, costs at daily frequency would
multiply (the monthly trade pays ~₹368 across 10–12 orders over 30 days), and
there is no short-dated stock instrument to trade.

**Why it is worth having anyway:** it independently corroborates Gate 0's
decay finding (+8.30% → +1.09%) from a completely different measurement, and
pushes it further — *the premium itself compressed*, which is a mechanism Gate 0
could only speculate about.

> ⚠️ These are single-pass measurements: no block bootstrap, no walk-forward, no
> cost model. Indicative, not a gate.

→ Standing record: [`../results/daily-horizon.md`](../results/daily-horizon.md)

---

## 2. What else does NSE publish?

**Question asked:** the project's docs state that *"NSE bulk/block deal history
cannot be fetched programmatically."* Is that NSE-wide, or specific to that
report?

### The access rule, corrected

It is specific. The boundary is the **host and path**, not NSE:

| Path | Behaviour |
|---|---|
| `nsearchives.nseindia.com/...` static files | **Full 5-year history**, with a warmed cookie session |
| `www.nseindia.com/api/*` | Bot-blocked — the 503 that killed the deals label |

Deals fail because they are served only via the API path. Several archive
families serve the complete window. The project's note generalised one failure
into a rule about NSE, and that cost it several usable sources.

### What is available — all verified live

| File | Content | Verified |
|---|---|---|
| `content/nsccl/fao_participant_vol_DDMMYYYY.csv` | Participant-wise **trading volume** (Client/DII/FII/Pro) | 200 |
| `archives/equities/mto/MTO_DDMMYYYY.DAT` | **Per-symbol daily delivery %** — 2,633 symbols, **213 of our 222** | 200 |
| `content/indices/ind_close_all_DDMMYYYY.csv` | 165 index closes + P/E, P/B, Div Yield | 200 |
| `archives/fo/sec_ban/fo_secban_DDMMYYYY.csv` | F&O ban list | 200 |
| `content/indices/ind_nifty<sector>list.csv` | Sector constituents **with an `Industry` column** | 200 |
| `content/nsccl/mwpl_cli_DDMMYYYY.xls` | Position-limit utilisation | 200 |
| `content/fo/fii_stats_DD-Mon-YYYY.xls` | FII derivatives statistics | 200 |
| `content/cm/BhavCopy_NSE_CM_...csv.zip` | Equity cash bhavcopy | 200 |

Not found: `fo_secban` under `content/` (wrong prefix), `volt_`, `combineoi_`,
`shortselling_`.

### History was verified, because that is what killed the deals label

Probed at 2021-08-02, 2023-01-03, 2024-01-02, 2025-04-01, 2026-09-04 and
2026-09-18 for four families. All returned 200 except a **uniform** 404 at
2022-03-01 — confirmed against our own `option_metrics` as a market holiday
(Mahashivratri: 0 rows, with 155 on each adjacent day). So the families are
complete, unlike `bulk.csv`/`block.csv` which serve only the latest day.

### Ranked by value to the project

1. **MTO delivery data.** The project's central documented obstacle is that *no
   per-symbol institutional label exists and none can be obtained*. Delivery
   percentage is per-symbol, daily, five years deep, 213/222 covered, mean
   58.3%. It is not a *who* label — retail takes delivery too — but it is the
   most direct free proxy for real ownership transfer, at ~1,250 observations
   per symbol against shareholding's 19.
2. **The F&O ban list.** See §3 — it fixed a live defect.
3. **Sector constituents.** The screen's sector cap is built, unit-tested and
   **inert** because "the catalog carries no sector field." These files carry
   `Industry` directly (Bank 14, IT 10, Pharma 20, Auto 15, FMCG 15, Metal 15).
4. **Participant trading volume.** We hold participant OI (a *stock*); this is
   *flow*. Volume ÷ OI is position turnover, separating "FII rolled the book"
   from "FII built new positions" — a different target that the expiry calendar
   may not dominate as it did in Stage 1.
5. **`ind_close_all`.** Fills the documented FINNIFTY/MIDCPNIFTY spot gap and
   adds daily P/E and P/B, where the project currently has no fundamental data.
6. **MWPL utilisation** — a crowding measure, and the leading indicator of #2.

→ Standing record: [`../data/nse-archive-survey.md`](../data/nse-archive-survey.md)

---

## 3. Defect 7 — the screen selected a banned symbol

**Found by** reading the F&O ban list for 2026-09-04 while surveying the archive,
and recognising a symbol in it.

```
Securities in Ban For Trade Date 04-SEP-2026:
1,INOXWIND   2,KAYNES   3,LICHSGFIN   4,SAIL
```

**INOXWIND was position #2 in the committed September screen record** —
₹12,543 credit, ₹57,155 margin. When a symbol's market-wide open interest
crosses 95% of its position limit, NSE permits only position-*reducing* trades.
Selling a fresh strangle opens new positions, so **that position could not have
been taken**, and nothing in the project had ever read this file.

### Extent, measured

Across all 62 month-entry dates (62/62 ban files retrieved):

| | |
|---|---|
| Backtest trades entered on a banned symbol | **17 of 10,411 (0.16%)** |
| Mean banned symbols per month-entry day | 0.6 |
| Affected names | CANBK, PNB, BHEL, AMBUJACEM, HINDCOPPER ×2, SAIL, BIOCON, IDEA, INDUSTOWER, GRANULES, BANDHANBNK, RBLBANK ×3, MANAPPURAM |

**Negligible in the backtest, material in the live tool.** The Gate 0 verdict is
not at risk. But the screen hit one on its *first ever run* — 1 of 6 positions —
because bans land on high-IV, high-activity names and that is exactly what the
screen orders by. **The selection mechanism and the defect are correlated**,
which is how a 0.16% population rate becomes a 1-in-6 rate in production.

### The fix, and the part that is actually the point

`data/secban.py` fetches and parses the list. The URL is under
`archives/fo/sec_ban/`, not the `content/` prefix that serves participant OI.

The substance is the **failure semantics**. NSE publishes this file on *every*
trading day, writing `NIL` on the title line when nothing is banned — verified
at 2023-01-03 and 2025-04-01, both `200` with `...: NIL`. So three cases must
stay distinct:

```
200 + symbols  ->  those symbols are banned
200 + NIL      ->  authoritatively zero bans
anything else  ->  UNKNOWN — never "zero"
```

Returning an empty set for the third case would re-admit the silent-omission
failure this project has already suffered twice. So `fetch_secban` raises on any
non-200 including the holiday 404, `run_screen` degrades to
`secban_checked=False` rather than failing, and **the record states on its face
that bans went unverified.** The parser also rejects a file served for the wrong
date rather than applying another day's bans.

Verified end to end on the date of the defect: INOXWIND, LICHSGFIN and SAIL
refused as `in F&O ban on 2026-09-04`; the freed margin admits GODREJPROP and
VEDL.

→ Standing record: [`../trading/results/bugs-and-fixes.md`](../trading/results/bugs-and-fixes.md)

---

## 4. Verifying the pipeline refresh

Data was refreshed to **2026-10-01**, October's first trading day.
`scripts/verify_db_health.py` exits 0 — *"PASS — database is ready for ML
training."*

| | |
|---|---|
| Range | 2021-08-02 → 2026-10-01 |
| Core tables | 216,619 rows each, **1:1**, 217 symbols (+3,240 rows) |
| `participant_oi` | 6,385 rows, through 2026-10-01 |
| `flow_regimes` / `flow_attribution` | 165,353 / 529,407 — model stages ran |
| Parquet | both rebuilt |
| Integrity | no duplicate `(symbol, date)`, no empty chains, no missing spot, strike/spot share one basis, `\|next_day_return\| > 0.30` = **0** |

### Four suspicious signatures, all documented behaviour

Each was chased rather than assumed. None is a defect:

| Signature | Explanation |
|---|---|
| **2026-09-29: zero `ce_iv_30` across all 180 symbols** | One of **51 monthly expiry dates** with the same signature. Time-to-expiry ≈ 0, so bisection returns `None` rather than a sentinel — the documented invariant working. |
| `fut_basis_excess` NULL on 09-25/28/29 | The documented DTE<5 expiry-week gap (annualising a one-day basis multiplies by 365). Repopulates 09-30 once the expiry rolls. |
| All 180 targets NULL on 2026-10-01 | Correct — no "tomorrow" yet. Inference rows, never training rows. |
| 24 degenerate symbol-days in the last 400 | Faithfully recorded; consumers skip them, and the screen now refuses them. |

Recording this explicitly because "I checked and it was fine" is only worth
something if the check is stated.

---

## 5. The symbol catalog has drifted

**Found by** chasing the health gate's claim that five symbols lack a *"Yahoo
spot source: FINNIFTY, LTIM, MIDCPNIFTY, PEL, TATAMOTORS."* Three of those are
liquid large caps, which did not fit the explanation.

### It is not a price-source problem

| Check | LTIM / PEL / TATAMOTORS |
|---|---|
| Yahoo `<SYMBOL>.NS` | **404 Not Found** (RELIANCE.NS returns 23 bars) |
| NSE **equity cash** bhavcopy 2026-10-01 | **absent** (RELIANCE present at ₹1,167.70) |
| NSE **F&O** bhavcopy 2026-10-01 | **absent** |

They no longer exist under those symbols. They were renamed:
**`TATAMOTORS → TMPV`**, **`LTIM → LTM`**, PEL gone. Both successors trade
actively and neither is in the catalog.

### The real number

Comparing the catalog against what actually traded on 2026-10-01:

| | Count |
|---|---|
| Live F&O stock symbols | 213 |
| Catalog `individual_securities` | 217 |
| **In catalog, no longer trading** | **40** |
| **Trading, absent from catalog** | **36** |
| **Actual overlap** | **177 — 83% of the live universe** |

And the absences are not marginal. By open interest on 2026-10-01:

| Symbol | OI rank | Shares |
|---|---|---|
| **SUZLON** | **3 of 213** | 691,184,800 |
| **ADANIPOWER** | **13** | 278,181,550 |
| **TMPV** | **24** | 180,704,000 |
| SAGILITY | 34 | 135,744,000 |
| SWIGGY | 45 | 116,177,675 |
| VMM / RVNL / MAHABANK | 58 / 64 / 67 | — |

The 40 dead symbols are *visible* — they are exactly the screen's
`40 no data for this date` refusals. **The 36 live ones are invisible**: nothing
refuses them because nothing knows they exist.

### What follows, and what deliberately does not

`CLAUDE.md` states the catalog "must never regenerate." That is the **right call
for the research**: a fixed universe avoids look-ahead in universe selection, and
re-deriving it would break comparability with every published result. The
survivorship direction is worth stating though — names that *left* F&O are
retained while names that *joined* are excluded.

It is the **wrong call for the screen**, which should trade what is tradeable
today and currently cannot see the third-largest F&O name in the market. That
change was *not* made here: it alters the live tool's universe mid-way through
Gate 3's six-month window, which is exactly the kind of change that should not
land between a gate's first and second records.

→ Standing record: [`../data/catalog-drift.md`](../data/catalog-drift.md)

---

## 6. Gate 3's first eligible record

Gate 3 had **0 of 6** months. October's first trading day was **2026-10-01** —
two days before this session — and the screen had not been run, because the
pipeline refresh had not yet happened.

### The question, and a reversed judgement

The initial advice in-session was *not* to generate an Oct-1 record after the
fact, on the grounds that it repeats the precedent Phase 3 explicitly refused:
*"forward with respect to the outcome, backward with respect to the entry."*

**That advice was wrong**, and the reason is a fact that had not been checked:

| Date | |
|---|---|
| Thu 1 Oct | traded — bhavcopy publishes after the close |
| Fri 2 Oct | Gandhi Jayanti, closed (ban-list and participant-OI 404) |
| Sat 3 Oct | weekend |

**No trading session had closed between the entry date and the record's
creation.** The information set was exactly the one a trader held at the Oct 1
close. The September case it was compared against had *three* trading sessions
of hindsight on entry and ten days of outcome knowledge.

The backtest's own convention settles it: entry is at the first trading day's
**settlement**, which bhavcopy publishes only after the close. So *every* screen
is necessarily generated after its own entry date.

### What was actually wrong, and was fixed

`write_screen_record` opened with `"**Written {as_of}**"`, which would have
asserted this record was written on 10-01. It now separates the entry date from
the generation date and publishes `sessions_after_entry`, computed from the data:

```
Entry date 2026-10-01, before any outcome exists. 7 positions …

Generated 2026-10-03, after the 2026-10-01 entry date — but no trading session
has closed in between (0 sessions in the data after 2026-10-01), so this
selection used no information a trader lacked at the entry.
```

A non-zero count prints as **hindsight-contaminated** instead of being argued
away. The reader audits the gap rather than taking the record's word for it.

### The record

| | |
|---|---|
| Positions | 7 — POLICYBZR, IDEA, KALYANKJIL, ANGELONE, PATANJALI, TATAELXSI, HINDZINC |
| Margin | ₹492,066 (49.2% of capital) |
| Net credit | ₹117,269 |
| Expiry / exit day | 2026-10-27 / **2026-10-26** |
| Committed | **2026-10-03 14:15:41 +0530** — 24 days before the outcome |
| Bans caught | **BANDHANBNK, SAIL** — BANDHANBNK was September's largest position |

Conditions 1, 2 and 4 are met; condition 3 awaits the expiry. **Scoreable from
~27 October**, at which point Gate 3 stands at **1 of 6**.

### September's record now has an outcome

**6/6 scored, portfolio +0.30%, worst trade −61.69%, net ₹1,414** — against a
backtested +4.93% mean monthly. One month proves nothing, and it remains
excluded (wrong entry day, and it contained the banned INOXWIND), but it is the
only genuine out-of-sample datapoint the strategy has, and it is not
encouraging.

→ Standing record: [`../trading/results/gate-3-forward-validation.md`](../trading/results/gate-3-forward-validation.md)

---

## 7. Defect 8 — Gate 3's provenance check never worked

**Found by** the October record being excluded as *"not committed before its
outcome"* despite having been committed 24 days before its expiry. September's
record — committed since `8e5073e` — resolved the same way, which is what
showed it was the check and not the record.

### What was wrong

```python
subprocess.run(["git", "log", "--diff-filter=A", "--format=%cs",
                "--", str(path)], cwd=path.resolve().parent)
```

The pathspec was passed through **as given** while `cwd` was set to the file's
own parent. `load_records` hands it a repo-root-relative path, so git resolved
`docs/trading/results/screens/screen-X.md` *underneath* the screens directory,
matched nothing, and the function returned `None`.

Demonstrated three ways:

| Invocation | Result |
|---|---|
| relative pathspec, `cwd` = the file's dir (as the code did) | **empty** |
| pathspec relative to that `cwd` | `2026-09-11` |
| absolute pathspec | `2026-09-11` |

### What it cost

`committed_at` returned `None` for **every record ever written**. So
`provenance_verified` was false universally, `in_calibration` with it, and
**Gate 3 could not be reached** however many valid months were recorded — while
the report blamed each record for failing the one condition that is the entire
point of the gate.

### Why it stayed silent

Two reasons, and both are the instructive part:

1. **It fails closed.** Excluding everything meant the eight backdated screens
   were still correctly refused, so no published verdict was ever wrong.
2. **"Unverifiable" is an answer this function is *allowed* to give.** A
   universal `None` is indistinguishable from a repository with no commits.

> **A correction this forces.** `docs/understanding/v1/22-forward-validation.md`
> called the +94.55% exclusion *"a better demonstration of the check than any
> passing test."* It was not a demonstration of anything — the check refused
> everything. The exclusion was right for the wrong reason.

### The fix

Resolve the path before using it as the pathspec. Tests now build a real git
repo and commit into it, covering the repo-root-relative shape that failed, the
absolute shape that worked, an uncommitted file, and a path outside any repo —
the last two being the cases that must still yield `None`.

`score-screens` now reports October as **"nothing scored yet"** rather than a
provenance failure, which is its actual state.

---

## 8. Is the time gradient sample composition?

**The one threat to the project's headline result**, and the reason this audit
was worth doing before submission rather than after.

### The threat

Stage 5's partial IC nearly triples between halves — **+0.0701 → +0.1981**. The
stage investigated that against period covariates and concluded gross
mutual-fund flow was the only survivor (+0.369).

**Effective sample size was not among the covariates tested**, and it moves
sharply inside the window:

| Period | Symbols carrying the feature |
|---|---|
| 2021-10 → 2024-07 | 126–144 |
| **2024-10 → 2026-04** | **165–191** |

NSE widened F&O eligibility in late 2024, so more names clear the feature's own
`DTE ≥ 5` gate. An IC is a correlation measured *within* each quarter, and a
correlation over 190 names is less noisy than one over 130 — so the second half
could score higher with no change in the underlying signal. **The step lands
almost exactly on the half boundary.**

### The test

Hold the universe fixed and re-measure, sweeping the presence threshold rather
than fixing one arbitrary cut:

| Min presence | Symbols | Per period | Full | 1st half | 2nd half | **Gradient** |
|---|---|---|---|---|---|---|
| none (as published) | 209 | 151.9 | +0.1374 | +0.0701 | +0.1981 | **+0.1280** |
| ≥50% | 144 | 132.8 | +0.1394 | +0.0701 | +0.2017 | +0.1316 |
| ≥70% | 136 | 127.6 | +0.1450 | +0.0753 | +0.2078 | +0.1326 |
| ≥80% | 120 | 115.2 | +0.1352 | +0.0675 | +0.1961 | +0.1286 |
| ≥90% | 93 | 91.6 | +0.1567 | +0.0940 | +0.2131 | +0.1191 |
| **100%** | **62** | **62.1** | **+0.1537** | **+0.1045** | **+0.1980** | **+0.0935** |

### Verdict: SURVIVES

The gradient is **stable at +0.119 to +0.133 from 50% through 90% presence**,
narrowing only at the strictest cut to +0.0935 — **73% retained**. The
second-half IC barely moves anywhere (+0.196 to +0.213).

**The shape of the shrinkage is the finding.** It is not the second half falling
but the **first half rising** (+0.0701 → +0.1045): symbols present throughout
are the established, liquid names and carry a stronger early signal, so
composition had been *understating* the early period rather than inflating the
late one.

Two side results:

- **The headline is not composition-inflated either.** On cleaner universes it is
  *higher* — +0.1537 at 100% presence, +0.1567 at 90%, against +0.1374 published.
- The second half stays positive in **100% of its ten periods at every
  threshold**.

⚠️ Reported with its cost: the 100% cut keeps 62 of 209 symbols, so each
quarterly correlation rests on far fewer names and the first-half positive rate
falls to 7 of 9. The sweep is published whole rather than quoting the strictest
cut alone — which would be cherry-picking in the unfavourable direction, but
cherry-picking still.

**Reproduce:** `uv run python scripts/check_constant_universe.py` →
`models/evaluation/constant_universe.json`.

→ Propagated to [`../results/README.md`](../results/README.md),
[`model-06-accumulation-detection.md`](model-06-accumulation-detection.md) and
[understanding doc 12](../understanding/v1/12-the-models.md).

---

## What this audit changed

| | |
|---|---|
| **Defects found** | 2 (ban list, provenance check) — project total **18**, of which exactly one crashed |
| **Threats closed** | 1 — the time gradient is not composition |
| **New negative results** | 1 — no daily-horizon strategy; the premium compressed to gross-negative |
| **Claims corrected** | doc 22's provenance "demonstration"; the catalog's "no Yahoo spot source" |
| **Gate 3** | 0 → 1 pending eligible record |
| **New data sources identified** | 8, all with 5-year history |
| **Tests** | 568 → **601** |

### The pattern, again

Both defects produced plausible output and raised nothing, and both were found by
checking something nobody required:

- The ban defect surfaced because a survey of unrelated data happened to list a
  symbol that looked familiar.
- The provenance defect surfaced only because a record that *should* have passed
  was refused — i.e. it took a correct input to reveal that the check rejected
  everything. **A check that fails closed cannot be validated by anything it
  rejects.**

That second one is the most transferable lesson in this document, and it
generalises beyond this project: a safety check which is never exercised with a
*passing* case has not been tested at all.
