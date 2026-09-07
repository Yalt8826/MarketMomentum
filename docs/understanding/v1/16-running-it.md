# 16 — Running It Yourself

[← Bugs and lessons](15-bugs-and-lessons.md) · [Index](README.md) · Next: [Glossary →](17-glossary.md)

A beginner's operating guide. The authoritative version, with more operational
detail, is [`docs/runbook.md`](../../runbook.md); this one explains *why* each
step exists.

---

## 1. What you need

| | |
|---|---|
| **Python** | 3.12, managed with [`uv`](https://docs.astral.sh/uv/) |
| **PostgreSQL** | A running instance you can write to |
| **Disk** | ~45 GB — 20 GB of raw files, 25 GB of database |
| **RAM** | 14 GiB is enough *in incremental mode*. A full rebuild needs more. |
| **Time** | 4–6 hours with the ETL; ~25 minutes without it |

Setup:

```bash
uv sync                                                # install dependencies
psql "$PG_DSN" -f src/mktmtm/storage/sql/init_db.sql   # create the schema
```

The schema script is idempotent and safe to re-run.

> **Configuration note.** Everything goes through a single pydantic `Settings`
> object (`config/settings.py`) — never bare environment reads. `PG_DSN`
> overrides the individual `PG_*` fields.

---

## 2. The one command

```bash
uv run mktmtm run-pipeline
```

Seventeen stages, each timed, each with its own progress bar, stopping at the
first failure, printing a summary table of timings at the end.

Variants:

```bash
uv run mktmtm run-pipeline --mode no-etl     # rebuild derived tables only (~25 min)
uv run mktmtm run-pipeline --mode models     # modelling stages only
uv run mktmtm run-pipeline --only "flow features" --only accumulation
```

`--only` matches on substrings of stage names, and is how you resume after a
failure without repeating hours of upstream work.

**For an overnight run, redirect to a file:**

```bash
uv run mktmtm run-pipeline > logs/tonight.log 2>&1 &
tail -f logs/tonight.log
```

Progress automatically becomes plain log lines rather than rich bars when output
is not a terminal, so a redirected log stays readable. `MKTMTM_NO_PROGRESS=1`
forces that explicitly.

---

## 3. What each stage does, and how long it takes

Measured on the development machine, five years of data.

| # | Stage | Command | Time | Why it exists |
|---|---|---|---|---|
| 0 | Schema | `init_db.sql` | <1s | Ten DDLs, idempotent |
| 1 | Bhavcopy | `fetch-bhavcopy` | mins | Every contract, every day |
| 2 | Yahoo spot | `fetch-yahoo` | mins | The underlying prices |
| 3 | Participant OI | `fetch-participant-oi` | mins | The only named daily data |
| 4 | Deals | `fetch-deals` | ~10s | Latest day only — structurally limited |
| 5 | Shareholding | `fetch-shareholding` | **~35m first run**, ~30s cached | **The label** |
| 6 | **Preparse** | `preparse-bhavcopy` | mins | **Must follow every bhavcopy fetch** |
| 7 | **ETL** | `etl` | **~1 h incremental** | IV, Greeks, chain snapshots |
| 8 | Dataset | `make-dataset` | mins | The training matrix |
| 9 | Flow features | `flow-features` | **7m 20s** | Outliers, basis, quadrants |
| 10 | **Health gate** | `verify_db_health.py` | ~12s | **Stops the run on failure** |
| 11 | Stage 1 | `market-flow` | ~4m | |
| 12 | Stage 2 | `flow-regimes` | ~11m | 36 HMM fits |
| 13 | Stage 3 | `flow-attribution` | ~25s | |
| 14 | Stage 4 | `flow-momentum` | ~35s | |
| 15 | Stage 5 | `accumulation` | ~60s | **The result** |
| 16 | Nowcast | `nowcast` | ~30s | Dated file; refuses to overwrite |

---

## 4. Three things that will bite you

### Preparse after every bhavcopy fetch

The ETL reads `data/processed/bhavcopy_cache.parquet`, **not** the raw files. A
stale cache silently omits the new days — no error, just missing data. Stage 6
exists for this and `run-pipeline` always runs it.

### The health gate is a real gate

It exits non-zero and stops the run. Every data defect this project has hit was
*silent*, so the gate sits deliberately between the data and every model. **If it
fails, fix the data — do not skip it.**

### The nowcast record is frozen

`nowcast` writes a dated filename and **raises rather than overwriting** an
existing one. A forecast made before an outcome, silently replaced by one made
after it under the same filename, would be worse than no forecast at all.
Re-running on a new day creates a new record; re-running on the same day leaves
the existing one alone.

---

## 5. The ETL: incremental vs full

```bash
uv run mktmtm etl                      # incremental (the default)
uv run mktmtm etl --since 2026-01-01   # reprocess AND rewrite from a date
uv run mktmtm etl --full               # rebuild everything — needs the memory
```

| | Full | Incremental |
|---|---|---|
| Cache rows loaded | 60,768,679 | **10,450,235** |
| Cache load time | ~1m 45s | **7s** |
| Peak memory | ~5–9 GB (**OOM on 14 GiB**) | **~2.5 GB** |
| Runtime | 10–12 h | **~1 h** |

A symbol more than 400 days behind needs `--since` or `--full` to backfill.

---

## 6. Checking it worked

```bash
uv run python scripts/verify_db_health.py     # all 7 sections should be ✓
uv run pytest -m "not integration"            # 463 tests, no database needed
uv run python scripts/decile_spread.py        # restate Stage 5 as a decile spread
```

Expected row counts (they grow with each new trading day):

| Table | Rows |
|---|---|
| `option_metrics` / `ml_dataset` / `flow_features` | ~212k, all three **exactly equal** |
| `flow_regimes` | ~162k |
| `flow_attribution` | ~519k |
| `shareholding` | ~4.4k |
| `participant_oi` | ~6.3k |

`flow_features` **must** equal `ml_dataset` exactly. The health gate checks it,
because every evaluation depends on that 1:1 join.

---

## 7. Smoke runs — trying things without waiting hours

```bash
uv run mktmtm make-dataset --limit-symbols RELIANCE,TCS --limit-dates 30
uv run mktmtm process --limit-days 5
uv run mktmtm backtest --symbols RELIANCE --years 2024 --months 1
```

---

## 8. When something fails

The log names the stage. Re-run from there rather than from the top:

```bash
uv run mktmtm run-pipeline --only "<failed stage>"
uv run mktmtm run-pipeline --mode models          # then resume from modelling
```

Most stages are idempotent UPSERTs, so re-running one is safe. The two exceptions
are the ETL (long, but still idempotent) and the nowcast (refuses to overwrite,
by design).

**Acquisition stages can fail on NSE rate limiting**, which surfaces as an HTTP
error rather than as empty data — `fetch-participant-oi` in particular reports a
typed outcome per day, so a throttled run can never be mistaken for a run of
market holidays. Wait and re-run that stage.

---

## 9. Development commands

```bash
uv run pytest                                     # full suite
uv run pytest -m "not integration"                # skip tests needing PostgreSQL
uv run pytest tests/unit/test_pricing.py -v       # one file
uv run mypy src/mktmtm                            # strict type checking
uv run ruff check src/mktmtm                      # lint
```

---

## 10. Scoring the frozen nowcast — the one dated commitment

[`docs/results/nowcast-2026-08-28.md`](../../results/nowcast-2026-08-28.md)
predicts mutual-fund accumulation for the quarter ending **2026-09-30**,
checkable once the filing publishes around **2026-11-14**.

```bash
uv run mktmtm run-pipeline                                  # catch the data up
uv run mktmtm accumulation --holder mf --window contemporaneous
```

Then rank-correlate the record's `score` column against the realised `mf_shares`
change. **Expect ≈ +0.11. The null is 0.**

Apply the same corporate-action filter the evaluation uses, or a bonus issue will
read as a spectacular failure.

> **A materially negative result is evidence against Stage 5**, and should be
> reported as such. That is the entire point of having written it down in
> advance.

[← Bugs and lessons](15-bugs-and-lessons.md) · [Index](README.md) · Next: [Glossary →](17-glossary.md)
