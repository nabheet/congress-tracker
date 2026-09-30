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
import shutil
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
        "report_types": "[11]",           # Inert: the server ignores filer_types/report_types
        "filer_types": "[1]",             # (verified 2026-09-06: every value, incl. "[4]" candidates /
                                          # "[5]" former senators, returns identical record counts).
                                          # "[11]" is the site's own PTR value; both are kept verbatim
                                          # only because the endpoint rejects payloads that omit them.
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
# Asset-name tails that end a code line but are not tickers ("HM Companies LLC
# [OI]", "U.S. Treasury Bills [GS]", "... Rev Bonds [GS]"). Bare-ticker
# candidates are additionally required to be ALL-CAPS (real tickers like "DIA",
# "QQQ", "BRK.B" are), which rejects the title-case tails. This set covers the
# all-caps tails that the case rule alone cannot reject.
NON_TICKER_WORDS = {
    "BILLS", "BONDS", "CORP", "ETF", "FUND", "INC", "LLC", "LP",
    "NOTES", "STOCK", "TRUST", "UNITS", "CLASS", "WTS", "SHRS",
    "DEP", "REIT", "ADR", "GDR", "CEF", "MF",
}
BOUNDARY_RE = re.compile(
    r"^(Filing ID|Name:|Status:|State/District:|ID Owner|Type Date|\$200\?|"
    r"\* For the complete list|I CERTIFY|Digitally Signed|Clerk of the House|"
    r"Yes No|Real estate investments|Stocks, Bonds, & Mutual Funds|"
    r"P T R|F I|I V D|I P O|C S|T$|C$|"
    r"(?!D ?:)[A-Z] ?[A-Z]?:)"
)


def house_ptr_trades(s, pdf_url):
    """Download a PTR PDF and parse its transactions from the text layer.

    Returns a list of trade dicts. Scanned/image-only PDFs (no text layer)
    fall back to OCR when tesseract is available; otherwise they yield [] and
    are logged so they can be revisited.
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
        return house_ptr_trades_ocr(raw, pdf_url)
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
    # Index roles across this parser: `i` is the anchor line (carries the
    # TXN_RE: type code + both dates + amount). Because pdfplumber wraps each
    # logical table row across 2-4 lines, the fields around an anchor can
    # spill onto neighbors, so three separate forward/backward cursors walk
    # the lines around `i`:
    #   `j` — amount continuation (second half of a split amount range);
    #   `k` — the S O: owner line, asset text, and the D: comment block.
    # All three stop at a TXN_RE (next anchor) or a BOUNDARY_RE (a label /
    # filing-boundary line) so a wrapped row never bleeds into the next one.
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
                        if bt and bt.group(1).isupper() and bt.group(1) not in NON_TICKER_WORDS:
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
            if bt and bt.group(1).isupper() and bt.group(1) not in NON_TICKER_WORDS:
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
            if TXN_RE.search(lines[k]) or BOUNDARY_RE.search(lines[k]) or \
                    lines[k].startswith(
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


# OCR fallback for scanned House PTRs ----------------------------------------
#
# ~12% of House PTR PDFs (112/912) are image-only scans of the paper form with
# no text layer, so pdfplumber extracts nothing. We render the page with
# pypdfium2, detect the form's gridlines, OCR each data-row cell with
# tesseract, detect checkbox marks by a diagonal-stroke signature, then
# synthesize e-filed-format lines and feed them through the existing
# house_trades_from_text() parser so output shape is identical. Every row is
# tagged source="ocr" + confidence (high/medium/low); low-confidence rows are
# logged for review and never silently ingested.
#
# The paper form is the standard "A-K" PTR table. Column layout (native scan
# width fractions) and the 12 amount checkboxes were verified against the
# official ethics.house.gov form and multiple real Clerk PTR scans.

PTR_AMOUNT_RANGES = [
    "$200 - $1,000",
    "$1,001 - $15,000",
    "$15,001 - $50,000",
    "$50,001 - $100,000",
    "$100,001 - $250,000",
    "$250,001 - $500,000",
    "$500,001 - $1,000,000",
    "$1,000,001 - $5,000,000",
    "$5,000,001 - $25,000,000",
    "$25,000,001 - $50,000,000",
    "Over $50,000,000",
    "Over $1,000,000",        # wide box: spouse/dependent-child asset
]

# Column boundaries as fractions of the form's width. Index i gives the start
# fraction of column i; a trailing 1.0 marks the right edge. Order (verified):
# owner | ticker | asset | type P/S/X (3 boxes) | txn date | notif date |
# 11 amount boxes | wide spouse/DC amount box.
_PTR_COL_FRACS = [
    0.0000,  # 0  left edge
    0.0861,  # 1  owner | ticker        (146/1696)
    0.1132,  # 2  ticker | asset        (192/1696)
    0.2995,  # 3  asset | type          (508/1696)
    0.3296,  # 4  type P | S            (559/1696)
    0.3585,  # 5  type S | X            (608/1696)
    0.3874,  # 6  type X | txn date     (657/1696)
    0.4729,  # 7  txn | notif date      (802/1696)
    0.5383,  # 8  notif | amount        (913/1696)
    0.5743, 0.6097, 0.6450, 0.6810, 0.7158, 0.7512,
    0.7860, 0.8213, 0.8567, 0.8921, 0.9269,  # amount box edges
    1.0000,  # 20 right edge
]
# Expected number of vertical gridlines on the form (incl. left/right edges).
_PTR_EXPECTED_GRIDLINES = len(_PTR_COL_FRACS)

# Instruction rows ("ASSET NAME: PROVIDE FULL NAME...") can bleed into the
# OCR of a real row; any asset cell containing one of these phrases is a
# form header/footer, not a trade.
_PTR_OCR_GARBAGE = (
    "ASSET NAME", "PROVIDE FULL", "ANSWERED", "THE ATTACHED",
    "TICKER SYMBE", "FULL NAME", "YOU ANSWERED",
)
# Data-row band starts as fractions of the form's height (native 2200px).
_PTR_ROW_FRACS = [0.6305, 0.6568, 0.6827, 0.7082, 0.7341]
_PTR_HEADER_BOTTOM_FRAC = 0.614  # below this lie the data rows


def _tess_available():
    return shutil.which("tesseract") is not None


def _render_ptr_pages(raw, scale=2):
    """Render a PTR PDF's pages to PIL grayscale images via pypdfium2."""
    import pypdfium2 as pdfium
    from PIL import Image

    pages = []
    with pdfium.PdfDocument(raw) as doc:
        for page in doc:
            img = page.render(scale=int(scale)).to_pil()
            pages.append(img.convert("L"))
    return pages


