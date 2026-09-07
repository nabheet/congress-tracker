#!/usr/bin/env python3
"""Congress stock-trade tracker — pull filings from Senate eFD and House Clerk, upsert into postgres.

Sources:
  - Senate: efdsearch.senate.gov (JSON search API + per-filing HTML)
  - House: disclosures-clerk.house.gov (bulk FD index ZIPs -> structured filing
    index; PTR PDFs parsed for trade-level data)

Idempotent: filings are upserted by natural key; re-runs never duplicate.
"""

import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta

import pdfplumber
import psycopg
from dotenv import load_dotenv

# Load optional .env (local dev; never required — container config comes from
# the environment/compose). Does not override already-set env vars.
load_dotenv()

# ---------------------------------------------------------------- config

# Endpoint URLs (overridable via .env / environment for mirrors or proxies)
SENATE_BASE = os.environ.get("SENATE_BASE", "https://efdsearch.senate.gov")
HOUSE_SITE = os.environ.get("HOUSE_SITE", "https://disclosures-clerk.house.gov")
HOUSE_PUBLIC = os.environ.get("HOUSE_PUBLIC", HOUSE_SITE + "/public_disc")

# FilingType codes from the Clerk's YYYYFD.xml index. P = Periodic Transaction
# Report (the trade filings); the rest are annual/other disclosures. Codes
# C/D/W/B never appear in the member-search page (candidates, staff, etc.).
HOUSE_TYPE_LABELS = {
    "P": "PTR Original",
    "X": "Extension",
    "T": "Termination",
    "H": "New Filer",
    "O": "FD Original",
    "A": "FD Amendment",
    "E": "Term. Exemption",
    "G": "Gift Waiver",
    "C": "Candidate",
    "D": "Designation",
    "W": "Withdrawal",
    "B": "Other",
}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

PG_HOST = os.environ.get("PG_HOST", "db")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "congress")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASS_FILE = os.environ.get("POSTGRES_PASSWORD_FILE", "/run/secrets/postgres_password")
MIGRATIONS_DIR = os.environ.get("MIGRATIONS_DIR", "migrations")


# ---------------------------------------------------------------- http helpers

class RateLimited(Exception):
    pass


class Session:
    def __init__(self):
        self.cookies = {}
        self._jar = urllib.request.HTTPCookieProcessor().cookiejar
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))

    def _sync_cookies(self):
        self.cookies = {c.name: c.value for c in self._jar}

    def _fetch(self, url, data=None, headers=None, retries=4):
        last = None
        for attempt in range(retries):
            try:
                req_headers = {
                    "User-Agent": UA,
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": SENATE_BASE + "/search/home/",
                }
                if headers:
                    req_headers.update(headers)
                body = None
                if data is not None:
                    body = urllib.parse.urlencode(data).encode()
                req = urllib.request.Request(url, data=body, headers=req_headers,
                                             method="POST" if data is not None else "GET")
                with self.opener.open(req, timeout=60) as resp:
                    raw = resp.read()
                self._sync_cookies()
                return raw, resp.geturl()
            except urllib.error.HTTPError as e:
                if e.code in (429, 503, 403):
                    last = e
                    wait = (2 ** attempt) * 10  # 10s, 20s, 40s, 80s
                    print(f"rate-limit {e.code}; retrying in {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                raise
            except urllib.error.URLError as e:
                last = e
                wait = (2 ** attempt) * 10
                print(f"network error {e!r}; retrying in {wait}s", flush=True)
                time.sleep(wait)
                continue
        raise RateLimited(f"giving up after {retries} attempts: {last!r}")

    def request(self, url, data=None, headers=None, retries=4):
        raw, final = self._fetch(url, data=data, headers=headers, retries=retries)
        return raw.decode("utf-8", errors="replace"), final

    def request_bytes(self, url, headers=None, retries=4):
        return self._fetch(url, headers=headers, retries=retries)


def csrf_from(html_text):
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html_text)
    if not m:
        m = re.search(r'csrfmiddlewaretoken[^>]*value="([^"]+)"', html_text)
    return m.group(1) if m else None


# ---------------------------------------------------------------- senate

