# Bugs found in Phase 0 — and the fixes applied

[Results index](README.md) · [Gate 0 verdict](gate-0-base-strategy.md)

**Eight** defects were found across Phases 0 to 3 and the October 2026
pre-submission audit. **All eight were silent**: none raised an exception, none
failed a run, and three had been dropping or distorting data since before this
branch existed.

Defects 1–4 came out of making the backtester's P&L economically real. Defects
5 and 6 surfaced later, during the stop-loss measurement, and are the more
instructive pair — both would have produced a *published number* rather than a
crash. Defects **7 and 8** came out of the
[pre-submission audit](../../implementation/pre-submission-audit.md): one let
an un-tradeable position into a committed record, and the other had quietly
disabled Gate 3 entirely.

They are collected here because the *method* that found them transfers, and
because three of the four are visible only from the tail of a distribution
nobody would have looked at if the headline number had seemed reasonable.

---

## How they were found

One number did not make sense.

> Gate 0's first run reported a worst single trade of **−1,677%** — a loss of
> **16× the margin posted**.

A short strangle on a 12%-margin position cannot lose 16× its margin; that would
require the underlying to move by roughly twice its own price. The mean return
(+4.79%), the win rate (68.3%), the Sharpe (2.22) and the year-by-year table all
looked entirely plausible. Only the tail was absurd.

Chasing that one number found three defects. A fourth had already been found by
inspection while wiring the cost model in.

**The lesson worth carrying:** the headline statistics were not the ones that
revealed the corruption. A backtest that reports only a mean, a win rate and a
Sharpe has no way of telling you it is wrong.

---

## Defect 1 — P&L dropped the replacement leg's credit

**Found by** reading the accounting while wiring costs in, not by a failing test.

### What was wrong

```python
pnl_points = initial_entry_credit - final_exit_cost + total_adjustment_pnl
```

`initial_entry_credit` is the **original** entry premiums, recorded once and
never updated. But `call_exit_price` is the exit price of the **current** leg,
and `current_call_sold_price` *is* updated when the weekly hedge sells a
replacement.

So the formula credits the original sale, debits the replacement's buyback, and
the replacement's own sale credit is never counted anywhere.

### Worked example

Sell CE@50 / PE@45. The hedge buys the call back at 30 and re-sells at 58. Exit
CE@20 / PE@25.

| Leg | P&L |
|---|---|
| CE leg 1: sold 50, bought back 30 | +20 |
| CE leg 2: sold 58, bought back 20 | **+38 ← never counted** |
| PE leg: sold 45, bought back 25 | +20 |
| **Truth** | **78** |
| Old formula | 70 |

The error is exactly `(final sold price − original sold price)` per side.

### Why the direction matters

The hedge re-sells *near the price of the losing leg*, so replacements are
systematically sold **higher** than the original. The bug therefore
**understated** P&L on every month that adjusted — conservative, but wrong, and
Gate 0's entire verdict is that number.

### The fix

Sum leg by leg: every leg ever sold, minus what it cost to close.

```python
pnl_points = (
    total_adjustment_pnl
    + (current_call_sold_price - call_exit_price)
    + (current_put_sold_price - put_exit_price)
)
```

**Pinned by** `test_pnl_counts_the_replacement_legs_credit`, which checks
`strangle.py` against `economics.price_strangle_trade` — the two reach the same
figure by different routes. Two independent implementations agreeing is a
stronger statement than either matching a hand-typed constant.

---

## Defect 2 — untraded strikes were priced anyway

### What was wrong

NSE assigns a settlement price to **every listed strike**, whether or not it
traded. `find_option_near_price` — which the weekly hedge uses to re-sell "near
the price of the other leg" — accepted any contract with a finite positive
settle.

So the hedge could sell a contract that had never traded, at a price nobody had
ever transacted.

### Extent

**32 of 7,683 trades (0.4%), for −₹5.7M.** Removing them moved the mean from
+4.79% to +5.75%.

