-- 002_ptr_parse_attempts.sql — stop re-parsing filings that never yield trades.
--
-- A PTR PDF that produces zero trade rows is never marked done, so every run
-- downloads and re-parses it again. The scanned ones cost minutes of OCR each
-- and are re-paid daily for output the parser discards. Count attempts and let
-- the caller give up after a few.
--
-- REFRESH_HOUSE resets these so a parser improvement can retry the backlog.

ALTER TABLE filings ADD COLUMN IF NOT EXISTS parse_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE filings ADD COLUMN IF NOT EXISTS parse_attempted_at TIMESTAMPTZ;

-- The skip query filters on attempts, and most filings are at zero.
CREATE INDEX IF NOT EXISTS idx_filings_parse_attempts
    ON filings(parse_attempts) WHERE parse_attempts > 0;

-- Backfill: every PTR PDF with no trades has already been downloaded and
-- re-parsed on every run since it was indexed, so mark them exhausted (999 is
-- above any sane cap) rather than making the next runs pay for them again.
-- REFRESH_HOUSE resets these, which is how a better parser retries them.
UPDATE filings f
   SET parse_attempts = 999,
       parse_attempted_at = now()
 WHERE f.raw_url LIKE '%ptr-pdfs%'
   AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.filing_id = f.id);
