# 15 — Bugs We Found, and What They Taught

[← Results](14-results.md) · [Index](README.md) · Next: [Running it →](16-running-it.md)

This may be the most useful document in the folder, and it is certainly the most
transferable.

Nine defects are recorded here. **Every single one was silent** — none raised an
exception, none made a stage fail, several made a stage report success. In a data
pipeline, the bug that crashes is the easy one. The bug that quietly returns the
wrong number is the one that ends up in a report.

---

## The pattern

```mermaid
flowchart LR
    A["A wrong assumption<br/>about the data"] --> B["Code that runs<br/>perfectly"]
    B --> C["Output that looks<br/>entirely plausible"]
    C --> D["A result that is<br/>confidently wrong"]
    D -.->|"found only by<br/>measuring something<br/>you didn't have to"| E["Fix"]

    style D fill:#4a1e1e,stroke:#ff6b6b,color:#fff
    style E fill:#1a3a1a,stroke:#4caf50,color:#fff
```

---

## 1. Two price series, mixed up

**What happened.** Yahoo returns a raw price and a split-adjusted price. Strikes
and settlement prices from NSE are **unadjusted**. Using the *adjusted* price to
choose which strike is at-the-money selects entirely the wrong part of the chain
after any split.

**Damage.** Roughly **10% of implied-volatility rows** corrupted.

**Why it was silent.** An option was still selected. An IV was still computed.
The number was just wrong.

**Fix.** Two explicit columns with documented purposes: `underlying_price` (raw —
strikes, IV, Greeks, basis) and `underlying_price_adj` (adjusted — returns,
momentum, labels).

> **Lesson:** when a source gives you two versions of the same quantity, they
> exist because they are *for different things*. Name both, document both, and
> never let one silently substitute for the other.

---

## 2. `0.0` and `500.0` — garbage wearing a number's clothes

**What happened.** When the bisection solver failed to converge — a degenerate
input, a price outside the possible range — it returned `0.0` or `500.0` instead
of nothing.

**Damage.** Those fake values propagated into every downstream volatility
feature, and into any average computed over them.

**Why it was silent.** `0.0` is a perfectly valid float. Nothing downstream had
any way to know it meant "I failed".

**Fix.** `implied_volatility_bisection` returns `None`, never a sentinel. `None`
becomes SQL `NULL`, which propagates as missing rather than as a plausible
number.

> **Lesson:** a failure must be representable as *absent*, not as a value. Any
> sentinel value will eventually be averaged with real ones.

---

## 3. Open interest is in shares, not contracts

**What happened.** The project's own reference file asserted that lot size
*multiplies* to convert open interest to contracts. It divides.

**Damage.** Every notional and exposure calculation off by the lot size.

**Why it was silent.** The numbers were large, plausible, and consistently wrong
in the same direction.

**How it was caught.** By measuring rather than reading: `SUM(OPEN_INT / lot)`
across all stock futures matches NSE's participant-wise contract total **to
within 0.02%**, where the raw sum is off by a factor of ~2,158.

> **Lesson:** units are an empirical question. When two independent sources
> report the same quantity, reconcile them — do not read the documentation and
> assume.

---

## 4. The parquet cache dropped 75 symbols

**What happened.** The cache is one sorted frame plus a `{symbol: (start, stop)}`
offset map, sliced with a binary search. But parquet stores text as
dictionary-encoded categories, each row group carrying its own dictionary. Read
back across 1,254 row groups, the categories follow *first-appearance* order —
not alphabetical. Sorting then orders by category **code**, leaving the column
unsorted for the binary search.

**Damage.** **75 of 222 symbols dropped entirely.** TATASTEEL, TATAPOWER and
TATAMOTORS received spans containing *other symbols' rows*.

**Why it was silent.** `searchsorted` on an unsorted array does not error. It
returns a span. The span is simply wrong.

**Fix.** Cast to `str` before sorting, plus an assertion that makes any
regression loud.

> **Lesson:** an optimisation that relies on an invariant must *assert* that
> invariant, not assume it. Especially when the storage layer is allowed to
> reorder things behind you.

---

## 5. The row groups are not in chronological order

**What happened.** The cache is written one row group per trade date, but the
frame is sorted by symbol — so the row groups come out with **~293 backward steps
across five years.** The first buildup-quadrant implementation streamed them in
file order and treated "the previous row group" as yesterday.

**Damage.** Every day was joined against an arbitrary other day. Price changes
spanning *months* were used to classify a single day's buildup.

**Why it was silent.** This is the purest example in the whole project. **Every
merge succeeded** — the same contract exists on both days, so the join found a
match and returned a number. It surfaced only as a coverage discontinuity that
initially looked like the 2024 file-format change, and was not.

**Fix.** `_ordered_row_groups()` — sort the row groups by date before streaming.

> **Lesson:** "the merge succeeded" is not evidence the merge was correct. When
> the join key exists on both sides regardless of whether the pairing is
> meaningful, you get a plausible answer to the wrong question, every time.

---

## 6. The raw premium quadrant was 36% wrong

**What happened.** Classifying a buildup as buyer- or writer-driven from the raw
change in the option's price. But an option's price moves with the underlying,
through delta, whether or not anyone traded.

**Damage.** Measured over 163,383 traded contracts: **35.8% of contracts
classified differently** from the delta-adjusted version.

**Why it was silent — and this is the subtle part.** The *aggregate* barely
moved. Writer share went from 63.4% to 61.0%. **A validation that checked only
summary statistics would have passed a feature that was wrong a third of the
time.**

**Fix.** Classify on the residual:
`(mark − mark_prev) − delta_prev × (spot − spot_prev)`.

