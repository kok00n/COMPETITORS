-- Portfolio Snapshots - jeden wiersz per (fund_id, report_date).
-- Wymaga juz odpalonego funds_schema.sql.
--
-- Snapshot = stan portfela subfunduszu na konkretny month-end (z PDFa pobranego
-- z https://dokumenty.analizy.pl/pobierz/fi/{parasol_code}/SP/{YYYY-MM-DD}).
--
-- Specjalne pola:
--   pdf_hash      - SHA256 calego pliku PDF (do deduplikacji przy backfill -
--                   jesli kolejny request zwroci ten sam PDF nie tworzymy
--                   duplikatu snapshot-a; tez zlapie sytuacje "parsowany ten
--                   sam plik raz w analizy.pl ma fixed URL ale 404 dla niektorych
--                   month-end").
--   holdings_hash - SHA256 sortowanej listy (isin, weight_nav_pct) JUZ TYLKO
--                   dla TEGO subfunduszu (nie calego parasola). Service warstwa
--                   compute_metrics.py uzupelnia po INSERT holdings.
--   unchanged_flag - holdings_hash == poprzedni snapshot tego samego fund_id?
--                    Wtedy compute_metrics moze pominac kosztowne metryki, LLM
--                    moze tylko stwierdzic 'no change since X' (oszczednosc tokenow).
--                    Wazne dla funduszy z kwartalnym raportowaniem - 2 z 3 month-end
--                    raportow w kwartale beda mialy unchanged_flag=TRUE.
--   aum_pln       - suma value_pln z holdings (NAV total). Liczone po INSERT holdings.

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id      BIGSERIAL   PRIMARY KEY,
    parasol_code     TEXT        NOT NULL,
    fund_id          TEXT,                          -- z PDFa, NULLABLE jesli parser jeszcze nie sparsowal
    report_date      DATE        NOT NULL,
    pdf_path         TEXT,                          -- Supabase Storage path (bucket/raw_pdfs/{parasol}_{date}.pdf)
    pdf_hash         TEXT,                          -- SHA256 pliku
    holdings_hash    TEXT,                          -- SHA256 sortowanych (isin, weight_nav_pct) per fund_id
    unchanged_flag   BOOLEAN     NOT NULL DEFAULT FALSE,
    aum_pln          NUMERIC(20,2),
    holdings_count   INT,                           -- dla quick stats bez COUNT(*) holdings
    scrape_status    TEXT        NOT NULL DEFAULT 'ok'
                     CHECK (scrape_status IN ('ok', 'partial', 'error', 'pending')),
    error_message    TEXT,                          -- jesli scrape_status != 'ok'
    inserted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (parasol_code) REFERENCES funds(parasol_code) ON DELETE CASCADE,
    -- Jeden snapshot per (parasol_code, fund_id, report_date). fund_id moze byc NULL
    -- przy pendingu/error, wiec COALESCE w UNIQUE.
    UNIQUE (parasol_code, fund_id, report_date)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_fund_date
    ON portfolio_snapshots (fund_id, report_date DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_parasol_date
    ON portfolio_snapshots (parasol_code, report_date DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_date
    ON portfolio_snapshots (report_date DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_pdf_hash
    ON portfolio_snapshots (pdf_hash) WHERE pdf_hash IS NOT NULL;

-- Status partial/error - do retry przez scraper
CREATE INDEX IF NOT EXISTS idx_snapshots_status_needs_retry
    ON portfolio_snapshots (scrape_status, report_date) WHERE scrape_status IN ('partial', 'error', 'pending');

DROP TRIGGER IF EXISTS trg_snapshots_updated_at ON portfolio_snapshots;
CREATE TRIGGER trg_snapshots_updated_at
    BEFORE UPDATE ON portfolio_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION peers_set_updated_at();

-- =====================================================================
--  RPC: ostatni snapshot per fund_id (do quick lookup w dashboardzie)
-- =====================================================================
CREATE OR REPLACE FUNCTION latest_snapshot_per_fund()
RETURNS TABLE (
    snapshot_id     BIGINT,
    parasol_code    TEXT,
    fund_id         TEXT,
    report_date     DATE,
    aum_pln         NUMERIC,
    holdings_count  INT,
    unchanged_flag  BOOLEAN
)
LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (s.fund_id)
        s.snapshot_id, s.parasol_code, s.fund_id, s.report_date,
        s.aum_pln, s.holdings_count, s.unchanged_flag
    FROM portfolio_snapshots s
    WHERE s.fund_id IS NOT NULL AND s.scrape_status = 'ok'
    ORDER BY s.fund_id, s.report_date DESC;
$$;
