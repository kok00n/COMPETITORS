-- Funds - master tabela funduszy w grupach porownawczych FSK/FOD oraz
-- junction table fund_peer_groups (subfund moze byc w wielu grupach).
--
-- Klucze:
--   parasol_code (PK) - stabilny od momentu discover (kod w URL analizy.pl
--     np. 'PCS05', 'BPS34'). Jeden parasol_code = jeden URL do pobrania PDFa.
--   fund_id (UNIQUE, nullable) - identyfikator subfunduszu z PDFu (np. 'PKO005').
--     Uzupelniany przez parser po pierwszym udanym pobraniu PDFa. NULL dopoki
--     nie pobierzemy ani jednego raportu dla tego parasola.
--
-- WAZNE: kilka subfunduszy w jednym parasolu (np. PKO Parasolowy FIO ma
-- PKO005, PKO014, PKO075, PKO076...) to OSOBNE wpisy w tej tabeli z TYM SAMYM
-- parasol_name ale ROZNYM fund_id i parasol_code. Discover wciaga je jako
-- osobne (parasol_code per subfund), bo na analizy.pl kazdy subfund ma swoj
-- wlasny kod URL.
--
-- Schema idempotentny.

-- =====================================================================
--  COMMON: trigger function dla updated_at (uzywana przez wszystkie tabele)
-- =====================================================================
CREATE OR REPLACE FUNCTION peers_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  FUNDS - master tabela funduszy
-- =====================================================================
CREATE TABLE IF NOT EXISTS funds (
    parasol_code    TEXT        NOT NULL,
    fund_id         TEXT,                          -- np. PKO005 (z PDFa), NULL dopoki parser nie pobierze
    parasol_name    TEXT,                          -- np. 'PKO Parasolowy FIO', uzupelnia parser z naglowka PDFa
    subfund_name    TEXT        NOT NULL,          -- z discover_funds, np. 'PKO Konserwatywny'
    tfi_name        TEXT,                          -- inferred z prefixu nazwy, nadpisywane przez parser
    analizy_slug    TEXT        NOT NULL,          -- np. 'pko-konserwatywny' do konstrukcji URLa strony funduszu
    refresh_freq    TEXT        NOT NULL DEFAULT 'monthly'
                    CHECK (refresh_freq IN ('monthly', 'quarterly', 'unknown')),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,  -- FALSE dla wycofanych/zmergowanych
    notes           TEXT,
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (parasol_code)
);

-- fund_id musi byc unikalne (jesli not null), ale moze byc NULL w okresie
-- "discovered, not yet parsed". Partial unique index na not-null wartosci.
CREATE UNIQUE INDEX IF NOT EXISTS uidx_funds_fund_id
    ON funds (fund_id) WHERE fund_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_funds_tfi
    ON funds (tfi_name);

CREATE INDEX IF NOT EXISTS idx_funds_active
    ON funds (is_active) WHERE is_active = TRUE;

DROP TRIGGER IF EXISTS trg_funds_updated_at ON funds;
CREATE TRIGGER trg_funds_updated_at
    BEFORE UPDATE ON funds
    FOR EACH ROW
    EXECUTE FUNCTION peers_set_updated_at();

-- =====================================================================
--  FUND_PEER_GROUPS - junction table (M:N funds <-> peer_groups)
-- =====================================================================
CREATE TABLE IF NOT EXISTS fund_peer_groups (
    parasol_code    TEXT        NOT NULL,
    peer_group      TEXT        NOT NULL
                    CHECK (peer_group IN ('fsk', 'fod')),
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (parasol_code, peer_group),
    FOREIGN KEY (parasol_code) REFERENCES funds(parasol_code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fund_peer_groups_group
    ON fund_peer_groups (peer_group, parasol_code);

-- =====================================================================
--  WIDOK: fundusze z list peer_groups (do uzytku w dashboardzie/seed)
-- =====================================================================
CREATE OR REPLACE VIEW v_funds_with_groups AS
SELECT
    f.parasol_code,
    f.fund_id,
    f.parasol_name,
    f.subfund_name,
    f.tfi_name,
    f.analizy_slug,
    f.refresh_freq,
    f.is_active,
    f.notes,
    ARRAY_AGG(g.peer_group ORDER BY g.peer_group) FILTER (WHERE g.peer_group IS NOT NULL) AS peer_groups,
    f.inserted_at,
    f.updated_at
FROM funds f
LEFT JOIN fund_peer_groups g ON g.parasol_code = f.parasol_code
GROUP BY f.parasol_code, f.fund_id, f.parasol_name, f.subfund_name, f.tfi_name,
         f.analizy_slug, f.refresh_freq, f.is_active, f.notes,
         f.inserted_at, f.updated_at;
