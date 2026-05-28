-- Holdings - linie portfela per snapshot (jeden wiersz per instrument w portfelu).
-- Wymaga juz odpalonych funds_schema.sql + portfolio_snapshots_schema.sql.
--
-- Mapowanie kolumn z PDFa KNF -> holdings:
--   Identyfikator funduszu             -> snapshot.fund_id (parser filtruje wiersze per fund_id)
--   Nazwa emitenta                     -> issuer_name
--   Identyfikator instrumentu          -> isin (moze byc NULL dla derywatow bez ISINa)
--   Typ instrumentu                    -> instrument_type
--   Kategoria instrumentu              -> instrument_category (czesto N/D w PDFach)
--   Kraj emitenta                      -> issuer_country
--   Kraj ryzyka                        -> risk_country
--   Waluta instrumentu                 -> currency
--   Ilosc instrumentow w portfelu      -> quantity
--   Wartosc instrumentu w PLN          -> value_pln
--   Udzial w wartosci aktywow ogolem   -> weight_assets_pct
--   Udzial w NAV                       -> weight_nav_pct  (uzywane jako primary waga)
--   Informacje uzupelniajace           -> info
--
-- weight_nav_pct moze byc UJEMNE dla Zobowiazania Repo (krotki dlug w portfelu),
-- albo dla pochodnych z negative notional (np. CME futures short).

CREATE TABLE IF NOT EXISTS holdings (
    holding_id          BIGSERIAL   PRIMARY KEY,
    snapshot_id         BIGINT      NOT NULL,
    isin                TEXT,                       -- NULLABLE: Spot-Forward, FX Swap, IRS, Kontrakt terminowy bez ISIN
    issuer_name         TEXT,                       -- 'Nazwa emitenta' z PDFa (NULL dla pure cash)
    security_name       TEXT,                       -- nazwa instrumentu (jesli inna od issuer_name; czesto = issuer_name)
    instrument_type     TEXT        NOT NULL,       -- 'Obligacje', 'Akcje', 'Aktywa Reverse Repo', 'Zobowiazania Repo',
                                                    -- 'Tytuly i jednostki uczestnictwa', 'Kontrakt terminowy',
                                                    -- 'Spot-Forward', 'FX Swap', 'Swap walutowy', 'IRS',
                                                    -- 'Gotowka/Depozyty/Naleznosci', 'Pozyczki papierow wartosciowych'
    instrument_category TEXT,                       -- usually 'N/D' w PDFach KNF
    issuer_country      TEXT,                       -- 2-char ISO: 'PL', 'RO', 'HU', 'IL', 'GO' (Global), 'EU', etc.
    risk_country        TEXT,
    currency            TEXT,                       -- 'PLN', 'EUR', 'USD', 'JPY', 'ILS', etc.
    quantity            NUMERIC(24,4),
    value_pln           NUMERIC(20,2),
    weight_assets_pct   NUMERIC(8,4),               -- z PDFa 'Udzial w wartosci aktywow ogolem (%)'
    weight_nav_pct      NUMERIC(8,4),               -- z PDFa 'Udzial w NAV (%)', PRIMARY waga do analizy
    info                TEXT,                       -- 'Informacje uzupelniajace'
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_holdings_snapshot
    ON holdings (snapshot_id);

CREATE INDEX IF NOT EXISTS idx_holdings_isin
    ON holdings (isin) WHERE isin IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_holdings_instrument_type
    ON holdings (instrument_type);

-- Index do bbg_queue view (filtruje obligacje per snapshot+isin do uzupelnienia BBG-em)
CREATE INDEX IF NOT EXISTS idx_holdings_bbg_lookup
    ON holdings (snapshot_id, isin, instrument_type)
    WHERE isin IS NOT NULL AND instrument_type IN ('Obligacje', 'Aktywa Reverse Repo');
