-- Holdings Delta - zmiany skladu portfela miedzy dwoma kolejnymi snapshotami
-- tego samego subfunduszu. Wymaga juz odpalonych funds_schema.sql +
-- portfolio_snapshots_schema.sql.
--
-- Liczone przez compute_metrics.py jako pelny outer join holdings(prev) z
-- holdings(curr) po (isin, security_name). Per holding:
--   prev_weight=NULL, curr_weight=W  -> 'added'
--   prev_weight=W, curr_weight=NULL  -> 'removed'
--   prev_weight<curr_weight i diff > threshold -> 'increased'
--   prev_weight>curr_weight i diff > threshold -> 'decreased'
--   ROUND(prev,2)==ROUND(curr,2)     -> 'unchanged' (tylko jak chcemy logowac, zwykle pomijane)
--
-- weight_delta_pct = curr_weight - prev_weight (signed). Sortowanie po
-- ABS(weight_delta_pct) daje 'top movers'.

CREATE TABLE IF NOT EXISTS holdings_delta (
    delta_id            BIGSERIAL   PRIMARY KEY,
    prev_snapshot_id    BIGINT,                     -- NULL dla pierwszego snapshotu funduszu w bazie
    curr_snapshot_id    BIGINT      NOT NULL,
    fund_id             TEXT        NOT NULL,       -- denormalized dla szybkich queries per fund
    isin                TEXT,                       -- NULL dla derywatow
    security_name       TEXT,
    instrument_type     TEXT,
    change_type         TEXT        NOT NULL
                        CHECK (change_type IN ('added', 'removed', 'increased', 'decreased', 'unchanged')),
    prev_weight_pct     NUMERIC(8,4),
    curr_weight_pct     NUMERIC(8,4),
    weight_delta_pct    NUMERIC(9,4),               -- curr - prev, signed
    inserted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (prev_snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
    FOREIGN KEY (curr_snapshot_id) REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_holdings_delta_curr
    ON holdings_delta (curr_snapshot_id);

CREATE INDEX IF NOT EXISTS idx_holdings_delta_fund_curr
    ON holdings_delta (fund_id, curr_snapshot_id);

CREATE INDEX IF NOT EXISTS idx_holdings_delta_change_type
    ON holdings_delta (change_type, curr_snapshot_id);

CREATE INDEX IF NOT EXISTS idx_holdings_delta_isin
    ON holdings_delta (isin) WHERE isin IS NOT NULL;

-- Idempotencja: unikalna trojka (prev_snapshot_id, curr_snapshot_id, COALESCE(isin, security_name))
-- - dwukrotne odpalenie compute_metrics nie tworzy duplikatow.
-- COALESCE bo isin moze byc NULL dla derywatow, wtedy klucz pomocniczy = security_name.
CREATE UNIQUE INDEX IF NOT EXISTS uidx_holdings_delta_pair
    ON holdings_delta (curr_snapshot_id, COALESCE(prev_snapshot_id, 0), COALESCE(isin, ''), COALESCE(security_name, ''));
