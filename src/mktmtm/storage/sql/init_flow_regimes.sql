-- Per-symbol latent positioning regimes (Stage 2), decoded from a pooled HMM.
--
-- One row per (symbol, trade date), the same grain as flow_features and
-- ml_dataset. Written by `mktmtm flow-regimes`; see
-- docs/implementation/model-03-regime-hmm.md.
--
-- The per-symbol level has 212,441 rows and no label — participant_oi is
-- market-wide, so supervised learning is impossible there. A hidden Markov
-- model is the honest tool: positioning is *persistent* (an institution
-- accumulates over days, not in one print), and a transition matrix is what
-- encodes that. A clustering method would treat each day as independent and
-- throw the persistence away.
--
-- `state` is the Viterbi-decoded state, canonicalised by ascending mean
-- q_fut_direction so the integers mean the same thing across refits —
-- hmmlearn's own numbering is arbitrary and changes between restarts.
--
-- Symbol-days whose observables were incomplete get NO ROW rather than a
-- guessed state. NULL is used for "not computable", never a sentinel value,
-- the rule recorded in docs/data/iv-integrity-audit.md.
--
-- Idempotent: safe to re-run via init_db.sql.
CREATE TABLE IF NOT EXISTS flow_regimes (
    symbol      VARCHAR(32) NOT NULL,
    date        DATE        NOT NULL,

    state       SMALLINT,           -- canonicalised Viterbi state
    state_prob  DOUBLE PRECISION,   -- posterior of the decoded state

    -- Full posterior. Four columns is the ceiling the BIC search allows;
    -- unused states stay NULL rather than 0, since 0 is a real probability.
    p_state_0   DOUBLE PRECISION,
    p_state_1   DOUBLE PRECISION,
    p_state_2   DOUBLE PRECISION,
    p_state_3   DOUBLE PRECISION,
    p_state_4   DOUBLE PRECISION,
    p_state_5   DOUBLE PRECISION,

    PRIMARY KEY (symbol, date)
);

-- Migration for databases created before the grid ceiling rose to 6.
ALTER TABLE flow_regimes
    ADD COLUMN IF NOT EXISTS p_state_4 DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p_state_5 DOUBLE PRECISION;

-- Cross-sectional access (every symbol's state on one date) is the shape the
-- daily state-occupancy validation uses; the primary key already covers the
-- per-symbol time series that the transition matrix describes.
CREATE INDEX IF NOT EXISTS idx_flow_regimes_date
    ON flow_regimes (date);
