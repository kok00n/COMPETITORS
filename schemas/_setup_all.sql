-- =========================================================================
-- PEERS_COMPARISON - Full schema setup (zlozone wszystkie pliki w jednym).
--
-- Wykonaj raz w Supabase SQL Editor (Project -> SQL Editor -> New query),
-- alternatywnie wykonuj indywidualne *_schema.sql w kolejnosci:
--   1. funds_schema.sql
--   2. portfolio_snapshots_schema.sql
--   3. holdings_schema.sql
--   4. isin_metrics_schema.sql
--   5. portfolio_metrics_schema.sql
--   6. exposures_schema.sql
--   7. holdings_delta_schema.sql
--   8. llm_reports_schema.sql
--   9. bbg_queue_view.sql
--   10. dashboard_rpcs.sql
--
-- WSZYSTKIE statement-y sa idempotentne (CREATE IF NOT EXISTS / CREATE OR REPLACE) -
-- mozesz uruchamiac wielokrotnie bez ryzyka utraty danych.
-- =========================================================================

-- =====================================================================
-- 1. FUNDS + PEER GROUPS
-- =====================================================================

-- Common trigger function dla updated_at
CREATE OR REPLACE FUNCTION peers_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS funds (
    parasol_code    TEXT        NOT NULL,
    fund_id         TEXT,
    parasol_name    TEXT,
    subfund_name    TEXT        NOT NULL,
    tfi_name        TEXT,
    analizy_slug    TEXT        NOT NULL,
    refresh_freq    TEXT        NOT NULL DEFAULT 'monthly'
                    CHECK (refresh_freq IN ('monthly', 'quarterly', 'unknown')),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    notes           TEXT,
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (parasol_code)
);

CREATE UNIQUE INDEX IF NOT EXISTS uidx_funds_fund_id
    ON funds (fund_id) WHERE fund_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_funds_tfi ON funds (tfi_name);
CREATE INDEX IF NOT EXISTS idx_funds_active ON funds (is_active) WHERE is_active = TRUE;

DROP TRIGGER IF EXISTS trg_funds_updated_at ON funds;
CREATE TRIGGER trg_funds_updated_at BEFORE UPDATE ON funds
    FOR EACH ROW EXECUTE FUNCTION peers_set_updated_at();

CREATE TABLE IF NOT EXISTS fund_peer_groups (
    parasol_code    TEXT        NOT NULL,
    peer_group      TEXT        NOT NULL CHECK (peer_group IN ('fsk', 'fod')),
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (parasol_code, peer_group),
    FOREIGN KEY (parasol_code) REFERENCES funds(parasol_code) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fund_peer_groups_group ON fund_peer_groups (peer_group, parasol_code);

CREATE OR REPLACE VIEW v_funds_with_groups AS
SELECT f.parasol_code, f.fund_id, f.parasol_name, f.subfund_name, f.tfi_name,
       f.analizy_slug, f.refresh_freq, f.is_active, f.notes,
       ARRAY_AGG(g.peer_group ORDER BY g.peer_group) FILTER (WHERE g.peer_group IS NOT NULL) AS peer_groups,
       f.inserted_at, f.updated_at
FROM funds f
LEFT JOIN fund_peer_groups g ON g.parasol_code = f.parasol_code
GROUP BY f.parasol_code, f.fund_id, f.parasol_name, f.subfund_name, f.tfi_name,
         f.analizy_slug, f.refresh_freq, f.is_active, f.notes, f.inserted_at, f.updated_at;

-- =====================================================================
-- 2. PORTFOLIO SNAPSHOTS
-- =====================================================================

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id      BIGSERIAL   PRIMARY KEY,
    parasol_code     TEXT        NOT NULL,
    fund_id          TEXT,
    report_date      DATE        NOT NULL,
    pdf_path         TEXT,
    pdf_hash         TEXT,
    holdings_hash    TEXT,
    unchanged_flag   BOOLEAN     NOT NULL DEFAULT FALSE,
    aum_pln          NUMERIC(20,2),
    holdings_count   INT,
    scrape_status    TEXT        NOT NULL DEFAULT 'ok'
                     CHECK (scrape_status IN ('ok', 'partial', 'error', 'pending')),
    error_message    TEXT,
    inserted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (parasol_code) REFERENCES funds(parasol_code) ON DELETE CASCADE,
    UNIQUE (parasol_code, report_date)
);

