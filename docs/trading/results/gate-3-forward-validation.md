# Gate 3 — forward validation

[Results index](README.md) · [Gate 0](gate-0-base-strategy.md) · [Gate 1](gate-1-feature-filters.md) · [Stop loss](stop-loss.md)

**Status: 0 of 6 scored; 1 eligible record pending.** This page will stay
honest or stay empty.

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
| 2026-10 | 2026-10-01 | 7 | *pending — expiry 2026-10-27* | — | — |

**One eligible record, not yet scoreable.** Its exit day is **2026-10-26**, so
it contributes a number from roughly 27 October. A second record is excluded;
see §4.

### The October record

| | |
|---|---|
| Entry | **2026-10-01** — the month's first trading day |
| Generated | 2026-10-03, with **0 trading sessions closed in between** |
| Committed | **2026-10-03 14:15:41 +0530** — 24 days before the outcome |
| Positions | 7 — POLICYBZR, IDEA, KALYANKJIL, ANGELONE, PATANJALI, TATAELXSI, HINDZINC |
| Margin | ₹492,066 (49.2% of capital) |
| Net credit | ₹117,269 |
| Expiry / exit | 2026-10-27 / 2026-10-26 |
| F&O bans caught | **BANDHANBNK, SAIL** |

Conditions 1, 2 and 4 are satisfied; condition 3 awaits the expiry.

**On the generation date.** The record was written two days after its entry
date, and says so on its face rather than claiming otherwise. That is safe here
for a reason stated in the record itself: **no trading session closed in
between** — 2026-10-02 was Gandhi Jayanti and 10-03 a Saturday — so the
selection used no information a trader lacked at the 10-01 close. The backtest's
own convention enters at the first trading day's *settlement*, which bhavcopy
publishes only after the close, so every screen is necessarily generated after
its own entry date. `write_screen_record` now reports `sessions_after_entry`
from the data, and a non-zero count prints as hindsight-contaminated rather
than being argued away.

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

A +94.55% cumulative return the tool refuses to count is the behaviour you want.
`committed_at()` reads the git commit that introduced the record and compares it
to the positions' expiry; an **uncommitted record cannot be verified and is
excluded however good its numbers look**.

> ### ⚠️ A correction — this was not a demonstration of the check
>
> An earlier version of this page called the exclusion above *"a better
> demonstration of the check than any passing test."* **That was wrong.**
>
> `committed_at` was broken from the day it was written: it passed a
> repo-root-relative pathspec while setting `cwd` to the file's own directory,
> so git matched nothing and it returned `None` for **every record ever
> written**. `provenance_verified` was therefore false universally and **Gate 3
> was unpassable.** The eight backdated screens were refused because
> *everything* was refused.
>
> The failure was invisible precisely because it fails **closed**: the output
> was right, the verdict was never wrong, and "unverifiable" is an answer the
> function is allowed to give. It surfaced in October 2026 only when a record
> that genuinely *should* have passed was refused.
>
> **A check that fails closed cannot be validated by anything it rejects.** The
> fix and its tests are in
> [`bugs-and-fixes.md` Defect 8](bugs-and-fixes.md); the provenance check now
> works and is exercised against a real git repository in both directions.

## 4. The excluded record

[`screens/screen-2026-09-04.md`](screens/screen-2026-09-04.md) — 6 positions,
₹493,581 margin, ₹100,642 net credit, expiry 2026-09-29.

It is genuinely forward: committed 2026-09-11, outcome unknown until 2026-09-29.
But it is **excluded** on two counts. It entered on 2026-09-04 when the first
trading day of that month was **2026-09-01** — and it contained **INOXWIND,
which was in an F&O ban on its entry date** and could not legally have been
opened ([Defect 7](bugs-and-fixes.md)). The live screen now refuses banned
symbols.

**Its outcome is now known: portfolio +0.30%, worst trade −61.69%, net ₹1,414
across 6 of 6 positions scored.** Against a backtested +4.93% mean monthly
portfolio return. It stays excluded and contributes nothing to the verdict, but
it is the only out-of-sample number the strategy has, and it is not
encouraging — nor is it, at one month and with a position that could not have
been opened, evidence of anything much either way.

That was an unforced error — the screen was simply run on the latest available
data — and the fix is a discipline, not code: **run the screen on the first
trading day of the month**. The tool now warns when it is not, and the record
carries the warning on its face.

> No 2026-09-01 record was manufactured to replace it. Generating one on
> 2026-09-11 would claim fills at prices ten days stale — forward with respect
> to the outcome, backward with respect to the entry.

That reasoning turns on **how many sessions closed between the entry and the
writing**, not on the calendar gap alone, and the October record makes the
distinction concrete: written two days after its entry date with **zero**
intervening sessions, where September's would have had seven. The screen now
computes and publishes that count, so the judgement is auditable from the record
rather than argued in prose.

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
| 1 | ≥ 6 eligible months, none omitted | ❌ 0 scored, 1 pending |
| 2 | Realised return consistent with Gate 0 | pending |
| 3 | No month beyond twice Gate 0's worst (−16.12%) | pending |
| 4 | Every record committed before its outcome | pending |

**Verdict: INCOMPLETE.** The first record is in place (2026-10-01) and scores
from ~27 October. Expected to become answerable around **March 2027** — six
monthly cycles from October 2026.

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