def _render_ptr_pages_hires(raw):
    """Yield (SC6, SC12) grayscale renders per page, one page at a time.

    SC12 pages are ~130MB, so they are rendered lazily per page instead of
    materializing the whole document. Only date-cell OCR uses these renders.
    """
    import pypdfium2 as pdfium
    from PIL import Image

    with pdfium.PdfDocument(raw) as doc:
        for page in doc:
            yield (page.render(scale=6).to_pil().convert("L"),
                   page.render(scale=12).to_pil().convert("L"))


def _detect_gridlines(img, vertical=True, min_frac=0.55):
    """Detect long dark gridlines (table rules) in a rendered page.

    Returns clustered line positions. For vertical lines, a column is a line
    when its dark-pixel share of the row band exceeds min_frac; positions are
    clustered so a 1-2px rule yields one coordinate.
    """
    w, h = img.size
    px = img.load()
    if vertical:
        lo, hi = int(h * 0.55), int(h * 0.80)     # data-row band
        scores = []
        for x in range(w):
            dark = sum(1 for y in range(lo, hi) if px[x, y] < 128)
            scores.append(dark / (hi - lo))
    else:
        lo, hi = int(w * 0.05), int(w * 0.95)
        scores = []
        for y in range(h):
            dark = sum(1 for x in range(lo, hi) if px[x, y] < 128)
            scores.append(dark / (hi - lo))
    lines = []
    in_line = False
    start = 0
    for i, s in enumerate(scores):
        if s >= min_frac and not in_line:
            start = i
            in_line = True
        elif s < min_frac and in_line:
            lines.append((start + i - 1) / 2)
            in_line = False
    if in_line:
        lines.append((start + len(scores) - 1) / 2)
    return lines


