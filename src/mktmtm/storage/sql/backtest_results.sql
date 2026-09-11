-- backtest_results — one row per (variant, symbol, year, month).
--
-- `variant` is load-bearing: without it a parameter sweep overwrites itself and
-- Phase 1 cannot compare a filtered run against the unfiltered baseline. It was
-- added when the table was still empty, so no migration of real rows was needed.
--
-- The money columns are kept SEPARATE (gross / costs / slippage / net) rather
-- than collapsed into one. When a result comes out marginal, "which component
-- killed it" has to be answerable, and a single net figure cannot answer it.
-- See docs/trading/implementation/03-phase-0-economics.md.

CREATE TABLE IF NOT EXISTS backtest_results (
    variant TEXT NOT NULL DEFAULT 'base',
    symbol TEXT NOT NULL,
    entry_date DATE,
    exit_date DATE,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,

    -- Strategy mechanics, in points per share (what strangle.py has always held)
    entry_credit DOUBLE PRECISION,
    exit_cost DOUBLE PRECISION,
    pnl_points DOUBLE PRECISION,
    hedge_pnl_points DOUBLE PRECISION,
    skipped_reason TEXT,
    call_entry_strike DOUBLE PRECISION,
    put_entry_strike DOUBLE PRECISION,
    call_entry_delta DOUBLE PRECISION,
    put_entry_delta DOUBLE PRECISION,
    call_entry_price DOUBLE PRECISION,
    put_entry_price DOUBLE PRECISION,
    call_exit_price DOUBLE PRECISION,
    put_exit_price DOUBLE PRECISION,

    -- Economics (Phase 0). NULL until the trade has been priced.
    call_entry_volume BIGINT,      -- Gate 0 disclosure: a strike that never
    put_entry_volume BIGINT,       -- traded makes the fill assumption fiction
    lot_size INTEGER,              -- in force on entry_date, never the latest
    n_orders INTEGER,              -- 4 + 2 * n_adjustments
    n_adjustments INTEGER,
    entry_spot DOUBLE PRECISION,   -- margin basis
    gross_pnl_rupees DOUBLE PRECISION,
    costs_rupees DOUBLE PRECISION,
    slippage_rupees DOUBLE PRECISION,
    net_pnl_rupees DOUBLE PRECISION,
    margin_rupees DOUBLE PRECISION,
    return_on_margin DOUBLE PRECISION,
    cost_breakdown JSONB,          -- itemised: brokerage, stt, exchange, gst, ...

    -- Provenance: a result is unreproducible without the assumptions behind it.
    rates_effective_from DATE,     -- which CostRates set priced this trade
    slippage_fraction DOUBLE PRECISION,
    margin_pct DOUBLE PRECISION,

    PRIMARY KEY (variant, symbol, year, month)
);

CREATE INDEX IF NOT EXISTS idx_backtest_results_symbol_date
    ON backtest_results (symbol, entry_date);
CREATE INDEX IF NOT EXISTS idx_backtest_results_pnl
    ON backtest_results (pnl_points);
CREATE INDEX IF NOT EXISTS idx_backtest_results_variant
    ON backtest_results (variant, year, month);

-- ── Migration for an existing table ────────────────────────────────────────
-- Idempotent, and safe on the empty table this project actually has.

ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS variant TEXT NOT NULL DEFAULT 'base';
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS hedge_pnl_points DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS call_entry_volume BIGINT;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS put_entry_volume BIGINT;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS lot_size INTEGER;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS n_orders INTEGER;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS n_adjustments INTEGER;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS entry_spot DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS gross_pnl_rupees DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS costs_rupees DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS slippage_rupees DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS net_pnl_rupees DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS margin_rupees DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS return_on_margin DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS cost_breakdown JSONB;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS rates_effective_from DATE;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS slippage_fraction DOUBLE PRECISION;
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS margin_pct DOUBLE PRECISION;

-- A month with no trading days has no entry_date, and the original table
-- declared that column NOT NULL — so every such row was REJECTED on insert.
-- The failure was logged per row and swallowed (the writer logs and keeps
-- going), so 3,308 months simply never appeared in the table and the skip
-- taxonomy silently under-reported. CREATE TABLE IF NOT EXISTS cannot repair
-- this on an existing table; the constraint has to be dropped explicitly.
ALTER TABLE backtest_results ALTER COLUMN entry_date DROP NOT NULL;

-- Rebuild the primary key to include `variant`, if it does not already.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'backtest_results'
          AND c.contype = 'p'
          AND array_length(c.conkey, 1) = 3
    ) THEN
        ALTER TABLE backtest_results DROP CONSTRAINT backtest_results_pkey;
        ALTER TABLE backtest_results
            ADD CONSTRAINT backtest_results_pkey PRIMARY KEY (variant, symbol, year, month);
    END IF;
END $$;
