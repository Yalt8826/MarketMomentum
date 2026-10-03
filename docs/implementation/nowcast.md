# Nowcast — turning the detector into a falsifiable prediction

**Built 2026-09-05.** `ml/accumulation.py` (`nowcast`, `_calibrate_window`,
`_verification_note`, `write_nowcast_record`), CLI `mktmtm nowcast`, 4 unit
tests.

**Method:** [`model-06-accumulation-detection.md`](model-06-accumulation-detection.md).
**Prediction record:** [`../results/nowcast-2026-08-28.md`](../results/nowcast-2026-08-28.md).

---

## 1. Why

Stage 5 established that `basis_excess_mean` ranks symbols by how much
actively managed domestic mutual funds accumulated them, at partial IC +0.137
across 18 of 19 quarters. Every one of those quarters is **closed**: the
shareholding filing exists, the label is known, and the IC is measured against
it.

That is a fit, however carefully controlled. The stronger claim available is a
**prediction made before the outcome exists**, and the data structure hands
one over for free:

```
last shareholding filing        most recent flow data       filing publishes
     2026-06-30      ────────────────►  2026-08-28   ...........►  ~2026-11-14
                     └─ 59 days of F&O footprint ─┘   └─ no label yet ─┘
```

SEBI allows 21 days after a quarter to file. So there is always an **open
window** in which the F&O footprint is observable and the institutional holding
is not.

> ### ⚠️ Correction (measured 2026-10-03): the window is ~3 weeks, not ~6
>
> This document previously said *"NSE publication has run a few weeks behind
> that,"* and every `~2026-11-14` in the nowcast records follows from it. **That
> was an assumption, never measured, and it is wrong by about three weeks.**
>
> The master API carries a **`broadcastDate`** field that `fetch_filing_index`
> discards. Measured across 53 symbols and 1,087 filings:
>
> | Quarter end | n | Median lag | 90th pct | Max |
> |---|---|---|---|---|
> | 2025-09-30 | 53 | 16 d | **21 d** | **21 d** |
> | 2025-12-31 | 53 | 19 d | **21 d** | 77 d |
> | 2026-03-31 | 53 | 17 d | **21 d** | 143 d |
> | 2026-06-30 | 53 | 17 d | **21 d** | 38 d |
>
> Pooled from 2024 onward: **50% within 17 days, 90% within 21, 95% within 26.**
>
> **The 90th percentile is exactly 21 days in every quarter** — the statutory
> deadline binding hard. NSE does not run weeks behind it; it publishes
> essentially *at* it. The long tails (77/143 days) are revisions and amended
> filings, not initial publication.
>
> For the **2026-09-30** quarter this means roughly **21–26 October 2026**, not
> mid-November. The precedent is clean: for the same quarter last year
> (2025-09-30), **all 53 sampled symbols filed within 21 days.**
>
> **The frozen nowcast records are deliberately not edited.** Their
> `Checkable from ~2026-11-14` line is now known to be conservative, but a
> prediction record whose contents are revised after the fact is not a
> prediction record. The correction lives here instead.
>
> A cross-sectional IC needs most of the universe, not all of it —
> `MIN_SYMBOLS_PER_PERIOD` is 30 and the nowcast ranks 177 — so 90% coverage at
> day 21 is comfortably sufficient.

Ranking symbols over that window produces a claim that can be **scored later
and can fail**. That is a materially different epistemic object from another
robustness table, and it is the point of this work.

---

## 2. The design problem: the open window is never a published window

The validated variants are 30 days (`early`, IC +0.085) and a full quarter
(`contemporaneous`, +0.137). The *open* window is whatever has elapsed since
the last filing — on 2026-08-28 that was **59 days**, which is neither.

Interpolating between the two published numbers would be an assumption. The
implementation measures instead.

### Implementation

`_window_bounds(row, window, *, early_days=EARLY_DAYS)` was generalised so the
`early` variant takes an arbitrary day count, and `build_accumulation_frame`
threads `early_days` through. `_calibrate_window(conn, *, holder, feature,
window_days, ...)` then rebuilds the whole historical panel at that length and
returns the partial IC measured on it.

So a nowcast over a 59-day window is calibrated against 19 historical 59-day
windows, not against the nearest published figure.

