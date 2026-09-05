-- SEBI-mandated quarterly shareholding pattern: institutional holdings per symbol.
--
-- **This is the per-symbol institutional label the project spent five stages
-- working around.** Stage 1 had only a market-wide series, Stage 2 was
-- unsupervised, Stage 3's earnings instrument lost to its own placebo, and
-- Stage 4 was self-supervised. The change in FII/DII holding in a *named
-- symbol* over a quarter is exactly the quantity the project claims to detect.
--
-- One row per (symbol, quarter-end date). Sourced from the XBRL filing linked
-- by NSE's corporate-share-holdings-master API.
--
-- SHARE COUNTS ARE THE LOAD-BEARING COLUMNS, not the percentages. A holding
-- percentage moves when the company buys back stock or issues new shares, with
-- nobody having bought or sold anything; the share count only moves when
-- somebody trades. Detection work should difference `*_shares`, and use
-- `*_pct` for reporting and cross-symbol comparison.
--
-- Percentages are stored as PERCENT (17.20), not as the fractions the XBRL
-- carries (0.1720) — converted on ingest so the column reads the way the
-- filing does.
--
-- Filing dates are usually quarter ends, but not always: a company can file an
-- off-cycle revision (RELIANCE has 08-DEC-2021 and 29-OCT-2024). The date is
-- stored as filed rather than snapped to a quarter, and any analysis that
-- needs regular quarters must resample rather than assume.
--
-- Idempotent: safe to re-run via init_db.sql.
CREATE TABLE IF NOT EXISTS shareholding (
    symbol            VARCHAR(32) NOT NULL,
    date              DATE        NOT NULL,   -- filing/period-end date, as filed

    -- Institutional holdings. Foreign = FPI/FII, Domestic = MF + insurance +
    -- banks + other financial institutions.
    fii_shares        BIGINT,
    fii_pct           DOUBLE PRECISION,
    dii_shares        BIGINT,
    dii_pct           DOUBLE PRECISION,

    -- Domestic components, useful because mutual funds and insurers behave
    -- very differently: MFs respond to retail inflows, insurers are slow.
    mf_shares         BIGINT,
    mf_pct            DOUBLE PRECISION,
    insurance_shares  BIGINT,
    insurance_pct     DOUBLE PRECISION,

    promoter_pct      DOUBLE PRECISION,       -- from the master API
    public_pct        DOUBLE PRECISION,       -- from the master API
    total_shares      BIGINT,

    record_id         VARCHAR(32),            -- NSE filing id, for provenance
    PRIMARY KEY (symbol, date)
);

-- The evaluation ranks the cross-section within one filing period.
CREATE INDEX IF NOT EXISTS idx_shareholding_date ON shareholding (date);
