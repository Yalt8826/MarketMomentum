# 07 — The Project Idea

[← How derivatives move markets](06-how-derivatives-move-markets.md) · [Index](README.md) · Next: [The data →](08-the-data.md)

Background over. This is what MktMtm actually is.

---

## 1. The question

> **Can we see, in publicly published end-of-day market data, where large
> institutional players are positioning themselves in individual Indian stocks?**

Note what that question is *not*. It is not "will RELIANCE go up tomorrow." The
project deliberately abandoned price prediction, and document 13 tells the story
of why.

## 2. Detection, not prediction — and why the difference is everything

This distinction was clarified partway through the project and it changed the
entire research design. It is worth being very precise about.

| | **Forecasting** | **Detection / nowcasting** |
|---|---|---|
| The question | Will institutions buy this stock next month? | Have institutions been buying this stock over the past few weeks? |
| What you need | Something that predicts a future decision | Something that reveals a decision already being executed |
| The evidence available | Only correlations with future outcomes | The *actual* footprint of the ongoing activity |
| When it can be right | Only if human decisions are predictable | Whenever the mechanism in document 06 holds |
| Is it useful? | Extremely, if it worked | Yes — because the official answer is 4 months late |

**Forecasting requires predicting a human decision that has not been made.**
Detection only requires reading the consequences of one already in progress.

The project's first four modelling stages asked forecasting questions, inherited
from an earlier price-prediction design. All four returned null or negative
results. The fifth asked a detection question against a real per-symbol label and
produced the project's only surviving positive finding. That is not a
coincidence, and it is the single most important structural lesson in the whole
repository.

### 2.1 Why detection is still worth something

If a detector only tells you what already happened, why bother?

Because of the **disclosure lag**:

```
   quarter starts        quarter ends       SEBI deadline    NSE publishes
   1 Jul 2026 ──────────► 30 Sep 2026 ──────► +21 days ──────► ~14 Nov 2026
        │                                                           │
        └──── F&O footprint visible daily, all along ───────────────┘
                                                            ▲
                              a detector firing 30 days into the quarter
                              leads the public disclosure by ~4 months
```

A detector that fires in *July* about a position the world learns about in
*November* is not describing the past. It is describing the present, four months
before the present becomes public.

## 3. The problem that shapes everything: there is no label

Machine learning needs examples with answers attached. Here is the situation:

```
   WHAT WE WANT TO PREDICT              WHAT NAMES THE PARTICIPANTS

   Institutional flow in RELIANCE       NSE participant-wise open interest:
   on 2026-08-28                        FII / DII / Pro / Client positions
                                        for the WHOLE MARKET, one row per day

     ↓ per symbol                         ↓ market-wide
     217 symbols × 1,254 days             1,258 days, no symbol dimension
     = 212,441 rows                       = 6,290 rows
     ❌ NO LABEL EXISTS                    ✅ LABEL EXISTS, ❌ BUT IS BLIND
```

The data that has symbols names nobody. The data that names people has no
symbols. **Every modelling stage in this project is a different response to that
single fact:**

| Stage | Its answer to the missing label |
|---|---|
| 1 | Work where the label exists — predict the market-wide number |
| 1b | Not a model; make the features carry a *direction* so later stages can use them |
| 2 | Abandon labels entirely — look for latent structure without supervision |
| 3 | Build a bridge — distribute the market-wide number across symbols |
| 4 | Self-supervise — ask whether the features predict *themselves* forward |
| **5** | **Find a different label** — SEBI quarterly filings, per-symbol and named |

Stage 5 is the one that worked, and it worked because it stopped trying to be
clever about a missing label and went and got a real one.

## 4. The label that finally worked

SEBI requires every listed company to file a quarterly shareholding pattern. The
filing is an XBRL document naming, among other categories:

- foreign portfolio investors (**FII**)
- domestic institutions (**DII**)
- **mutual funds** specifically
- **insurance companies** specifically

...and, crucially, the **number of shares** each holds, not just a percentage.

From two consecutive filings, the project constructs its target:

```
   accumulation(stock X, filing f)  =    shares(f) − shares(f−1)
                                       ────────────────────────────
                                          total_shares(f−1)
```

"Mutual funds bought 0.8% of this company last quarter." Comparable across a
large cap and a mid cap; moves only when somebody actually trades; per symbol;
independently filed.

**Three deliberate choices in that formula**, each of which prevents a specific
error:

1. **Share counts, not percentages.** A holding percentage changes when the
   company buys back stock or issues new shares, with nobody having traded at
   all.
2. **Divided by total shares outstanding.** Raw share counts would rank purely
   by company size.
3. **Consecutive filings, not calendar quarters.** Filing dates are usually
   quarter-ends but not always — RELIANCE has filings dated 08-Dec-2021 and
   29-Oct-2024. Differencing on calendar quarters would silently misalign those.

## 5. The hypothesis, stated so it can fail

> **An institution accumulating a large equity position in stock X leaves a
> detectable footprint in X's derivatives book — through hedging with puts,
> building synthetic exposure with futures and calls, or through the hedging
> their trades force on the dealers who take the other side.**

This is falsifiable, per-symbol, and checkable against ground truth. That is what
distinguishes it from the earlier stages, whose validation was always indirect.

## 6. The rule the project judges itself by

One methodological principle appears in almost every document in this repository,
and it deserves stating on its own:

> **Every claim is measured against the most boring available explanation, and
> the *partial* result after that explanation is controlled for is what gets
> reported — never the raw number.**

The boring explanation differs by context:

| Context | The boring explanation that must be ruled out |
|---|---|
| Market-level flow prediction | The **expiry calendar** |
| Per-symbol accumulation | **Price momentum** — institutions buy what is going up |
| Any volatility feature | **Implied volatility**, which already knows everything |
| Any lead-lag claim | The **same-day** relationship between the two series |

Two examples of what this rule caught:

- `oi_outlier_moneyness` had a raw correlation of **+0.0368** with next-day
  movement. After controlling for implied volatility: **+0.0004**. It was
  measuring IV.
- `q_fut_direction` — a feature with price direction baked into its construction
  — posts a large raw correlation with FII accumulation, collapses to the
  shuffled-data baseline once momentum is controlled, and **flips sign** between
  momentum-chasing FII and contrarian mutual funds.

FII accumulation itself correlates with price momentum at **+0.360** — by far the
largest number in the project. Interpreted naively, "foreign funds buy stocks
that are going up" would look like a triumphant discovery. It is a truism, and
recognising it as one is what makes the mutual-fund result credible by contrast.

## 7. Scope: what this project is and is not

**It is:**
- A research system for detecting institutional positioning
- Built entirely on free, public, end-of-day data
- Honest about negative results — most of what it established is negative
- A final-year academic project

**It is not:**
- A trading system. Measured effect sizes imply an AUC ceiling around 0.55–0.60.
  Nothing here is an edge.
- Intraday. Everything is end-of-day.
- Tested outside NSE, or outside 2021–2026, a period in which retail
  participation in Indian F&O grew several-fold.
- Able to name an institution. It ranks symbols; it does not identify actors.

[← How derivatives move markets](06-how-derivatives-move-markets.md) · [Index](README.md) · Next: [The data →](08-the-data.md)
