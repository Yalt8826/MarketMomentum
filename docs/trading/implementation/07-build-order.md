# 07 — Build order

[← Phase 3](06-phase-3-forward-validation.md) · [Index](README.md)

Concrete task list: what to write, where, what tests pin it, and which gate it
feeds. Tasks are ordered; each assumes the previous ones are done.

---

## Phase 0 — economics and the base result

| # | Task | Files | Tests |
|---|---|---|---|
| **0.1** | `CostRates` / `MarginModel` dataclasses, dated and configurable | `backtest/economics.py`, `config/settings.py` | rates resolve by date; no rate hard-coded in a function |
| **0.2** | `contract_value`, `order_costs`, `trade_costs` — itemised | `backtest/economics.py` | known-value cases; costs scale with order count; sell-side STT only |
| **0.3** | `strangle_margin` + sensitivity hook | `backtest/economics.py` | monotone in notional; ±25% band reachable by parameter |
| **0.4** | `apply_slippage` | `backtest/economics.py` | **always against the trader**, both sides |
| **0.5** | Lot-size resolution at entry date | `backtest/strangle.py` ← `data/lots.py` | picks the lot in force, not the latest; unresolvable → skip with reason |
| **0.6** | Schema: `variant` + rupee columns; PK change | `sql/backtest_results.sql` | `init_db.sql` idempotent re-run |
| **0.7** | Wire economics into `run_backtest_month`; persist gross/costs/slippage/net | `backtest/strangle.py` | `net = gross − costs − slippage`; order count = `4 + 2n` |
| **0.8** | Skip taxonomy + zero-volume counter | `backtest/strangle.py` | every skip carries a typed reason |
| **0.9** | Run 217 symbols × 5 years, progress-barred | `backtest/strangle.py`, `pipeline_runner.py` | stage wrapper contract test (see below) |
| **0.10** | `analysis.py`: returns, Sharpe, drawdown, tails, equity curve, cost breakdown | `backtest/analysis.py` | drawdown on a known series; tail percentiles |
| **0.11** | Slippage grid + **breakeven slippage** | `backtest/analysis.py` | breakeven solves to zero net |
| | **→ GATE 0** | | |

> **Task 0.9 needs a contract test.** `pipeline_runner.py` duplicates what CLI
> commands do, and that duplication has already produced three bugs — one of
> which reported success in 58 seconds while its table stayed stale. Every new
> stage wrapper gets a test in `tests/unit/test_pipeline_runner.py`.

## Phase 1 — does the research add anything?

| # | Task | Files | Tests |
|---|---|---|---|
| 1.1 | Join features at **prior close** | `backtest/analysis.py` | ⭐ look-ahead tripwire: an outcome-conditioned filter must be rejected |
| 1.2 | Filter harness: threshold / tercile / rank | `backtest/analysis.py` | a filter that admits everything reproduces unfiltered exactly |
| 1.3 | Baselines: unfiltered, **IV rank**, random placebo (seeded, many draws) | `backtest/analysis.py` | placebo distribution reproducible from the seed |
| 1.4 | Walk-forward via `ml/splits.py`; select on train, report on test | `backtest/analysis.py` | no fold selects and reports on the same rows |
| 1.5 | Moving-block bootstrap **over months** | reuse research-branch code | blocks preserve within-month grouping |
| 1.6 | Optional stop-loss as a parameter | `backtest/strangle.py` | stop changes the distribution, not the entry |
| | **→ GATE 1** | | |

## Phase 2 — the tool

| # | Task | Files | Tests |
|---|---|---|---|
| 2.1 | `mktmtm screen` | `serving/screen.py`, `cli.py` | output carries filter values, never a bare score |
| 2.2 | Liquidity gate | `serving/screen.py` | zero-volume strikes excluded |
| 2.3 | Risk controls: position cap, margin cap, correlation cap, stop | `serving/screen.py` | caps bind; unresolvable inputs → refusal with reason |
| 2.4 | Streamlit page | `ui/` | ⚠️ tailnet bind only, never `0.0.0.0` |

## Phase 3 — forward validation

| # | Task | Files | Tests |
|---|---|---|---|
| 3.1 | `write_screen_record` — dated, committed, **refuses overwrite** | `serving/screen.py` | raises on an existing record; `--overwrite` only for same-day correction |
| 3.2 | Monthly scoring harness | `backtest/analysis.py` | scores a synthetic record correctly |
| 3.3 | Running record, **losing months included** | `docs/trading/results/` | — |
| | **→ GATE 3** | | |

---

## Standing requirements

Inherited from the research branch, and non-negotiable here:

- `uv run pytest -m "not integration"` green (463 tests today).
- `uv run python scripts/verify_db_health.py` → PASS before any evaluation run.
- Every economic claim reports **net** of costs and slippage; gross alone is
  never quoted as a result.
- Every write-up states the **cost rate set and its date**, the **margin
  assumption**, and the **breakeven slippage**.
- Negative results go to `docs/trading/results/` at the same resolution as
  positive ones.

## The first thing to do

**Task 0.1 through 0.11, then stop at Gate 0.** It is a few hours of work and it
settles the largest question in this branch: whether there is any strategy here
at all, before a single feature is evaluated.

[← Phase 3](06-phase-3-forward-validation.md) · [Index](README.md)