### The fix

Selection requires evidence of a real transaction:

```python
def _is_tradeable(opt: dict[str, Any]) -> bool:
    volume = opt.get("volume")
    return isinstance(volume, (int, float)) and math.isfinite(volume) and volume > 0
```

Applied in both `find_option_near_price` and `find_options_near_delta`, behind
`require_liquidity=True`. The old behaviour stays reachable as
`require_liquidity=False` so the effect remains measurable rather than merely
asserted.

**Open interest is deliberately not accepted as evidence** — a position can
exist without a price being struck today.

---

## Defect 3 — degenerate chains ⭐

The most instructive of the four.

### What was wrong

On **NMDC, 2022-10-25**, every one of 206 chain elements carried an identical
settle of **₹130.75** against a spot of **₹130.71**. The underlying's price had
been written into every option's premium. Every delta was `0.0`, because the IV
solver cannot converge on prices that make no sense.

```
type  strike  settle   delta  volume       oi
  CE    72.5  130.75     0.0       0        0
  CE   100.0  130.75     0.0       0        0
  CE   135.0  130.75     0.0    2492  2113850   ← heavily traded, still corrupt
  PE   117.5  130.75     0.0      10   150750
  ...all 206 rows identical...
```

### Extent

**47 of 213,379 symbol-days (0.022%)** — and the distribution is the diagnosis:

| | |
|---|---|
| On an **expiry date** | **39 of 47 (83%)** |
| Scattered singletons | 8 (NMDC 2022-10-25, RELIANCE 2023-07-19, ITC 2025-01-03, …) |

On expiry day the expiring series settles against the underlying, and for these
rows that settlement value has been written into every contract's premium —
which is exactly the shape observed: every settle equal to spot, every delta
0.0. The eight non-expiry cases are unexplained.

**Consequence for the strategy:** the exit day is the last trading day *before*
expiry, so the strategy does not normally touch an expiry-day chain. The
exposure comes from hedge dates and from months whose exit lands on one — which
is why the guard is applied at all three points rather than only at exit.

### Why the obvious filters fail

This is the part worth remembering.

| Filter | Why it misses |
|---|---|
| **Volume > 0** | Affected strikes include one with **2,492 lots traded** |
| **No-arbitrage bound** (a call cannot be worth more than the stock) | The corrupt value sits at **99.97% of spot** — inside any tolerance loose enough to admit a legitimate deep-ITM call |

Both are correct filters. Neither sees this. What identifies the defect is the
**degeneracy itself**, so that is what the check tests.

### And it must be per expiry

**IOC, 2022-06-29** is the same defect in partial form: **146 of 436 rows** carry
the spot price, and they are exactly the contracts of the expiring 30-Jun series,
while July and August price normally.

> A whole-chain test sees **193 distinct settles** and passes it.

That single trade was the −1,677%.

### The fix

```python
def chain_is_degenerate(option_chain, expiry_date=None) -> bool:
    rows = [o for o in option_chain if o.get("expiry") == target] if expiry_date else option_chain
    if len(rows) < 10:
        return False          # too thin to distinguish corruption from a sparse listing
    settles = {o.get("settle") for o in rows if isinstance(o.get("settle"), (int, float))}
    return len(settles) < 2
```

Checked at **entry**, at **each hedge date**, and at **exit**. A month touching
such an expiry is skipped with a typed reason rather than priced.

A secondary no-arbitrage guard (`_is_plausible_price`) was kept as well — it
catches the put side of this defect directly, and other corruption shapes — but
it is not sufficient on its own, for the reason above.

---

## Defect 4 — 3,308 rows never persisted, in every run

### What was wrong

The original table declared:

```sql
entry_date DATE NOT NULL
```

A month with no trading days has no entry date. Every such row was **rejected on
insert** — and `insert_backtest_result` logs the failure and continues:

```python
except Exception:  # noqa: BLE001 - legacy behaviour: log, keep going
    logger.error("Failed to insert/update result for %s %s-%s: %s", ...)
```

