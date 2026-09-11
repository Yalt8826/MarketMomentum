# 10 — Phases 2 and 3 as built

[← Phase 1 as built](09-phase-1-as-built.md) · [Index](README.md)

**Status: built 2026-09-11.** The screen ships; the first frozen record is
committed. Phase 3's verdict needs six months of records and cannot exist yet.

---

## 1. What was built

| Module | Lines | Role |
|---|---|---|
| `serving/screen.py` | 470 | Candidates, refusals, risk caps, frozen record |
| `cli.py` `screen` command | 90 | CLI surface |
| `tests/unit/test_screen.py` | 230 | 15 tests |
| `backtest/strangle.py` | +110 | Stop loss (measured; not shipped) |

**553 tests pass.**

## 2. The design decision that shaped everything

The original Phase 2 spec ranked candidates by a validated filter. Gate 1
removed that, and the stop-loss measurement removed the obvious fallback. So:

> **The screen does not rank by conviction. It orders by tradeability.**

Ordering is **net credit per rupee of margin**, then liquidity. That is an
efficiency question — which positions pay best for the capital they tie up — not
a prediction, and nothing in it claims an edge that was refuted.

The research features appear as `ctx_*` columns carrying an explicit label:
*tested in Gate 1; did not beat chance*. Showing them unlabelled would read as a
recommendation; suppressing them would withhold information a human might use
better than the harness did.

## 3. Refusals are the product

Gate 0's skip taxonomy became a feature. Every refusal is typed:

| Refusal | Why |
|---|---|
| `degenerate chain for this expiry` | The prices are fiction — 83% of these fall on an expiry date |
| `lot size unresolvable` | Guessing mis-scales the entire position |
| `earnings <date> before expiry` | IV collapses after results; the backtest excluded these, so their behaviour is unmeasured |
| `no tradeable 20-delta strikes` | Selection requires a contract that actually traded |
| `leg never traded today` | The settlement mark is not a price |
| `margin budget exhausted` / `position count cap reached` / `sector cap` | The risk limits binding |

A screen that silently omitted a name would teach nothing, and two of this
project's worst defects were silent omissions.

## 4. Sizing

Each candidate carries credit, entry costs, net credit, margin, credit-to-margin
and a **modelled loss at a 2σ move** — labelled as a scenario, because a
strangle's true maximum is unbounded.

That column earned its place on the first live run: **IDEA showed a ₹133,937
modelled loss against ₹24,220 of credit.** A 5.5:1 risk-to-credit ratio is
exactly what a trader needs before sizing a position, and no ordering by
"attractiveness" would have surfaced it.

## 5. Risk caps

| Cap | Default |
|---|---|
| Concurrent positions | 10 |
| Capital committed to margin | 50% |
| Positions per sector | 3 |
| Minimum leg volume | 1 lot traded today |

The sector cap exists because **15 strangles across one sector is one trade
wearing fifteen hats** — and a short-vol book's risk is overwhelmingly
correlated. All four are tested.

> ⚠️ Sector data is not currently wired: `apply_risk_limits` accepts a
> `sectors` mapping and `run_screen` does not supply one, so the sector cap is
> **inert in production today**. The catalog carries no sector field. This is
> the one acceptance criterion not fully met.

## 6. The frozen record — Phase 3.1

`write_screen_record` follows `ml.accumulation.write_nowcast_record` exactly:

- **Committed to git**, not written to gitignored `models/` — a record whose
  date cannot be established is not a forward record.
- **Refuses to overwrite.** Verified live: the second invocation on the same
  path exits non-zero.
- **States its own limits in its output**, not only in these documents. Five
  disclosures, each asserted by a test: the +4.56% and −37.27% together, no
  validated filter, the stop loss not helping, no crash in the sample, and the
  decaying edge.

First record: [`../results/screens/screen-2026-09-04.md`](../results/screens/screen-2026-09-04.md)
— 6 positions from 177 tradeable candidates, ₹493,581 margin on a ₹10L book.

## 7. The stop loss — built, measured, not shipped

Implemented as a multiple of entry credit, checked at each weekly hedge date
**before the adjustment** (the adjustment re-sells into a losing position, so a
stop firing afterwards has already taken the risk it exists to avoid).

Measured at 1.5× / 2× / 3× on the common set of 7,706 trades: **every measured
outcome got worse**. Full account: [`../results/stop-loss.md`](../results/stop-loss.md).

It remains in the code as a `run_backtest` parameter, because a negative result
that cannot be re-run is not reproducible.

## 8. What is not done

| | |
|---|---|
| **Sector data** | Not in the catalog; the sector cap is inert (§5) |
| **Streamlit page** | The CLI ships; the UI page does not |
| **Phase 3 verdict** | Needs ≥ 6 monthly records. One exists. |
| **Cost rates vs a contract note** | Brokerage is 57.9% of costs and is assumed at ₹20 flat |

## 9. Running it

```bash
uv run mktmtm run-pipeline --mode no-etl        # refresh
uv run mktmtm screen --capital 1000000 --max-positions 10
uv run mktmtm screen --out docs/trading/results/screens/screen-$(date +%F).md
```

[← Phase 1 as built](09-phase-1-as-built.md) · [Index](README.md)