def senate_session():
    """Reproduce the verified curl flow: home GET -> agreement POST -> data POST."""
    s = Session()
    html_text, _ = s.request(SENATE_BASE + "/search/home/")
    csrf = csrf_from(html_text)
    if not csrf:
        raise RuntimeError("senate: no csrf token on /search/home/")
    s.request(SENATE_BASE + "/search/home/",
              data={"csrfmiddlewaretoken": csrf, "prohibition_agreement": "1"})
    # agreement POST sets sessionid; subsequent requests carry it via cookie jar
    return s


def senate_report_list(s, submitted_start, submitted_end, start=0, length=100):
    """POST /search/report/data/ with the EXACT DataTables payload the site's own
    search page sends (verified working 2026-09-05; anything shorter gets 503).
    """
    payload = {
        "draw": "1",
        "columns[0][data]": "0", "columns[0][name]": "", "columns[0][searchable]": "true",
        "columns[0][orderable]": "true", "columns[0][search][value]": "", "columns[0][search][regex]": "false",
        "columns[1][data]": "1", "columns[1][name]": "", "columns[1][searchable]": "true",
        "columns[1][orderable]": "true", "columns[1][search][value]": "", "columns[1][search][regex]": "false",
        "columns[2][data]": "2", "columns[2][name]": "", "columns[2][searchable]": "true",
        "columns[2][orderable]": "true", "columns[2][search][value]": "", "columns[2][search][regex]": "false",
        "columns[3][data]": "3", "columns[3][name]": "", "columns[3][searchable]": "true",
        "columns[3][orderable]": "true", "columns[3][search][value]": "", "columns[3][search][regex]": "false",
        "columns[4][data]": "4", "columns[4][name]": "", "columns[4][searchable]": "true",
        "columns[4][orderable]": "true", "columns[4][search][value]": "", "columns[4][search][regex]": "false",
        "order[0][column]": "1", "order[0][dir]": "asc",
        "order[1][column]": "0", "order[1][dir]": "asc",
        "start": str(start), "length": str(length),
        "search[value]": "", "search[regex]": "false",
        "report_types": "[11]",           # 11 = Periodic Transaction Report (PTR)
        "filer_types": "[1]",             # Server IGNORES filer_types/report_types (verified 2026-09-06:
                                          # every value, incl. "[4]" candidates / "[5]" former senators,
                                          # returns identical record counts). Kept for compatibility with
                                          # the site's own payload shape.
        "submitted_start_date": submitted_start,   # MM/DD/YYYY HH:MM:SS
        "submitted_end_date": submitted_end,       # empty = no upper bound
        "candidate_state": "", "senator_state": "", "office_id": "",
        "first_name": "", "last_name": "",
    }
    headers = {
        "X-CSRFToken": s.cookies.get("csrftoken", ""),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": SENATE_BASE + "/search/",
        "Origin": SENATE_BASE,
    }
    text, _ = s.request(SENATE_BASE + "/search/report/data/", data=payload, headers=headers)
    return json.loads(text)


def senate_filing_trades(s, uuid):
    """Fetch a PTR print view and extract transactions.

    Verified table columns: #, Transaction Date, Owner, Ticker, Asset Name,
    Asset Type, Type, Amount, Comment (header row skipped).

    Paper filings are scanned images served under /search/view/paper/<uuid>/;
    their PTR URL resolves to the generic search home page (no <tr> rows), so
    this returns [] and the caller logs it. OCR of paper scans is out of scope.
    """
    url = f"{SENATE_BASE}/search/view/ptr/{uuid}/"
    text, _ = s.request(url)
    return senate_trades_from_html(text)


def senate_trades_from_html(text):
    """Parse the PTR print-view HTML table into raw transaction rows.

    Pure function (no I/O): given the HTML of a filing's print view, return
    the non-header rows as lists of cell strings. Paper filings return [].
    """
    rows = []
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S | re.I)]
        if len(cells) < 9:
            continue
        if cells[0] == "#" or cells[1] == "Transaction Date":
            continue  # header row
        rows.append(cells)
    return rows


# ---------------------------------------------------------------- house

