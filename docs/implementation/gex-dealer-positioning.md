# Dealer Positioning (GEX/DEX) — Detecting Institutional Flow from Option Greeks

> **Goal:** Detect institutional ("whale") positioning in NSE F&O by reconstructing the
> **dealer hedging exposure** implied by per-strike open interest and Greeks, and test whether
> that exposure predicts next-day **realized volatility**.
> **Branch:** `feat/gex-dealer-positioning`, cut from `main` at `e13e4b9`.
> **Data:** `option_metrics.option_chain` — the full per-strike CE/PE snapshot for
> 217 symbols × 1,254 days (~320M chain elements), 2021-08-02 → 2026-08-28.
> **Requires a full ETL re-run** (Phase 2). 3–5 h.
> **Status:** NOT STARTED.

---

## 0. Why this, and why it is not the direction model

The direction model (`docs/models/xgboost-direction-model-card.md`) asked *"will this symbol rise
tomorrow?"* and answered: detectably, but not tradeably (honest walk-forward IC ≈ 0.009, decile
spread negative on clean liquid rows). That model is **retained as the price-only control** for
this project's write-up. It is not the goal and never was.

The goal is institutional flow. The causal chain this project exploits:

1. Whales trade size in options — market makers take the other side.
2. Dealers do not want directional risk, so they hedge in the underlying.
3. That hedging is **mechanical and forced**, not discretionary.
4. Therefore **open interest × Greeks = a map of predictable future hedging flow**.

You never observe the whale. You observe the footprint their dealer is *obliged* to leave. The
central testable claim is about volatility, not direction:

> **H1 (gamma regime).** When net dealer gamma is **positive**, dealers sell rallies and buy dips
> — realized volatility is suppressed. When it is **negative**, they buy rallies and sell dips —
> realized volatility is amplified. So the sign of net GEX at time *t* predicts realized
> volatility at *t+1*.

This is falsifiable, theoretically motivated, and — unlike next-day direction — points the model
at a target with a workable signal-to-noise ratio.

### Why the target changes to volatility

GEX theory predicts volatility *behaviour*. It makes no claim about direction. Keeping
`next_day_direction` as the target would pair the worst-SNR label available with a theory that
does not predict it. Phase 5 adds volatility targets; the direction targets stay as controls.

This also **unifies the repository**. `backtest/strangle.py` is a short-strangle — a
volatility-*selling* strategy. A GEX regime signal answers "when is it safe to sell vol?", so
Phase 8 closes the loop by gating the existing backtester on the signal.

---

## 1. Execution prerequisites (read first)

**Repo:** `/mnt/storage/Study/Major Project/Code/MktMtm` · branch `feat/gex-dealer-positioning`.

**Shell:** the interactive default is fish; run everything through bash.

```bash
cd "/mnt/storage/Study/Major Project/Code/MktMtm"
uv sync --group dev
uv run pytest -m "not integration" -q     # must be green before you start
uv run python scripts/verify_db_health.py # must exit 0 before you touch the ETL
```

### Hard rules for the executor

1. **The enrichment logic lives in two places.** `storage/etl.py` and `pipeline/process.py`
   compute the same enrichment two ways (PG rows vs JSON file). Per `CLAUDE.md`, **any change to
   enrichment must land in both**, or they silently diverge. This applies to every chain-element
   change in Phase 2.
2. **`DATASET_COLUMNS` in `ml/dataset.py` is the single source of truth** for column identity
   *and order*; `sql/init_ml_dataset.sql` mirrors it. Adding a feature or a target means editing
   **both**.
3. **Two price series, two purposes.** `underlying_price` is the **raw/unadjusted** close and is
   the *only* correct basis for anything strike-relative — moneyness, GEX, DEX, max pain, walls —
   because NSE strikes are unadjusted. `underlying_price_adj` is the *only* correct basis for
   returns and targets. **Mixing them is defect F9 and it corrupted ~10% of IV rows before it was
   caught.** Every formula in this plan states which basis it uses.
