-- NSE participant-wise F&O open interest (Client / DII / FII / Pro / TOTAL).
--
-- One row per (trade date, participant category); ~5 rows per trading day.
-- This is MARKET-WIDE data, not per-symbol: joined to ml_dataset on date alone
-- it is constant within a date and therefore contributes exactly zero to any
-- cross-sectional rank IC. Use it for dealer-sign validation and market-level
-- regime targets, not as a per-symbol feature.
--
-- Counts are contracts (not lots or notional) and fit comfortably in BIGINT.
-- Only NSE's raw columns are stored; net positions and day-over-day flows are
-- derived on read (data.participant_oi.add_net_positions) so this table stays
-- a faithful copy of the source file.
--
-- Idempotent: safe to re-run via init_db.sql.
CREATE TABLE IF NOT EXISTS participant_oi (
    date                     DATE        NOT NULL,
    client_type              VARCHAR(16) NOT NULL,

    future_index_long        BIGINT,
    future_index_short       BIGINT,
    future_stock_long        BIGINT,
    future_stock_short       BIGINT,

    option_index_call_long   BIGINT,
    option_index_put_long    BIGINT,
    option_index_call_short  BIGINT,
    option_index_put_short   BIGINT,

    option_stock_call_long   BIGINT,
    option_stock_put_long    BIGINT,
    option_stock_call_short  BIGINT,
    option_stock_put_short   BIGINT,

    total_long_contracts     BIGINT,
    total_short_contracts    BIGINT,

    PRIMARY KEY (date, client_type)
);

-- The common access pattern is one participant's series through time
-- (e.g. the Pro book, for validating DEALER_SIGN).
CREATE INDEX IF NOT EXISTS idx_participant_oi_client_date
    ON participant_oi (client_type, date);