### The calibration curve

| Window | Partial IC | Positive periods |
|---|---|---|
| 30d | +0.0846 | 85% of 13 |
| 45d | +0.0947 | 89% of 19 |
| **59d (live)** | **+0.1145** | **89% of 19** |
| 75d | +0.1335 | 89% of 19 |
| Full quarter (~91d) | +0.1374 | 95% of 19 |

**Monotone in window length.** This was not designed for and is worth noting:
a signal that accumulates information over a window should get stronger as the
window lengthens, and it does, smoothly, across five points. A jagged or
non-monotone curve would have suggested the window length was interacting with
something else.

The 30-day row has 13 periods rather than 19 because a 30-day window needs a
prior filing far enough back to leave 20 trading days; the shorter the window,
the more symbol-periods fall below `MIN_DAYS_IN_WINDOW`.

---

## 3. Implementation

### 3.1 Determining each symbol's window

```python
as_of  = max(flow_features.date)                    # 2026-08-28
filed  = max(shareholding.date) per symbol          # mostly 2026-06-30
window = (filed, as_of]
```

Per symbol, not globally: 208 of 213 symbols filed on 2026-06-30 but five did
not, and using a single global start would silently mis-window those.

**Stale filings are excluded.** `NOWCAST_WINDOW_TOLERANCE = 1.5` drops any
symbol whose window exceeds 1.5× the median. One symbol's last filing was
2024-09-30 — a 697-day window spanning eight quarters, whose accumulation is
not comparable to a cross-section of 59-day ones.

### 3.2 Feature aggregation

Reuses `aggregate_window` unchanged, which is the point: the nowcast computes
its features with exactly the same code path the historical evaluation uses.
A separate implementation would be a place for the two to drift apart.

`MIN_DAYS_IN_WINDOW = 20` still applies, so a symbol with a fortnight of data
is not ranked.

### 3.3 Momentum residualisation

The validated result is a **partial** IC controlling price momentum, so the
shipped score must be a partial too. Handing back a raw `basis_excess` ranking
would largely hand back a momentum ranking — and momentum explains *foreign*
flow, not domestic fund flow (Stage 5 §9.1).

Cross-sectionally, rank both, regress the signal rank on the momentum rank,
keep the residual:

```python
ranked = frame[["signal_raw", "window_return"]].rank(method="average")
x, z   = ranked["signal_raw"], ranked["window_return"]
beta   = x.cov(z) / z.var()
score  = x - beta * (z - z.mean()) - x.mean()
```

Ranks rather than levels, because the validated metric is a rank IC and
`basis_excess` has heavy tails (the live run spans +2.75 to −41.43).

### 3.4 The verification note

`_verification_note(as_of)` computes when the prediction becomes checkable:
the open quarter's end plus 45 days. That is deliberately later than SEBI's
21-day statutory deadline, because NSE publication has consistently lagged it
in this dataset.

### 3.5 Recording it so the timestamp means something

`write_nowcast_record(result, path)` writes a markdown record to
`docs/results/`, which is **committed**. `models/` is gitignored, so an
artifact there would prove nothing about *when* a prediction was written — and
a forward prediction whose date cannot be established is not a forward
prediction. The machine-readable full ranking still goes to
`models/evaluation/nowcast.json` for scoring.

The record states its own limits on its face: the calibrated IC, that this is a
claim about *ordering across the universe* rather than about any name in it,
and the exact commands to score it.

**The record is immutable by default.** `write_nowcast_record` raises
`NowcastRecordExists` rather than overwriting. A record whose immutability
depends on nobody re-running the command with the same `--out` is not frozen,
and the failure would be silent: a forecast made before the outcome replaced by
one made after it, under the original filename and the original claim.
`--overwrite` exists only to correct a record on the day it was written.

---

## 4. Results — the 2026-08-28 run

| | |
|---|---|
| As of | 2026-08-28 |
| Window | 59 calendar days, 43 trading days (median), minimum 20 |
| Universe | **177 symbols** ranked |
| Excluded | 1 stale filing, 35 with no F&O flow in the window |
| **Calibrated IC** | **+0.1145**, positive in 17 of 19 historical quarters |
| Open quarter ends | 2026-09-30 |
| **Checkable from** | **~2026-11-14** |