> **Lesson:** aggregate agreement does not imply per-row agreement. If a feature
> makes a decision per row, validate it per row.

---

## 7 and 8. Two shareholding bugs, in the same parser

These two are worth reading together, because the second was found only because
the first was fixed properly.

### 7. The pre-2023 taxonomy uses different member names

SEBI changed its XBRL taxonomy in 2023. Pre-2023 filings name the same categories
differently, and the parser did not know the old names.

**Damage.** 899 NULL FII rows; 74.6% of mutual-fund rows NULL.

**Why it was silent.** NULL is a legitimate value. The rows simply dropped out of
every evaluation, silently narrowing the sample.

### 8. Dead categories held as explicit zeros

The nastier one. `ForeignInstitutionsMember` is the *legacy* FII bucket, emptied
when SEBI folded FII into FPI in 2014 — and it still sits in the same filing,
carrying an explicit `0`, right next to
`InstitutionsForeignPortfolioInvestorMember` carrying the real holding.

The alias resolver picked the first alias **present**. Present it was; zero it
was.

**Damage.** `fii_shares` read **zero for 100% of 2021 filings and 50% of 2022**.
And through the "domestic = institutions − foreign" fallback, DII was inflated by
exactly the same amount. **Both target variables were wrong, and nothing
failed.**

**Fix.** `_pick` prefers the first **non-zero** alias.

**A note on the fix itself.** The first attempt was to *reorder* the alias list
so the FPI member came first. An existing unit test caught that as an
over-correction — it would break other filings where the ordering is genuinely
meaningful. The non-zero rule is the correct general fix, and the test that
caught the wrong one had been written earlier for a different reason.

> **Lesson:** a zero in a regulatory filing can mean "genuinely zero" or "this
> category no longer exists." Distinguish them. And sanity-check any new column
> for a discontinuity at a known taxonomy boundary.

---

## 9. Three pipeline wrapper bugs — one of which reported success

The 17-stage pipeline runner duplicates what each CLI command does, and that
duplication is a bug surface. Its first real overnight run exposed three:

| Bug | Behaviour |
|---|---|
| `preparse` pointed at the wrong directory | **Crashed loudly** — the good kind |
| `fetch-bhavcopy` missing `max_workers` | Crashed |
| **`fetch-participant-oi` fetched but never loaded** | **Reported success in 58 seconds while the table stayed exactly as stale as before** |

That third one is the whole lesson. The stage downloaded the CSVs correctly. It
just never called the function that puts them in the database. Green tick, zero
exit code, no data.

**Fix.** Contract tests in `tests/unit/test_pipeline_runner.py` pinning each
wrapper against what the CLI command actually does. Any new stage wrapper gets
one.

> **Lesson:** "the stage passed" means the code did not raise. It does not mean
> the stage did its job. For any stage whose job is to change state, assert the
> state changed.

---

## 10. The ETL was killed twice, silently

**What happened.** A full ETL rebuild loads the whole ~60M-row cache — about 9 GB
resident — on a 14 GiB host. The Linux OOM killer terminated it at 21:39:16, and
again at 00:00:21 the next night.

**Why it was silent.** SIGKILL cannot be caught. There is no `FAILED` line, no
traceback, no exit handler. **The log just stops mid-line.**

**Fix.** The ETL is now incremental by default: 10.4M rows instead of 60.7M, 2.5
GB instead of 9, ~1 hour instead of 10–12.

Two details in that fix are themselves lessons:

- **The write cutoff is per symbol.** After the OOM, three symbols sat at 09-04
  while 214 stopped at 08-28. A global frontier would have frozen that partial
  state permanently.
- **The recomputed overlap is not rewritten.** Existing rows hold IV percentile
  and rank computed with a complete 252-day history; a trailing window cannot
  reproduce that, so rewriting would replace correct values with worse ones.

> **Lesson:** when a process can be killed by something outside itself, the log
> ending is not evidence it finished. Check the data, not the log.

---

## 11. Not a bug — a statistical artefact that produced the project's biggest number

Worth including, because it is the same failure mode in a different medium.

An intermediate version of Stage 4 used causal within-symbol z-scoring to remove
symbol fixed effects — a completely standard technique. It reported
self-persistence of **+0.2979** with smooth decay across lags. By far the largest
number the project had produced.

It was entirely an artefact. The causal z subtracts a **slowly updating**
expanding mean, so consecutive z-scores share a baseline error and *look*
strongly persistent on a series whose raw autocorrelation is **−0.009**.

> **The standard repair for symbol fixed effects introduced a worse confound than
> the one it removed.**

It is now a standing rule in `CLAUDE.md`: never use causal within-symbol
z-scoring to measure persistence here. (It remains correct for its original
purpose — comparability across symbols.)

---

## The lessons, collected

1. **A silent wrong answer is worse than a crash.** Design for loud failure.
2. **`NULL` must be representable.** Never encode failure as a value.
3. **Units are empirical.** Reconcile two independent sources; do not trust
   documentation.
4. **Optimisations must assert their invariants.**
5. **A successful join is not a correct join.**
6. **Aggregate agreement does not imply per-row agreement.**
7. **A zero can mean "empty category", not "nothing held".**
8. **"The stage passed" means the code did not raise, nothing more.**
9. **When a process can be killed externally, verify the data, not the log.**
10. **A standard technique can introduce a worse confound than it removes.**
11. **Check whether your labels are correlated by construction before citing
    their opposition as validation.**

And the meta-lesson, which is why this project has a health gate, a placebo, a
tripwire, and a partial-correlation rule:

> **Every one of these was found by measuring something nobody required us to
> measure.**

[← Results](14-results.md) · [Index](README.md) · Next: [Running it →](16-running-it.md)
