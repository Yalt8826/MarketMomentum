# 13 — The Journey: How the Project Got Here

[← The models](12-the-models.md) · [Index](README.md) · Next: [Results →](14-results.md)

This is the chronological story, covering both branches: what `main` built, what
`feat/gex-dealer-positioning` added, and — most instructively — the two whole
approaches that were built, evaluated and abandoned.

Nothing here was planned in advance. Each phase is a response to what the
previous one measured.

---

## The shape of it

```mermaid
flowchart TB
    subgraph MAIN["Branch: main — the foundation"]
        M0["Phase 0-1<br/>Project skeleton +<br/>pure options mathematics"]
        M1["Phase 2-3<br/>Data acquisition +<br/>the JSON pipeline"]
        M2["Phase 4-5<br/>PostgreSQL storage,<br/>threaded ETL, backtester"]
        M3["Phase 6-7<br/>CLI, UI,<br/>smoke-run fixes"]
        M4["Integrity work<br/><i>IV audit, look-ahead leak,<br/>cache offset map, spot basis</i>"]
    end

    subgraph BRANCH["Branch: feat/gex-dealer-positioning — 54 commits"]
        B1["<b>Attempt 1</b><br/>XGBoost price direction<br/>❌ archived"]
        B2["<b>Attempt 2</b><br/>GEX dealer positioning<br/>❌ hypothesis fails"]
        B3["Data foundations<br/><i>participant OI, lot sizes,<br/>flow features, quadrants</i>"]
        B4["<b>Attempt 3</b><br/>Stages 1-4: flow modelling<br/>⚠️ null / negative / mixed"]
        B5["<b>The reframe</b><br/>detection, not forecasting"]
        B6["Label hunt<br/><i>deals ❌ → shareholding ✅</i>"]
        B7["<b>Attempt 4</b><br/>Stage 5: accumulation<br/>✅ the surviving result"]
        B8["Nowcast + engineering<br/><i>frozen prediction,<br/>pipeline runner, incremental ETL</i>"]
    end

    M0 --> M1 --> M2 --> M3 --> M4 --> B1
    B1 --> B2 --> B3 --> B4 --> B5 --> B6 --> B7 --> B8

    style B7 fill:#1a3a1a,stroke:#4caf50,color:#fff
    style B5 fill:#3d2f00,stroke:#ffc107,color:#fff
```

---

## Part I — `main`: building the machinery

The `main` branch contains no modelling at all. It is entirely infrastructure,
and it was built test-first in numbered phases.

### Phases 0–1 — skeleton and pure mathematics

Project setup (pytest, mypy strict, ruff), then `options/` — Black-Scholes
pricing, the Greeks, bisection implied volatility, Yang-Zhang realised
volatility, at-the-money strike selection.

**Design decision that paid off repeatedly:** these are *pure functions with no
I/O*. They can be unit-tested against known values with no database, no network
and no fixtures. Every later bug in this area was caught here rather than in
production data.

### Phases 2–3 — data acquisition and the JSON pipeline

`data/` — bhavcopy (both file formats), Yahoo spot, the symbol catalog,
interest rates, earnings dates. Then `pipeline/process.py`, which enriches
bhavcopy into processed JSON files.

That module is now **legacy**: it feeds only the old UI, not the modelling path.
It is retained because the UI uses it, and it carries a standing hazard —
*any change to enrichment logic must land in both it and the ETL, or they
silently diverge*. The constants `_IV_WINDOW = 252` and `_IV_MIN_PERIODS = 60`
are duplicated in each file precisely because they must match, and an
anti-divergence test keeps the two chain builders byte-identical.

### Phases 4–5 — PostgreSQL and the backtester

`storage/` — a pooled, batched, NaN-scrubbing writer; the threaded per-symbol
ETL; the SQL schema. And `backtest/strangle.py`, a short-strangle strategy with
weekly delta-hedge adjustments: sell the ~20-delta call and put of the 30-day
expiry on the first trading day of each month, rebalance weekly, close before
expiry.

