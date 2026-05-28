-- Exposures - generic agregacja portfela po wymiarach (currency, country,
-- rating bucket, instrument type, sector).
-- Wymaga juz odpalonych funds_schema.sql + portfolio_snapshots_schema.sql.
--
-- Wszystkie dimensions w jednej tabeli (vs. osobna tabela per dimension) - tak
-- mozna zrobic jednolity wykres "exposure {dimension}" przez WHERE dimension=X.
-- Wpisy generowane przez compute_metrics.py po INSERT holdings.
--
-- Przyklady wpisow:
--   (snapshot=42, 'currency', 'PLN', 87.5)
--   (snapshot=42, 'currency', 'EUR', 8.2)
--   (snapshot=42, 'issuer_country', 'PL', 91.0)
--   (snapshot=42, 'rating_bucket', 'AAA', 25.0)  -- glownie polskie OK (rating SP A-)
--   (snapshot=42, 'instrument_type', 'Obligacje', 88.5)
--   (snapshot=42, 'instrument_type', 'Aktywa Reverse Repo', 8.3)

CREATE TABLE IF NOT EXISTS exposures (
    exposure_id      BIGSERIAL   PRIMARY KEY,
    snapshot_id      BIGINT      NOT NULL,
    dimension        TEXT        NOT NULL
                     CHECK (dimension IN ('currency', 'issuer_country', 'risk_country',
                                          'rating_bucket', 'instrument_type', 'sector')),
    bucket           TEXT        NOT NULL,          -- np. 'PLN', 'PL', 'AAA', 'Obligacje'
    weight_nav_pct   NUMERIC(8,4) NOT NULL,         -- suma weight_nav_pct dla holdings pasujacych do (dimension, bucket)
    inserted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
    UNIQUE (snapshot_id, dimension, bucket)
);

CREATE INDEX IF NOT EXISTS idx_exposures_snapshot_dim
    ON exposures (snapshot_id, dimension);

CREATE INDEX IF NOT EXISTS idx_exposures_dimension_bucket
    ON exposures (dimension, bucket);
