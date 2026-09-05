# Runbook — running the pipeline end to end

```bash
uv run mktmtm run-pipeline                       # everything, incl. the 3-5h ETL
uv run mktmtm run-pipeline --mode no-etl         # rebuild derived tables only
uv run mktmtm run-pipeline --mode models         # modelling stages only
uv run mktmtm run-pipeline --only "flow features" --only accumulation
```

Every stage is timed, stops the run on failure, and renders **its own progress
bar**. A summary table of per-stage timings prints at the end.

`--only` matches on substrings of stage names and is how you resume after a
failure without repeating hours of upstream work.

**For an overnight run, redirect to a file:**

```bash
uv run mktmtm run-pipeline > logs/tonight.log 2>&1 &
tail -f logs/tonight.log
```

Progress automatically becomes log lines rather than rich bars when stderr is
not a TTY, so a redirected log stays readable. `MKTMTM_NO_PROGRESS=1` forces
that behaviour explicitly.

---

## Order and timings

Measured on this machine, five years of data.

| # | Stage | Command | Time | Notes |
|---|---|---|---|---|
| 0 | Schema | `init_db.sql` | <1s | Idempotent, 10 DDLs |
| 1 | Bhavcopy | `fetch-bhavcopy` | mins | Incremental |
| 2 | Yahoo spot | `fetch-yahoo` | mins | |
| 3 | Participant OI | `fetch-participant-oi` | mins | |
| 4 | Deals | `fetch-deals` | ~10s | Latest day only — see [deals-label.md](implementation/deals-label.md) |
| 5 | Shareholding | `fetch-shareholding` | ~35m first run, ~30s cached | Gzip-cached under `data/raw/shareholding/` |
| 6 | **Preparse** | `preparse-bhavcopy` | mins | **Must follow every bhavcopy fetch** |
| 7 | **ETL** | `etl` | **3–5 h** | The long one |
| 8 | Dataset | `make-dataset` | mins | |
| 9 | Flow features | `flow-features` | **7m 20s** | |
| 10 | **Health gate** | `verify_db_health.py` | ~12s | **Stops here on failure** |
| 11 | Stage 1 | `market-flow` | ~4m | |
| 12 | Stage 2 | `flow-regimes` | ~11m | HMM, 36 EM fits |
| 13 | Stage 3 | `flow-attribution` | ~25s | |
| 14 | Stage 4 | `flow-momentum` | ~35s | |
| 15 | Stage 5 | `accumulation` | ~60s | |
| 16 | Nowcast | `nowcast --out …` | ~30s | Dated filename, refuses to overwrite |

**Without the ETL: ~25 minutes.** With it: **4–6 hours.**

---

## Three things that will bite

**Preparse after every bhavcopy fetch.** The ETL reads
`data/processed/bhavcopy_cache.parquet`, not the raw files. A stale cache
silently omits the new days — no error, just missing data. Step 6 exists for
this and the script always runs it.

**The health gate is a real gate.** It exits non-zero and stops the run. Both
data defects this project has hit were *silent*, so the gate sits between the
data and every model deliberately. If it fails, fix the data — do not skip it.

**The nowcast record is frozen.** `nowcast` writes a dated filename and
`write_nowcast_record` **raises rather than overwriting** an existing one. A
forecast made before the outcome, silently replaced by one made after it under
the same filename, would be worse than no forecast at all. Re-running on a new
day creates a new record; re-running on the same day leaves the existing one
alone.

---

## What to check afterwards

```bash
uv run python scripts/verify_db_health.py     # all 7 sections should be ✓
uv run pytest -m "not integration"            # 409 tests
```

Expected row counts after a full run (they grow with new trading days):

| Table | Rows |
|---|---|
| `option_metrics` / `ml_dataset` / `flow_features` | ~212k, all three **equal** |
| `flow_regimes` | ~162k |
| `flow_attribution` | ~519k (3 participants × scored rows) |
| `shareholding` | ~4.4k |
| `participant_oi` | ~6.3k |

`flow_features` must equal `ml_dataset` exactly — the health gate checks it,
because every evaluation depends on that 1:1 join.

---

## If a stage fails

The log names the stage. Re-run from there rather than from the top:

```bash
uv run mktmtm run-pipeline --only "<failed stage>"   # re-run just that stage
uv run mktmtm run-pipeline --mode models            # then resume from modelling
```

Most stages are idempotent UPSERTs, so re-running one is safe. The two
exceptions are the ETL (long, but still idempotent) and the nowcast (refuses to
overwrite, by design).

**Acquisition stages can fail on NSE rate limiting**, which surfaces as an HTTP
error rather than empty data — `fetch-participant-oi` in particular reports a
typed outcome per day so a throttled run can never be mistaken for a run of
market holidays. Wait and re-run that stage.

---

## Scoring the frozen nowcast

The one dated commitment this project has.
[`nowcast-2026-08-28.md`](results/nowcast-2026-08-28.md) predicts mutual-fund
accumulation for the quarter ending **2026-09-30**, checkable once the filing
publishes around **2026-11-14**.

```bash
uv run mktmtm run-pipeline                             # catch data up first
uv run mktmtm accumulation --holder mf --window contemporaneous
```

Then rank-correlate the record's `score` column against the realised
`mf_shares` change. Expect ≈ **+0.11**; the null is 0. Apply the same
corporate-action filter the evaluation uses, or a bonus issue will read as a
spectacular failure.

**A materially negative result is evidence against Stage 5**, and should be
reported as such. That is the point of having written it down in advance.