def _ptr_layout(img):
    """Map the form layout onto a rendered page; verify against gridlines.

    Returns (col_boxes, row_bands, structural_ok) where col_boxes is a list of
    (x0, x1) per column and row_bands is a list of (y0, y1) data-row bands.
    structural_ok is False when detected gridlines deviate from the standard
    form (whole filing should be flagged low-confidence).
    """
    w, h = img.size
    cols = []
    for i in range(len(_PTR_COL_FRACS) - 1):
        cols.append((int(_PTR_COL_FRACS[i] * w), int(_PTR_COL_FRACS[i + 1] * w)))

    vlines = _detect_gridlines(img, vertical=True)
    structural_ok = True
    if vlines:
        # Compare count, then mean position error of the interior gridlines.
        if abs(len(vlines) - _PTR_EXPECTED_GRIDLINES) > 2:
            structural_ok = False
        else:
            expected = [_PTR_COL_FRACS[i] * w for i in range(1, len(_PTR_COL_FRACS) - 1)]
            # Match detected lines to expected positions by nearest fraction.
            errs = []
            for ex in expected:
                if vlines:
                    nearest = min(vlines, key=lambda v: abs(v - ex))
                    errs.append(abs(nearest - ex) / w)
            if errs and sum(errs) / len(errs) > 0.03:
                structural_ok = False

    hlines = _detect_gridlines(img, vertical=False)
    header_bottom = int(_PTR_HEADER_BOTTOM_FRAC * h)
    rows = [r for r in hlines if r > header_bottom]
    row_bands = []
    if len(rows) >= 2:
        # Adjacent rules delimit a band; take the tight pairs as data rows.
        for a, b in zip(rows, rows[1:]):
            row_bands.append((int(a), int(b)))
    else:
        # Fall back to the standard row pitch.
        row_bands = [(int(_PTR_ROW_FRACS[i] * h), int(_PTR_ROW_FRACS[i + 1] * h))
                     for i in range(len(_PTR_ROW_FRACS) - 1)]
        structural_ok = False
    return cols, row_bands, structural_ok


def _ocr_cell(img, box, psm=7, scale=2.0):
    """OCR a crop with tesseract (stdin), returning cleaned text.

    Cleans gridline bleed: scanned forms often include the cell's rule line
    at the crop edge, which tesseract reads as a stray '|' or run of dashes.
    """
    from PIL import Image
    import subprocess

    x0, y0, x1, y1 = box
    crop = img.crop((x0, y0, x1, y1))
    if crop.size[0] < 4 or crop.size[1] < 4:
        return ""
    crop = crop.resize((crop.size[0] * 2, crop.size[1] * 2), Image.Resampling.LANCZOS)
    # tesseract needs a real image file on stdin, not raw pixel bytes.
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    proc = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", str(psm)],
        input=buf.getvalue(), capture_output=True, timeout=20)
    text = proc.stdout.decode("utf-8", "replace").strip()
    text = re.sub(r"\s+", " ", text)
    # strip rule-line bleed: leading/trailing pipes, dashes, colons, bars
    text = re.sub(r"^[\s|:,.\-]+", "", text)
    text = re.sub(r"[\s|:,.\-]+$", "", text)
    return text


