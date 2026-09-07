# congress-tracker

Daily congressional stock-trade tracker. Pulls financial disclosure filings
(Senate eFD + House Clerk) into a Postgres database, with one row per filing
and one row per disclosed trade.

## What it does

- **Senate**: eFD (electronic Financial Disclosure) — periodic transaction
  reports (PTRs) with ticker-level trade data, via the efdsearch.senate.gov
  JSON API. The API ignores `filer_types`/`report_types`, so the pull captures
  every PTR in the window regardless of filer class (senators, candidates, and
  former senators alike). Paper filings (scanned, no parseable table) are
  logged and skipped.
- **House**: Clerk of the House financial disclosures. The bulk index
  (`public_disc/financial-pdfs/YYYYFD.zip` → `YYYYFD.xml`) provides one row
  per filing — member, filing type, filing date, PDF URL (PTRs live under
  `ptr-pdfs/`). Trade-level data exists only inside the individual PTR PDFs,
  so each new PTR is downloaded and parsed from its PDF text layer (no OCR:
  scanned image-only PDFs are logged and skipped). Two text layouts are
  handled: the common amount-on-anchor-line form, plus a columnar variant
  where the amount range rides on the following line.
- Runs daily via cron (supercronic) in a Docker container; idempotent —
  re-runs skip filings that already have parsed trades.

## Schema

Managed by SQL migrations in `migrations/` (applied in filename order, tracked
in the `schema_version` table). See `migrations/001_init.sql` for the current
schema: `filings` (natural-keyed, one per disclosure) and `trades`
(ticker, owner, transaction type, amount range, dates, raw JSONB payload).

## Layout

```
pull_congress.py   main script (fetch → parse → upsert)
migrations/        versioned SQL migrations
Dockerfile         container image
crontab            schedule (supercronic format)
pyproject.toml     project + pinned runtime deps
uv.lock            lockfile (frozen installs)
.env.example       template for local config (copy to .env)
docs/backfill.md   re-parsing filings with stale comment data
```

## Running locally

Uses [uv](https://docs.astral.sh/uv/). Dev tools (pyright) live in the
`dev` dependency group.

```sh
cp .env.example .env   # edit: PG_HOST, POSTGRES_PASSWORD_FILE, etc.
uv sync                # create .venv, install runtime deps + dev tools
uv run pyright pull_congress.py   # type check
uv run python pull_congress.py    # run a pull
```

The script loads `.env` if present (real environment variables always
win), so local config lives in one file.

Runtime deps are pinned exactly in `pyproject.toml`; `uv.lock` pins the
full resolved tree. Rebuild the lockfile after dependency changes with
`uv lock`.

Environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `PG_HOST` / `PG_PORT` | `db` / `5432` | Postgres host / port |
| `PG_DB` / `PG_USER` | `congress` / `postgres` | Database / user |
| `POSTGRES_PASSWORD_FILE` | `/run/secrets/postgres_password` | File containing the DB password (container secret) |
| `SENATE_BASE` | `https://efdsearch.senate.gov` | Senate eFD site base |
| `HOUSE_SITE` | `https://disclosures-clerk.house.gov` | House Clerk site base |
| `HOUSE_PUBLIC` | `https://disclosures-clerk.house.gov/public_disc` | House public disclosure dir (ZIP index + PDFs) |
| `DAYS_BACK` | `30` | Rolling window of Senate days to pull when no explicit range |
| `START_DATE` / `END_DATE` | — | Explicit ISO date range (overrides `DAYS_BACK`; Senate only) |
| `REFRESH_HOUSE` | `0` | Boolean (`1`/`true`/`yes`/`on`); re-parse existing House PTR filings and replace their trades (see backfills below) |
| `MIGRATIONS_DIR` | `migrations` | Directory of `.sql` migration files |

## Data coverage and backfills

`DAYS_BACK` is a rolling window of Senate submission dates to query on each
run. A window of 30 days keeps the API request load low and catches the
overwhelming majority of filings, which the Senate publishes electronically
within days of submission. Two edge cases can fall outside a short window
and would be missed permanently (the pull only ever re-queries the most
recent `DAYS_BACK` days):

- **paper filings**, which can take weeks to months to appear, and
- **late-published electronic filings** (e.g. a senator who files near the
  45-day deadline when the site publishes the filing a month or more later).

To sweep up stragglers, periodically run one wider pass. It is idempotent
(rows upsert by natural key), so re-pulling an overlapping range is safe:

```sh
DAYS_BACK=365 uv run python pull_congress.py   # or set an explicit range:
START_DATE=2025-01-01 END_DATE=2026-12-31 uv run python pull_congress.py
```

A quarterly backfill (every ~3 months) is a reasonable cadence.

### Re-parsing House PTR filings

House comments are joined across continuation lines during parsing. Filings
stored by an older image can hold truncated comments; the daily pull skips
filings that already have trades, so redeploying does not heal them. Force a
full House re-parse with:

```sh
REFRESH_HOUSE=1 uv run python pull_congress.py
```

Each PTR is re-downloaded, re-parsed, and its trades replaced (per-filing
DELETE + insert); filings that fail to parse keep their existing rows. The
cron schedule never sets this. See `docs/backfill.md` for details and a
targeted (single-filing) alternative.

## Deploying

This repo ships the script and a container image; it does not assume any
particular orchestrator. The script only needs three things:

- a Postgres database reachable via `PG_*` (see `.env.example`),
- the DB password in a file named by `POSTGRES_PASSWORD_FILE`,
- a way to run `pull_congress.py` on the schedule in `crontab` (via
  supercronic, which runs as PID 1 in the image).

For a local run (not in a container), copy the template to a real `.env`
and fill in your values — `.env` is gitignored, so it never gets committed:

```sh
cp .env.example .env   # then edit PG_* / DAYS_BACK / etc.
uv run python pull_congress.py
```

Deploy however you like — plain `docker run`, compose, Swarm, Kubernetes, or
a host cron using `uv run python pull_congress.py`. Any new `migrations/002_*.sql`
is applied automatically on the next run. Build the image with:

```sh
docker build -t congress-pull:latest .
```

## Adding a migration

1. Create `migrations/002_<name>.sql` with the SQL.
2. Rebuild + redeploy your service (restarting the container is enough).
3. The runner applies it once and records it in `schema_version`.

## Analyzing the data

Ready-to-run SQL analysis queries live in `queries/analysis.sql` — top
filers, per-ticker trade history, largest disclosed amounts, buy/sell
skew, asset-class breakdowns, and more. Run them against your `congress`
database:

```sh
psql "$DATABASE_URL" -f queries/analysis.sql
# parameterized example — all trades for one ticker:
psql "$DATABASE_URL" -v ticker=NVDA -f queries/analysis.sql
```

The file is self-documenting; every section shows its output columns in a
comment. It works against any database populated by this tracker, so you
can use the same queries on your own data.