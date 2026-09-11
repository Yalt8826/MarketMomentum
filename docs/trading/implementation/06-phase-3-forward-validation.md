# 06 — Phase 3: forward validation

> **Status: harness BUILT 2026-09-11; 0 of 6 eligible months recorded.**
> Results: [`../results/gate-3-forward-validation.md`](../results/gate-3-forward-validation.md).
> As built: [`10-phase-2-3-as-built.md`](10-phase-2-3-as-built.md#6b-the-scoring-harness--phase-32-and-33).
>
> One deviation from this specification, and it is the important one. §5's
> fourth criterion — "every record written before its outcome, verifiable by
> commit date" — is not a reporting line but the **binding constraint**: it is
> enforced in code by `committed_at()`, and a record failing it is excluded from
> the verdict however good its numbers are. Eight backtested screens returning a
> cumulative +94.55% were excluded on exactly that basis while the harness was
> being validated.

[← Phase 2](05-phase-2-serving.md) · [Index](README.md) · Next: [Build order →](07-build-order.md)

Everything in Phases 0–2 is **in-sample**: filters, thresholds and parameters all
selected on the same 2021–2026 window they are then measured on. Subset
stability is weaker evidence than an independent test, and this project has said
so about its own headline result.

The remedy already exists in the codebase and should be reused exactly.

---

## 1. The nowcast precedent

`ml/accumulation.write_nowcast_record` writes a dated markdown record to
`docs/results/` and **raises `NowcastRecordExists` rather than overwriting**.
Three properties make it real, and all three transfer:

| Property | Why |
|---|---|
| **Committed to git** | `models/` is gitignored — an artefact there proves nothing about *when* a prediction was made, and a forward prediction whose date cannot be established is not a forward prediction |
| **Refuses to overwrite** | A forecast made before an outcome, silently replaced by one made after it under the same filename, is worse than no forecast |
| **States its own limits on its face** | Including that a materially negative result counts as evidence against the method |

## 2. What to record, monthly

`docs/trading/results/screen-YYYY-MM.md`, written on entry day **before any
outcome exists**:

- Every candidate: symbol, strikes, expected credit, margin, filter values, rank
- Which were "taken" under the stated risk limits
- The filter rule and parameter set in force, by name and commit
- The expected outcome from Phase 1 calibration
- The date the record becomes scoreable

Machine-readable detail goes to `models/` as usual; the markdown record is the
dated commitment.

## 3. Scoring

At each expiry, score the prior month's record: realised net return on margin
per trade, versus the Phase 1 expectation.

**Report the running record including losing months.** A tool that publishes only
its good months is not being validated, it is being marketed. This is the same
standard the research branch applied when it reported that four of eighteen
Stage 1 combinations were significantly *negative*.

## 4. How long before this means anything

**Minimum six monthly cycles before any real capital**, and that is a floor, not
a target:

- One profitable month is one observation.
- The strategy wins most months by construction — premium selling has a high win
  rate and a fat left tail — so six wins are weak evidence and one large loss is
  strong evidence.
- The 2021–2026 sample contained no severe crash. The forward period may not
  either, in which case it confirms nothing about the risk that actually matters.

> State plainly in the write-up: **a run of profitable months does not establish
> that the tail risk has been survived.** It establishes that the tail has not
> yet arrived.

## 5. Gate 3

| Criterion | |
|---|---|
| ≥ 6 recorded months, none omitted | required |
| Realised net return within the Phase 1 confidence interval | required |
| No month exceeding the modelled max loss | required |
| Every record written before its outcome, verifiable by commit date | required |

Failing Gate 3 does not necessarily kill the strategy — but it does mean the
Phase 1 calibration was optimistic, and the write-up must say by how much.

[← Phase 2](05-phase-2-serving.md) · [Index](README.md) · Next: [Build order →](07-build-order.md)