def _ocr_date_cell(img6, img12, box, y0, y1):
    """OCR a date cell: SC12 bottom-half recipe first, SC6 text-bounds fallback.

    box/y0/y1 are SC2 layout coordinates; img6/img12 are SC6/SC12 renders
    (the crop recipes are tuned in SC6 units, so coordinates are scaled x3).
    The SC12 pass reads the bottom half of the band (the digits sit on the
    bottom rule) with tight text bounds, binarization, 3x upscale and a digit
    whitelist; it handles faint scans but misreads a '1' that touches the box
    border on clean pages. An SC12 result is accepted only if it normalizes to
    a plausible date, otherwise the SC6 recipe (which handles clean pages) is
    used. Returns the raw OCR string (year may be truncated; caller pads it).
    """
    from PIL import Image
    import subprocess

    bx0, bx1 = box
    y0_6, y1_6 = y0 * 3, y1 * 3
    bx0_6, bx1_6 = bx0 * 3, bx1 * 3

    def _tess(crop, scale=3):
        gg = crop.resize((crop.size[0] * scale, crop.size[1] * scale),
                         Image.Resampling.NEAREST)
        buf = io.BytesIO()
        gg.save(buf, format="PNG")
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "7",
             "-c", "tessedit_char_whitelist=0123456789/"],
            input=buf.getvalue(), capture_output=True, timeout=20)
        return proc.stdout.decode("utf-8", "replace").strip()

    # SC12 pass: bottom half of the band, tight text bounds, binarize, 3x.
    k = 2  # SC12 / SC6
    cy0 = int((y1_6 - int((y1_6 - y0_6) * 0.5)) * k)
    cy1 = int((y1_6 - 3) * k)
    cx0 = int((bx0_6 + 8) * k)
    cx1 = int((bx1_6 - 8) * k)
    # Clamp to image bounds: Pillow pads out-of-bounds crop regions with
    # black, which would fabricate phantom dark pixels and send a blank cell
    # to tesseract (crashes on hosts without tesseract, e.g. CI).
    sub = img12.crop((max(0, cx0), max(0, cy0),
                      min(img12.width, cx1), min(img12.height, cy1)))
    pxs = sub.load()
    ys = [y for y in range(sub.height) for x in range(sub.width) if pxs[x, y] < 128]
    if ys:
        t0, t1 = max(0, min(ys) - 2), min(sub.height, max(ys) + 2)
        xs = [x for y in range(t0, t1) for x in range(sub.width) if pxs[x, y] < 128]
        if xs:
            x0, x1 = max(0, min(xs) - 2), min(sub.width, max(xs) + 2)
            crop = sub.crop((x0, t0, x1, t1))
            b = crop.point(lambda p: 0 if p < 170 else 255)
            r = _tess(b, scale=3)
            if r and _normalize_ocr_date(r):
                return r

    # SC6 fallback: text bounds inside the band, binarize, 6x.
    px = img6.load()
    ys = [y for y in range(y0_6 + 5, y1_6 - 5)
          for x in range(bx0_6 + 8, bx1_6) if px[x, y] < 128]
    if not ys:
        return ""
    t0, t1 = min(ys) - 4, max(ys) + 4
    xs = [x for y in range(t0, t1)
          for x in range(bx0_6 + 8, bx1_6) if px[x, y] < 128]
    if not xs:
        return ""
    x0, x1 = min(xs) - 4, max(xs) + 6
    crop = img6.crop((x0, t0, x1, t1))
    g = crop.convert("L").point(lambda p: 0 if p < 170 else 255)
    return _tess(g, scale=6)


def _normalize_ocr_date(s, filing_year=2025):
    """Normalize an OCR'd date ('7/30/26', '07/30/2026') to MM/DD/YYYY.

    Returns None when the parts are not a plausible date (month > 12 or
    day > 31), which filters OCR misreads like '40/01/25' and '10/32/25'
    that a raw regex would otherwise accept. A truncated 1-digit year is
    padded from the filing year.
    """
    m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{1,4})", s)
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    if len(y) == 1:
        # truncated final digit: pad from filing year
        yy = str(filing_year)
        if y == yy[-1]:
            y = yy
        elif y == "2":
            y = str(filing_year)
        else:
            return None
    elif len(y) == 2:
        y = "20" + y
    elif len(y) == 3:
        y = "20" + y
    return f"{mo:02d}/{d:02d}/{y}"


def _box_score(img, x0, y0, x1, y1, inset=10):
    """Fraction of interior dark pixels lying on/near either diagonal.

    Checkbox marks (X or fill) darken the box diagonals; an empty box only has
    its frame, which the inset excludes.
    """
    px = img.load()
    x0i, y0i = x0 + inset, y0 + inset
    x1i, y1i = x1 - inset, y1 - inset
    if x1i <= x0i or y1i <= y0i:
        return 0.0
    dark_on_diag = 0
    dark_total = 0
    wbox = x1i - x0i
    hbox = y1i - y0i
    for yy in range(y0i, y1i):
        for xx in range(x0i, x1i):
            if px[xx, yy] < 128:
                dark_total += 1
                # distance to main diagonal (top-left -> bottom-right)
                d_main = abs((yy - y0i) * wbox - (xx - x0i) * hbox) / (wbox + hbox)
                d_anti = abs((yy - y0i) * wbox - (x1i - xx) * hbox) / (wbox + hbox)
                if d_main <= 1.5 or d_anti <= 1.5:
                    dark_on_diag += 1
    return dark_on_diag / dark_total if dark_total else 0.0