-- Migration: poprzednie wersje schemy mialy UNIQUE (parasol_code, fund_id, report_date).
-- Zmieniamy na (parasol_code, report_date) bo scrape tworzy snapshot z fund_id=NULL
-- przed parsowaniem - potrzebny stabilny klucz upsert.
--
-- pg_attribute.attname to type `name`, nie text - cast attname::TEXT przed
-- porownaniem z ARRAY['...']::TEXT[].
DO $migr$
DECLARE
    old_constraint_name TEXT;
    new_constraint_exists BOOLEAN;
BEGIN
    -- Znajdz stary constraint (po liscie kolumn 3-element-ami: parasol_code+fund_id+report_date)
    SELECT c.conname INTO old_constraint_name
    FROM pg_constraint c
    JOIN pg_class t ON c.conrelid = t.oid
    WHERE t.relname = 'portfolio_snapshots'
      AND c.contype = 'u'
      AND (
          SELECT COUNT(*) FROM unnest(c.conkey) AS k
      ) = 3
      AND ARRAY['parasol_code','fund_id','report_date']::TEXT[] <@ (
          SELECT ARRAY_AGG(attname::TEXT) FROM pg_attribute
          WHERE attrelid = c.conrelid AND attnum = ANY(c.conkey)
      );

    IF old_constraint_name IS NOT NULL THEN
        EXECUTE FORMAT('ALTER TABLE portfolio_snapshots DROP CONSTRAINT %I', old_constraint_name);
        RAISE NOTICE 'Migration: dropped old constraint %', old_constraint_name;
    END IF;

    -- Czy nowy constraint juz istnieje? (lista kolumn = exactly [parasol_code, report_date])
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint c2
        JOIN pg_class t2 ON c2.conrelid = t2.oid
        WHERE t2.relname = 'portfolio_snapshots'
          AND c2.contype = 'u'
          AND (SELECT COUNT(*) FROM unnest(c2.conkey) AS k) = 2
          AND ARRAY['parasol_code','report_date']::TEXT[] <@ (
              SELECT ARRAY_AGG(attname::TEXT) FROM pg_attribute
              WHERE attrelid = c2.conrelid AND attnum = ANY(c2.conkey)
          )
    ) INTO new_constraint_exists;

    IF NOT new_constraint_exists THEN
        ALTER TABLE portfolio_snapshots
            ADD CONSTRAINT portfolio_snapshots_parasol_code_report_date_key
            UNIQUE (parasol_code, report_date);
        RAISE NOTICE 'Migration: added new constraint (parasol_code, report_date)';
    END IF;
END
$migr$;

