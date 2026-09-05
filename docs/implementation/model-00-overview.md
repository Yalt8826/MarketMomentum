# Institutional-flow modelling — overview and build contract

**Status:** built, 2026-09-05. All four stages ship; every stage file's Results
section records what was measured.
**Audience:** anyone extending or writing up the model work.
**Read this file first.** It carries the framing, the shared conventions, and
the inter-stage contracts. The per-stage files are useless without it.

### Outcomes at a glance

| Stage | Verdict | The number that decides it |
|---|---|---|
| 1 — market flow | **Null.** No feature set beats the calendar. | Best AUC margin over `calendar_only`: **+0.002**; four combinations significantly *negative*. Calendar alone: AUC 0.61–0.72. |
| 1b — quadrants | **Built and useful.** | Delta adjustment is required: raw vs adjusted disagree on **35.8%** of contracts. Direction features are the only block that improves Stage 1. |
| 2 — regime HMM | **Negative.** No persistent discrete regimes. | Min transition diagonal **never exceeds 0.5 at any k** (0.367 → 0.072); BIC never turns around. |
| 3 — attribution | **Mixed, leaning negative.** Decomposes but does not identify. | Placebo passes (R² 0.45 vs −0.01) but earnings corroboration **fails**: shuffled labels cluster around earnings *more* than real ones for 2 of 3 participants. |

**Full results report: [`../results/README.md`](../results/README.md)** — consolidated
across all four stages, with methodology, limitations and reproduction steps.

Three of four are negative or null. That is the honest state of the work, and
the write-up should lead with it rather than with the one stage that produced a
usable feature. The pipeline is demonstrably capable of detecting signal when
it is present — the Stage 1 leakage tripwire scores exactly 1.0000, and the
Stage 3 placebo collapses to zero — which is what makes the nulls credible.

| Stage | File | Builds | Depends on |
|---|---|---|---|
| 1 | [model-01-market-flow.md](model-01-market-flow.md) | Market-level supervised flow model | `flow_features`, `ml_dataset`, `participant_oi` |
| 1b | [model-02-buildup-quadrants.md](model-02-buildup-quadrants.md) | Direction features (buildup quadrants) | bhavcopy parquet cache |
| 2 | [model-03-regime-hmm.md](model-03-regime-hmm.md) | Per-symbol latent regime model | Stage 1b, `flow_features`, `ml_dataset` |
| 3 | [model-04-attribution-bridge.md](model-04-attribution-bridge.md) | Per-symbol flow attribution | Stages 1, 1b, 2 |

Build in that order. Each stage is independently useful and independently
falsifiable; stage 3 is the research contribution and the one most likely to
fail, which is precisely why it goes last.

---

## 1. The problem this is solving

The project's goal is **detecting institutional positioning, not predicting
price**. Everything built so far is evaluated against `next_day_abs_return` or
`next_day_direction`, which are price targets. That is a proxy, and it has been
quietly steering the work off-goal.

The central difficulty is a **label mismatch**:

```
What we want to predict        What we can actually observe
─────────────────────────      ────────────────────────────────────
Institutional flow in          NSE participant-wise OI:
RELIANCE on 2026-08-28         FII / DII / Pro / Client net positions
                               for the WHOLE MARKET, one row per day
   ↓ per-symbol                   ↓ market-wide
   217 symbols x 1,254 days       1,258 days, no symbol dimension
   = 212,441 rows                 = 1,258 rows
   NO LABEL                       LABEL EXISTS
```

There is no per-symbol institutional-flow label anywhere in the data, and none
can be bought within this project's means. Everything below is a response to
that single fact:

- **Stage 1** works where the label *does* exist (market level) and asks
  whether our features predict it at all. If they cannot predict flow in
  aggregate, no per-symbol story built on them is credible.
- **Stage 2** gives up on labels entirely and uses an unsupervised latent-state
  model per symbol, validated by whether its states aggregate into something
  that tracks the market-level label.
- **Stage 3** bridges the two: aggregate labels + per-symbol instances is
  formally **Learning from Label Proportions**, and that framing is what makes
  per-symbol inference defensible rather than hand-waved.

### Why there is no MLP here

Stated plainly so nobody "improves" the plan later: **a neural network is the
wrong tool at every level of this project.**

- Market level: 1,252 usable rows, ~20 features. A 3-layer MLP has more
  parameters than training examples.
- Per-symbol level: 212,441 rows but *no label*. There is nothing to
  backpropagate against.

Regularized linear models, gradient-boosted trees, and an HMM are the honest
choices for these data sizes. Do not add a deep model to make the project look
more sophisticated; at n=1,252 it will overfit, and the write-up will have to
explain why the fancy model lost to Ridge.

### Expected effect sizes — calibrate now

Measured partial correlations at market level (controlling the expiry
calendar) were **|r| ≈ 0.17**. That translates to roughly **AUC 0.55–0.60**.