The backtester is the one part of the project aimed at *trading* rather than
*detection*, and it has been quiet since the project's focus shifted.

### Phases 6–7 — CLI and UI

Typer subcommands (`fetch-bhavcopy`, `etl`, `backtest`, …), progress bars, and a
Streamlit control panel that runs pipeline steps as detached subprocesses.

> **Security note that is enforced, not aspirational:** `mktmtm-ui` binds the
> tailnet IP by default and has **no authentication**. It must never bind
> `0.0.0.0`.

### The integrity work — where `main` earned its keep

Four commits at the end of `main` are not features at all. They are the record of
finding and fixing defects that had already corrupted data:

- **The IV integrity audit** — the raw/adjusted price-basis mix-up (~10% of IV
  rows) and the `0.0`/`500.0` sentinel returns.
- **A look-ahead leak** in `ml_dataset`, plus corporate-action corruption.
- **The cache offset map dropping 75 symbols** — the parquet category-ordering
  trap from [document 09 §2](09-the-pipeline.md).
- **A health-gate script**, added because both of those defects were silent.

Document 15 tells these stories properly. They are the reason this project's
documentation is so preoccupied with *silent* failure.

---

## Part II — the branch: four attempts at the actual question

54 commits, and the story of three abandonments.

### Attempt 1 — XGBoost next-day price direction (archived)

The project's first modelling approach: a supervised per-symbol classifier
predicting whether tomorrow's close exceeds today's, from 42 option-derived
features plus 9 missingness indicators. It was completed through a sealed test.

**Why it was abandoned.** Two reasons, and the second is the important one:

1. The Phase 7 audit found the edge thin and **substantially tied to
   *missingness*** — rows where the IV solver failed, which happens on illiquid
   or violently-moving names. The model was partly learning "this row is
   broken", which correlates with volatility.
2. **It answers the wrong question.** The project's goal is detecting
   institutional positioning, not predicting price.

The code is at the git tag `archive/xgboost-direction` and **must never be
merged** — it predates the open-interest-units fix, the GEX sign correction, and
all of `flow_features`, so merging would revert measured bug fixes. Its model
card survives at `docs/models/xgboost-direction-model-card.md`.

**Cost of the lesson:** an entire modelling stack. **Value:** it forced the
question "what are we actually trying to detect?", which everything since has
been an answer to.

### Attempt 2 — GEX dealer positioning

Documented in detail in [document 06 §2.2](06-how-derivatives-move-markets.md)
and [document 10 §5](10-the-features.md). The hypothesis was the textbook one:
dealers short gamma → amplified moves → higher next-day volatility.

The work that went in was substantial: vanna and charm added to the Greeks, the
Greeks baked into every chain element at ETL time, dealer-exposure aggregates,
the gamma-flip level, a hypothesis-testing harness with confound controls.

**Three independent failures**, and one correction along the way that is more
interesting than the result: the dealer *sign* was assumed from US convention and
turned out to be inverted for Indian single stocks. It was fixed by
**measuring** against participant data — desks are net short calls on 99.9% of
days — and the correction was verified to be a pure negation (`gex == −gex` on
all 212,441 rows, `|gex|` identical).

**What survived:** a within-symbol magnitude effect, and the observation that
`dist_to_flip` — proximity to the regime boundary — beats exposure size.

### Interlude — building the data foundations

Between attempts, several things were built that everything afterwards depends
on:

- **Participant-wise OI ingestion** — the only named daily data, and the
  instrument that settled both the dealer sign and the OI-units question.
- **The lot-size schedule** — 853 change-points, because lot sizes are revised.
- **The units fix** — `OPEN_INT` is in shares, not contracts. The reference file
  asserted the reverse until it was measured; the raw sum was off by ~2,158×.
- **Flow features** — outlier strikes and the futures basis, built to read the
  parquet cache directly so they needed no ETL re-run.
- **Buildup quadrants** — and the delta-adjustment discovery.
- **The full architecture documentation** (`docs/architecture/`).

### Attempt 3 — Stages 1 to 4

