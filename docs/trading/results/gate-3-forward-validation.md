# Gate 3 — forward validation

[Results index](README.md) · [Gate 0](gate-0-base-strategy.md) · [Gate 1](gate-1-feature-filters.md) · [Stop loss](stop-loss.md)

**Status: 0 of 6 eligible months.** This page will stay honest or stay empty.

Harness built 2026-09-11 (`serving/scoring.py`, `mktmtm score-screens`).
Specification: [`../implementation/06-phase-3-forward-validation.md`](../implementation/06-phase-3-forward-validation.md).

---

## 1. Why this gate exists

Everything in Gates 0 and 1 is **in-sample**. The strategy, its cost model, its
margin assumption and its parameters were all measured on the same 2021–2026
window. Subset stability is weaker evidence than an independent test, and this
project has said exactly that about its own research result.

A screen record is the remedy: a dated, committed selection written **before its
outcome exists**. Six of them, scored without omission, is the first genuinely
out-of-sample evidence this strategy will have.

## 2. The running record

| Month | Entry | Positions | Portfolio return | Worst trade | Cumulative |
|---|---|---|---|---|---|
| — | — | — | — | — | — |

**Nothing eligible yet.** One record exists and is excluded; see §4.

## 3. What makes a record eligible

Four conditions, all required. Three are cheap; the fourth is the one that
matters.

| # | Condition | Why |
|---|---|---|
| 1 | Entered on the **month's first trading day** | The backtest always does. Any other entry is a shorter-dated position at different strikes, drawn from a distribution nobody measured. |
| 2 | **Committed to git before its outcome existed** | ⭐ See below |
| 3 | Something actually scored | An open month has no outcome to contribute |
| 4 | Losing months included | Non-negotiable — §5 |

### Condition 2 is the whole gate

Every other criterion can be satisfied by a record written *after* its outcome
was already known. That is a backtest wearing a forward test's filename, and
producing one is trivially easy — as this project demonstrated on itself.

While building the harness, eight screens were generated for the months
February–September 2026 and scored. They looked excellent:

| | |
|---|---|
| Mean monthly portfolio return | **+11.80%** |
| Positive months | 5 of 8 |
| Cumulative | **+94.55%** |

**None of it counts.** Those records were written on 2026-09-11 for months whose
outcomes were already in the database. The harness excludes all eight, and
reports why:

```
GATE 3: INCOMPLETE — 0 of 6 months recorded
  0 eligible month(s)  (8 excluded)
    8 — not committed before its outcome
```

A +94.55% cumulative return that the tool itself refuses to count is a better
demonstration of the check than any passing test. `committed_at()` reads the
git commit that introduced the record and compares it to the positions' expiry;
an **uncommitted record cannot be verified and is excluded however good its
numbers look**.

## 4. The one existing record

[`screens/screen-2026-09-04.md`](screens/screen-2026-09-04.md) — 6 positions,
₹493,581 margin, ₹100,642 net credit, expiry 2026-09-29.

It is genuinely forward: committed 2026-09-11, outcome unknown until 2026-09-29.
But it is **excluded**, because it entered on 2026-09-04 and the first trading
day of that month was **2026-09-01**.

That was an unforced error — the screen was simply run on the latest available
data — and the fix is a discipline, not code: **run the screen on the first
trading day of the month**. The tool now warns when it is not, and the record
carries the warning on its face.

> No 2026-09-01 record was manufactured to replace it. Generating one on
> 2026-09-11 would claim fills at prices ten days stale — forward with respect
> to the outcome, backward with respect to the entry. The first eligible record
> will be **October 2026's first trading day**.

## 5. The standard this page is held to

**Every scored month appears here, including the losses.** No "since inception
excluding the outlier", no quiet omission of a bad month.

This is the same standard the research branch applied when it reported that four
of eighteen Stage 1 combinations were significantly *negative*, and when it
published three null results at the same resolution as its one positive.

`running_record()` performs no filtering. The only exclusions are the
eligibility conditions in §3, and each one is reported with its reason.

## 6. Gate 3 criteria

| # | Criterion | Status |
|---|---|---|
| 1 | ≥ 6 eligible months, none omitted | ❌ 0 of 6 |
| 2 | Realised return consistent with Gate 0 | pending |
| 3 | No month beyond twice Gate 0's worst (−16.12%) | pending |
| 4 | Every record committed before its outcome | pending |

**Verdict: INCOMPLETE.** Expected to become answerable around **March 2027** —
six monthly cycles from October 2026.

## 7. What six good months would and would not establish

Gate 0 measured a **67.1% win rate with a fat left tail**. Premium selling wins
most months by construction. So six profitable months is roughly what you would
expect from a strategy with no edge at all *and* from one with a real edge — the
difference between them lives in the tail.

> **A run of profitable months does not establish that the tail risk has been
> survived. It establishes that the tail has not yet arrived.**

That sentence is returned by `gate_3_report()` on every call, so it cannot be
omitted from a write-up by accident.

The 2021–2026 sample contained no severe crash. If the forward period contains
none either, Gate 3 passing confirms the strategy behaves as measured in ordinary
conditions — and says nothing whatever about the −37.27% drawdown, which remains
unmitigated and is still the binding constraint.

## 8. Running it

```bash
# On the FIRST TRADING DAY of each month:
uv run mktmtm run-pipeline --mode no-etl
uv run mktmtm screen --out docs/trading/results/screens/screen-$(date +%F).md
git add docs/trading/results/screens/ && git commit -m "screen: $(date +%F)"
#                                       ^ the commit IS the evidence

# Any time after an expiry:
uv run mktmtm score-screens
```

The commit is not bookkeeping. It is the only external evidence of when the
prediction existed, and an uncommitted record is worth nothing to this gate.
