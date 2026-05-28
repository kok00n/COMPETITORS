-- BBG Queue View - lista (isin, as_of_date) ktore wymagaja uzupelnienia
-- przez scripts/bbg_fill.py odpalany na stacji z Bloombergiem.
--
-- Wymaga juz odpalonych funds_schema.sql + portfolio_snapshots_schema.sql +
-- holdings_schema.sql + isin_metrics_schema.sql.
--
-- Logika:
--   1. Wez wszystkie (snapshot, holding.isin) dla obligacji + reverse repo
--      (jedyne typy gdzie BBG ma sensowne yield/dur/CS01).
--   2. Wyrzuc te ktore juz maja isin_metrics z ready=TRUE LUB no_data_flag=TRUE
--      (no_data oznacza ze BBG nie ma tych danych - JST, nowe emisje, etc.).
--   3. Sortuj wg as_of_date DESC zeby BBG fill skupil sie na najnowszych
--      snapshotach (potrzebne do biezacych raportow).
--
-- BBG fill workflow:
--   1. local: SELECT * FROM bbg_queue (LIMIT N)
--   2. local: blpapi -> pobiera yield/dur/CS01/rating per (isin, as_of_date)
--   3. local: UPSERT do isin_metrics z ready=TRUE lub no_data_flag=TRUE
--   4. local: gh workflow_dispatch -> compute_and_report.yml (trigger drugi
--      pipeline w cloud).

CREATE OR REPLACE VIEW bbg_queue AS
SELECT DISTINCT
    h.isin,
    h.issuer_name,
    h.currency,
    h.issuer_country,
    s.report_date    AS as_of_date,
    -- Statystyki: ile snapshotow zawiera ten ISIN na ten dzien
    -- (po lookthrough wielu funduszy - moga go trzymac np. 8 funduszy
    -- co skrocenia BBG fill - jeden API call wystarczy dla wszystkich).
    COUNT(*)         AS held_in_n_snapshots,
    SUM(h.value_pln) AS total_value_pln,
    -- Najnizsza waga w portfelu (jesli mala, mozemy zaakceptowac no_data)
    MIN(h.weight_nav_pct) AS min_weight_pct,
    MAX(h.weight_nav_pct) AS max_weight_pct
FROM holdings h
JOIN portfolio_snapshots s
    ON h.snapshot_id = s.snapshot_id
   AND s.scrape_status = 'ok'
WHERE h.isin IS NOT NULL
  AND h.instrument_type IN ('Obligacje', 'Aktywa Reverse Repo')
  AND NOT EXISTS (
      SELECT 1 FROM isin_metrics m
      WHERE m.isin = h.isin
        AND m.as_of_date = s.report_date
        AND (m.ready OR m.no_data_flag)
  )
GROUP BY h.isin, h.issuer_name, h.currency, h.issuer_country, s.report_date
ORDER BY s.report_date DESC, total_value_pln DESC NULLS LAST;

-- =====================================================================
--  RPC: liczebnosc kolejki BBG per data raportu
--  (do powiadomien email po scrape - "X nowych ISIN-ow do uzupelnienia w BBG")
-- =====================================================================
CREATE OR REPLACE FUNCTION bbg_queue_summary()
RETURNS TABLE (
    as_of_date    DATE,
    pending_count BIGINT,
    isins         TEXT[]
)
LANGUAGE sql STABLE AS $$
    SELECT
        as_of_date,
        COUNT(*)::BIGINT AS pending_count,
        ARRAY_AGG(isin ORDER BY total_value_pln DESC NULLS LAST) AS isins
    FROM bbg_queue
    GROUP BY as_of_date
    ORDER BY as_of_date DESC;
$$;