4. **Row order is load-bearing.** Every momentum/change feature is a `groupby().pct_change()`.
   The SQL `ORDER BY symbol, date` and the explicit re-sorts are not cosmetic.
   `validate_no_lookahead()` runs inside `build_dataset` and must not be weakened or skipped.
5. **Targets are NULL on each symbol's most recent day.** Those are inference rows, never
   training rows. New targets in Phase 5 must preserve this.
6. **Progress UI is a requirement, not a nice-to-have** (see §2). Every new long-running command
   must report progress through `mktmtm.progress`.
7. One commit per task, `feat|fix|test|docs: <scope>: <description>`. Full unit suite green
   before every commit. At each phase end: run the **Exit test**, commit, then **stop and report**.
8. **Do not re-run the ETL until Phase 2 is committed and its unit tests are green.** The re-run
   is 3–5 h; starting it against unverified enrichment code wastes a night.

### The two things that are already true (verified 2026-09-01)

**The chain already stores what GEX needs.** `storage/etl.py` writes, for every CE/PE row of
every day:

```
expiry, strike, type, settle, open, high, low, close,
volume, oi, chg_oi, iv, delta
```

Per-strike **open interest**, per-strike **OI change**, and per-strike **IV** are already
persisted for all ~320M chain elements. No new data acquisition is needed for the core work.

**Gamma is already computed and then thrown away.** `etl.py` calls
`black_scholes_greeks(...)["delta"]` — and `black_scholes_greeks` already returns `gamma` in the
same dict, from the same `d1`/`d2`. Storing it costs approximately zero additional compute. Only
`vanna` and `charm` need new formulas.

---

## 2. Cross-cutting requirement — the CLI progress UI

`mktmtm.progress` already provides the house pattern: `progress_stage(...)` for manually-advanced
work and `track(iterable, ...)` for loops. Both render rich bars on a TTY and degrade to log
lines when stderr is not a TTY or `MKTMTM_NO_PROGRESS=1` is set — so `mktmtm ... > run.log`
stays readable. The ETL and fetch commands already use it.

**Every command added by this plan must:**

- wrap its long phases in `progress_stage` / `track`, with a `label` that names the *symbol or
  stage being worked on*, not just a count;
- print a **final summary table** (rich `Table` on a TTY) of what was produced — rows written,
  symbols covered, anything skipped;