CREATE INDEX IF NOT EXISTS idx_snapshots_fund_date ON portfolio_snapshots (fund_id, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_parasol_date ON portfolio_snapshots (parasol_code, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON portfolio_snapshots (report_date DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_pdf_hash ON portfolio_snapshots (pdf_hash) WHERE pdf_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_snapshots_status_needs_retry
    ON portfolio_snapshots (scrape_status, report_date) WHERE scrape_status IN ('partial', 'error', 'pending');

DROP TRIGGER IF EXISTS trg_snapshots_updated_at ON portfolio_snapshots;
CREATE TRIGGER trg_snapshots_updated_at BEFORE UPDATE ON portfolio_snapshots
    FOR EACH ROW EXECUTE FUNCTION peers_set_updated_at();

CREATE OR REPLACE FUNCTION latest_snapshot_per_fund()
RETURNS TABLE (snapshot_id BIGINT, parasol_code TEXT, fund_id TEXT, report_date DATE,
               aum_pln NUMERIC, holdings_count INT, unchanged_flag BOOLEAN)
LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (s.fund_id)
        s.snapshot_id, s.parasol_code, s.fund_id, s.report_date,
        s.aum_pln, s.holdings_count, s.unchanged_flag
    FROM portfolio_snapshots s
    WHERE s.fund_id IS NOT NULL AND s.scrape_status = 'ok'
    ORDER BY s.fund_id, s.report_date DESC;
$$;

-- =====================================================================
-- 3. HOLDINGS
-- =====================================================================

CREATE TABLE IF NOT EXISTS holdings (
    holding_id          BIGSERIAL   PRIMARY KEY,
    snapshot_id         BIGINT      NOT NULL,
    isin                TEXT,
    issuer_name         TEXT,
    security_name       TEXT,
    instrument_type     TEXT        NOT NULL,
    instrument_category TEXT,
    issuer_country      TEXT,
    risk_country        TEXT,
    currency            TEXT,
    quantity            NUMERIC(24,4),
    value_pln           NUMERIC(20,2),
    weight_assets_pct   NUMERIC(8,4),
    weight_nav_pct      NUMERIC(8,4),
    info                TEXT,
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_holdings_snapshot ON holdings (snapshot_id);
CREATE INDEX IF NOT EXISTS idx_holdings_isin ON holdings (isin) WHERE isin IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_holdings_instrument_type ON holdings (instrument_type);
CREATE INDEX IF NOT EXISTS idx_holdings_bbg_lookup
    ON holdings (snapshot_id, isin, instrument_type)
    WHERE isin IS NOT NULL AND instrument_type IN ('Obligacje', 'Aktywa Reverse Repo');

-- =====================================================================
-- 4. ISIN METRICS (BBG data)
-- =====================================================================

CREATE TABLE IF NOT EXISTS isin_metrics (
    isin                TEXT        NOT NULL,
    as_of_date          DATE        NOT NULL,
    yield_ytm           NUMERIC(8,4),
    mod_duration        NUMERIC(8,4),
    mac_duration        NUMERIC(8,4),
    cs01_pln            NUMERIC(14,4),
    spread_duration    NUMERIC(8,4),
    convexity           NUMERIC(10,4),
    coupon              NUMERIC(8,4),
    maturity_date       DATE,
    sp_rating           TEXT,
    moody_rating        TEXT,
    fitch_rating        TEXT,
    rating_numeric      SMALLINT,
    rating_bucket       TEXT,
    source              TEXT        NOT NULL DEFAULT 'BLPAPI',
    ready               BOOLEAN     NOT NULL DEFAULT FALSE,
    no_data_flag        BOOLEAN     NOT NULL DEFAULT FALSE,
    no_data_reason      TEXT,
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (isin, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_isin_metrics_date_ready ON isin_metrics (as_of_date, ready);
CREATE INDEX IF NOT EXISTS idx_isin_metrics_isin ON isin_metrics (isin);
CREATE INDEX IF NOT EXISTS idx_isin_metrics_pending
    ON isin_metrics (as_of_date, isin) WHERE NOT ready AND NOT no_data_flag;

DROP TRIGGER IF EXISTS trg_isin_metrics_updated_at ON isin_metrics;
CREATE TRIGGER trg_isin_metrics_updated_at BEFORE UPDATE ON isin_metrics
    FOR EACH ROW EXECUTE FUNCTION peers_set_updated_at();

CREATE OR REPLACE FUNCTION bbg_coverage_summary()
RETURNS TABLE (as_of_date DATE, total BIGINT, ready_count BIGINT,
               no_data_count BIGINT, pending_count BIGINT, coverage_pct NUMERIC)
LANGUAGE sql STABLE AS $$
    SELECT as_of_date,
           COUNT(*) AS total,
           COUNT(*) FILTER (WHERE ready) AS ready_count,
           COUNT(*) FILTER (WHERE no_data_flag) AS no_data_count,
           COUNT(*) FILTER (WHERE NOT ready AND NOT no_data_flag) AS pending_count,
           ROUND(100.0 * COUNT(*) FILTER (WHERE ready OR no_data_flag) / NULLIF(COUNT(*),0), 2) AS coverage_pct
    FROM isin_metrics GROUP BY as_of_date ORDER BY as_of_date DESC;
$$;

-- =====================================================================
-- 5. PORTFOLIO METRICS (weighted)
-- =====================================================================

CREATE TABLE IF NOT EXISTS portfolio_metrics (
    snapshot_id              BIGINT      PRIMARY KEY,
    w_avg_yield              NUMERIC(8,4),
    w_avg_mod_duration       NUMERIC(8,4),
    w_avg_spread_duration    NUMERIC(8,4),
    w_avg_cs01_pln           NUMERIC(16,2),
    w_avg_rating_numeric     NUMERIC(6,3),
    nav_with_metrics_pct     NUMERIC(6,2),
    bonds_count              INT,
    bonds_with_metrics_count INT,
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_portfolio_metrics_computed ON portfolio_metrics (computed_at DESC);
DROP TRIGGER IF EXISTS trg_portfolio_metrics_updated_at ON portfolio_metrics;
CREATE TRIGGER trg_portfolio_metrics_updated_at BEFORE UPDATE ON portfolio_metrics
    FOR EACH ROW EXECUTE FUNCTION peers_set_updated_at();

-- =====================================================================
-- 6. EXPOSURES (FX / geo / rating bucket / instrument type)
-- =====================================================================

CREATE TABLE IF NOT EXISTS exposures (
    exposure_id      BIGSERIAL   PRIMARY KEY,
    snapshot_id      BIGINT      NOT NULL,
    dimension        TEXT        NOT NULL
                     CHECK (dimension IN ('currency', 'issuer_country', 'risk_country',
                                          'rating_bucket', 'instrument_type', 'sector')),
    bucket           TEXT        NOT NULL,
    weight_nav_pct   NUMERIC(8,4) NOT NULL,
    inserted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
    UNIQUE (snapshot_id, dimension, bucket)
);

CREATE INDEX IF NOT EXISTS idx_exposures_snapshot_dim ON exposures (snapshot_id, dimension);
CREATE INDEX IF NOT EXISTS idx_exposures_dimension_bucket ON exposures (dimension, bucket);

-- =====================================================================
-- 7. HOLDINGS DELTA (changes between consecutive snapshots)
-- =====================================================================

CREATE TABLE IF NOT EXISTS holdings_delta (
    delta_id            BIGSERIAL   PRIMARY KEY,
    prev_snapshot_id    BIGINT,
    curr_snapshot_id    BIGINT      NOT NULL,
    fund_id             TEXT        NOT NULL,
    isin                TEXT,
    security_name       TEXT,
    instrument_type     TEXT,
    change_type         TEXT        NOT NULL
                        CHECK (change_type IN ('added', 'removed', 'increased', 'decreased', 'unchanged')),
    prev_weight_pct     NUMERIC(8,4),
    curr_weight_pct     NUMERIC(8,4),
    weight_delta_pct    NUMERIC(9,4),
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (prev_snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
    FOREIGN KEY (curr_snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_holdings_delta_curr ON holdings_delta (curr_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_holdings_delta_fund_curr ON holdings_delta (fund_id, curr_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_holdings_delta_change_type ON holdings_delta (change_type, curr_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_holdings_delta_isin ON holdings_delta (isin) WHERE isin IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uidx_holdings_delta_pair
    ON holdings_delta (curr_snapshot_id, COALESCE(prev_snapshot_id, 0), COALESCE(isin, ''), COALESCE(security_name, ''));

-- =====================================================================
-- 8. LLM REPORTS
-- =====================================================================

CREATE TABLE IF NOT EXISTS llm_reports (
    report_id           BIGSERIAL   PRIMARY KEY,
    snapshot_id         BIGINT,
    peer_group          TEXT        CHECK (peer_group IN ('fsk', 'fod') OR peer_group IS NULL),
    report_type         TEXT        NOT NULL
                        CHECK (report_type IN ('per_fund_monthly', 'cross_fund_monthly', 'recommendations')),
    report_date         DATE        NOT NULL,
    model               TEXT        NOT NULL DEFAULT 'claude-opus-4-7',
    prompt              TEXT        NOT NULL,
    content             TEXT        NOT NULL,
    input_tokens        INT,
    output_tokens       INT,
    cache_read_tokens   INT,
    cache_write_tokens  INT,
    rendered_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_llm_reports_snapshot ON llm_reports (snapshot_id) WHERE snapshot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_reports_type_date ON llm_reports (report_type, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_llm_reports_peer_group_date ON llm_reports (peer_group, report_date DESC) WHERE peer_group IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_reports_rendered ON llm_reports (rendered_at DESC);

CREATE OR REPLACE FUNCTION llm_report_history(p_fund_id TEXT, p_limit INT DEFAULT 3)
RETURNS TABLE (report_date DATE, rendered_at TIMESTAMPTZ, content TEXT)
LANGUAGE sql STABLE AS $$
    SELECT r.report_date, r.rendered_at, r.content
    FROM llm_reports r
    JOIN portfolio_snapshots s ON r.snapshot_id = s.snapshot_id
    WHERE s.fund_id = p_fund_id AND r.report_type = 'per_fund_monthly'
    ORDER BY r.report_date DESC, r.rendered_at DESC LIMIT p_limit;
$$;

CREATE OR REPLACE FUNCTION llm_token_usage_summary()
RETURNS TABLE (month DATE, reports_count BIGINT, total_input BIGINT,
               total_output BIGINT, total_cache_read BIGINT, total_cache_write BIGINT)
LANGUAGE sql STABLE AS $$
    SELECT DATE_TRUNC('month', rendered_at)::DATE AS month,
           COUNT(*)::BIGINT,
           COALESCE(SUM(input_tokens), 0)::BIGINT,
           COALESCE(SUM(output_tokens), 0)::BIGINT,
           COALESCE(SUM(cache_read_tokens), 0)::BIGINT,
           COALESCE(SUM(cache_write_tokens), 0)::BIGINT
    FROM llm_reports GROUP BY DATE_TRUNC('month', rendered_at) ORDER BY month DESC;
$$;

-- =====================================================================
-- 9. BBG QUEUE (view + helper)
-- =====================================================================

CREATE OR REPLACE VIEW bbg_queue AS
SELECT DISTINCT
    h.isin, h.issuer_name, h.currency, h.issuer_country,
    s.report_date AS as_of_date,
    COUNT(*) AS held_in_n_snapshots,
    SUM(h.value_pln) AS total_value_pln,
    MIN(h.weight_nav_pct) AS min_weight_pct,
    MAX(h.weight_nav_pct) AS max_weight_pct
FROM holdings h
JOIN portfolio_snapshots s ON h.snapshot_id = s.snapshot_id AND s.scrape_status = 'ok'
WHERE h.isin IS NOT NULL
  AND h.instrument_type IN ('Obligacje', 'Aktywa Reverse Repo')
  AND NOT EXISTS (
      SELECT 1 FROM isin_metrics m
      WHERE m.isin = h.isin AND m.as_of_date = s.report_date AND (m.ready OR m.no_data_flag)
  )
GROUP BY h.isin, h.issuer_name, h.currency, h.issuer_country, s.report_date
ORDER BY s.report_date DESC, total_value_pln DESC NULLS LAST;

CREATE OR REPLACE FUNCTION bbg_queue_summary()
RETURNS TABLE (as_of_date DATE, pending_count BIGINT, isins TEXT[])
LANGUAGE sql STABLE AS $$
    SELECT as_of_date, COUNT(*)::BIGINT,
           ARRAY_AGG(isin ORDER BY total_value_pln DESC NULLS LAST)
    FROM bbg_queue GROUP BY as_of_date ORDER BY as_of_date DESC;
$$;

-- =====================================================================
-- 10. DASHBOARD RPCs / VIEWS
-- =====================================================================

CREATE OR REPLACE VIEW v_fund_metrics_history AS
SELECT s.snapshot_id, s.parasol_code, s.fund_id, f.subfund_name, f.parasol_name, f.tfi_name,
       s.report_date, s.aum_pln, s.holdings_count, s.unchanged_flag,
       m.w_avg_yield, m.w_avg_mod_duration, m.w_avg_spread_duration, m.w_avg_cs01_pln,
       m.w_avg_rating_numeric, m.nav_with_metrics_pct, m.bonds_count, m.bonds_with_metrics_count
FROM portfolio_snapshots s
JOIN funds f ON f.parasol_code = s.parasol_code
LEFT JOIN portfolio_metrics m ON m.snapshot_id = s.snapshot_id
WHERE s.scrape_status = 'ok' AND s.fund_id IS NOT NULL;

CREATE OR REPLACE VIEW v_fund_exposures_history AS
SELECT s.snapshot_id, s.fund_id, f.subfund_name, s.report_date,
       e.dimension, e.bucket, e.weight_nav_pct
FROM exposures e
JOIN portfolio_snapshots s ON s.snapshot_id = e.snapshot_id
JOIN funds f ON f.parasol_code = s.parasol_code
WHERE s.scrape_status = 'ok' AND s.fund_id IS NOT NULL;

CREATE OR REPLACE VIEW v_fund_top_holdings AS
SELECT h.holding_id, s.snapshot_id, s.fund_id, f.subfund_name, s.report_date,
       h.isin, h.issuer_name, h.security_name, h.instrument_type,
       h.weight_nav_pct, h.value_pln, h.currency, h.issuer_country,
       ROW_NUMBER() OVER (PARTITION BY s.snapshot_id ORDER BY h.weight_nav_pct DESC NULLS LAST) AS rank_in_snapshot
FROM holdings h
JOIN portfolio_snapshots s ON s.snapshot_id = h.snapshot_id
JOIN funds f ON f.parasol_code = s.parasol_code
WHERE s.scrape_status = 'ok' AND s.fund_id IS NOT NULL AND h.weight_nav_pct IS NOT NULL;

CREATE OR REPLACE VIEW v_consensus_holdings AS
SELECT g.peer_group, s.report_date, h.isin,
       MODE() WITHIN GROUP (ORDER BY h.issuer_name)   AS issuer_name_modal,
       MODE() WITHIN GROUP (ORDER BY h.security_name) AS security_name_modal,
       h.instrument_type, h.currency,
       COUNT(DISTINCT s.fund_id) AS funds_holding_count,
       AVG(h.weight_nav_pct)     AS avg_weight_pct,
       MIN(h.weight_nav_pct)     AS min_weight_pct,
       MAX(h.weight_nav_pct)     AS max_weight_pct,
       STDDEV(h.weight_nav_pct)  AS stddev_weight_pct,
       SUM(h.value_pln)          AS total_value_pln
FROM holdings h
JOIN portfolio_snapshots s ON s.snapshot_id = h.snapshot_id
JOIN fund_peer_groups g   ON g.parasol_code = s.parasol_code
WHERE s.scrape_status = 'ok' AND s.fund_id IS NOT NULL AND h.isin IS NOT NULL
GROUP BY g.peer_group, s.report_date, h.isin, h.instrument_type, h.currency;

CREATE OR REPLACE VIEW v_delta_summary AS
SELECT d.curr_snapshot_id, s.fund_id, s.report_date,
       COUNT(*) FILTER (WHERE d.change_type = 'added')     AS added_count,
       COUNT(*) FILTER (WHERE d.change_type = 'removed')   AS removed_count,
       COUNT(*) FILTER (WHERE d.change_type = 'increased') AS increased_count,
       COUNT(*) FILTER (WHERE d.change_type = 'decreased') AS decreased_count,
       SUM(ABS(d.weight_delta_pct)) FILTER (WHERE d.change_type IN ('added','removed','increased','decreased'))
                                                            AS total_abs_delta_pct
FROM holdings_delta d
JOIN portfolio_snapshots s ON s.snapshot_id = d.curr_snapshot_id
GROUP BY d.curr_snapshot_id, s.fund_id, s.report_date;

CREATE OR REPLACE FUNCTION yield_duration_scatter(p_date DATE, p_peer_group TEXT DEFAULT NULL)
RETURNS TABLE (fund_id TEXT, subfund_name TEXT, tfi_name TEXT, peer_groups TEXT[],
               w_avg_yield NUMERIC, w_avg_mod_duration NUMERIC, aum_pln NUMERIC)
LANGUAGE sql STABLE AS $$
    SELECT h.fund_id, h.subfund_name, h.tfi_name,
           (SELECT ARRAY_AGG(peer_group ORDER BY peer_group)
              FROM fund_peer_groups WHERE parasol_code = h.parasol_code) AS peer_groups,
           h.w_avg_yield, h.w_avg_mod_duration, h.aum_pln
    FROM v_fund_metrics_history h
    WHERE h.report_date = p_date
      AND (p_peer_group IS NULL OR EXISTS (
          SELECT 1 FROM fund_peer_groups g
          WHERE g.parasol_code = h.parasol_code AND g.peer_group = p_peer_group
      ))
      AND h.w_avg_yield IS NOT NULL AND h.w_avg_mod_duration IS NOT NULL;
$$;

-- =====================================================================
-- 11. STORAGE BUCKET (raw-pdfs)
-- =====================================================================

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('raw-pdfs', 'raw-pdfs', FALSE, 52428800, ARRAY['application/pdf'])
ON CONFLICT (id) DO UPDATE
SET file_size_limit = EXCLUDED.file_size_limit,
    allowed_mime_types = EXCLUDED.allowed_mime_types;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'storage' AND policyname = 'raw_pdfs_read_authenticated') THEN
        DROP POLICY raw_pdfs_read_authenticated ON storage.objects;
    END IF;
    EXECUTE $POL$
        CREATE POLICY raw_pdfs_read_authenticated
        ON storage.objects FOR SELECT
        TO authenticated, service_role
        USING (bucket_id = 'raw-pdfs')
    $POL$;
    IF EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'storage' AND policyname = 'raw_pdfs_write_service_role') THEN
        DROP POLICY raw_pdfs_write_service_role ON storage.objects;
    END IF;
    EXECUTE $POL$
        CREATE POLICY raw_pdfs_write_service_role
        ON storage.objects FOR INSERT
        TO service_role
        WITH CHECK (bucket_id = 'raw-pdfs')
    $POL$;
END $$;

-- =========================================================================
-- KONIEC SETUPU. Po wykonaniu uruchom 'python scripts/seed_funds.py' lokalnie
-- aby zaladowac 39 funduszy z config/funds.yaml do tabel funds + fund_peer_groups.
-- =========================================================================