Four modelling stages against the market-wide label, covered in document 12.
Results: **null, negative, mixed, split.**

The most valuable output of this phase was not a model. It was a set of
*measurement habits*: the calendar baseline, the placebo, the leakage tripwire,
the partial-correlation rule, the moving-block bootstrap. Every one of them was
built because something looked good and turned out not to be.

### The reframe — the turning point

Stated by the project owner, and it changed everything:

> *"What I want is not to predict when the whales have entered to buy stocks by
> looking at the F&O that they purchase when they buy the stocks — not to
> accurately predict if they will enter at a particular time."*

Detection, with a short lead. Not forecasting.

In hindsight the pattern is obvious: Stages 1 and 4 asked forecasting questions
and returned nulls; Stage 3 asked a contemporaneous question and was the only one
with a promising shape. But it took four negative results to see it, which is
worth recording honestly rather than presenting the final design as if it had
been obvious.

### The label hunt

The reframe demanded a per-symbol label. Two candidates:

**NSE bulk and block deals** — named large trades. Built the fetcher, the parser,
the table. Then found: (a) history is bot-blocked, only the latest day is
served programmatically, and (b) more fundamentally, the 0.5%-of-listed-shares
threshold selects microcaps by construction — **0 of 158 bulk-deal rows on the
day measured were among our 217 F&O symbols.** Structurally unusable.

**SEBI quarterly shareholding** — worked. ~4,700 XBRL filings, cached gzipped,
giving per-symbol share counts for FII, DII, mutual funds and insurers.

Two silent bugs in parsing it were found and fixed (document 15).

### Attempt 4 — Stage 5, and the surviving result

Document 12. `basis_excess_mean` → mutual-fund accumulation, partial IC +0.1374,
18 of 19 quarters.

Then the robustness work: time halves, size halves, period bootstrap, the
counterparty check, the time gradient, and a re-test of the transfer hypothesis
(which was **not** supported — the FII/MF spread is weaker than mutual funds
alone).

### The nowcast and the engineering finish

Two frozen prediction records committed to git before the data to score them
exists. Then the operational work needed to actually run all of this
unattended:

- **A 17-stage pipeline runner** (`run-pipeline`) with per-stage timing and
  progress bars — plus three wrapper bugs it exposed on its first real run, one
  of which reported success in 58 seconds while leaving its table untouched.
- **The ETL made incremental**, after being OOM-killed twice on a 14 GiB host —
  silently, because SIGKILL cannot be caught.
- **A health gate** extended to validate the five newer tables.

---

## What each abandonment cost, and taught

| Abandoned | Effort | What it bought |
|---|---|---|
| **XGBoost price direction** | A full modelling stack | The question got corrected: *positioning*, not price |
| **GEX dealer positioning** | Greeks, aggregates, hypothesis harness | The confound-control methodology, and `dist_to_flip` |
| **Bulk/block deals** | A fetcher, parser, table, tests | Ruled out a plausible label conclusively, with a measured reason |
| **Stages 1–4** | Four models | The evaluation discipline that makes Stage 5 believable |

None of that was wasted. The measurement apparatus Stage 5 was judged by — the
partial control, the placebo, the period bootstrap, the counterparty check — is
entirely the product of four things that did not work.

---

## Where the project stands

| | |
|---|---|
| **Data** | 2021-08-02 → 2026-09-04, 217 symbols, all tables 1:1, health gate PASS |
| **Established, positive** | `basis_excess_mean` detects MF accumulation (+0.1374, 18/19) |
| **Established, negative** | FII accumulation is momentum; GEX does not predict volatility; no flow forecasting beats the calendar; no persistent regimes |
| **Committed forward predictions** | 2 nowcast records, scorable ~2026-11-14 |
| **Open** | Monthly MF portfolio scrape (the only true out-of-sample test); replacing `basis_excess_med` with `fut_basis_pct` in Stage 1; index-inclusion events as a better instrument for Stage 3 |

[← The models](12-the-models.md) · [Index](README.md) · Next: [Results →](14-results.md)