So the run reported success while **3,308 months vanished**.

### Why the schema rewrite did not fix it

The Phase 0 schema declared the column nullable — but
`CREATE TABLE IF NOT EXISTS` is a **no-op on an existing table**, so the original
constraint survived untouched. The migration has to drop it explicitly:

```sql
ALTER TABLE backtest_results ALTER COLUMN entry_date DROP NOT NULL;
```

### What it cost

The **skip taxonomy** was computed over a denominator missing its largest
category. It reported 74.0% of months traded; the true figure is **51.0%**.

A Gate 0 disclosure was wrong by 23 percentage points, and nothing indicated it.

---

## Defect 5 — the variant was unset on skipped months

**Found by** reading a row count that could not be true.

### What was wrong

`result["variant"]` was set inside `price_result()` — which a **skipped month
never reaches**, because a month with no trading days or no exit price returns
before pricing.

So every variant run wrote its skipped months under the *default* variant. Three
stop-loss runs silently merged their skip taxonomies into the base run's.

### How it surfaced

A count that was arithmetically impossible:

```
variant    rows   priced
stop1.5    7729     7729     ← every row priced, no skips at all
base      15096     7706
```

A backtest over 15,096 candidate symbol-months cannot produce zero skips. The
base run showed 7,390 of them; `stop1.5` showed none, because they had all been
written as `base`.

### Why it mattered

The skip taxonomy is a **Gate 0 disclosure**. Had a stop variant been promoted,
its "51% of months trade" figure would have been silently wrong — and this is
the second time in this branch that a skip-handling bug corrupted exactly that
statistic (see Defect 4).

### The fix

Set `variant` at function entry, alongside `symbol`/`year`/`month`, rather than
during pricing. Regression test included.

> **Lesson:** metadata that identifies a row must be attached where the row is
> *created*, not where it is *completed*. Any early return is a path that skips
> the late assignment.

---

## Defect 6 — an unequal-sample comparison that looked like a finding

Not a code defect. A **measurement** defect, and the most dangerous kind here:
it produced a plausible, dramatic, publishable number.

### What it looked like

The first stop-loss comparison showed June 2023 at **−56.79%** under a 3× stop
against **+4.76%** for base. A stop that turns a positive month into a −57%
catastrophe is a striking result, and it is the sort of thing that gets written
up.

### What it was

| 2023-06 | base | stop3 |
|---|---|---|
| Rows | 222 | 8 |
| **Priced trades** | **2** | **8** |

The stop provides an exit *before* the scheduled exit date, so months whose
exit-day lookup fails under base — the 191 "Missing Exit Price" skips — get
priced under the stop variants. **The comparison was measuring a different set
of trades, not a different strategy.**

On the common set of 7,706 trades the effect vanishes: worst month −16.12%
against −16.26%.

### This is a repeat

The research branch hit the identical trap in Stage 1, scoring a calendar
baseline on 1,004 rows against flow models' 556. Same fix: restrict to the
common set.

> **Lesson:** when a change appears to help or harm dramatically, check whether
> it changed *which observations exist* before believing it changed their
> outcomes. Any intervention that alters eligibility — a stop, a filter, a
> different exit rule — can do this, and the resulting number is plausible in
> both directions.

---

---

## Defect 7 — the screen selected a symbol in an F&O ban

**Found by** reading NSE's ban list for 2026-09-04 while surveying the archive
for unrelated reasons, and recognising a name in it.

### What was wrong

```
Securities in Ban For Trade Date 04-SEP-2026:
1,INOXWIND   2,KAYNES   3,LICHSGFIN   4,SAIL
```

**INOXWIND was position #2 in the committed September screen record** —
₹12,543 of credit against ₹57,155 of margin.

When a symbol's market-wide open interest crosses 95% of its position limit, NSE
places it in a **ban period**: existing positions may only be *reduced*, and no
new ones may be created. Selling a fresh strangle creates new positions, so the
position could not have been opened at all. Nothing in the project had ever read
this file.

