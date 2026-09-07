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

## How it works

`upsert_filing` returns `False` for filings that already exist, and the main
loop does `continue  # already have it` — so a normal pull never re-parses
known filings. A backfill must force a re-parse of a specific filing's PDF
and replace its stored trades.

## Procedure (one-off)

Run inside the container with network + DB access:

```sh
docker exec -it <home_container> python - <<'EOF'
import pull_congress as pc

# 1. Pick the filings to re-parse (e.g. by truncated comments):
#    SELECT filing_id FROM trades
#    WHERE raw->>'comment' LIKE '%...%' AND ...
ids = ["20026590", "20032149", "20033725", "20030630", "20034351"]

with pc.Session() as s, s.cursor() as cur:
    for fid in ids:
        filing = pc.fetch_house_filing(cur, fid)      # row incl. raw_url
        trades = pc.parse_house_filing(s, filing)     # re-downloads + parses PDF
        pc.delete_trades(cur, fid)                    # remove stale rows
        pc.insert_trades(cur, fid, trades)            # insert fresh rows
        print(fid, len(trades))
EOF
```

Replace `fetch_house_filing` / `parse_house_filing` / `delete_trades` with the
actual function names in `pull_congress.py` if they differ; the pattern is:
fetch the filing row, re-parse its PDF with the current parser, delete old
trades for that filing, insert the new ones.

## When this is needed again

Only after a parser change that alters comment extraction, or if another
pre-join image is ever deployed. A quarterly re-parse of all House filings is
an option if comment fidelity matters more than keeping `fetched_at` history;
otherwise run this manually when truncation is observed.