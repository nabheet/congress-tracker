# Backfilling stale House PTR comments

House PTR PDF comments are joined across continuation lines by the parser. A
deployed image predating the join logic stored truncated comments in `trades`
(the `raw->>'comment'` JSONB field). The daily pull is idempotent and skips
filings that already have trades, so simply redeploying does not heal those
rows — they must be re-parsed explicitly.

## When to use

- Comments in `trades.raw->>'comment'` are visibly truncated (missing
  continuation text).
- A parser fix changes how comments are extracted and you want existing
  filings re-processed.

## Why a normal pull doesn't heal them

`run_house` tracks `done` = `SELECT DISTINCT filing_id FROM trades` and skips
any filing already in that set. Re-running the pull therefore never re-parses
a PDF that already produced trades — the stale rows stay put.

## Procedure (preferred: refresh flag)

`REFRESH_HOUSE=1` makes `run_house` ignore the `done` set, re-download every
House PTR PDF, and replace each filing's trades (DELETE + insert). Run inside
a container with network + DB access:

```sh
REFRESH_HOUSE=1 uv run python pull_congress.py
```

Notes:

- Only the House re-parse is forced; Senate and the filing index behave as
  normal. The cron schedule never sets `REFRESH_HOUSE`.
- Each filing's rows are replaced atomically per filing, and filings that
  fail to parse keep their existing rows (they are skipped, not deleted).
- This re-parses the current + prior calendar years (same window as a normal
  run) — see `run_house(years=...)` if a wider window is needed.

## Procedure (one-off, manual)

If only a handful of filings need re-parsing, the same loop works without a
full refresh:

```python
import pull_congress as pc
from pull_congress import db_conn, Session

# 1. Pick the filings to re-parse (e.g. by truncated comments):
#    SELECT filing_id FROM trades WHERE raw->>'comment' LIKE '%...' ...

ids = ["house:20026590", "house:20032149", "house:20033725", "house:20030630", "house:20034351"]

conn = db_conn()
s = Session()
for fid in ids:
    cur = conn.cursor()
    cur.execute("SELECT raw_url FROM filings WHERE id = %s", (fid,))
    row = cur.fetchone()
    if not row:
        continue
    trades = pc.house_ptr_trades(s, row[0])   # re-downloads + parses PDF
    if trades:
        cur.execute("DELETE FROM trades WHERE filing_id = %s", (fid,))
        pc.insert_trades(cur, fid, trades)
        conn.commit()
    print(fid, len(trades))
conn.close()
```

Notes:

- `house_ptr_trades(s, pdf_url)` is the pure parser entry point (network via
  `Session`). It returns a list of trade dicts including `comment`.
- The `DELETE` + `insert_trades` replace the stale rows atomically per
  filing. There is no upsert-on-trades key, so delete-then-insert is
  required.

## When this is needed again

Only after a parser change that alters comment extraction, or if another
pre-join image is ever deployed. `REFRESH_HOUSE=1` makes the re-parse a
single command; a quarterly refresh is an option if comment fidelity matters
more than keeping `fetched_at` history.