### Extent

Across all 62 month-entry dates, with 62 of 62 ban files retrieved:

| | |
|---|---|
| Backtest trades entered on a banned symbol | **17 of 10,411 (0.16%)** |
| Mean banned symbols per month-entry day | 0.6 |
| Names affected | CANBK, PNB, BHEL, AMBUJACEM, HINDCOPPER ×2, SAIL, BIOCON, IDEA, INDUSTOWER, GRANULES, BANDHANBNK, RBLBANK ×3, MANAPPURAM |

**Negligible in the backtest, material in the live tool** — and the reason is
the interesting part:

> The screen hit one on its **first ever run** — 1 of 6 positions — because bans
> land on high-IV, high-activity names, and that is exactly what the screen
> orders by. **The selection mechanism and the defect are correlated**, which is
> how a 0.16% population rate becomes a 1-in-6 rate in production.

A population-level error rate is therefore not a safe estimate of a *selected*
sample's error rate, whenever the selection criterion and the error share a
cause.

### The fix, and the part that is the actual point

`mktmtm.data.secban` fetches and parses the list. The URL sits under
`archives/fo/sec_ban/`, **not** the `content/` prefix that serves participant OI
and bhavcopy; the `content/` paths 404.

The substance is the **failure semantics**. NSE publishes this file on every
trading day, writing `NIL` on the title line when nothing is banned, so three
cases must stay distinct:

```
200 + symbols  ->  those symbols are banned
200 + NIL      ->  authoritatively zero bans
anything else  ->  UNKNOWN — never "zero"
```

Returning an empty set for the third case would read exactly like a clean day —
the silent-omission failure this project has already suffered twice. So
`fetch_secban` **raises** on any non-200 including the holiday 404,
`run_screen` degrades to `secban_checked=False` rather than failing, and the
record **states on its face that bans went unverified**. The parser also
rejects a file served for the wrong date rather than applying another day's
bans.

Verified end to end on the date of the defect: INOXWIND, LICHSGFIN and SAIL
refused as `in F&O ban on 2026-09-04`, with the freed margin admitting
GODREJPROP and VEDL.

> **Lesson:** a population error rate does not transfer to a selected sample
> when the selection and the error share a cause. And "legally tradeable" is a
> gate distinct from "priceable" — the skip taxonomy had covered the second and
> never considered the first.

---

## Defect 8 — Gate 3's provenance check had never worked ⭐

The most consequential defect in the branch: it had silently disabled the gate
that the entire forward-validation story rests on.

**Found by** the October 2026 record being excluded as *"not committed before
its outcome"* despite having been committed 24 days before its expiry.
September's record — committed since `8e5073e` — resolved identically, which is
what showed it was the check rather than the record.

### What was wrong

```python
subprocess.run(["git", "log", "--diff-filter=A", "--format=%cs",
                "--", str(path)], cwd=path.resolve().parent)
```

The pathspec was passed through **as given** while `cwd` was set to the file's
own parent. `load_records` hands it a repo-root-relative path, so git resolved
`docs/trading/results/screens/screen-X.md` *underneath* the screens directory,
matched nothing, and returned `None`.

Demonstrated three ways on the same file:

| Invocation | Result |
|---|---|
| relative pathspec, `cwd` = the file's own dir (**as the code did**) | **empty** |
| pathspec relative to that `cwd` | `2026-09-11` |
| absolute pathspec | `2026-09-11` |

### What it cost

`committed_at` returned `None` for **every record ever written**. Therefore
`provenance_verified` was false universally, `in_calibration` with it, and
**Gate 3 could not be reached** however many valid months were recorded — while
the report blamed each record for failing the one condition that is the entire
point of the gate.

### Why it stayed silent — the transferable part

1. **It fails closed.** Excluding everything meant the eight backdated screens
   were still correctly refused, so no published verdict was ever wrong. The
   check appeared to be working because its *output* was right.