def _pick_checked(scores, min_score=0.10, min_margin=0.15):
    """Pick the marked checkbox in a group by diagonal-score argmax.

    Returns (index, ok). ok=False when the best score is too weak or the
    margin over the runner-up is too thin to be trustworthy.
    """
    if not scores:
        return None, False
    best = max(range(len(scores)), key=lambda i: scores[i])
    if scores[best] < min_score:
        return None, False
    rest = sorted((scores[i] for i in range(len(scores)) if i != best), reverse=True)
    margin = (scores[best] - rest[0]) / scores[best] if rest and scores[best] else 1.0
    if margin < min_margin:
        return best, False
    return best, True


def _ocr_owner(text):
    t = text.upper()
    if "SP" in t:
        return "Spouse"
    if "DC" in t:
        return "Dependent Child"
    if "JT" in t:
        return "Joint"
    return "Self"


def house_ptr_trades_ocr(raw, pdf_url):
    """Extract trades from a scanned (image-only) House PTR PDF via OCR.

    Returns a list of trade dicts with source="ocr" and a confidence score.
    Low-confidence rows are included here but filtered by the caller.
    """
    if not _tess_available():
        print(f"house: tesseract not installed; skipping OCR {pdf_url}", flush=True)
        return []
    try:
        pages = _render_ptr_pages(raw)
        hires = _render_ptr_pages_hires(raw)
    except Exception as e:
        print(f"house: ocr render failed {pdf_url}: {e!r}", flush=True)
        return []

    trades = []
    for page_idx, img in enumerate(pages):
        img6, img12 = next(hires)
        cols, row_bands, structural_ok = _ptr_layout(img)
        type_boxes = [cols[3], cols[4], cols[5]]          # P, S, X
        amount_boxes = cols[8:]                            # 11 narrow + wide
        if len(amount_boxes) != 12 or len(type_boxes) != 3:
            structural_ok = False

        for (y0, y1) in row_bands:
            asset = _ocr_cell(img, (cols[2][0], y0, cols[2][1], y1)).strip(" ,-:")
            if any(g in asset.upper() for g in _PTR_OCR_GARBAGE):
                continue  # form header/footer instruction row, not a trade
            ticker_text = _ocr_cell(img, (cols[1][0], y0, cols[1][1], y1))
            owner_text = _ocr_cell(img, (cols[0][0], y0, cols[0][1], y1))
            txn_text = _ocr_date_cell(img6, img12, cols[6], y0, y1)
            notif_text = _ocr_date_cell(img6, img12, cols[7], y0, y1)

            type_scores = [_box_score(img, bx[0], y0, bx[1], y1) for bx in type_boxes]
            type_idx, type_ok = _pick_checked(type_scores)
            amt_scores = [_box_score(img, bx[0], y0, bx[1], y1) for bx in amount_boxes]
            amt_idx, amt_ok = _pick_checked(amt_scores)

            # Skip empty rows: no marks anywhere and no asset text.
            if not asset and type_idx is None and amt_idx is None and not ticker_text:
                continue

            txn_date = _normalize_ocr_date(txn_text)
            notif_date = _normalize_ocr_date(notif_text)
            owner = _ocr_owner(owner_text)
            code = ("P", "S", "X")[type_idx] if type_idx is not None else None
            ticker = ticker_text.strip("() ") if ticker_text.strip() else None
            if ticker and not re.fullmatch(r"[A-Z][A-Z0-9.]{0,4}", ticker):
                ticker = None

            confidence = "high"
            if (not structural_ok or not asset or not txn_date or not notif_date
                    or not type_ok or not amt_ok):
                confidence = "low"
            elif ticker is None or owner == "Self" and not owner_text.strip():
                confidence = "medium"

            if code is None:
                continue   # unreadable type code: skip, log below

            amt_label = PTR_AMOUNT_RANGES[amt_idx] if amt_idx is not None else None
            if amt_label is None:
                confidence = "low"
                continue
            # Build an e-filed-format line the standard parser understands.
            # TXN_RE needs a plain numeric anchor, so "Over"/spouse-DC boxes
            # are patched in afterwards.
            anchor_amt = re.sub(r"^Over\s+", "", amt_label)
            line = (f"{asset} {code} {txn_date} {notif_date} {anchor_amt}"
                    if ticker is None else
                    f"{asset} ({ticker}) {code} {txn_date} {notif_date} {anchor_amt}")
            parsed = house_trades_from_text(line)
            if not parsed:
                confidence = "low"
                continue
            t = parsed[0]
            t["owner"] = owner
            t["amount"] = amt_label
            t["source"] = "ocr"
            t["confidence"] = confidence
            t["pdf_page"] = page_idx
            trades.append(t)
            if confidence == "low":
                print(f"house: LOW-CONFIDENCE OCR row {pdf_url} p{page_idx}: "
                      f"{t!r}", flush=True)
    return trades