- degrade cleanly under `MKTMTM_NO_PROGRESS=1` (test this, don't assume it);
- never write bare `print`; use `typer.echo` or the module logger.

A reference orchestrator implementing exactly this shape (`run-pipeline`, stage banners + results
table) exists on branch `model/xgboost-direction` as `src/mktmtm/pipeline_run.py`. It depends on
`ml/train_direction.py`, which is not on this branch, so it cannot be cherry-picked directly —
**port the UI pattern, not the file** (Task 7.3).

---

## Phase 1 — Second-order Greeks (`options/pricing.py`)

**Objective:** Vanna and charm as pure, unit-tested functions. No I/O, no ETL, no DB.

### Task 1.1 — extend `black_scholes_greeks`

Add `vanna` and `charm` to the returned dict, reusing the existing `d1`/`d2`. Keep the existing
five keys and their values byte-identical — the ETL, the backtester and
`idx_ce_delta` all depend on them.

With `φ(x)` the standard normal PDF and `q = 0`:

```
vanna = -φ(d1) · d2 / σ                        # ∂delta/∂σ  (same for call and put)
charm_call = -φ(d1) · (2rT - d2·σ√T) / (2T·σ√T)
charm_put  = charm_call + ... (sign differs; derive, do not guess)
```

Preserve the existing degenerate guard: `T <= 0 or sigma <= 0` returns all-zeros. Extend it to
the new keys.

### Task 1.2 — tests

`tests/unit/test_pricing.py`:

- **Put-call parity for vanna**: identical for calls and puts at the same (S,K,T,r,σ).
- **Finite-difference agreement**: `vanna ≈ (delta(σ+h) - delta(σ-h)) / 2h` and
  `charm ≈ -(delta(T-h) - delta(T)) / h`, `h = 1e-5`, tolerance 1e-4. This is the real test —
  it catches sign errors, which are the entire risk here.
- **Degenerate inputs**: `T=0`, `sigma=0` → 0.0, not NaN.
- **ATM sanity**: vanna ≈ 0 at-the-money, peaks either side.

**Exit test:** `uv run pytest tests/unit/test_pricing.py -q` green; finite-difference tests pass.
**Checkpoint:** report and stop.

---

## Phase 2 — Bake per-strike Greeks into the chain, then re-run the ETL

**Objective:** Every chain element carries `gamma`, `vanna`, `charm` alongside `delta`, so Phase 3
can aggregate in SQL instead of a Python pass over 320M elements.

> **This is the phase that costs a night.** Everything before it is cheap; everything after it
> depends on it.

### Task 2.1 — extend the chain element (both engines)

In `storage/etl.py`, replace the discard-everything-but-delta call:

```python
greeks = black_scholes_greeks(S, K, t_row, r_dec, iv / 100, is_call=...)
```

and store `delta`, `gamma`, `vanna`, `charm` on the chain dict. Keep the existing
`delta = 0.0` fallback semantics for non-converged IV — **do not** introduce `None` here; the
chain's `delta` is consumed by the backtester and by `idx_ce_delta`.

Mirror the identical change in `pipeline/process.py` (hard rule 1).

### Task 2.2 — schema + docs

Update `docs/database/README.md` §6 (JSONB shapes) with the new keys. No DDL change: the chain is
`JSONB` and the GIN index is `jsonb_path_ops`, so new keys need no migration.

### Task 2.3 — tests

- Chain elements carry all four Greeks with finite values for a converged fixture row.
- Non-converged IV (`iv is None`) still yields the zero-Greek fallback, not a crash or a NaN.
- `pipeline/process.py` and `storage/etl.py` produce the **same** Greeks for the same fixture
  input — the anti-divergence test that hard rule 1 exists to protect.

### Task 2.4 — the re-run

Only after 2.1–2.3 are committed and green:

```bash
uv run mktmtm preparse-bhavcopy          # rebuild the parquet cache first
uv run mktmtm etl 2>&1 | tee etl_gex.log # 3-5 h
uv run python scripts/verify_db_health.py
```

`preparse-bhavcopy` must run first — `CLAUDE.md` requires it after every fetch, and the ETL's
`_load_cache` offset map is built from it (this is defect F8's home; do not skip it).

**Exit test:** health gate exits 0; spot-check 5 symbols × 3 dates for finite `gamma`/`vanna`/
`charm` on every chain element with a non-null `iv`; row count ≈ 212k as before (this phase adds
columns, not rows).
**Checkpoint:** report row counts and any symbols that lost coverage; wait for go.

---

## Phase 3 — Dealer-exposure aggregates (`ml/dataset.py`)

**Objective:** Per symbol-day dealer-positioning features, computed in SQL, landing in
`ml_dataset`.

### Task 3.1 — the sign convention, as a named constant

Dealer side is an **assumption**, not a measurement. Encode it once, explicitly:

```python
# Standard GEX convention: dealers are long calls and short puts against
# customer flow. This is a heuristic — without participant-level data it
# cannot be verified. See "Known limitations" in the model card.
DEALER_SIGN = {"CE": +1.0, "PE": -1.0}
```

Never inline `+1/-1` at a call site. Phase 6 tests the opposite convention as a robustness check,
and that must be a one-line change.

### Task 3.2 — the aggregates

Extend `_CHAIN_AGG_SQL` with lateral aggregations over `jsonb_array_elements(m.option_chain)`.
**All of these use `m.underlying_price` — the RAW basis** (hard rule 3).

| Feature | Definition |
|---|---|
| `dex` | `Σ oi · delta · S` — net delta dealers must hedge |
| `gex` | `Σ sign · oi · gamma · S² · 0.01` — P&L-per-1%-move convention |
| `gex_chg` | `Σ sign · chg_oi · gamma · S² · 0.01` — *today's* positioning change |
| `vanna_exp` | `Σ sign · oi · vanna` |
| `charm_exp` | `Σ sign · oi · charm` |
| `oi_hhi` | Herfindahl of `oi` across strikes — concentration |
| `max_pain` | strike minimising total ITM value across the near expiry |
| `call_wall` / `put_wall` | strike with max CE / PE `oi` |
| `dist_to_call_wall` | `(call_wall - S) / S` |
| `otm_put_oi_build` | `Σ chg_oi` over PE strikes with `K < 0.95·S` — the hedging tell |

Add `_chg` day-over-day variants in `_add_derived` for `dex`, `gex`, `oi_hhi` — via
`groupby("symbol").diff()`, respecting hard rule 4.

### Task 3.3 — the gamma flip level

`gamma_flip` is the spot at which net GEX crosses zero. It cannot be read off a single row: it
needs GEX evaluated across a grid of hypothetical spot levels.

**Compute it at ETL time, not in SQL** — the chain is already in memory there, and doing it in
SQL means re-evaluating the chain 21× per symbol-day. Add to `etl.py`: evaluate net GEX at
`S · [0.90, 0.91, ..., 1.10]`, find the sign change, linearly interpolate, store as a scalar
column. Emit `NULL` when there is no crossing in range — that is a real state, not a failure.

Then expose `dist_to_flip = (S - gamma_flip) / S` in `ml/dataset.py`.

> If Phase 2 has already run when you reach this task, this needs a **second** ETL pass. Prefer
> to land Task 3.3's ETL change *before* the Phase 2 re-run and do both in one night. Read this
> phase before starting Phase 2.4.

### Task 3.4 — contract sync

Add every new column to `DATASET_COLUMNS` **and** `sql/init_ml_dataset.sql`, in the same order
(hard rule 2). Extend `compute_chain_aggregates`'s `pd.to_numeric` cast — `SUM(bigint)` returns
`Decimal`, which object-ifies the frame and breaks estimators downstream.

**Exit test:** `mktmtm make-dataset --limit-symbols RELIANCE,TCS --limit-dates 30` writes every
new column non-null; `validate_no_lookahead()` passes; hand-verify `gex` for one symbol-day
against a Python recomputation from the raw chain.
**Checkpoint:** report the new column list and the hand-verification; wait for go.

---

## Phase 4 — Lot sizes, or an honest normalization

**Objective:** Make exposure numbers comparable across symbols, or state precisely why they
aren't.

Bhavcopy reports `OPEN_INT` in **contracts**. True share exposure needs the lot size, and **no
lot-size data exists anywhere in this repo** (verified). Two routes:

### Task 4.1 (preferred) — ingest the historical lot-size schedule

NSE publishes F&O lot sizes; they are **revised periodically**, so the *historical schedule* is
required, not current values. Land it as a shipped reference table under `data/reference/`,
loaded like `ADJUSTED_MIFOR.csv`. Verify formats and history depth on the NSE site before
committing to this route.

### Task 4.2 (fallback) — within-symbol normalization

Scale each exposure by that symbol's own trailing 60-day median. Lot size then cancels *except*
at revision dates, which appear as artificial jumps. If you take this route you **must** detect
and flag those jumps (a step change in `chain_oi` with no matching volume change), and say so in
the model card. Cross-symbol comparisons are then invalid — cross-sectional ranking must be done
within-symbol-normalized, not on raw exposure.

**Exit test:** either real lot sizes join cleanly for ≥95% of symbol-days, or the normalized
features have no step-change artifacts at known revision dates.
**Checkpoint:** report which route and the coverage; wait for go.

---

## Phase 5 — Volatility targets (`ml/dataset.py`)

**Objective:** Targets the theory actually predicts. Direction targets stay as controls.

### Task 5.1 — add the targets

All computed on the **adjusted** basis (hard rule 3), all NULL on each symbol's last day
(hard rule 5):

| Target | Definition |
|---|---|
| `next_day_abs_return` | `abs(next_day_return)` — primary, always available |
| `next_day_rv_5d` | forward 5-day realized vol from futures OHLC |
| `next_day_iv_chg` | change in `ce_iv_30` — does positioning predict *implied* vol |
| `next_day_vrp` | `next_day_abs_return − ce_iv_30/(100·√252)` — realized minus implied |

`next_day_vrp` is the one that matters for the strangle: it is the vol-risk premium the strategy
harvests.

### Task 5.2 — contract sync and look-ahead

Add to `TARGET_COLUMNS`, `DATASET_COLUMNS` and `sql/init_ml_dataset.sql`. Extend
`validate_no_lookahead()` to cover every new target. `next_day_rv_5d` needs **5** trailing NULLs
per symbol, not 1 — get this wrong and you leak five days of future into every fold.

**Exit test:** `validate_no_lookahead()` green; per symbol, exactly the right number of trailing
NULLs per target; a deliberately shifted target makes the validator raise.
**Checkpoint:** report; wait for go.

---

## Phase 6 — Hypothesis tests (reuse the existing harness)

**Objective:** Answer H1. Use the Phase 3–5 harness from the direction project **unchanged** —
`ml/splits.py`, `ml/metrics.py`, `ml/baselines.py` all transfer.

> The harness already provides purged walk-forward splits with an embargo, `inner_holdout` for
> honest early stopping, daily rank IC/ICIR, and a leak tripwire. Do not rebuild any of it.
> Do not weaken the embargo for a 5-day target — `next_day_rv_5d` needs `embargo_days=5`.

### Task 6.1 — H1, the sign test (no ML)

Split symbol-days by `sign(gex)`. Compare mean `next_day_abs_return` in each group, with a
t-test and the cross-sectional IC of `-gex` against `next_day_abs_return`.

**This is the whole thesis in one table.** If it fails here, no gradient-boosted model will
rescue it — report the negative result and move to Task 6.4.

### Task 6.2 — H2, positioning change

Does `gex_chg` / `otm_put_oi_build` predict next-day absolute return beyond `gex` level? Same
protocol, plus a partial IC controlling for the level.

### Task 6.3 — the model

Only if 6.1 or 6.2 shows signal. XGBoost regression (`objective="reg:squarederror"`) on
`next_day_abs_return`, config adapted from `docs/implementation/xgboost-direction-model.md`
Task 6.1 — the regularization rationale carries over unchanged. Early-stop on `inner_holdout`,
**never** on the reporting split.

### Task 6.4 — robustness (mandatory, whatever 6.1 says)

| Check | Why |
|---|---|
| Flip `DEALER_SIGN` | If the "wrong" convention scores the same, you are measuring OI concentration, not dealer gamma |
| Liquidity filter | Restrict to strikes with `volume > 0`; IV on untraded strikes is settlement-derived model output, and GEX sums across all strikes |
| Index vs stock | NIFTY/BANKNIFTY have a different dealer ecology; report separately |
| Expiry-week | Charm/vanna flows concentrate into monthly expiry; report with and without expiry week |

**Exit test:** `models/evaluation/gex_h1.json` with the sign test, ICs, and all four robustness
checks.
**Checkpoint:** report H1's verdict plainly — including if it is negative. Wait for go.

---

## Phase 7 — CLI and progress UI

**Objective:** Every step above runs as a first-class command with the §2 progress contract.

### Task 7.1 — `mktmtm gex-features`

Recompute dealer aggregates into `ml_dataset` without a full `make-dataset`. `progress_stage`
over symbols, label = current symbol, final summary table.

### Task 7.2 — `mktmtm test-gex`

Run Phase 6's hypothesis tests, write the JSON, print a results table.

### Task 7.3 — port the orchestrator

Port the `run-pipeline` staged-UI pattern from `model/xgboost-direction`'s
`src/mktmtm/pipeline_run.py` (stage banners, per-stage summary lines, final rich `Table`) to a
`gex-pipeline` command covering preparse → etl → gex-features → test-gex. **Port the pattern,
not the file** — the original imports `ml/train_direction.py`, which is not on this branch.

### Task 7.4 — progress tests

Assert both paths: with `MKTMTM_NO_PROGRESS=1` set (log lines) and unset. The rich path is the
one that breaks silently in CI, so cover it explicitly.

**Exit test:** each command runs end to end on a 5-symbol smoke subset, TTY and redirected.
**Checkpoint:** report; wait for go.

---

## Phase 8 — Close the loop with the backtester (optional)

**Objective:** Turn the signal into a decision, using the strategy that already exists.

Gate `backtest/strangle.py` entries on the GEX regime — take the short strangle only when net
dealer gamma is positive (vol suppressed), skip when negative. Compare P&L, hit rate and max
drawdown against the ungated baseline over the same period.

This is the strongest possible result for the write-up: it connects a microstructure signal to a
strategy P&L rather than stopping at an IC. **Do not touch `backtest/` until Phase 6 has
reported a positive H1** — gating a backtester on a signal that does not exist just produces a
noisier backtest.

**Exit test:** side-by-side P&L table, gated vs ungated.
**Checkpoint:** report; wait for go.

---

## Phase 9 — Write-up

`docs/models/gex-dealer-positioning-model-card.md`, following the structure of the direction
model card: config, splits, all metrics, robustness results, and **Known limitations** stated
plainly.

### Limitations that must appear, whatever the results say

- **The dealer sign convention is an assumption, not a measurement.** Without participant-level
  data you cannot verify who is long which side. State it; report the Task 6.4 flip test.
- **IV comes from `SETTLE_PR`, not traded prices.** For illiquid far-OTM strikes, settlement is a
  theoretical value, so their IV — and every Greek derived from it — is model output, not market
  data. GEX sums across all strikes, so this contaminates the tails.
- **Lot-size treatment** (Phase 4) and what it invalidates.
- **No participant-level ground truth.** This work *infers* institutional activity from dealer
  hedging mechanics. NSE's participant-wise F&O open interest (FII/DII/Pro/Client) and
  bulk/block deal disclosures would provide external validation and are the obvious extension —
  note that participant-wise OI is published by instrument *category*, not per symbol, so it
  validates index-level work directly and per-symbol work only indirectly.
- **The price-only control.** Report the direction model's honest numbers (walk-forward
  IC ≈ 0.009, negative decile spread on clean rows) as the baseline this project set out to beat.

---

## Appendix — Definitions

**GEX (gamma exposure).** `Σ_k sign_k · OI_k · Γ_k · S² · 0.01`, over all strikes *k*, with
`sign` from `DEALER_SIGN`. Units: currency P&L per 1% move in the underlying. `S` is the **raw**
`underlying_price`.

**DEX (delta exposure).** `Σ_k OI_k · Δ_k · S`. Net delta dealers carry and must hedge.

**Gamma flip.** The spot at which net GEX crosses zero. Above it dealers are long gamma and damp
volatility; below it they are short gamma and amplify it.

**Vanna.** `∂Δ/∂σ`. Drives hedging flow when *volatility* moves rather than spot — the mechanism
behind vol-triggered spot flows.

**Charm.** `∂Δ/∂t`. Delta decay: forced hedging flow that arrives purely from time passing.
Concentrates into expiry, which is why Task 6.4 splits out expiry week.

**Max pain.** The strike minimising aggregate ITM value across open contracts — a coarse proxy
for where concentrated OI exerts pinning pressure.
