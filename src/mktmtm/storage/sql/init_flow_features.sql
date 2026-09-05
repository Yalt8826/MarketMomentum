-- Per-symbol institutional-flow features (outlier strikes + futures basis/roll).
--
-- One row per (symbol, trade date), the same grain as ml_dataset. Kept in its
-- own table rather than as new DATASET_COLUMNS because these are candidate
-- features under evaluation: promoting one into ml_dataset means editing
-- DATASET_COLUMNS *and* init_ml_dataset.sql and re-running make-dataset, which
-- should happen once a feature has earned it, not while it is being tested.
-- Join on (symbol, date) to evaluate; see docs/implementation/flow-features.md.
--
-- Two independent families live here:
--
--   oi_outlier_*  Strikes whose open-interest change is extreme against the
--                 other strikes of the same expiry on the same day. Sparse and
--                 localised by design — the existing chain_oi_chg sums the
--                 whole chain, which is exactly what hides a single loaded-up
--                 strike. NOTE: an OI build cannot distinguish a buyer from a
--                 writer, so none of these columns carry a direction. That
--                 needs the price-change quadrant (a separate feature).
--
--   fut_*         Near-month futures basis against the cost of carry, and the
--                 near->next roll. This is the family that maps directly onto
--                 participant_oi.net_fut_stock / net_fut_index, so it is the
--                 one with an external validation series.
--
-- NULL is used throughout for "not computable", never 0 — a symbol-day with no
-- outlier strikes has count 0 but NULL ratios, and a basis with no next-month
-- contract has NULL roll columns. A 0 here would be a sentinel wearing a
-- number's clothes, the defect documented in docs/data/iv-integrity-audit.md.
--
-- Idempotent: safe to re-run via init_db.sql.
CREATE TABLE IF NOT EXISTS flow_features (
    symbol                    VARCHAR(32) NOT NULL,
    date                      DATE        NOT NULL,

    -- ── outlier-strike detector ───────────────────────────────────────────
    oi_outlier_count          INTEGER,           -- flagged contracts that day
    oi_outlier_max_absz       DOUBLE PRECISION,  -- strongest robust |z|
    oi_outlier_share          DOUBLE PRECISION,  -- flagged |chg_oi| / chain |chg_oi|
    oi_outlier_call_frac      DOUBLE PRECISION,  -- CE share of flagged |chg_oi|
    oi_outlier_moneyness      DOUBLE PRECISION,  -- weighted mean (K - S) / S
    oi_outlier_dte            DOUBLE PRECISION,  -- weighted mean days to expiry
    oi_outlier_build_notional DOUBLE PRECISION,  -- rupees opened  (chg_oi > 0)
    oi_outlier_unwind_notional DOUBLE PRECISION, -- rupees closed  (chg_oi < 0)
    -- Notional is oi * spot with NO lot factor: bhavcopy open interest is
    -- reported in SHARES, not contracts. Verified against participant_oi,
    -- whose TOTAL contract count equals SUM(OPEN_INT / lot) to within 0.02%.

    -- ── futures basis and roll ────────────────────────────────────────────
    fut_dte                   INTEGER,           -- near-month days to expiry
    fut_basis                 DOUBLE PRECISION,  -- near close - spot (rupees)
    fut_basis_pct             DOUBLE PRECISION,  -- near / spot - 1
    fut_basis_annual          DOUBLE PRECISION,  -- annualised %, NULL when dte < 5
    fut_basis_excess          DOUBLE PRECISION,  -- annualised % - carry rate
    fut_oi                    BIGINT,            -- shares, not contracts
    fut_oi_chg                BIGINT,            -- shares, not contracts
    fut_volume                BIGINT,
    fut_oi_notional           DOUBLE PRECISION,  -- oi x lot x spot
    fut_oi_chg_notional       DOUBLE PRECISION,
    fut_roll_ratio            DOUBLE PRECISION,  -- next_oi / (near_oi + next_oi)
    fut_roll_spread           DOUBLE PRECISION,  -- (next - near) / near

    -- ── buildup quadrants (direction) ─────────────────────────────────────
    -- OI change crossed with price change, which is what gives the families
    -- above a direction. Futures: the four classic cases over every expiry.
    -- Options: a rising premium with rising OI is buyer-driven, a falling one
    -- writer-driven; writers are the institutional side far more often, since
    -- writing needs margin. Computed only on contracts that actually traded
    -- (CONTRACTS > 0) — on an untraded strike SETTLE_PR is a theoretical mark
    -- and the "premium change" between two such marks is model noise.
    q_long_buildup_notional   DOUBLE PRECISION,  -- price up,   OI up   (bullish)
    q_short_buildup_notional  DOUBLE PRECISION,  -- price down, OI up   (bearish)
    q_short_cover_notional    DOUBLE PRECISION,  -- price up,   OI down (bullish)
    q_long_unwind_notional    DOUBLE PRECISION,  -- price down, OI down (bearish)
    q_fut_direction           DOUBLE PRECISION,  -- (bull - bear) / total, [-1, 1]
    ce_writer_frac            DOUBLE PRECISION,  -- writer share of new CE interest
    pe_writer_frac            DOUBLE PRECISION,  -- writer share of new PE interest
    opt_direction             DOUBLE PRECISION,  -- calls bought + puts written = bullish
    oi_outlier_writer_frac    DOUBLE PRECISION,  -- the same, flagged strikes only
    oi_outlier_direction      DOUBLE PRECISION,  -- the outlier detector's direction

    PRIMARY KEY (symbol, date)
);

-- Migration for databases created before the quadrant family existed. The
-- CREATE above covers a fresh bootstrap; this covers an existing install.
-- Both are idempotent, and they must stay in sync — FLOW_COLUMNS in
-- features/flow.py is the source of truth and test_flow_columns_mirror_the_ddl
-- parses this file to enforce it.
ALTER TABLE flow_features
    ADD COLUMN IF NOT EXISTS q_long_buildup_notional  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS q_short_buildup_notional DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS q_short_cover_notional   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS q_long_unwind_notional   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS q_fut_direction          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS ce_writer_frac           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS pe_writer_frac           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS opt_direction            DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS oi_outlier_writer_frac   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS oi_outlier_direction     DOUBLE PRECISION;

-- Cross-sectional access (all symbols on one date) is the shape every rank-IC
-- and HMM-fitting query uses; the primary key already covers the per-symbol
-- time series.
CREATE INDEX IF NOT EXISTS idx_flow_features_date
    ON flow_features (date);
