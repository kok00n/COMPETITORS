-- LLM Reports - historia odpowiedzi Claude Opus per (snapshot lub cross-fund).
-- Wymaga juz odpalonych funds_schema.sql + portfolio_snapshots_schema.sql.
--
-- Typy raportow:
--   per_fund_monthly  - analiza pojedynczego subfunduszu na danym month-end
--                       (snapshot_id NOT NULL, peer_group NULL).
--                       Zawiera: co kupili/sprzedali, jak zmienily sie metryki,
--                       red flags, propozycje inwestycyjne dla obserwujacego.
--   cross_fund_monthly - porownanie wszystkich funduszy w grupie na danym
--                        month-end (snapshot_id NULL, peer_group NOT NULL).
--                        Konsensus, najpopularniejsze nowe pozycje, rozproszenie
--                        strategii.
--   recommendations    - sugestie ISIN do dodania/usuniecia z portfela uzytkownika
--                        na podstawie konsensusu konkurencji (snapshot_id NULL,
--                        peer_group NULL, report_date = end of month).
--
-- prompt + content - pelny request/response do audytu i debugowania.
-- input_tokens + output_tokens - do trackowania kosztu Claude API.

CREATE TABLE IF NOT EXISTS llm_reports (
    report_id           BIGSERIAL   PRIMARY KEY,
    snapshot_id         BIGINT,                      -- NULL dla cross-fund i recommendations
    peer_group          TEXT        CHECK (peer_group IN ('fsk', 'fod') OR peer_group IS NULL),
    report_type         TEXT        NOT NULL
                        CHECK (report_type IN ('per_fund_monthly', 'cross_fund_monthly', 'recommendations')),
    report_date         DATE        NOT NULL,        -- month-end ktorego dotyczy raport
    model               TEXT        NOT NULL DEFAULT 'claude-opus-4-7',
    prompt              TEXT        NOT NULL,
    content             TEXT        NOT NULL,        -- response markdown
    input_tokens        INT,
    output_tokens       INT,
    cache_read_tokens   INT,                          -- z prompt caching API field
    cache_write_tokens  INT,
    rendered_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_llm_reports_snapshot
    ON llm_reports (snapshot_id) WHERE snapshot_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llm_reports_type_date
    ON llm_reports (report_type, report_date DESC);

CREATE INDEX IF NOT EXISTS idx_llm_reports_peer_group_date
    ON llm_reports (peer_group, report_date DESC) WHERE peer_group IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llm_reports_rendered
    ON llm_reports (rendered_at DESC);

-- =====================================================================
--  RPC: historia raportow dla danego funduszu (do prompt context w nowych
--  raportach - LLM moze odwolac sie do swoich poprzednich analiz).
-- =====================================================================
CREATE OR REPLACE FUNCTION llm_report_history(
    p_fund_id  TEXT,
    p_limit    INT DEFAULT 3
)
RETURNS TABLE (
    report_date  DATE,
    rendered_at  TIMESTAMPTZ,
    content      TEXT
)
LANGUAGE sql STABLE AS $$
    SELECT r.report_date, r.rendered_at, r.content
    FROM llm_reports r
    JOIN portfolio_snapshots s ON r.snapshot_id = s.snapshot_id
    WHERE s.fund_id = p_fund_id
      AND r.report_type = 'per_fund_monthly'
    ORDER BY r.report_date DESC, r.rendered_at DESC
    LIMIT p_limit;
$$;

-- =====================================================================
--  RPC: koszty LLM per miesiac (do monitoringu wydatkow)
-- =====================================================================
CREATE OR REPLACE FUNCTION llm_token_usage_summary()
RETURNS TABLE (
    month             DATE,
    reports_count     BIGINT,
    total_input       BIGINT,
    total_output      BIGINT,
    total_cache_read  BIGINT,
    total_cache_write BIGINT
)
LANGUAGE sql STABLE AS $$
    SELECT
        DATE_TRUNC('month', rendered_at)::DATE AS month,
        COUNT(*)::BIGINT AS reports_count,
        COALESCE(SUM(input_tokens), 0)::BIGINT       AS total_input,
        COALESCE(SUM(output_tokens), 0)::BIGINT      AS total_output,
        COALESCE(SUM(cache_read_tokens), 0)::BIGINT  AS total_cache_read,
        COALESCE(SUM(cache_write_tokens), 0)::BIGINT AS total_cache_write
    FROM llm_reports
    GROUP BY DATE_TRUNC('month', rendered_at)
    ORDER BY month DESC;
$$;
