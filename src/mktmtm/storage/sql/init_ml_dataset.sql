-- ===================================================
--  Table: ml_dataset
--  Purpose: ML training matrix — one row per (symbol, trading day) with
--           features derived from option_metrics plus the supervised targets
--           (next-day return / direction). Populated by `mktmtm make-dataset`.
--
--  Targets are NULL on the most recent day of each symbol (no "tomorrow"
--  yet) — those rows are future-inference rows, never training rows.
--
--  Column order mirrors src/mktmtm/ml/dataset.py DATASET_COLUMNS.
-- ===================================================

CREATE TABLE IF NOT EXISTS ml_dataset (
    symbol               TEXT NOT NULL,
    date                 DATE NOT NULL,
    underlying_price     DOUBLE PRECISION,
    interest_rate        DOUBLE PRECISION,
    rv_yz                DOUBLE PRECISION,
    ce_iv_30             DOUBLE PRECISION,
    pe_iv_30             DOUBLE PRECISION,
    ce_iv_60             DOUBLE PRECISION,
    pe_iv_60             DOUBLE PRECISION,
    ce_iv_90             DOUBLE PRECISION,
    pe_iv_90             DOUBLE PRECISION,
    ce_ivp               DOUBLE PRECISION,
    pe_ivp               DOUBLE PRECISION,
    ce_ivr               DOUBLE PRECISION,
    pe_ivr               DOUBLE PRECISION,
    ce_delta             DOUBLE PRECISION,
    pe_delta             DOUBLE PRECISION,
    ce_gamma             DOUBLE PRECISION,
    pe_gamma             DOUBLE PRECISION,
    ce_theta             DOUBLE PRECISION,
    pe_theta             DOUBLE PRECISION,
    ce_vega              DOUBLE PRECISION,
    pe_vega              DOUBLE PRECISION,
    ce_rho               DOUBLE PRECISION,
    pe_rho               DOUBLE PRECISION,
    ce_close             DOUBLE PRECISION,
    pe_close             DOUBLE PRECISION,
    ce_volume            DOUBLE PRECISION,
    pe_volume            DOUBLE PRECISION,
    ce_oi                DOUBLE PRECISION,
    pe_oi                DOUBLE PRECISION,
    ret_1d               DOUBLE PRECISION,
    ret_5d               DOUBLE PRECISION,
    ret_10d              DOUBLE PRECISION,
    ret_20d              DOUBLE PRECISION,
    iv_rv_spread         DOUBLE PRECISION,
    term_slope           DOUBLE PRECISION,
    put_call_iv_spread   DOUBLE PRECISION,
    days_to_expiry_30d   INTEGER,
    days_to_earnings     DOUBLE PRECISION,
    atm_straddle         DOUBLE PRECISION,
    skew_25d             DOUBLE PRECISION,
    put_call_vol_ratio   DOUBLE PRECISION,
    put_call_oi_ratio    DOUBLE PRECISION,
    otm_put_iv_premium   DOUBLE PRECISION,
    chain_volume         DOUBLE PRECISION,
    chain_oi             DOUBLE PRECISION,
    chain_oi_chg         DOUBLE PRECISION,
    chain_breadth        DOUBLE PRECISION,
    next_day_return      DOUBLE PRECISION,
    next_day_direction   SMALLINT,
    PRIMARY KEY (symbol, date)
);

-- Chronological train/test splits + class-balance queries.
CREATE INDEX IF NOT EXISTS idx_ml_dataset_date ON ml_dataset(date);
CREATE INDEX IF NOT EXISTS idx_ml_dataset_direction ON ml_dataset(next_day_direction);
