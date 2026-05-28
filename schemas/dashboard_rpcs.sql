-- Dashboard RPCs - widoki i funkcje SQL uzywane przez notebooks/peers_dashboard.ipynb.
-- Wymaga juz odpalonych wszystkich poprzednich schemow.
--
-- Wszystkie eksponowane przez PostgREST automatycznie (Supabase RPC = SQL function
-- accessible via /rest/v1/rpc/{name}).

-- =====================================================================
--  WIDOK: historia metryk per fund (do wykresow time-series)
--  Yield/duration/CS01/rating po czasie + bookkeeping coverage.
-- =====================================================================
CREATE OR REPLACE VIEW v_fund_metrics_history AS
SELECT
    s.snapshot_id,
    s.parasol_code,
    s.fund_id,
    f.subfund_name,
    f.parasol_name,
    f.tfi_name,
    s.report_date,
    s.aum_pln,
    s.holdings_count,
    s.unchanged_flag,
    m.w_avg_yield,
    m.w_avg_mod_duration,
    m.w_avg_spread_duration,
    m.w_avg_cs01_pln,
    m.w_avg_rating_numeric,
    m.nav_with_metrics_pct,
    m.bonds_count,
    m.bonds_with_metrics_count
FROM portfolio_snapshots s
JOIN funds f ON f.parasol_code = s.parasol_code
LEFT JOIN portfolio_metrics m ON m.snapshot_id = s.snapshot_id
WHERE s.scrape_status = 'ok'
  AND s.fund_id IS NOT NULL;

-- =====================================================================
--  WIDOK: exposures time-series (stacked area charts: FX, geo, rating bucket)
-- =====================================================================
CREATE OR REPLACE VIEW v_fund_exposures_history AS
SELECT
    s.snapshot_id,
    s.fund_id,
    f.subfund_name,
    s.report_date,
    e.dimension,
    e.bucket,
    e.weight_nav_pct
FROM exposures e
JOIN portfolio_snapshots s ON s.snapshot_id = e.snapshot_id
JOIN funds f ON f.parasol_code = s.parasol_code
WHERE s.scrape_status = 'ok'
  AND s.fund_id IS NOT NULL;

-- =====================================================================
--  WIDOK: top holdings per snapshot (do heatmap holdings_top × snapshot)
-- =====================================================================
CREATE OR REPLACE VIEW v_fund_top_holdings AS
SELECT
    h.holding_id,
    s.snapshot_id,
    s.fund_id,
    f.subfund_name,
    s.report_date,
    h.isin,
    h.issuer_name,
    h.security_name,
    h.instrument_type,
    h.weight_nav_pct,
    h.value_pln,
    h.currency,
    h.issuer_country,
    ROW_NUMBER() OVER (PARTITION BY s.snapshot_id ORDER BY h.weight_nav_pct DESC NULLS LAST) AS rank_in_snapshot
FROM holdings h
JOIN portfolio_snapshots s ON s.snapshot_id = h.snapshot_id
JOIN funds f ON f.parasol_code = s.parasol_code
WHERE s.scrape_status = 'ok'
  AND s.fund_id IS NOT NULL
  AND h.weight_nav_pct IS NOT NULL;

-- =====================================================================
--  WIDOK: consensus holdings cross-fund (ile funduszy w grupie trzyma dany
--  ISIN na dany month-end, srednia waga + spread).
--  Klucz: (peer_group, report_date, isin).
-- =====================================================================
CREATE OR REPLACE VIEW v_consensus_holdings AS
SELECT
    g.peer_group,
    s.report_date,
    h.isin,
    -- Wybor jednej reprezentatywnej nazwy/issuer (czesto rozne TFI zapisuja
    -- nazwy w roznych wariantach np. 'PKO BP' vs 'POWSZECHNA KASA OSZCZEDNOSCI B').
    MODE() WITHIN GROUP (ORDER BY h.issuer_name)    AS issuer_name_modal,
    MODE() WITHIN GROUP (ORDER BY h.security_name)  AS security_name_modal,
    h.instrument_type,
    h.currency,
    COUNT(DISTINCT s.fund_id)         AS funds_holding_count,
    AVG(h.weight_nav_pct)             AS avg_weight_pct,
    MIN(h.weight_nav_pct)             AS min_weight_pct,
    MAX(h.weight_nav_pct)             AS max_weight_pct,
    STDDEV(h.weight_nav_pct)          AS stddev_weight_pct,
    SUM(h.value_pln)                  AS total_value_pln
FROM holdings h
JOIN portfolio_snapshots s ON s.snapshot_id = h.snapshot_id
JOIN fund_peer_groups g   ON g.parasol_code = s.parasol_code
WHERE s.scrape_status = 'ok'
  AND s.fund_id IS NOT NULL
  AND h.isin IS NOT NULL
GROUP BY g.peer_group, s.report_date, h.isin, h.instrument_type, h.currency;

-- =====================================================================
--  WIDOK: delta summary per snapshot (added/removed/changed counts dla
--  szybkich wskazyowek "co sie wydarzylo w ostatnim raporcie funduszu").
-- =====================================================================
CREATE OR REPLACE VIEW v_delta_summary AS
SELECT
    d.curr_snapshot_id,
    s.fund_id,
    s.report_date,
    COUNT(*) FILTER (WHERE d.change_type = 'added')     AS added_count,
    COUNT(*) FILTER (WHERE d.change_type = 'removed')   AS removed_count,
    COUNT(*) FILTER (WHERE d.change_type = 'increased') AS increased_count,
    COUNT(*) FILTER (WHERE d.change_type = 'decreased') AS decreased_count,
    SUM(ABS(d.weight_delta_pct)) FILTER (WHERE d.change_type IN ('added','removed','increased','decreased'))
                                                        AS total_abs_delta_pct
FROM holdings_delta d
JOIN portfolio_snapshots s ON s.snapshot_id = d.curr_snapshot_id
GROUP BY d.curr_snapshot_id, s.fund_id, s.report_date;

-- =====================================================================
--  RPC: ramka danych dla wykresu yield-vs-duration (scatter cross-fund
--  na konkretny month-end).
-- =====================================================================
CREATE OR REPLACE FUNCTION yield_duration_scatter(p_date DATE, p_peer_group TEXT DEFAULT NULL)
RETURNS TABLE (
    fund_id            TEXT,
    subfund_name       TEXT,
    tfi_name           TEXT,
    peer_groups        TEXT[],
    w_avg_yield        NUMERIC,
    w_avg_mod_duration NUMERIC,
    aum_pln            NUMERIC
)
LANGUAGE sql STABLE AS $$
    SELECT
        h.fund_id,
        h.subfund_name,
        h.tfi_name,
        (SELECT ARRAY_AGG(peer_group ORDER BY peer_group)
           FROM fund_peer_groups WHERE parasol_code = h.parasol_code) AS peer_groups,
        h.w_avg_yield,
        h.w_avg_mod_duration,
        h.aum_pln
    FROM v_fund_metrics_history h
    WHERE h.report_date = p_date
      AND (p_peer_group IS NULL OR EXISTS (
          SELECT 1 FROM fund_peer_groups g
          WHERE g.parasol_code = h.parasol_code AND g.peer_group = p_peer_group
      ))
      AND h.w_avg_yield IS NOT NULL
      AND h.w_avg_mod_duration IS NOT NULL;
$$;