def house_filing_index(s, year):
    """Download YYYYFD.zip and return the parsed filing index rows.

    The Clerk publishes the complete filing index at
    public_disc/financial-pdfs/YYYYFD.zip, containing YYYYFD.xml (structured
    index) plus YYYYFD.txt (tab-delimited mirror). Member fields: Prefix /
    Last / First / Suffix / FilingType / StateDst / Year / FilingDate / DocID.
    DocID equals the PDF filename; PTRs (FilingType 'P') are served under
    ptr-pdfs/, everything else under financial-pdfs/. The bulk ZIP regenerates
    frequently and is strictly more complete than the member-search page
    (which omits candidates, staff, and filing dates entirely).
    """
    raw, _ = s.request_bytes(HOUSE_PUBLIC + f"/financial-pdfs/{year}FD.zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        data = zf.read(f"{year}FD.xml")
    root = ET.fromstring(data)
    rows = []
    for m in root.findall("Member"):
        docid = (m.findtext("DocID") or "").strip()
        if not docid:
            continue
        code = (m.findtext("FilingType") or "").strip()
        first = (m.findtext("First") or "").strip()
        last = (m.findtext("Last") or "").strip()
        filed = (m.findtext("FilingDate") or "").strip()
        filed_date = None
        if filed:
            try:
                filed_date = datetime.strptime(filed, "%m/%d/%Y").date()
            except ValueError:
                filed_date = None
        is_ptr = code == "P"
        rows.append({
            "id": f"house:{docid}",
            "source": "house",
            "filer": f"{first} {last}".strip(),
            "report_type": HOUSE_TYPE_LABELS.get(code, code or "Unknown"),
            "filed_at": filed_date,
            "raw_url": (HOUSE_PUBLIC + f"/ptr-pdfs/{year}/{docid}.pdf" if is_ptr
                        else HOUSE_PUBLIC + f"/financial-pdfs/{year}/{docid}.pdf"),
            "is_ptr": is_ptr,
        })
    return rows


# PTR PDF text-layer parsing -------------------------------------------------
#
# E-filed PTRs are landscape tables. pdfplumber's line extraction wraps each
# logical table row across 2-4 lines, so we locate transaction anchors (type
# code + two dates + amount range, possibly split across lines) and assemble
# the asset name/ticker/owner from adjacent lines. Labels are letter-spaced
# with NUL padding in the font ("Filed Status:" -> "F S:"), so control
# characters are stripped before matching.

TXN_RE = re.compile(
    r"([A-Z])\s*(\(partial\))?\s+"
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"
    r"(\$\d{1,3}(?:,\d{3})*)(?: - (\$\d{1,3}(?:,\d{3})*))?"
)
AMOUNT_RE = re.compile(r"\$\d{1,3}(?:,\d{3})*")
OWNER_PREFIX_RE = re.compile(r"^(SP|JT|Self)\b\s*(.*)$", re.I)
TICKER_RE = re.compile(r"\(([^()]*)\)\s*(\[[A-Z0-9]+\])?")
CODE_RE = re.compile(r"\[([A-Z0-9]+)\]")
TYPE_LABEL = {"P": "Purchase", "S": "Sale", "X": "Exchange", "E": "Exchange"}
BOUNDARY_RE = re.compile(
    r"^(Filing ID|Name:|Status:|State/District:|ID Owner|Type Date|\$200\?|"
    r"\* For the complete list|I CERTIFY|Digitally Signed|Clerk of the House|"
    r"Yes No|Real estate investments|Stocks, Bonds, & Mutual Funds|"
    r"P T R|F I|I V D|I P O|C S|T$|C$|"
    r"[A-Z] ?[A-Z]?:)"
)


def house_ptr_trades(s, pdf_url):
    """Download a PTR PDF and parse its transactions from the text layer.

    Returns a list of trade dicts. Scanned/image-only PDFs (no text layer)
    yield [] and are logged so they can be revisited.
    """
    try:
        raw, _ = s.request_bytes(pdf_url)
    except Exception as e:
        print(f"house: pdf fetch failed {pdf_url}: {e!r}", flush=True)
        return []
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        print(f"house: pdf parse failed {pdf_url}: {e!r}", flush=True)
        return []
    if not text.strip():
        print(f"house: no text layer (scanned?) {pdf_url}", flush=True)
        return []
    return house_trades_from_text(text)


def house_trades_from_text(text):
    """Parse PTR transactions from the extracted PDF text layer.

    Pure function (no I/O): accepts the raw text of a filing's PDF pages and
    returns the list of trade dicts. Empty/scanned (no text layer) input
    yields [] so callers can log and move on.
    """
    if not text.strip():
        return []
    lines = []
    for l in text.splitlines():
        l = re.sub(r"[\x00-\x1f]", "", l)   # NUL-padded letter-spaced labels
        l = re.sub(r"\s+", " ", l).strip()
        if l:
            lines.append(l)

    trades = []
    i = 0
    n = len(lines)
    while i < n:
        m = TXN_RE.search(lines[i])
        if not m:
            i += 1
            continue
        code = m.group(1)
        partial = m.group(2)
        txn_date = m.group(3)
        notif_date = m.group(4)
        amt1 = m.group(5)
        amt2 = m.group(6)
        prefix = lines[i][: m.start()].strip()

        # Amount ranges sometimes split across lines ("$15,001 -" then
        # "Stock (STT) [ST] $50,000"). Consume the continuation, keeping any
        # asset text that rides along with the second amount.
        j = i + 1
        tail = lines[i][m.end():].strip()
        if amt2 is None and tail.endswith("-"):
            while j < n:
                am = AMOUNT_RE.search(lines[j])
                if am:
                    amt2 = am.group(0)
                    if am.start() > 0:
                        prefix += " " + lines[j][: am.start()].strip()
                    j += 1
                    break
                j += 1

        # Owner may be a line-start prefix (SP/JT) or an "S O:" line below.
        owner = None
        om = OWNER_PREFIX_RE.match(prefix)
        if om:
            owner = {"SP": "Spouse", "JT": "Joint", "SELF": "Self"}.get(
                om.group(1).upper(), om.group(1))
            prefix = om.group(2).strip()
        k = i + 1
        while k < n and k <= i + 6:
            if lines[k].startswith("S O:"):
                owner = lines[k][4:].strip()
                break
            if TXN_RE.search(lines[k]) or BOUNDARY_RE.search(lines[k]):
                break
            k += 1
        if owner is None:
            owner = "Self"

        # Asset text: the anchor-line prefix, or the wrapped lines above when
        # the anchor carries no asset text.
        asset_text = prefix
        if not asset_text:
            k = i - 1
            parts = []
            while k >= 0:
                lk = lines[k]
                if TXN_RE.search(lk) or BOUNDARY_RE.search(lk):
                    break
                parts.insert(0, lk)
                k -= 1
            asset_text = " ".join(parts)

        # Ticker/code ride in the asset text itself ("Invesco QQQ [OT]") or on
        # the following wrapped lines: "(AMZN) [ST]", or wrapped asset text
        # then a ticker/code line ("Indust Avg ETF Trust NYSEARCA:" +
        # "DIA [OT]"). Date notes like "12/01/25 [GS]" must not be read as a
        # ticker, so bare-ticker candidates must start with a letter.
        asset_code = None
        ticker = None
        wrapped = []
        k = j
        while k < n and k <= i + 3:
            lk = lines[k]
            if TXN_RE.search(lk) or BOUNDARY_RE.search(lk):
                break
            if lk.startswith(("D:", "D :")):
                break                  # a comment line ends the asset block
            cm = CODE_RE.search(lk)
            tm = TICKER_RE.search(lk)
            if cm:
                if asset_code is None:
                    asset_code = cm.group(1)
                if ticker is None:
                    if tm:
                        ticker = tm.group(1)
                    else:
                        bt = re.search(
                            r"(?<![A-Za-z0-9.])([A-Za-z][A-Za-z0-9.]{0,4})\s*\[[A-Z0-9]+\]\s*$",
                            lk)
                        if bt:
                            ticker = bt.group(1)
                k += 1
                break                      # a code line ends the asset block
            if tm and ticker is None:
                ticker = tm.group(1)
                k += 1
                continue
            wrapped.append(lk)              # more wrapped asset text
            k += 1
        if wrapped:
            asset_text = " ".join([asset_text] + wrapped)

        cm = CODE_RE.search(asset_text)
        if cm and asset_code is None:
            asset_code = cm.group(1)
        tm = TICKER_RE.search(asset_text)
        if tm and ticker is None:
            ticker = tm.group(1)
            asset_text = (asset_text[: tm.start()] + asset_text[tm.end():]).strip()
        if ticker is None:
            bt = re.search(
                r"(?<![A-Za-z0-9.])([A-Za-z][A-Za-z0-9.]{0,4})\s*\[[A-Z0-9]+\]\s*$",
                asset_text)
            if bt:
                ticker = bt.group(1)
        asset_name = CODE_RE.sub("", asset_text).strip(" ,-:")
        asset_name = re.sub(r"\s{2,}", " ", asset_name)

        # Comment line ("D:" / "D :"), if any, before the next anchor.
        # Continuation lines (indented text that is not a new txn/anchor)
        # are joined into the comment.
        comment = None
        k = i + 1
        while k < n and k <= i + 10:
            if lines[k].startswith(("D:", "D :")):
                comment = re.sub(r"^D\s*:", "", lines[k]).strip()
                k += 1
                while k < n and k <= i + 10:
                    lk = lines[k]
                    if TXN_RE.search(lk) or BOUNDARY_RE.search(lk) or lk.startswith(
                            ("* For the complete list", "I CERTIFY", "Digitally Signed")):
                        break
                    if lk.startswith(("D:", "D :")):
                        comment = " ".join(
                            [comment, re.sub(r"^D\s*:", "", lk).strip()]).strip()
                        k += 1
                        continue
                    comment = " ".join([comment, lk.strip()]).strip()
                    k += 1
                break
            if TXN_RE.search(lines[k]) or lines[k].startswith(
                    ("* For the complete list", "I CERTIFY", "Digitally Signed")):
                break
            k += 1

        txn_type = TYPE_LABEL.get(code, code)
        if partial:
            txn_type += " (partial)"
        amount = amt1 + (f" - {amt2}" if amt2 else "")
        trades.append({
            "owner": owner,
            "ticker": ticker,
            "asset_name": asset_name,
            "asset_type": asset_code,
            "type": txn_type,
            "amount": amount,
            "txn_date": txn_date,
            "notif_date": notif_date,
            "comment": comment,
        })
        i += 1

    if not trades and COL_HEADER_RE.search("\n".join(lines)):
        trades = house_ptr_trades_columnar(lines)
    return trades


# Columnar-layout PTRs -------------------------------------------------------
#
# Some e-filed PTRs (e.g. Matsui 2026/20033695) print the amount range on the
# line AFTER the anchor instead of riding on it, and close the anchor row with
# an "Over" bracket marker. TXN_RE cannot see those (no "$" on the anchor
# line), so this fallback parses that variant. It fires only when the standard
# parser found nothing AND the "ID Owner Asset..." table header is present,
# so scanned/paper filings are never fed to it.

COL_HEADER_RE = re.compile(r"ID\s+Owner\s+Asset|Owner Asset Transaction Date")
COL_ANCHOR_RE = re.compile(
    r"^(SP|JT|S|SELF|SO)\s+"
    r"(?P<asset>.*?)\s+"
    r"(?P<code>[A-Z])\s+"
    r"(?P<d1>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<d2>\d{1,2}/\d{1,2}/\d{4})\s*"
    r"(?P<tail>.*)$"
)


def house_ptr_trades_columnar(lines):
    """Parse the amount-on-next-line PTR variant into trade dicts."""
    trades = []
    i = 0
    n = len(lines)
    while i < n:
        m = COL_ANCHOR_RE.match(lines[i])
        if not m:
            i += 1
            continue
        owner = {"SP": "Spouse", "JT": "Joint", "SELF": "Self", "SO": "Spouse"}.get(
            m.group(1).upper(), m.group(1))
        code = m.group("code")
        txn_date, notif_date = m.group("d1"), m.group("d2")
        asset_text = m.group("asset").strip()
        tail = m.group("tail").strip()
        txn_type = TYPE_LABEL.get(code, code)

        # Amount and asset-type code ride on the following lines ("[GS]
        # $1,000,000"); a trailing "Over" marker widens the bracket the same
        # way the old paper form printed it.
        amount = None
        asset_code = None
        comment = None
        k = i + 1
        while k < n and k <= i + 4:
            lk = lines[k]
            if COL_ANCHOR_RE.match(lk) or TXN_RE.search(lk):
                break
            if lk.startswith(("F S:", "S O:")):
                k += 1
                continue
            if BOUNDARY_RE.search(lk):
                break
            cm = CODE_RE.search(lk)
            if cm and asset_code is None:
                asset_code = cm.group(1)
            am = AMOUNT_RE.search(lk)
            if am and amount is None:
                amount = am.group(0)
            if lk.startswith(("D:", "D :")):
                comment = re.sub(r"^D\s*:", "", lk).strip()
                k += 1
                while k < n and k <= i + 4:
                    clk = lines[k]
                    if COL_ANCHOR_RE.match(clk) or TXN_RE.search(clk) or \
                            BOUNDARY_RE.search(clk) or clk.startswith(("F S:", "S O:")):
                        break
                    if clk.startswith(("D:", "D :")):
                        comment = " ".join(
                            [comment, re.sub(r"^D\s*:", "", clk).strip()]).strip()
                        k += 1
                        continue
                    comment = " ".join([comment, clk.strip()]).strip()
                    k += 1
                break
            if TXN_RE.search(lk):
                break
            k += 1
        if amount is None:
            i += 1
            continue
        if tail.startswith("Over"):
            amount = "Over " + amount

        trades.append({
            "owner": owner,
            "ticker": None,
            "asset_name": asset_text,
            "asset_type": asset_code,
            "type": txn_type,
            "amount": amount,
            "txn_date": txn_date,
            "notif_date": notif_date,
            "comment": comment,
        })
        i += 1
    return trades


def run_house(conn, years=None):
    """Pull House filings from the Clerk's bulk index ZIPs and parse PTR trades.

    Trade-level data exists only inside the individual PTR PDFs, so each PTR
    filing that has no trades yet is downloaded and parsed from its text layer
    (0.2s pause per PDF; index rows commit first so an interrupted run keeps
    its progress). This also backfills PTRs indexed before stage 2 existed.
    """
    s = Session()
    if years is None:
        today = date.today()
        years = [today.year, today.year - 1]  # current + prior year catches late filings
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT filing_id FROM trades")
        done = {r[0] for r in cur.fetchall()}
    new_filings = 0
    new_trades = 0
    for year in sorted(years):
        rows = house_filing_index(s, year)
        print(f"house {year}: {len(rows)} filings in index", flush=True)
        for filing in rows:
            with conn.cursor() as cur:
                if upsert_filing(cur, filing):
                    new_filings += 1
                    conn.commit()  # index row durable before the slow PDF fetch
            if not filing["is_ptr"] or filing["id"] in done:
                continue
            trades = house_ptr_trades(s, filing["raw_url"])
            if trades:
                with conn.cursor() as cur:
                    insert_trades(cur, filing["id"], trades)
                new_trades += len(trades)
                conn.commit()
            time.sleep(0.2)  # be polite to the PDF server
    conn.commit()
    return new_filings, new_trades


# ---------------------------------------------------------------- db

def db_conn():
    with open(PG_PASS_FILE) as fh:
        pw = fh.read().strip()
    return psycopg.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                           user=PG_USER, password=pw, connect_timeout=10)