def run_house(conn, years=None, refresh=False):
    """Pull House filings from the Clerk's bulk index ZIPs and parse PTR trades.

    Trade-level data exists only inside the individual PTR PDFs, so each PTR
    filing that has no trades yet is downloaded and parsed from its text layer
    (0.2s pause per PDF; index rows commit first so an interrupted run keeps
    its progress). This also backfills PTRs indexed before stage 2 existed.

    With refresh=True, every PTR filing is re-downloaded and re-parsed and its
    trades are replaced, healing rows stored by older parser versions (e.g.
    truncated comments). Never set by the cron schedule.
    """
    s = Session()
    if years is None:
        today = date.today()
        years = [today.year, today.year - 1]  # current + prior year catches late filings
    if not refresh:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT filing_id FROM trades")
            done = {r[0] for r in cur.fetchall()}
    else:
        done = set()
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
                # OCR rows never silently ingested: low-confidence ones are
                # logged for review and skipped.
                low = [t for t in trades
                       if t.get("source") == "ocr" and t.get("confidence") == "low"]
                ingest = [t for t in trades if t not in low]
                for t in low:
                    print(f"house: SKIP low-confidence OCR {filing['id']}: "
                          f"{t.get('asset_name')!r} {t.get('type')} "
                          f"{t.get('txn_date')} {t.get('amount')}", flush=True)
                trades = ingest
            if trades:
                with conn.cursor() as cur:
                    if refresh:
                        cur.execute("DELETE FROM trades WHERE filing_id = %s",
                                    (filing["id"],))
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
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_version (version) VALUES (%s)", (f,))
            conn.commit()
        except Exception as e:
            # Transaction is rolled back (no commit) so schema_version is
            # untouched; surface which migration failed for fast triage.
            raise RuntimeError(f"migration {f} failed (rolled back): {e!r}") from e
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
        txn = t.get("txn_date")
        notif = t.get("notif_date")
        # Zero-padded MM/DD/YYYY sorts lexicographically as chronological.
        # A txn dated after its notification is impossible; flag it for review
        # (source PDFs occasionally contain such typos) but store as-is.
        if txn and notif and txn > notif:
            print(f"WARN {filing_id}: txn {txn} after notif {notif} (check source)",
                  flush=True)
        cur.execute(
            """INSERT INTO trades (filing_id, ticker, owner, transaction_type,
                                   amount_range, transaction_date, notification_date, raw)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (filing_id, t.get("ticker"), t.get("owner"), t.get("type"),
             t.get("amount"), txn, notif,
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
                    ticker = cells[3] if cells[3] not in ("", "--") else None
                    trades.append({
                        "owner": cells[2],
                        "ticker": ticker,
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
    days_back = os.environ.get("DAYS_BACK", "30")
    if os.environ.get("START_DATE"):
        start = date.fromisoformat(os.environ["START_DATE"])
    if os.environ.get("END_DATE"):
        end = date.fromisoformat(os.environ["END_DATE"])
    log(f"starting (start={start} end={end} days_back={days_back})")
    try:
        with db_conn() as conn:
            run_migrations(conn)
            nf, nt = run_senate(conn, start=start, end=end)
            log(f"senate done: {nf} new filings, {nt} new trades")
            refresh = os.environ.get("REFRESH_HOUSE", "").lower() in ("1", "true", "yes", "on")
            hf, ht = run_house(conn, refresh=refresh)
            log(f"house done: {hf} new filings, {ht} new trades")
    except Exception as e:
        log(f"ERROR: {e!r}")
        sys.exit(1)
    log("done")


if __name__ == "__main__":
    main()