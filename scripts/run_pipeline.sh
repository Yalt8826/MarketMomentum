#!/usr/bin/env bash
# Full pipeline, in dependency order, for an unattended run.
#
#   ./scripts/run_pipeline.sh              # everything, including the 3-5h ETL
#   ./scripts/run_pipeline.sh --no-etl     # skip fetch+ETL, rebuild derived only
#   ./scripts/run_pipeline.sh --models     # modelling stages only
#
# Every stage is timed and logged. The run stops at the first failure rather
# than carrying a broken table forward into six hours of downstream work.
set -euo pipefail

cd "$(dirname "$0")/.."
LOG_DIR="logs"; mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/pipeline-$(date +%Y%m%d-%H%M%S).log"
export MKTMTM_NO_PROGRESS=1     # log lines, not rich bars — stdout is redirected

MODE="${1:-all}"

log()  { printf '\n\033[1m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*" | tee -a "$RUN_LOG"; }
fail() { printf '\n\033[31m[%s] FAILED: %s\033[0m\n' "$(date +%H:%M:%S)" "$*" | tee -a "$RUN_LOG"; exit 1; }

stage() {
  local name="$1"; shift
  local start; start=$(date +%s)
  log "▶ $name"
  if ! "$@" >>"$RUN_LOG" 2>&1; then
    fail "$name — see $RUN_LOG"
  fi
  local secs=$(( $(date +%s) - start ))
  log "✓ $name  (${secs}s)"
}

log "pipeline run · mode=$MODE · log=$RUN_LOG"

# ── schema. Idempotent; safe before every run. ─────────────────────────────
stage "schema bootstrap" psql "${PG_DSN:?PG_DSN must be set}" -v ON_ERROR_STOP=1 \
      -f src/mktmtm/storage/sql/init_db.sql

if [ "$MODE" != "--models" ]; then
  if [ "$MODE" != "--no-etl" ]; then
    # ── acquisition ──────────────────────────────────────────────────────
    stage "fetch bhavcopy"        uv run mktmtm fetch-bhavcopy
    stage "fetch yahoo"           uv run mktmtm fetch-yahoo
    stage "fetch participant OI"  uv run mktmtm fetch-participant-oi
    stage "fetch deals"           uv run mktmtm fetch-deals
    stage "fetch shareholding"    uv run mktmtm fetch-shareholding

    # preparse MUST follow every bhavcopy fetch — the ETL reads the parquet
    # cache, not the raw files, and a stale cache silently omits new days.
    stage "preparse bhavcopy"     uv run mktmtm preparse-bhavcopy

    # ── the long one: 3-5h for five years ────────────────────────────────
    stage "ETL"                   uv run mktmtm etl
  fi

  stage "make dataset"            uv run mktmtm make-dataset
  stage "flow features"           uv run mktmtm flow-features

  # Gate before any modelling. Both data defects this project has hit were
  # silent, so this is the checkpoint that stops a bad table reaching a model.
  stage "health gate"             uv run python scripts/verify_db_health.py
fi

# ── modelling ──────────────────────────────────────────────────────────────
stage "Stage 1  market flow"      uv run mktmtm market-flow
stage "Stage 2  flow regimes"     uv run mktmtm flow-regimes
stage "Stage 3  attribution"      uv run mktmtm flow-attribution
stage "Stage 4  flow momentum"    uv run mktmtm flow-momentum
stage "Stage 5  accumulation"     uv run mktmtm accumulation

# The nowcast writes a DATED record and refuses to overwrite an existing one,
# so this creates a new file per run day and never touches a frozen forecast.
NOWCAST_OUT="docs/results/nowcast-$(date +%Y-%m-%d).md"
if [ -e "$NOWCAST_OUT" ]; then
  log "· nowcast record for today already exists, leaving it frozen: $NOWCAST_OUT"
else
  stage "nowcast"                 uv run mktmtm nowcast --out "$NOWCAST_OUT"
fi

log "pipeline complete · full log: $RUN_LOG"