That is a real, publishable, defensible result for a research project. It is
**not** a trading edge, and the write-up must not imply it is. A stage that
reports AUC 0.85 has a leak; find it before celebrating.

---

## 2. State of the repository (verified 2026-09-04)

### Already built — reuse, do not reinvent

| Module | Provides | Notes |
|---|---|---|
| `ml/splits.py` | `walk_forward_splits(dates, n_splits, embargo_days, min_train_dates)`, `train_valid_test(frame)`, `inner_holdout(train, frac, embargo_days)` | Purged walk-forward with embargo is **done**. Splits on unique dates, never rows. |
| `ml/metrics.py` | `daily_rank_ic`, `icir`, `decile_spread`, `summarize`, `_ic_t_stat` | **Cross-sectional only.** Does not apply to Stage 1 (no cross-section in a market-level series). |
| `ml/baselines.py` | `constant_up_scores`, `random_uniform_scores`, `reversal_scores`, `raw_ret_1d_scores`, `score_baselines` | The **pattern** to copy, especially the leakage tripwire. |
| `features/flow.py` | `detect_outlier_strikes`, `futures_basis`, `build_flow_features`, `write_flow_features`, `export_flow_parquet`, `run_flow_features`, `load_spot_frame` | Row-group streaming over the bhavcopy cache; `_robust_scale` conventions. |
| `data/participant_oi.py` | `add_net_positions(df)` | Derives `net_*` levels and `d_net_*` flows. **This produces the Stage 1 label.** |

### Defect to fix before Stage 1

`src/mktmtm/ml/baselines.py:116` does:

```python
from mktmtm.ml.features import load_training_frame  # Phase 2 loader
```

**`src/mktmtm/ml/features.py` does not exist on this branch.** Calling
`build_baselines_table()` raises `ImportError` today. The module lives on the
unmerged branch `model/xgboost-direction`, which also carries
`train_direction.py`, `audit_direction.py` and `promote.py`.

Inspect it before writing any loader of your own:

```bash
git show model/xgboost-direction:src/mktmtm/ml/features.py
git show model/xgboost-direction:src/mktmtm/ml/train_direction.py
```

It already solves problems Stage 1 will hit — a pinned feature contract, a
quality gate (`GATE`), missingness indicators, rank normalization, and a null-rate
threshold. **Port the patterns; do not merge the branch wholesale** (it is built
for the per-symbol direction task, which is the price-prediction proxy this plan
is moving away from). Either restore a `features.py` that serves both, or give
Stage 1 its own loader and leave a comment in `baselines.py` recording that its
import is broken until the branch is reconciled.

### Dependencies

Installed: `scikit-learn` 1.9.0, `xgboost` 3.4.1, `scipy` 1.18.1, `pandas` 3.0.5,
`pyarrow`, `numpy`.

**Not installed, needed by Stage 2:** `hmmlearn`. Add to `pyproject.toml`
`dependencies` as `"hmmlearn>=0.3.3"` and run `uv sync`. Do not substitute
`sklearn.mixture.GaussianMixture` — it has no transition matrix, and the
temporal persistence of states is the entire hypothesis being tested.

### Data on hand

| Table | Rows | Dates | Symbols | Range |
|---|---|---|---|---|
| `ml_dataset` | 212,441 | 1,254 | 217 | 2021-08-02 → 2026-08-28 |
| `flow_features` | 212,441 | 1,254 | 217 | 2021-08-02 → 2026-08-28 |
| `participant_oi` | 1,258 x 5 types | 1,258 | — (market-wide) | 2021-08-02 → 2026-09-03 |

`ml_dataset` and `flow_features` are an exact 1:1 join on `(symbol, date)`.
Parquet mirrors live in `data/processed/`.

---

## 3. Shared conventions — every stage obeys these

These are not style preferences. Each one encodes a bug that already happened
in this repo; see `CLAUDE.md` and `docs/data/`.

1. **No look-ahead, enforced not assumed.** Every feature at date `t` uses only
   data timestamped `≤ t`. Every normalization statistic (mean, std, quantile,
   MAD) is computed on an expanding window ending at `t-1` — never on the full
   history. Where a stage z-scores per symbol, use the established convention:
   `groupby(symbol).expanding().mean().shift(1)`, minimum 60 observations,
   `NULL` before that.

2. **Row order is load-bearing.** Every `diff()`, `pct_change()` and `shift()`
   in this codebase is a `groupby` operation whose correctness depends on a
   prior `sort_values`. Sort explicitly before every one of them, even when the
   frame "looks" sorted. `hmmlearn`'s `lengths=` argument in Stage 2 is
   especially unforgiving here.

3. **`NULL` never `0`.** "Not computable" is `None` → SQL `NULL`. A sentinel
   `0.0` or `500.0` corrupted every derived vol feature once already
   (`docs/data/iv-integrity-audit.md`). This applies to model outputs too: a
   symbol-day the model cannot score gets `NULL`, not a neutral 0.5.

