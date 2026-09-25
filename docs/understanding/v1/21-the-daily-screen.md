# 21 — The Daily Screen: The Actual Tool

[← Does it make money?](20-does-it-make-money.md) · [Index](README.md) · Next: [Forward validation →](22-forward-validation.md)

Everything before this document is analysis. This one is the product — the thing
a person actually runs, on the first trading day of the month, to decide what to
trade.

```bash
uv run mktmtm screen --capital 1000000
```

It is a small tool, and the interesting thing about it is how thoroughly it is
shaped by results that came out *negative*.

---

## 1. The design constraint

The original specification for this phase said: rank candidates by a validated
filter, show the best ones first.

Then [Gate 1](20-does-it-make-money.md#3-gate-1--fail) measured that no such
filter exists. Six research features plus implied-volatility rank, and none beat
picking trades at random. The obvious fallback — a stop loss to control the
downside — was measured next, and made every outcome worse.

So the tool was left with no basis for conviction whatsoever. What it does
instead:

> **The screen does not rank by conviction. It orders by tradeability.**

Ordering is **net credit per rupee of margin**, then liquidity. That is an
*efficiency* question — which positions pay best for the capital they tie up —
not a prediction. Nothing in it claims an edge that was refuted.

This is a meaningful distinction, not a euphemism. "Rank by expected return"
asserts the tool knows which trades will do better. "Order by credit per rupee
of margin" asserts only that ₹20,000 of premium for ₹70,000 of blocked capital
is a more efficient use of that capital than ₹9,000 for ₹53,000 — which is
arithmetic, and true regardless of which trade wins.

### What happened to the research features

They are still displayed, as `ctx_*` columns, each carrying an explicit label:

> *tested in Gate 1; did not beat chance*

Both alternatives were worse. Showing them unlabelled would read as a
recommendation for something that was measured not to work. Suppressing them
entirely would withhold information a human might use better than the harness
did — a person looking at one specific name knows things the backtest does not.

Labelling them is the honest middle: here is the number, here is the measured
fact that it did not help.

---

## 2. Refusals are the product

Gate 0's skip taxonomy — the table showing that only 51% of candidate months
actually trade — became the tool's central feature. **Every refusal is typed and
shown**, with the symbols it applied to.

| Refusal | Why |
|---|---|
| `degenerate chain for this expiry` | The prices are fiction — **83% of these fall on an expiry date** |
| `lot size unresolvable` | Guessing mis-scales the entire position |
| `earnings <date> before expiry` | IV collapses after results; the backtest excluded these, so their behaviour is unmeasured |
| `no tradeable 20-delta strikes` | Selection requires a contract that actually traded |
| `leg never traded today` | A settlement mark is not a price ([doc 18 §5](18-what-a-trade-costs.md)) |
| `margin budget exhausted` | A risk limit binding |
| `position count cap reached` | A risk limit binding |
| `sector cap` | A risk limit binding |

The reasoning behind making these visible is stated plainly in the build record:

> A screen that silently omitted a name would teach nothing, and **two of this
> project's worst defects were silent omissions.**

That is not a rhetorical flourish. [Document 15](15-bugs-and-lessons.md)
documents a parquet cache that silently dropped 75 symbols and a writer that
silently discarded 3,308 rows in every run. Both produced plausible output. A
tool built by people who lived through that does not get to hide a skipped
symbol.

> Note the `degenerate chain` line especially. That refusal exists because of
> [Defect 3](20-does-it-make-money.md) — days where the underlying's price was
> written into every option's premium in the chain, making a call on a ₹130
> stock appear to be worth ₹130.75. It was responsible for the backtest's
> impossible −1,677% trade. The live tool checks for it **per expiry**, because
> a whole-chain check misses the partial form.

---

## 3. Sizing: what each candidate tells you

Every candidate carries the full economics from
[document 18](18-what-a-trade-costs.md):

| Column | What it is |
|---|---|
| Call / Put | The two strikes being sold |
| Net credit | Premium collected, **after entry costs**, in rupees |
| Margin | Capital the position blocks |
| Credit/margin | The efficiency ratio the ordering uses |
| **Modelled loss (2σ)** | What a two-standard-deviation move would cost |
| Min leg vol | Contracts traded on the thinner of the two legs today |

### The column that earned its place immediately

The 2σ loss is labelled explicitly as a **scenario, not a maximum** — a
strangle's true maximum loss is unbounded, and any number presented as "the
worst case" would be a lie.

On the first live run it justified itself:

> **IDEA showed ₹133,937 of modelled loss against ₹24,220 of credit.**

A 5.5:1 risk-to-credit ratio. That is exactly what a trader needs to see before
sizing a position, and **no ordering by attractiveness would have surfaced it** —
IDEA ranked fourth of six on credit-to-margin, which is to say it looked
perfectly reasonable by the metric the tool sorts on.

This is the argument for showing risk beside reward rather than folding both
into a single score. A single number would have hidden it.

---

## 4. Risk caps

| Cap | Default | Flag |
|---|---|---|
| Concurrent positions | 10 | `--max-positions` |
| Capital committed to margin | 50% | `--margin-fraction` |
| Positions per sector | 3 | — |
| Minimum leg volume | 1 lot traded today | `--min-volume` |

The **50% margin cap** deserves a word. It might look timid — why leave half the
capital idle? Because margin requirements *rise* when positions move against
you. If you are fully committed and the market gaps, the broker demands more
collateral you do not have, and closes your positions for you at the worst
possible moment. The idle half is not idle; it is what stops a bad month from
becoming a forced liquidation.

The **sector cap** exists because of correlation:

> **15 strangles across one sector is one trade wearing fifteen hats.**

A short-volatility book's risk is overwhelmingly correlated — the scenario that
hurts it is "everything moves at once", and everything in one sector moves
together. Spreading across ten positions gives far less diversification than the
count suggests if they are all banks.

> ⚠️ **The sector cap is currently inert.** `apply_risk_limits` accepts a
> sectors mapping and `run_screen` does not supply one, because the symbol
> catalog carries no sector field. It is implemented and unit-tested; it is not
> wired. This is the one acceptance criterion of Phase 2 that was not fully met,
> and it is disclosed rather than quietly dropped.

---

## 5. A real run, read line by line

This is the actual first live screen, committed as
`docs/trading/results/screens/screen-2026-09-04.md`:

```
Capital ₹1,000,000 · margin cap 50% · max 10 positions · max 3 per sector

- Margin committed: ₹493,581 (49.4% of capital)
- Net credit collected: ₹100,642
```

| # | Symbol | Call | Put | Net credit | Margin | Credit/margin | Modelled loss (2σ) | Min leg vol |
|---|---|---|---|---|---|---|---|---|
| 1 | BANDHANBNK | 175 | 155 | ₹20,408 | ₹71,258 | 0.286 | ₹24,485 | 63 |
| 2 | INOXWIND | 80 | 67.5 | ₹12,543 | ₹57,155 | 0.219 | ₹20,596 | 25 |
| 3 | KALYANKJIL | 660 | 555 | ₹18,354 | ₹97,001 | 0.189 | ₹74,809 | 140 |
| 4 | IDEA | 17 | 14 | ₹24,220 | ₹128,827 | 0.188 | **₹133,937** | 4464 |
| 5 | PRESTIGE | 1700 | 1460 | ₹16,071 | ₹85,823 | 0.187 | ₹38,891 | 79 |
| 6 | KPITTECH | 620 | 540 | ₹9,046 | ₹53,518 | 0.169 | ₹32,787 | 90 |

**What a reader should take from this:**

- Six positions out of **177 tradeable candidates**. The margin budget bound
  first — 171 symbols were refused as `margin budget exhausted`, and a further
  40 for `no data for this date`.
- **₹100,642 of credit against ₹493,581 of margin** — a 20.4% credit-to-margin
  ratio for the book. That is the most you can make; it is not the most you can
  lose.
- **Row 4 is the point of the whole table.** IDEA pays the largest single credit
  (₹24,220) and carries a modelled 2σ loss of ₹133,937 — more than a quarter of
  the entire book's margin, in one name. It is also by far the most liquid leg
  (4,464 contracts). Nothing about "largest credit, most liquid" warns you; only
  the risk column does.
- BANDHANBNK, by contrast, pays ₹20,408 against ₹24,485 of modelled loss —
  roughly one-to-one. A very different proposition at a similar credit.

### The tool states its own limits

Every run prints, and every written record carries, a block like this:

```
Backtested +4.56% mean return on margin, -37.27% max drawdown.
No entry filter is validated; a stop loss was measured and does not help.
The sample contains no severe crash, which is where this risk lives.
```

The design principle: **the caveats live in the output, not only in the
documentation.** A person running the tool six months from now will not re-read
[document 20](20-does-it-make-money.md), and the return figure and the drawdown
figure should never appear apart from each other.

---

## 6. The frozen record

With `--out`, the screen writes a dated markdown record, which is then
**committed to git**:

```bash
uv run mktmtm screen --out docs/trading/results/screens/screen-$(date +%F).md
git add docs/trading/results/screens/ && git commit -m "screen: $(date +%F)"
```

Two properties, both borrowed from the research branch's nowcast record:

- **It refuses to overwrite.** Running it twice on the same path exits non-zero.
  A record that can be silently rewritten after the fact is not a record.
- **It is committed, not written to gitignored `models/`.** A prediction whose
  date cannot be independently established proves nothing about when it was
  made.

Why the commit is the entire point of the exercise is the subject of
[document 22](22-forward-validation.md).

---

## 7. Running it

```bash
# On the FIRST TRADING DAY of the month — this matters, see doc 22
uv run mktmtm run-pipeline --mode no-etl        # refresh features
uv run mktmtm screen --capital 1000000 \
    --out docs/trading/results/screens/screen-$(date +%F).md
git add docs/trading/results/screens/ && git commit -m "screen: $(date +%F)"

# Any time after an expiry
uv run mktmtm score-screens
```

| Flag | Default | |
|---|---|---|
| `--capital` | ₹1,000,000 | Capital available |
| `--max-positions` | 10 | Hard cap on concurrent positions |
| `--margin-fraction` | 0.5 | Max fraction of capital in margin |
| `--min-volume` | 1 | Refuse a leg that traded fewer lots than this |
| `--top` | 15 | Rows to display |
| `--out` | — | Write a committed markdown record |

The screen reads the same tables as everything else, so it needs the pipeline to
be current. It does **not** need the ETL re-run — `--mode no-etl` is enough,
which takes minutes rather than hours ([document 16](16-running-it.md)).

[← Does it make money?](20-does-it-make-money.md) · [Index](README.md) · Next: [Forward validation →](22-forward-validation.md)
