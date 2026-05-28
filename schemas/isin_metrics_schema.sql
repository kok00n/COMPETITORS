-- ISIN Metrics - dane Bloomberga (yield, duration, CS01, ratingi) per ISIN
-- na konkretny snapshot date. Wymaga juz odpalonych funds_schema.sql +
-- portfolio_snapshots_schema.sql.
--
-- Uzupelniane przez scripts/bbg_fill.py odpalany LOKALNIE na stacji z BBG
-- (BLPAPI nie zadziala w GH Actions - licencja BBG per maszyna).
--
-- Klucz: (isin, as_of_date). as_of_date = data raportu z PDFa (zwykle
-- last day of month), NIE data Bloomberga. Tak jak BBG zwroci dane historyczne
-- dla pytanego dnia.
--
-- ready=FALSE -> wiersz jest w bbg_queue (BBG fill jeszcze go nie uzupelnil).
-- compute_metrics.py liczy weighted yield/dur/CS01 TYLKO z wierszy ready=TRUE.

CREATE TABLE IF NOT EXISTS isin_metrics (
    isin                TEXT        NOT NULL,
    as_of_date          DATE        NOT NULL,
    -- Bloomberg metrics
    yield_ytm           NUMERIC(8,4),               -- YTM/YTW w %, mid (YLD_YTM_MID lub YLD_YTW_MID dla callable)
    mod_duration        NUMERIC(8,4),               -- DUR_ADJ_MID (modified duration w latach)
    mac_duration        NUMERIC(8,4),               -- DUR_MID (Macaulay duration, opcjonalnie)
    cs01_pln            NUMERIC(14,4),              -- CS01 w PLN per 1bp spread move (na nominal=1mln; ETL skaluje per holding)
    spread_duration     NUMERIC(8,4),               -- SPREAD_DUR_BBG, dla obligacji ze spreadem
    convexity           NUMERIC(10,4),              -- CONVEXITY_MID, opcjonalnie
    coupon              NUMERIC(8,4),               -- CPN
    maturity_date       DATE,                       -- MATURITY
    -- Ratings (S&P/Moody/Fitch w formacie naturalnym + ujednolicony numeryczny)
    sp_rating           TEXT,                       -- np. 'BBB+', 'AA-', 'NR'
    moody_rating        TEXT,                       -- np. 'Baa1', 'Aa3', 'WR'
    fitch_rating        TEXT,                       -- np. 'BBB+', 'A-'
    rating_numeric      SMALLINT,                   -- 1=AAA, 2=AA+, 3=AA, ..., 21=D, 22=NR (highest avail or composite)
    rating_bucket       TEXT,                       -- 'AAA','AA','A','BBB','BB','B','CCC-D','NR' (per S&P bucketing)
    -- Audit
    source              TEXT        NOT NULL DEFAULT 'BLPAPI',
    ready               BOOLEAN     NOT NULL DEFAULT FALSE,  -- TRUE po uzupelnieniu (poza brakiem danych w BBG)
    no_data_flag        BOOLEAN     NOT NULL DEFAULT FALSE,  -- TRUE jesli BBG nie ma danych dla tego ISIN (JST, mlode emisje, etc.)
    no_data_reason      TEXT,                                -- 'not_in_bbg', 'incomplete_fields', 'manual_skip'
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (isin, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_isin_metrics_date_ready
    ON isin_metrics (as_of_date, ready);

CREATE INDEX IF NOT EXISTS idx_isin_metrics_isin
    ON isin_metrics (isin);

-- Index dla bbg_queue (szybkie wyszukanie not-ready, not-no-data)
CREATE INDEX IF NOT EXISTS idx_isin_metrics_pending
    ON isin_metrics (as_of_date, isin) WHERE NOT ready AND NOT no_data_flag;

DROP TRIGGER IF EXISTS trg_isin_metrics_updated_at ON isin_metrics;
CREATE TRIGGER trg_isin_metrics_updated_at
    BEFORE UPDATE ON isin_metrics
    FOR EACH ROW
    EXECUTE FUNCTION peers_set_updated_at();

-- =====================================================================
--  RPC: counts of ISIN-ow uzupelnionych vs do uzupelnienia per data raportu
-- =====================================================================
CREATE OR REPLACE FUNCTION bbg_coverage_summary()
RETURNS TABLE (
    as_of_date    DATE,
    total         BIGINT,
    ready_count   BIGINT,
    no_data_count BIGINT,
    pending_count BIGINT,
    coverage_pct  NUMERIC
)
LANGUAGE sql STABLE AS $$
    SELECT
        as_of_date,
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE ready)         AS ready_count,
        COUNT(*) FILTER (WHERE no_data_flag)  AS no_data_count,
        COUNT(*) FILTER (WHERE NOT ready AND NOT no_data_flag) AS pending_count,
        ROUND(100.0 * COUNT(*) FILTER (WHERE ready OR no_data_flag) / NULLIF(COUNT(*),0), 2) AS coverage_pct
    FROM isin_metrics
    GROUP BY as_of_date
    ORDER BY as_of_date DESC;
$$;