4. **Two price series, two purposes.** `underlying_price` is raw/unadjusted —
   the only correct basis for strikes, IV, Greeks, moneyness and basis.
   `underlying_price_adj` is adjusted — the only correct basis for returns,
   momentum and labels. Mixing them corrupted ~10% of IV rows.

5. **Open interest is in SHARES.** Bhavcopy `OPEN_INT` / `CHG_IN_OI` are share
   counts; the lot size **divides** to recover contracts, it never multiplies.
   Notional is `oi × spot`, with no lot factor. `participant_oi`, by contrast,
   reports **contracts**. Converting between the two is `OPEN_INT / lot`.

6. **Splits come from `ml/splits.py`.** Do not write a new splitter. Use
   `walk_forward_splits(dates, n_splits=5, embargo_days=1)` for model
   selection and `inner_holdout` for early stopping. Early-stopping on the
   same rows you then report a score from makes the score meaningless.

7. **Every model reports against baselines.** A stage that reports only its own
   metric is not finished. The mandatory baselines are named per stage; the
   **calendar-only** baseline is the one that matters most, because the expiry
   cycle mechanically explains roughly half the raw next-day flow correlation.

8. **Every stage ships a leakage tripwire.** A deliberately-leaking variant
   that *should* score near-perfectly. If the tripwire does not massively
   outperform the real model, the harness is broken — that is a stronger signal
   than any passing test.

9. **Artifacts go to `models/evaluation/*.json`.** Follow the existing shape:
   metrics plus provenance (split name, date range, row and date counts,
   feature list, git SHA). Fitted models go to `models/candidates/`.

10. **Quality bar.** `uv run mypy src/mktmtm` (strict) and
    `uv run ruff check src/mktmtm` (line-length 100) must pass. Unit tests go in
    `tests/unit/test_<module>.py`. Integration tests needing PostgreSQL get
    `@pytest.mark.integration` and must skip cleanly without credentials.

11. **Configuration via `Settings`.** Never read `PG_*` or other env vars
    directly; go through the pydantic singleton in `config/settings.py`.

---

## 4. Inter-stage contracts

Build these interfaces exactly; downstream stages are specified against them.

### Stage 1 produces

```python
# src/mktmtm/ml/market_flow.py
def build_market_frame(conn, *, targets: Sequence[str]) -> pd.DataFrame:
    """One row per trading date. Columns:
        date            datetime64[us]
        <feature cols>  market aggregates, all observable at end of day t
        y_<target>      the LABEL: value at t+1, NULL on the last date
    """
```

Written to `models/evaluation/market_flow_<target>.json`, and the assembled
frame cached at `data/processed/market_flow.parquet`.

### Stage 1b produces

New columns appended to the existing **`flow_features`** table (same
`(symbol, date)` grain — do not create a new table). Names are fixed in
[model-02](model-02-buildup-quadrants.md) §3.

### Stage 2 produces

```sql
CREATE TABLE flow_regimes (
    symbol      VARCHAR(32) NOT NULL,
    date        DATE        NOT NULL,
    state       SMALLINT,             -- Viterbi-decoded state, NULL if unscored
    state_prob  DOUBLE PRECISION,     -- posterior of the decoded state
    p_state_0   DOUBLE PRECISION,     -- full posterior, one column per state
    ...
    PRIMARY KEY (symbol, date)
);
```

### Stage 3 consumes all three and produces

A per-symbol-day institutional-flow score, written to `flow_attribution`,
whose date-level weighted aggregate reconstructs the `participant_oi` series.

---

## 5. What "done" looks like

A stage is finished when **all** of these hold:

- [ ] Code passes `mypy --strict` and `ruff`.
- [ ] Unit tests cover the pure functions, including at least one test that
      asserts the *absence* of look-ahead.
- [ ] A CLI command exists (`uv run mktmtm <stage-command>`), following the
      `flow-features` command in `cli.py` as the pattern.
- [ ] The leakage tripwire fires as expected.
- [ ] Results are written to `models/evaluation/` with provenance.
- [ ] A results section is appended to that stage's doc file — **including
      negative results.** A stage that finds no signal is a finding, and this
      project has already had two of them (pooled GEX H1 was a size confound;
      `outlier_rate` flips sign under the calendar control). Write them down.

---

## 6. Related documents

- [flow-features.md](flow-features.md) — the outlier-strike and futures-basis
  features these stages consume, including how the thresholds were measured
  and the OI-units defect.
- [gex-dealer-positioning.md](gex-dealer-positioning.md) — GEX construction and
  the measured `DEALER_SIGN` inversion versus US markets.
- [../architecture/](../architecture/) — full data, pipeline and storage
  reference; start at its `README.md`.
- [../data/ml-dataset-report.md](../data/ml-dataset-report.md) — feature
  analysis and the look-ahead incident.