def run_migrations(conn):
    """Apply migrations/*.sql in filename order, tracking applied files in
    schema_version. Each file runs in its own transaction."""
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                           version    TEXT PRIMARY KEY,
                           applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    conn.commit()
    with conn.cursor() as cur:
        applied = {r[0] for r in cur.execute("SELECT version FROM schema_version").fetchall()}
    for f in sorted(p for p in os.listdir(MIGRATIONS_DIR) if p.endswith(".sql")):
        if f in applied:
            continue
        with open(os.path.join(MIGRATIONS_DIR, f)) as fh:
            sql = fh.read()
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute("INSERT INTO schema_version (version) VALUES (%s)", (f,))
        conn.commit()
        print(f"applied migration {f}", flush=True)
    return len(applied)


def upsert_filing(cur, filing):
    cur.execute(
        """INSERT INTO filings (id, source, filer, report_type, filed_at, raw_url)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE
             SET filed_at = COALESCE(filings.filed_at, EXCLUDED.filed_at)
           RETURNING (xmax = 0) AS inserted""",
        (filing["id"], filing["source"], filing["filer"], filing.get("report_type"),
         filing.get("filed_at"), filing.get("raw_url")),
    )
    return cur.fetchone()[0]  # True if newly inserted, False if already known


def insert_trades(cur, filing_id, trades):
    for t in trades:
        cur.execute(
            """INSERT INTO trades (filing_id, ticker, owner, transaction_type,
                                   amount_range, transaction_date, notification_date, raw)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (filing_id, t.get("ticker"), t.get("owner"), t.get("type"),
             t.get("amount"), t.get("txn_date"), t.get("notif_date"),
             json.dumps(t)),
        )


# ---------------------------------------------------------------- main

def run_senate(conn, start=None, end=None):
    s = senate_session()
    end = end or date.today()
    if start is None:
        start = end - timedelta(days=int(os.environ.get("DAYS_BACK", "30")))
    # Site expects "MM/DD/YYYY HH:MM:SS"; empty end_date = no upper bound (verified
    # that's what the site's own search page sends).
    start_str = f"{start:%m/%d/%Y} 00:00:00"
    end_str = "" if end >= date.today() else f"{end:%m/%d/%Y} 23:59:59"
    new_filings = 0
    new_trades = 0
    offset = 0
    while True:
        resp = senate_report_list(s, start_str, "", start=offset, length=100)
        data = resp.get("data") or []
        if not data:
            break
        for row in data:
            # Row shape (verified): [first_name, last_name, office, link_html, filed_date]
            link_href = re.search(r'href="([^"]+)"', row[3] or "")
            if not link_href:
                continue
            uuid = link_href.group(1).rstrip("/").split("/")[-1]
            type_m = re.search(r">([^<]+)</a>", row[3])
            filing = {
                "id": f"senate:{uuid}",
                "source": "senate",
                "filer": f"{row[0]} {row[1]}".strip(),
                "report_type": type_m.group(1).strip() if type_m else None,
                "filed_at": datetime.strptime(row[4], "%m/%d/%Y").date() if len(row) > 4 else None,
                "raw_url": SENATE_BASE + link_href.group(1),
            }
            with conn.cursor() as cur:
                if not upsert_filing(cur, filing):
                    continue  # already have it
                # fetch trades for new filing
                rows = senate_filing_trades(s, uuid)
                trades = []
                if "/paper/" in link_href.group(1):
                    print(f"senate: paper filing (scanned, no table) {uuid}", flush=True)
                for cells in rows:
                    # [0]=#, [1]=txn date, [2]=owner, [3]=ticker, [4]=asset name,
                    # [5]=asset type, [6]=type, [7]=amount, [8]=comment
                    trades.append({
                        "owner": cells[2],
                        "ticker": cells[3],
                        "asset_name": cells[4],
                        "asset_type": cells[5],
                        "type": cells[6],
                        "amount": cells[7],
                        "txn_date": cells[1],
                        "comment": cells[8],
                    })
                insert_trades(cur, filing["id"], trades)
                new_trades += len(trades)
            new_filings += 1
            time.sleep(0.3)  # be polite
        if len(data) < 100:
            break
        offset += 100
    conn.commit()
    return new_filings, new_trades


def main():
    log = lambda msg: print(f"{datetime.now().isoformat()} {msg}", flush=True)
    start = end = None
    if os.environ.get("START_DATE"):
        start = date.fromisoformat(os.environ["START_DATE"])
    if os.environ.get("END_DATE"):
        end = date.fromisoformat(os.environ["END_DATE"])
    log(f"starting (start={start} end={end} days_back={os.environ.get("DAYS_BACK", "30")})")
    try:
        with db_conn() as conn:
            run_migrations(conn)
            nf, nt = run_senate(conn, start=start, end=end)
            log(f"senate done: {nf} new filings, {nt} new trades")
            hf, ht = run_house(conn)
            log(f"house done: {hf} new filings, {ht} new trades")
    except Exception as e:
        log(f"ERROR: {e!r}")
        sys.exit(1)
    log("done")


if __name__ == "__main__":
    main()