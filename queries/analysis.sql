-- ============================================================================
-- analysis.sql — ready-to-run analysis queries for the congress-tracker DB.
--
-- Run against the "congress" database, e.g.:
--
--   psql "$DATABASE_URL" -f queries/analysis.sql
--   # or inside the DB container:
--   docker exec -i <db-container> psql -U postgres -d congress -f - < queries/analysis.sql
--
-- Queries that need a parameter use psql variables so you can override them:
--
--   psql "$DATABASE_URL" -v ticker=NVDA -f queries/analysis.sql
--
-- The queries below that reference :ticker default to NVDA when you don't
-- pass one; override with -v ticker=XXX.
\if :{?ticker}
\else
\set ticker NVDA
\endif
--
-- Schema reminder:
--   filings(id, source, filer, report_type, filed_at, raw_url)
--   trades(id, filing_id, ticker, owner, transaction_type,
--          amount_range, transaction_date, notification_date, raw jsonb)
--   raw jsonb keys: type, asset_type, ticker, owner, txn_date,
--                   amount, comment, asset_name, notif_date
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Overview
-- ----------------------------------------------------------------------------

-- Row counts per table
SELECT 'filings' AS tbl, count(*) FROM filings
UNION ALL
SELECT 'trades', count(*) FROM trades;

-- Filings by source
SELECT source, count(*) AS filings
FROM filings
GROUP BY source
ORDER BY 2 DESC;

-- Date span of the data
SELECT
  min(transaction_date)  AS first_transaction,
  max(transaction_date)  AS last_transaction,
  min(notification_date) AS first_notification,
  max(notification_date) AS last_notification
FROM trades;

-- ----------------------------------------------------------------------------
-- 2. Filers
-- ----------------------------------------------------------------------------

-- Most active filers by number of filings
SELECT filer, source, count(*) AS filings
FROM filings
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 20;

-- Most active filers by number of trades
SELECT f.filer, count(*) AS trades
FROM trades t
JOIN filings f ON f.id = t.filing_id
GROUP BY 1
ORDER BY 2 DESC
LIMIT 20;

-- ----------------------------------------------------------------------------
-- 3. Tickers
-- ----------------------------------------------------------------------------

-- All trades for one ticker (set :ticker, e.g. -v ticker=NVDA)
SELECT f.filer, t.owner, t.transaction_type, t.amount_range,
       t.transaction_date, t.notification_date, t.raw->>'asset_name' AS asset
FROM trades t
JOIN filings f ON f.id = t.filing_id
WHERE upper(t.ticker) = upper(:'ticker')
ORDER BY t.transaction_date DESC;

-- Most-traded tickers
SELECT ticker, count(*) AS trades,
       count(*) FILTER (WHERE transaction_type ILIKE 'purchase%') AS buys,
       count(*) FILTER (WHERE transaction_type ILIKE 'sale%')    AS sells
FROM trades
WHERE ticker IS NOT NULL AND ticker <> ''
GROUP BY 1
ORDER BY 2 DESC
LIMIT 30;

-- Trades with no ticker (often funds, ETFs, or parse gaps — check raw)
SELECT ticker, raw->>'asset_name' AS asset, count(*)
FROM trades
WHERE ticker IS NULL OR ticker = ''
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 20;

-- ----------------------------------------------------------------------------
-- 4. Amounts
-- ----------------------------------------------------------------------------
-- amount_range is a display string ("$1,001 - $15,000"), not a number.
-- The bucket_min() mapping below lets you rank/compare by lower bound.

-- Largest disclosed trades by lower-bound dollar amount
WITH amounts AS (
  SELECT t.id, f.filer, t.ticker, t.owner, t.transaction_type,
         t.amount_range, t.transaction_date,
         CASE t.amount_range
           WHEN '$1,001 - $15,000'         THEN 1001
           WHEN '$15,001 - $50,000'        THEN 15001
           WHEN '$50,001 - $100,000'       THEN 50001
           WHEN '$100,001 - $250,000'      THEN 100001
           WHEN '$250,001 - $500,000'      THEN 250001
           WHEN '$500,001 - $1,000,000'    THEN 500001
           WHEN '$1,000,001 - $5,000,000'  THEN 1000001
           WHEN '$5,000,001 - $25,000,000' THEN 5000001
           WHEN '$25,000,001 - $50,000,000' THEN 25000001
           ELSE NULL
         END AS bucket_min
  FROM trades t
  JOIN filings f ON f.id = t.filing_id
)
SELECT filer, ticker, owner, transaction_type, amount_range, transaction_date
FROM amounts
WHERE bucket_min IS NOT NULL
ORDER BY bucket_min DESC
LIMIT 25;

