-- Portfolio Metrics - wazone wskazniki portfelowe per snapshot.
-- Wymaga juz odpalonych funds_schema.sql + portfolio_snapshots_schema.sql +
-- holdings_schema.sql + isin_metrics_schema.sql.
--
-- Liczone przez scripts/compute_metrics.py po BBG fill:
--   w_avg_yield = SUM(yield_ytm * weight_nav_pct) / SUM(weight_nav_pct WHERE yield_ytm IS NOT NULL)
--   (tylko holdings z ready isin_metrics; obligacje + reverse repo dla yield)
--
-- coverage_pct mowi ile % NAV jest pokryte przez ready metrics. Jesli < ~80%,
-- weighted metrics maja duzy bias - dashboard moze to oznaczyc 'low coverage'.

CREATE TABLE IF NOT EXISTS portfolio_metrics (
    snapshot_id              BIGINT      PRIMARY KEY,
    -- Weighted (po wagach NAV, tylko ready holdings z metryka)
    w_avg_yield              NUMERIC(8,4),       -- weighted YTM %
    w_avg_mod_duration       NUMERIC(8,4),       -- weighted modified duration (lata)
    w_avg_spread_duration    NUMERIC(8,4),       -- weighted spread duration (lata)
    w_avg_cs01_pln           NUMERIC(16,2),      -- portfolio CS01 = SUM(holding_cs01) w PLN
    w_avg_rating_numeric     NUMERIC(6,3),       -- weighted rating_numeric (1=AAA, ..., 21=D)
    -- Coverage stats
    nav_with_metrics_pct     NUMERIC(6,2),       -- % NAV z ready isin_metrics (denominator dla wag)
    bonds_count              INT,                -- liczba pozycji typu Obligacje
    bonds_with_metrics_count INT,                -- ile z nich ma ready isin_metrics
    -- Audit
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_portfolio_metrics_computed
    ON portfolio_metrics (computed_at DESC);

DROP TRIGGER IF EXISTS trg_portfolio_metrics_updated_at ON portfolio_metrics;
CREATE TRIGGER trg_portfolio_metrics_updated_at
    BEFORE UPDATE ON portfolio_metrics
    FOR EACH ROW
    EXECUTE FUNCTION peers_set_updated_at();