### Top and bottom of the ranking

| Rank | Symbol | Score | `basis_excess` | Window return |
|---:|---|---:|---:|---:|
| 1 | PAYTM | +89.6 | +2.55 | +44.63% |
| 2 | INDUSTOWER | +87.7 | +2.75 | +0.66% |
| 3 | KALYANKJIL | +85.6 | +1.00 | +59.24% |
| 4 | MUTHOOTFIN | +85.1 | +1.38 | +2.93% |
| 5 | JSWENERGY | +84.2 | +1.40 | −8.21% |
| … | | | | |
| 173 | HEROMOTOCO | −82.9 | −25.82 | +18.52% |
| 174 | LODHA | −84.5 | −29.79 | +33.84% |
| 175 | NHPC | −84.8 | −16.87 | −7.75% |
| 176 | LICHSGFIN | −87.0 | −26.37 | −3.24% |
| 177 | IREDA | −90.0 | −41.43 | −9.07% |

The window returns at both ends are mixed — +44.6% and −8.2% among the top
five, +18.5% and −7.8% among the bottom — which is what a momentum-neutral
ranking should look like.

### Residualisation, verified

| Measure | Value |
|---|---|
| rank corr(score, window return) | **−0.008** |
| corr(raw signal, window return) | −0.026 |
| rank corr(score, raw signal) | 0.9996 |

Momentum is neutralised. Note the third row: in *this* window the raw signal
was already nearly uncorrelated with momentum, so residualising barely
reordered anything. That is a property of this particular quarter and not a
reason to drop the step — the historical windows where the two *were*
correlated are exactly the ones the control exists for.

### Coverage

177 of the 213 symbols with shareholding filings:

- **1 dropped** for a stale filing (697-day window).
- **35 have no F&O flow data after 2026-06-30.** They have shareholding
  filings because they are listed, but they have left the derivatives
  universe — so there is no footprint to read. This is correct behaviour, not
  a gap, but it means the nowcast universe is smaller than the full F&O
  catalogue and will shrink as contracts are delisted.

---

## 5. How to score it

```bash
uv run mktmtm fetch-shareholding      # once the open quarter's filing publishes
uv run mktmtm accumulation --holder mf --window contemporaneous
```

Then rank-correlate the recorded `score` column against the realised
`mf_shares` change for the quarter. Under the null that correlation is 0; the
calibration says to expect **≈ +0.11**.

**A materially negative result would be evidence against Stage 5**, not merely
a bad quarter. That is the point of writing it down in advance, and it should
be reported as such if it happens.

---

## 6. What could go wrong, honestly

- **One quarter is one observation.** The historical hit rate is 17 of 19, so
  roughly one quarter in ten comes out negative even if the effect is real. A
  single failed nowcast falsifies nothing on its own; a run of them would.
- **The calibration is in-sample in one respect.** The 59-day window length was
  chosen by the data (it is what had elapsed), not by us — but the *feature*
  was selected on the same 19 quarters this is calibrated against.
- **The universe shifts.** Symbols leave the F&O catalogue between now and the
  filing; those drop out of the scoring rather than counting as misses.
- **Corporate actions.** The historical evaluation drops 3.4% of periods for
  splits and bonuses. The scoring run must apply the same filter, or a bonus
  issue will look like a spectacular detection failure.
- **The 2024–26 regime.** The effect is stronger in the second half of the
  sample and appears to scale with mutual-fund flow volume
  (model-06 §9.5c). If domestic inflows slow, the calibrated IC is optimistic.

---

## 7. Tests

`tests/unit/test_accumulation.py`, all pure:

| Test | Asserts |
|---|---|
| `test_verification_date_follows_the_open_quarter` | The checkable date is after SEBI's 21-day deadline, not on it |
| `test_early_window_is_parameterised` | An arbitrary day count produces the matching window, so calibration is possible |
| `test_nowcast_score_is_momentum_neutral` | A deliberately momentum-contaminated signal (corr > 0.7) residualises to \|corr\| < 0.15 |
| `test_stale_filings_are_excluded_from_the_nowcast` | A 697-day window is dropped against a cross-section of 59-day ones |

The momentum test is the load-bearing one: it plants the exact failure mode the
residualisation exists to prevent, and asserts the fix removes it.