-- Amount-range distribution
SELECT amount_range, count(*) AS trades
FROM trades
GROUP BY 1
ORDER BY 2 DESC;

-- Amount ranges that don't match a standard bucket (parse artifacts — check raw)
SELECT amount_range, count(*) AS trades
FROM trades
WHERE amount_range IS NOT NULL AND amount_range <> ''
  AND amount_range NOT IN (
    '$1,001 - $15,000', '$15,001 - $50,000', '$50,001 - $100,000',
    '$100,001 - $250,000', '$250,001 - $500,000', '$500,001 - $1,000,000',
    '$1,000,001 - $5,000,000', '$5,000,001 - $25,000,000', '$25,000,001 - $50,000,000')
GROUP BY 1
ORDER BY 2 DESC;

-- ----------------------------------------------------------------------------
-- 5. Timing
-- ----------------------------------------------------------------------------

-- Trade volume by month
SELECT date_trunc('month', transaction_date) AS month, count(*) AS trades
FROM trades
GROUP BY 1
ORDER BY 1 DESC
LIMIT 24;

-- Notification lag: how many days between transaction and disclosure
SELECT round(avg(notification_date - transaction_date), 1) AS avg_days,
       max(notification_date - transaction_date)           AS max_days
FROM trades
WHERE notification_date IS NOT NULL AND transaction_date IS NOT NULL;

-- Most recent activity
SELECT f.filer, t.ticker, t.owner, t.transaction_type, t.amount_range,
       t.transaction_date, t.raw->>'asset_name' AS asset
FROM trades t
JOIN filings f ON f.id = t.filing_id
WHERE t.transaction_date >= current_date - 30
ORDER BY t.transaction_date DESC
LIMIT 50;

-- ----------------------------------------------------------------------------
-- 6. Owners / relationships
-- ----------------------------------------------------------------------------

-- Trades by owner (self / spouse / joint / child)
SELECT owner, count(*) AS trades,
       count(*) FILTER (WHERE transaction_type ILIKE 'purchase%') AS buys,
       count(*) FILTER (WHERE transaction_type ILIKE 'sale%')    AS sells
FROM trades
GROUP BY 1
ORDER BY 2 DESC;

-- ----------------------------------------------------------------------------
-- 7. Asset classes & comments (raw jsonb)
-- ----------------------------------------------------------------------------

-- Asset-type breakdown (House values: ST=stock, GS=government security,
-- OT=other, CS=corporate security, HN/HN.., OI, OP, PS, etc.)
SELECT raw->>'asset_type' AS asset_type, count(*) AS trades
FROM trades
GROUP BY 1
ORDER BY 2 DESC;

-- Most common free-text comments
SELECT raw->>'comment' AS comment, count(*) AS trades
FROM trades
WHERE raw->>'comment' IS NOT NULL AND raw->>'comment' <> ''
GROUP BY 1
ORDER BY 2 DESC
LIMIT 20;

-- ----------------------------------------------------------------------------
-- 8. Buy / sell skew
-- ----------------------------------------------------------------------------

-- Net buy/sell skew per ticker (most net-bought and most net-sold)
WITH skew AS (
  SELECT ticker,
         count(*) FILTER (WHERE transaction_type ILIKE 'purchase%') AS buys,
         count(*) FILTER (WHERE transaction_type ILIKE 'sale%')    AS sells
  FROM trades
  WHERE ticker IS NOT NULL AND ticker <> ''
  GROUP BY 1
)
SELECT ticker, buys, sells, buys - sells AS net
FROM skew
WHERE buys + sells >= 3
ORDER BY net DESC
LIMIT 15;

-- Same, most net-sold
WITH skew AS (
  SELECT ticker,
         count(*) FILTER (WHERE transaction_type ILIKE 'purchase%') AS buys,
         count(*) FILTER (WHERE transaction_type ILIKE 'sale%')    AS sells
  FROM trades
  WHERE ticker IS NOT NULL AND ticker <> ''
  GROUP BY 1
)
SELECT ticker, buys, sells, buys - sells AS net
FROM skew
WHERE buys + sells >= 3
ORDER BY net ASC
LIMIT 15;