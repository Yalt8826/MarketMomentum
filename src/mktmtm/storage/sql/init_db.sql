-- ============================================================================
--  MktMtm — consolidated database schema bootstrap (idempotent)
--
--  Creates the full MktMtm PostgreSQL schema: the option_metrics fact table,
--  the backtest_results fact table, and the symbol_data dimension table with
--  its 222 seed rows (5 indices + 217 individual securities).
--
--  Usage (psql):
--      psql "$PG_DSN" -f src/mktmtm/storage/sql/init_db.sql
--
--  Safe to re-run: every table uses CREATE TABLE IF NOT EXISTS, every index
--  CREATE INDEX IF NOT EXISTS, the extra column ALTER ... ADD COLUMN IF NOT
--  EXISTS, and the symbol seeds INSERT ... ON CONFLICT DO NOTHING.
--
--  The three files below remain the individual source of truth; this runner
--  only ties them together in dependency order. \ir resolves paths relative
--  to this file, so the script works from any working directory.
-- ============================================================================

\ir init_option_metrics.sql
\ir backtest_results.sql
\ir symbol_data.sql
\ir init_ml_dataset.sql
\ir init_participant_oi.sql
\ir init_deals.sql
\ir init_shareholding.sql
\ir init_flow_features.sql
\ir init_flow_regimes.sql
\ir init_flow_attribution.sql

-- ============================================================================
--  Done. Sanity check:
--      SELECT count(*) FROM symbol_data;                       -- expect 222
--      \d option_metrics
--      \d backtest_results
-- ============================================================================
