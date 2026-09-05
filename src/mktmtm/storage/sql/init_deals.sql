-- NSE bulk and block deals: the only per-symbol, per-date, NAMED record of
-- institutional transactions available to this project.
--
-- Every previous modelling stage was validated indirectly, because no
-- per-symbol institutional label was thought to exist: Stage 1 used a
-- market-wide series, Stage 2 was unsupervised, Stage 3 used the earnings
-- calendar (and failed its placebo), Stage 4 was self-supervised. This table
-- is that missing label.
--
--   Bulk deal  — cumulative trades in one scrip exceeding 0.5% of listed shares
--                on a day, disclosed by the broker after the close.
--   Block deal — a single trade above Rs 10 crore in the separate block window.
--
-- One row per (date, symbol, client, side, deal kind). A client can appear
-- twice in a day on opposite sides, and the same deal may be reported in both
-- windows, so `kind` is part of the key.
--
-- IMPORTANT — this is a ONE-SIDED label. A disclosed deal implies a whale, but
-- the absence of one does NOT imply no whale: institutions slice orders below
-- the disclosure threshold precisely to avoid appearing here. So recall
-- against this table is measurable and precision is not, and an apparent false
-- positive may be a real institution that stayed under the threshold. Any
-- evaluation must say so.
--
-- Also note it is a CASH-market record while the project's features are F&O.
-- The link is plausible (institutions hedge and position in derivatives around
-- large cash trades) but is itself a hypothesis under test, not an assumption.
--
-- Idempotent: safe to re-run via init_db.sql.
CREATE TABLE IF NOT EXISTS deals (
    date          DATE         NOT NULL,
    symbol        VARCHAR(32)  NOT NULL,
    client_name   VARCHAR(256) NOT NULL,
    side          VARCHAR(4)   NOT NULL,   -- 'BUY' / 'SELL'
    kind          VARCHAR(8)   NOT NULL,   -- 'bulk' / 'block'

    quantity      BIGINT,                  -- shares
    price         DOUBLE PRECISION,        -- weighted average trade price
    value         DOUBLE PRECISION,        -- quantity * price, rupees
    security_name VARCHAR(256),
    remarks       VARCHAR(256),

    PRIMARY KEY (date, symbol, client_name, side, kind)
);

-- The evaluation scans one date's cross-section (which symbols saw a deal) and
-- one symbol's event history (did our features fire before its deals).
CREATE INDEX IF NOT EXISTS idx_deals_date ON deals (date);
CREATE INDEX IF NOT EXISTS idx_deals_symbol_date ON deals (symbol, date);
