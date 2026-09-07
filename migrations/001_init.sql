-- 001_init.sql — initial schema for congress stock-trade tracker.
-- Idempotent (IF NOT EXISTS) so it's safe on a DB that already has the tables
-- from the pre-migration inline bootstrap.

CREATE TABLE IF NOT EXISTS filings (
    id          TEXT PRIMARY KEY,          -- natural key: 'senate:<uuid>' | 'house:<...>'
    source      TEXT NOT NULL,             -- 'senate' | 'house'
    filer       TEXT NOT NULL,
    report_type TEXT,
    filed_at    DATE,
    raw_url     TEXT,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trades (
    id                BIGSERIAL PRIMARY KEY,
    filing_id         TEXT NOT NULL REFERENCES filings(id),
    ticker            TEXT,
    owner             TEXT,
    transaction_type  TEXT,                -- PURCHASE / SALE / EXCHANGE / ...
    amount_range      TEXT,
    transaction_date  DATE,
    notification_date DATE,
    raw               JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_trades_filing ON trades(filing_id);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);