2. **"Unverifiable" is an answer this function is allowed to give.** A universal
   `None` is indistinguishable from a repository with no commits.

> **A check that fails closed cannot be validated by anything it rejects.** It
> took a *correct* input — a record that genuinely should have passed — to
> reveal that the check rejected everything. Until October 2026 no such input
> had ever been presented to it.

### A claim this forces us to withdraw

`docs/understanding/v1/22-forward-validation.md` called the +94.55% exclusion
*"a better demonstration of the check than any passing test."* **It was not a
demonstration of anything.** The eight records were refused because everything
was refused. The exclusion was right for the wrong reason, and that document has
been corrected.

### The fix

Resolve the path before using it as the pathspec. Tests now build a real git
repo and commit into it, covering the repo-root-relative shape that failed, the
absolute shape that worked, an uncommitted file, and a path outside any repo —
the last two being the cases that must still return `None`.

`score-screens` now reports the October record as **"nothing scored yet"**
rather than a provenance failure, which is its actual state: conditions 1, 2
and 4 met, awaiting the expiry.

> **Lesson:** a safety check exercised only by cases it should reject has not
> been tested. Write the passing case first.


## What the fixes did to the answer

| Stage | Mean return | Worst trade |
|---|---|---|
| Contaminated | +4.79% | −1,677% |
| + liquidity gate (defect 2) | +4.15% | −1,677% |
| + degeneracy gate (3) + persistence (4) | **+4.56%** | **−245.8%** |

**The headline moved by less than half a percentage point while the worst trade
fell by a factor of seven.**

That is the shape of the whole episode: the mean was never the contaminated
statistic — the tail was, and in a premium-selling strategy the tail is the part
that decides whether the book is usable at all.

Note also that each fix moved the answer *downward* before the final one
recovered part of it. **A data cleanup that improves your result is the
suspicious outcome**; removing fictional prices should remove fictional wins
along with fictional losses.

---

## The transferable lessons

1. **Check the tail, not the mean.** Three of four defects were invisible in
   every headline statistic and obvious in the worst trade.
2. **An impossible number is a gift.** −1,677% could not be explained away, so it
   had to be chased. A merely *disappointing* number would have been accepted.
3. **A successful lookup is not a correct lookup.** Every chain query returned a
   row. The rows were fiction.
4. **Two obviously correct filters can both miss the same defect.** Volume and
   no-arbitrage are each right and each blind here.
5. **Test at the granularity the defect lives at.** Whole-chain degeneracy passes
   IOC; per-expiry catches it.
6. **`CREATE TABLE IF NOT EXISTS` never alters an existing table.** Schema
   intentions expressed only in the `CREATE` are not applied.
7. **A writer that logs and continues will hide a systematic failure.** 3,308
   identical errors scrolled past in every run.
8. **Cross-check independent implementations.** The P&L fix is verified against a
   separate code path rather than a constant.
9. **Attach identity where a row is created, not where it is completed.** Every
   early return is a path that skips a late assignment (Defect 5).
10. **A dramatic result is a reason to check the denominator.** Before believing
    an intervention changed outcomes, check that it did not change which
    observations exist (Defect 6).
11. **A population error rate does not transfer to a selected sample** when the
    selection criterion and the error share a cause — 0.16% became 1-in-6
    (Defect 7).
12. **"Legally tradeable" is a separate gate from "priceable."** The skip
    taxonomy covered the second and never considered the first (Defect 7).
13. **A check that fails closed cannot be validated by anything it rejects.**
    Present it a passing case, or you have not tested it (Defect 8).

These extend the [research branch's own catalogue of silent
defects](../../understanding/v1/15-bugs-and-lessons.md). The project's running
total is **eighteen**, of which exactly one crashed.

That ratio is the argument for everything else in this repository — the health
gate, the placebo, the leakage tripwire, the common-set discipline, and the
habit of chasing an impossible number instead of accepting a plausible one.
