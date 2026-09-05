-- Per-symbol institutional-flow attribution (Stage 3).
--
-- One row per (symbol, trade date, participant). Written by
-- `mktmtm flow-attribution`; see docs/implementation/model-04-attribution-bridge.md.
--
-- This is the bridge between the two grains the project is stuck between: a
-- market-wide label (participant_oi, ~1,250 rows) and a per-symbol panel with
-- no label at all (212,441 rows). Formally it is Learning from Label
-- Proportions — each date is a *bag* of ~170 symbols and only the bag
-- aggregate is observed.
--
-- IMPORTANT, and it belongs in the schema rather than only in a paper:
-- `flow_score` is NOT a measurement of institutional flow in that symbol. The
-- per-symbol allocation is not uniquely determined by the data — many
-- allocations reproduce the same market aggregate — so what picks this one is
-- the functional form, the regularisation and the weights, all of which are
-- assumptions. The defensible reading is "the allocation most consistent with
-- both the market-wide measurement and these per-symbol features", never
-- "estimated FII flow in RELIANCE".
--
-- `weight` is observed data, not learned: the symbol's share of that day's
-- total F&O open-interest notional. Learning it alongside the scores would
-- leave the model badly unidentified, with weight and score trading off
-- against each other and nothing to separate them.
--
-- Idempotent: safe to re-run via init_db.sql.
CREATE TABLE IF NOT EXISTS flow_attribution (
    symbol       VARCHAR(32) NOT NULL,
    date         DATE        NOT NULL,
    participant  VARCHAR(16) NOT NULL,   -- 'FII' / 'Pro' / 'Client'

    flow_score   DOUBLE PRECISION,       -- s_{i,t}: the allocated flow score
    weight       DOUBLE PRECISION,       -- w_{i,t}: OI-notional share, observed

    PRIMARY KEY (symbol, date, participant)
);

-- The cross-section on one date is what the aggregate reconstruction and the
-- event-window tests both scan.
CREATE INDEX IF NOT EXISTS idx_flow_attribution_date
    ON flow_attribution (date);
