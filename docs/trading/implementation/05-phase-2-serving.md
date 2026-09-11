# 05 — Phase 2: the daily tool

[← Phase 1](04-phase-1-evaluation.md) · [Index](README.md) · Next: [Phase 3 →](06-phase-3-forward-validation.md)

**Reached only if Gate 1 passes.** If it fails, a simplified version of this
still ships — see §6.

---

## 1. What the user actually needs

The audience is a trader with a broker account, not a researcher with
PostgreSQL. The output is a short morning list they can act on, with enough
context to decide *not* to act.

**Serving does not need the 24 GB fact table** — only today's chain and today's
feature row. That distinction keeps the daily job in minutes.

```bash
uv run mktmtm run-pipeline --mode no-etl   # incremental refresh
uv run mktmtm screen                       # NEW — the deliverable
```

## 2. `serving/screen.py`

The `serving/` package exists and is empty. One command, one output.

| Field | Why it is there |
|---|---|
| symbol, expiry | Which contract |
| call strike / put strike | At ~20 delta, from today's chain |
| expected credit (₹) | At the lot size **in force today** |
| **margin required (₹)** | Without it the trade cannot be sized |
| **max loss at ±2σ** | A strangle's true max is unbounded; state the modelled figure and say so |
| filter values | **Why** this name was selected |
| rank | An ordering, never a recommendation |

> **Every row must carry its reasons.** An unexplained score is unusable: the
> trader cannot tell a thin edge from a strong one, and cannot notice when the
> model is reacting to something they know is wrong. This mirrors the nowcast
> record, which ships `signal_raw` and `window_return` beside the score.

## 3. Risk controls — in the tool, not in a disclaimer

A short strangle can lose more than the capital allocated to it. These are
product features:

| Control | |
|---|---|
| Max concurrent positions | Hard cap |
| Max fraction of capital committed to margin | Hard cap |
| Per-trade stop as a multiple of credit received | From Phase 1, where the stop is a parameter |
| **Correlation cap** | 15 strangles across one sector is one trade wearing fifteen hats |
| Refuse on unresolvable inputs | No lot size, no margin, no liquidity → **no candidate**, with a reason |

That last rule is the Phase 0 skip discipline carried into production: never
default a missing input, always say why a name is absent.

## 4. Liquidity gate

Phase 0.5 measures how often a selected 20-delta strike had **zero volume**. Any
strike failing the liquidity bar is dropped here, because a screen that suggests
untradeable contracts is worse than one that suggests fewer.

## 5. Presentation

The Streamlit UI already exists, already renders charts, and already runs jobs as
detached subprocesses via `ui/jobs.py`. **Add a page; do not build a front end.**

> ⚠️ `mktmtm-ui` binds the tailnet IP and has **no authentication**. It must
> never bind `0.0.0.0`. This is now a panel that suggests trades, which raises
> the cost of getting that wrong.

## 6. If Gate 1 failed — ship the screen anyway

A screen is still worth building without a validated filter, provided it stops
claiming to be one:

- Rank by **IV rank alone** — the baseline that beat everything else.
- Show the project's features as **context columns**, explicitly labelled as not
  having beaten that baseline.
- Keep every risk control and the liquidity gate unchanged.

That is an honest product: *"here are the names where options are historically
expensive, here is what else is unusual about them, here is what the position
would cost and risk."* It makes no claim the evidence cannot support, and it is
genuinely useful to someone doing their own analysis.

[← Phase 1](04-phase-1-evaluation.md) · [Index](README.md) · Next: [Phase 3 →](06-phase-3-forward-validation.md)
