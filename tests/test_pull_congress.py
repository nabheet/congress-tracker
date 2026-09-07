"""Unit tests for congress-tracker's pure parsing logic and DB seams.

Coverage targets (no network / DB / PDF):
  - csrf_from: both token attribute orders + missing token
  - senate_trades_from_html: row/header/short-row/paper handling
  - house_trades_from_text: standard PTR variants (owner, partial, amount
    split, wrapped asset, comments, boundaries) + columnar fallback
  - house_filing_index: ZIP/XML parsing, type mapping, URL routing
  - upsert_filing / insert_trades: SQL + params via fake cursor
  - Session._fetch: retry/backoff on 429/503/403, success, give-up
"""

import json
import time
import urllib.error
import urllib.request
from datetime import date

import pytest

import pull_congress as pc
from tests.conftest import (
    FakeConn, FakeCursor, FakeOpener, FakeResponse, FakeSession, make_fd_zip)


# ---------------------------------------------------------------- csrf_from

class TestCsrfFrom:
    def test_standard_order(self):
        html = '<form><input type="hidden" name="csrfmiddlewaretoken" value="tok123"/></form>'
        assert pc.csrf_from(html) == "tok123"

    def test_value_before_name(self):
        html = '<input type="hidden" name="csrfmiddlewaretoken" value="tok456"/>'
        assert pc.csrf_from(html) == "tok456"

    def test_extra_attrs_between(self):
        html = '<input name="csrfmiddlewaretoken" id="f" class="x" value="tok789"/>'
        assert pc.csrf_from(html) == "tok789"

    def test_no_token(self):
        assert pc.csrf_from("<html>nothing here</html>") is None


# ---------------------------------------------------------------- senate parsing

SENATE_HTML = """
<html><body><table>
<tr><th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
    <th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr>
<tr><td>1</td><td>08/15/2026</td><td>Self</td><td>AAPL</td><td>Apple Inc.</td>
    <td>Stock</td><td>Purchase</td><td>$1,001 - $15,000</td><td></td></tr>
<tr><td>2</td><td>08/16/2026</td><td>Spouse</td><td></td><td>Bitcoin ETF</td>
    <td>Exchange Traded Fund</td><td>Sale (partial)</td><td>$15,001 - $50,000</td>
    <td>Partial sale &amp; rollover</td></tr>
</table></body></html>
"""


class TestSenateTradesFromHtml:
    def test_parses_rows_and_skips_header(self):
        rows = pc.senate_trades_from_html(SENATE_HTML)
        assert len(rows) == 2
        assert rows[0][1] == "08/15/2026"
        assert rows[0][2] == "Self"
        assert rows[0][3] == "AAPL"
        assert rows[0][6] == "Purchase"
        assert rows[0][7] == "$1,001 - $15,000"

    def test_html_entities_unescaped(self):
        rows = pc.senate_trades_from_html(SENATE_HTML)
        assert rows[1][8] == "Partial sale & rollover"

    def test_skips_short_rows(self):
        html = "<table><tr><td>only</td><td>two</td></tr></table>"
        assert pc.senate_trades_from_html(html) == []

    def test_paper_filing_no_table(self):
        assert pc.senate_trades_from_html("<html>scanned paper filing</html>") == []

    def test_senate_filing_trades_delegates(self):
        s = FakeSession(text_by_url={pc.SENATE_BASE + "/search/view/ptr/abc/": SENATE_HTML})
        rows = pc.senate_filing_trades(s, "abc")
        assert len(rows) == 2


# ---------------------------------------------------------------- house PTR text parsing

class TestHouseTradesFromText:
    def test_empty_text_returns_empty(self):
        assert pc.house_trades_from_text("") == []
        assert pc.house_trades_from_text("   \n  \n") == []

    def test_basic_purchase(self):
        text = "SP Apple Inc. (AAPL) [ST] P 09/01/2026 09/05/2026 $1,001 - $15,000"
        trades = pc.house_trades_from_text(text)
        assert len(trades) == 1
        t = trades[0]
        assert t["owner"] == "Spouse"
        assert t["ticker"] == "AAPL"
        assert t["asset_name"] == "Apple Inc."
        assert t["asset_type"] == "ST"
        assert t["type"] == "Purchase"
        assert t["amount"] == "$1,001 - $15,000"
        assert t["txn_date"] == "09/01/2026"
        assert t["notif_date"] == "09/05/2026"

    def test_default_owner_self(self):
        text = "Microsoft Corp. (MSFT) [ST] S 09/01/2026 09/05/2026 $15,001 - $50,000"
        trades = pc.house_trades_from_text(text)
        assert trades[0]["owner"] == "Self"
        assert trades[0]["type"] == "Sale"

    def test_self_prefix_owner(self):
        text = "Self Amazon.com Inc. (AMZN) [ST] S 09/01/2026 09/05/2026 $1,001 - $15,000"
        trades = pc.house_trades_from_text(text)
        assert trades[0]["owner"] == "Self"
        assert trades[0]["ticker"] == "AMZN"

    def test_partial_type(self):
        text = "Apple Inc. (AAPL) [ST] P (partial) 09/01/2026 09/05/2026 $1,001 - $15,000"
        trades = pc.house_trades_from_text(text)
        assert trades[0]["type"] == "Purchase (partial)"

    def test_amount_split_across_lines(self):
        text = ("Apple Inc. (AAPL) [ST] P 09/01/2026 09/05/2026 $1,001 -\n"
                "$15,000")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["amount"] == "$1,001 - $15,000"

    def test_wrapped_asset_lines_joined(self):
        text = ("P 09/01/2026 09/05/2026 $1,001 - $15,000\n"
                "Indust Avg ETF Trust NYSEARCA:\n"
                "DIA [OT]")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["asset_name"] == "Indust Avg ETF Trust NYSEARCA"
        assert trades[0]["ticker"] == "DIA"
        assert trades[0]["asset_type"] == "OT"

    def test_comment_from_d_line(self):
        text = ("Apple Inc. (AAPL) [ST] P 09/01/2026 09/05/2026 $1,001 - $15,000\n"
                "D: Broker-assisted purchase")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["comment"] == "Broker-assisted purchase"

    def test_untradeable_lines_ignored(self):
        text = ("Filing ID: 12345\n"
                "Name: Some Member\n"
                "I CERTIFY that the statements\n"
                "* For the complete list of transactions")
        assert pc.house_trades_from_text(text) == []

    def test_control_chars_stripped(self):
        text = ("Apple Inc. (AAPL) [ST]\x00\x00 P 09/01/2026 09/05/2026 $1,001 - $15,000")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["ticker"] == "AAPL"


# ---------------------------------------------------------------- house columnar fallback

COLUMNAR_TEXT = (
    "ID Owner Asset Transaction Date\n"
    "SP Apple Inc S 09/01/2026 09/05/2026\n"
    "[GS]\n"
    "$1,000,000\n"
    "JT Tesla Inc X 09/02/2026 09/06/2026\n"
    "[ST]\n"
    "$15,001 - $50,000\n"
)


class TestHousePtrTradesColumnar:
    def test_columnar_variant(self):
        trades = pc.house_trades_from_text(COLUMNAR_TEXT)
        assert len(trades) == 2
        t0 = trades[0]
        assert t0["owner"] == "Spouse"
        assert t0["asset_name"] == "Apple Inc"
        assert t0["asset_type"] == "GS"
        assert t0["type"] == "Sale"
        assert t0["amount"] == "$1,000,000"
        t1 = trades[1]
        assert t1["owner"] == "Joint"
        assert t1["type"] == "Exchange"

    def test_columnar_missing_amount_skipped(self):
        text = "ID Owner Asset Transaction Date\nJT Apple Inc S 09/01/2026 09/05/2026\nno amount here"
        assert pc.house_trades_from_text(text) == []

    def test_over_marker(self):
        text = ("ID Owner Asset Transaction Date\n"
                "SP Apple Inc S 09/01/2026 09/05/2026 Over\n"
                "[GS]\n$1,000,000")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["amount"] == "Over $1,000,000"

    def test_columnar_skips_fs_line(self):
        text = ("ID Owner Asset Transaction Date\n"
                "SP Apple Inc S 09/01/2026 09/05/2026\n"
                "F S: Smith, John\n"
                "[GS]\n"
                "$1,000,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["amount"] == "$1,000,000"
        assert trades[0]["asset_type"] == "GS"

    def test_columnar_boundary_stops_amount_scan(self):
        text = ("ID Owner Asset Transaction Date\n"
                "SP Apple Inc S 09/01/2026 09/05/2026\n"
                "I CERTIFY that the foregoing\n"
                "JT Tesla Inc X 09/02/2026 09/06/2026\n"
                "[ST]\n"
                "$15,001 - $50,000\n")
        trades = pc.house_trades_from_text(text)
        assert len(trades) == 1
        assert trades[0]["owner"] == "Joint"

    def test_columnar_d_line_ends_amount_scan(self):
        # BOUNDARY_RE no longer matches "D:" (excluded via "(?!D ?:)"),
        # so a comment line after the amount does not terminate the row:
        # the D: comment is captured, a second "D:" line joins it, and the
        # amount is preserved.
        text = ("ID Owner Asset Transaction Date\n"
                "SP Apple Inc S 09/01/2026 09/05/2026\n"
                "[GS]\n"
                "$1,000,000\n"
                "D: sold some\n"
                "D: second thought\n"
                "JT Tesla Inc X 09/02/2026 09/06/2026\n"
                "[ST]\n"
                "$15,001 - $50,000\n")
        trades = pc.house_trades_from_text(text)
        assert len(trades) == 2
        assert trades[0]["owner"] == "Spouse"
        assert trades[0]["amount"] == "$1,000,000"
        assert trades[0]["comment"] == "sold some second thought"
        assert trades[1]["owner"] == "Joint"
        assert trades[1]["comment"] is None


# ---------------------------------------------------------------- parser edge cases

class TestHouseParserEdgeCases:
    def test_amount_split_with_riding_asset_text(self):
        text = ("SP Apple Inc. (AAPL) [ST] P 09/01/2026 09/05/2026 $15,001 -\n"
                "no amount here\n"
                "Stock (STT) [ST] $50,000\n")
        trades = pc.house_trades_from_text(text)
        assert len(trades) == 1
        assert trades[0]["amount"] == "$15,001 - $50,000"
        assert trades[0]["ticker"] == "AAPL"

    def test_owner_from_so_line(self):
        text = ("P 09/01/2026 09/05/2026 $1,001 - $15,000\n"
                "S O: Jointly Held\n"
                "P 09/02/2026 09/06/2026 $1,001 - $15,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["owner"] == "Jointly Held"

    def test_wrapped_asset_lines_above_anchor(self):
        text = ("Boring Fund\n"
                "Class A\n"
                "P 09/01/2026 09/05/2026 $1,001 - $15,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["asset_name"] == "Boring Fund Class A"

    def test_comment_line_ends_asset_block(self):
        text = ("P 09/01/2026 09/05/2026 $1,001 - $15,000\n"
                "D: sold shares\n"
                "P 09/02/2026 09/06/2026 $1,001 - $15,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["comment"] == "sold shares"

    def test_code_line_with_ticker(self):
        text = ("P 09/01/2026 09/05/2026 $1,001 - $15,000\n"
                "(AMZN) [ST]\n"
                "P 09/02/2026 09/06/2026 $1,001 - $15,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["ticker"] == "AMZN"
        assert trades[0]["asset_type"] == "ST"

    def test_ticker_only_wrapped_line(self):
        text = ("P 09/01/2026 09/05/2026 $1,001 - $15,000\n"
                "(WMT)\n"
                "P 09/02/2026 09/06/2026 $1,001 - $15,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["ticker"] == "WMT"

    def test_bare_ticker_from_wrapped_asset_text(self):
        text = ("Indust Avg ETF Trust NYSEARCA:\n"
                "DIA [OT]\n"
                "P 09/01/2026 09/05/2026 $1,001 - $15,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["ticker"] == "DIA"
        assert trades[0]["asset_type"] == "OT"

    def test_comment_continuation_lines(self):
        # Plain-text continuation lines join the comment; a second "D:" line
        # is excluded from BOUNDARY_RE (via "(?!D ?:)") and joins too, so
        # the D:-continuation branch is reachable.
        text = ("SP Apple Inc. (AAPL) [ST] P 09/01/2026 09/05/2026 $1,001 - $15,000\n"
                "D: sold shares\n"
                "due to rebalancing\n"
                "D: second thought\n"
                "P 09/02/2026 09/06/2026 $1,001 - $15,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["comment"] == "sold shares due to rebalancing second thought"

    def test_footer_stops_comment_scan(self):
        text = ("P 09/01/2026 09/05/2026 $1,001 - $15,000\n"
                "I CERTIFY that the foregoing\n"
                "P 09/02/2026 09/06/2026 $1,001 - $15,000\n")
        trades = pc.house_trades_from_text(text)
        assert trades[0]["comment"] is None


# ---------------------------------------------------------------- house index

FD_XML = """<?xml version="1.0"?>
<Members>
  <Member>
    <Prefix>Hon.</Prefix><Last>Smith</Last><First>John</First><Suffix>Jr.</Suffix>
    <FilingType>P</FilingType><StateDst>CA-12</StateDst><Year>2026</Year>
    <FilingDate>09/01/2026</FilingDate><DocID>123456</DocID>
  </Member>
  <Member>
    <Last>Jones</Last><First>Jane</First>
    <FilingType>X</FilingType><Year>2026</Year>
    <FilingDate>not-a-date</FilingDate><DocID>654321</DocID>
  </Member>
  <Member>
    <Last>NoDoc</Last><First>Bob</First><FilingType>P</FilingType>
  </Member>
</Members>
"""


class TestHouseFilingIndex:
    def test_parses_members(self):
        s = FakeSession(bytes_by_url={
            pc.HOUSE_PUBLIC + "/financial-pdfs/2026FD.zip": make_fd_zip(2026, FD_XML)})
        rows = pc.house_filing_index(s, 2026)
        assert len(rows) == 2  # member without DocID is skipped

        ptr = rows[0]
        assert ptr["id"] == "house:123456"
        assert ptr["source"] == "house"
        assert ptr["filer"] == "John Smith"
        assert ptr["report_type"] == "PTR Original"
        assert ptr["filed_at"].isoformat() == "2026-09-01"
        assert ptr["raw_url"] == pc.HOUSE_PUBLIC + "/ptr-pdfs/2026/123456.pdf"
        assert ptr["is_ptr"] is True

        ext = rows[1]
        assert ext["report_type"] == "Extension"
        assert ext["filed_at"] is None
        assert ext["raw_url"] == pc.HOUSE_PUBLIC + "/financial-pdfs/2026/654321.pdf"
        assert ext["is_ptr"] is False

    def test_unknown_type_label(self):
        xml = FD_XML.replace("<FilingType>X</FilingType>", "<FilingType>ZZ</FilingType>")
        s = FakeSession(bytes_by_url={
            pc.HOUSE_PUBLIC + "/financial-pdfs/2026FD.zip": make_fd_zip(2026, xml)})
        rows = pc.house_filing_index(s, 2026)
        assert rows[1]["report_type"] == "ZZ"


# ---------------------------------------------------------------- db seams

class TestUpsertFiling:
    def test_sql_and_params(self):
        cur = FakeCursor(fetchone_result=(True,))
        filing = {
            "id": "senate:abc", "source": "senate", "filer": "John Smith",
            "report_type": "PTR", "filed_at": None, "raw_url": "https://example.test/ptr",
        }
        inserted = pc.upsert_filing(cur, filing)
        assert inserted is True
        sql, params = cur.calls[0]
        assert "ON CONFLICT (id) DO UPDATE" in sql
        assert params[0] == "senate:abc"
        assert params[1] == "senate"
        assert params[2] == "John Smith"

    def test_returns_false_when_known(self):
        cur = FakeCursor(fetchone_result=(False,))
        filing = {
            "id": "senate:abc", "source": "senate", "filer": "John Smith",
            "report_type": "PTR", "filed_at": None, "raw_url": "https://example.test/ptr",
        }
        assert pc.upsert_filing(cur, filing) is False


class TestInsertTrades:
    def test_one_execute_per_trade(self):
        cur = FakeCursor()
        trades = [
            {"ticker": "AAPL", "owner": "Self", "type": "Purchase", "amount": "$1,001 - $15,000",
             "txn_date": "09/01/2026", "notif_date": "09/05/2026", "comment": None},
            {"ticker": "MSFT", "owner": "Spouse", "type": "Sale", "amount": "$15,001 - $50,000",
             "txn_date": "09/02/2026", "notif_date": "09/06/2026", "comment": "x"},
        ]
        pc.insert_trades(cur, "house:123", trades)
        assert len(cur.calls) == 2
        sql, params = cur.calls[0]
        assert "ON CONFLICT DO NOTHING" in sql
        assert params[0] == "house:123"
        assert params[1] == "AAPL"
        assert json.loads(params[7])["type"] == "Purchase"


# ---------------------------------------------------------------- Session._fetch

def _http_error(code):
    return urllib.error.HTTPError("https://example.test/", code, "err", {}, None)


class TestSessionFetch:
    def test_success_no_retry(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(pc.time, "sleep", lambda s: sleeps.append(s))
        s = pc.Session()
        s.opener = FakeOpener([FakeResponse(raw=b"ok", final_url="https://f/")])
        raw, final = s._fetch("https://example.test/", retries=4)
        assert raw == b"ok"
        assert final == "https://f/"
        assert sleeps == []

    def test_retries_on_429_then_succeeds(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(pc.time, "sleep", lambda s: sleeps.append(s))
        s = pc.Session()
        s.opener = FakeOpener([
            _http_error(429),
            FakeResponse(raw=b"ok", final_url="https://f/"),
        ])
        raw, _ = s._fetch("https://example.test/", retries=4)
        assert raw == b"ok"
        assert sleeps == [10]  # 2**0 * 10

    def test_retries_on_503_and_403(self, monkeypatch):
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        s = pc.Session()
        for code in (503, 403):
            s.opener = FakeOpener([_http_error(code), FakeResponse(raw=b"ok")])
            assert s._fetch("https://example.test/", retries=4)[0] == b"ok"

    def test_exponential_backoff(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(pc.time, "sleep", lambda s: sleeps.append(s))
        s = pc.Session()
        s.opener = FakeOpener([
            _http_error(429), _http_error(429), _http_error(429),
            FakeResponse(raw=b"ok"),
        ])
        s._fetch("https://example.test/", retries=4)
        assert sleeps == [10, 20, 40]

    def test_gives_up_after_retries(self, monkeypatch):
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        s = pc.Session()
        s.opener = FakeOpener([_http_error(429)] * 4)
        with pytest.raises(pc.RateLimited):
            s._fetch("https://example.test/", retries=4)

    def test_non_retryable_error_raises(self, monkeypatch):
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        s = pc.Session()
        s.opener = FakeOpener([_http_error(404)])
        with pytest.raises(urllib.error.HTTPError):
            s._fetch("https://example.test/", retries=4)

    def test_post_urlencodes_data(self, monkeypatch):
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        s = pc.Session()
        s.opener = FakeOpener([FakeResponse(raw=b"ok")])
        s._fetch("https://example.test/", data={"a": "1", "b": "two words"})
        req, _ = s.opener.calls[0]
        assert req.data == b"a=1&b=two+words"
        assert req.get_method() == "POST"

    def test_retries_on_urlerror_then_succeeds(self, monkeypatch, capsys):
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        s = pc.Session()
        s.opener = FakeOpener([
            urllib.error.URLError("temp"),
            urllib.error.URLError("temp"),
            FakeResponse(raw=b"ok"),
        ])
        raw, _ = s._fetch("https://example.test/", retries=4)
        assert raw == b"ok"
        assert len(s.opener.calls) == 3
        out = capsys.readouterr().out
        assert "network error" in out
        assert "retrying in 10s" in out   # 2**0 * 10
        assert "retrying in 20s" in out   # 2**1 * 10

    def test_custom_headers_merged(self):
        s = pc.Session()
        s.opener = FakeOpener([FakeResponse(raw=b"ok")])
        s._fetch("https://example.test/", headers={"X-Custom": "yes"})
        req, _ = s.opener.calls[0]
        # urllib capitalizes header names when building the request
        assert req.headers["User-agent"] == pc.UA
        assert req.headers["X-custom"] == "yes"

    def test_request_decodes_body(self):
        s = pc.Session()
        s.opener = FakeOpener([FakeResponse(raw=b'{"ok": 1}', final_url="https://f/")])
        body, final = s.request("https://example.test/")
        assert body == '{"ok": 1}'
        assert final == "https://f/"

    def test_request_bytes_returns_raw(self):
        s = pc.Session()
        s.opener = FakeOpener([FakeResponse(raw=b"\x00\x01binary", final_url="https://f/")])
        raw, final = s.request_bytes("https://example.test/pdf")
        assert raw == b"\x00\x01binary"
        assert final == "https://f/"


class TestSenateReportList:
    def test_payload_shape(self):
        s = FakeSession(text_by_url={
            pc.SENATE_BASE + "/search/report/data/": '{"data": []}'})
        resp = pc.senate_report_list(s, "09/01/2026 00:00:00", "", start=0, length=100)
        assert resp == {"data": []}
        url, data, _headers = s.request_calls[0]
        assert url == pc.SENATE_BASE + "/search/report/data/"
        assert data["report_types"] == "[11]"
        assert data["submitted_start_date"] == "09/01/2026 00:00:00"
        assert data["submitted_end_date"] == ""
        assert data["length"] == "100"


# ---------------------------------------------------------------- senate session

class TestSenateSession:
    def test_home_then_agreement_post(self, monkeypatch):
        home = pc.SENATE_BASE + "/search/home/"
        fake = FakeSession(text_by_url={
            home: '<input name="csrfmiddlewaretoken" value="tok123"/>'})
        monkeypatch.setattr(pc, "Session", lambda: fake)
        s = pc.senate_session()
        assert s is fake
        # GET home first, then agreement POST with the csrf token
        assert fake.request_calls[0][0] == home
        assert fake.request_calls[0][1] is None
        assert fake.request_calls[1][0] == home
        assert fake.request_calls[1][1] == {
            "csrfmiddlewaretoken": "tok123", "prohibition_agreement": "1"}

    def test_missing_csrf_raises(self, monkeypatch):
        fake = FakeSession(text_by_url={
            pc.SENATE_BASE + "/search/home/": "<html>no token</html>"})
        monkeypatch.setattr(pc, "Session", lambda: fake)
        with pytest.raises(RuntimeError):
            pc.senate_session()


# ---------------------------------------------------------------- house PTR PDF seam

class FakePdfPage:
    def __init__(self, text=None):
        self._text = text

    def extract_text(self):
        return self._text


class FakePdf:
    def __init__(self, pages):
        self._pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def pages(self):
        return self._pages


class TestHousePtrTrades:
    def test_fetch_failure_returns_empty(self, capsys):
        s = FakeSession(bytes_errors={"https://pdf/1.pdf": OSError("boom")})
        assert pc.house_ptr_trades(s, "https://pdf/1.pdf") == []
        assert "fetch failed" in capsys.readouterr().out

    def test_parse_failure_returns_empty(self, capsys):
        s = FakeSession(bytes_by_url={"https://pdf/1.pdf": b"not a pdf"})
        assert pc.house_ptr_trades(s, "https://pdf/1.pdf") == []
        assert "parse failed" in capsys.readouterr().out

    def test_no_text_layer_returns_empty(self, monkeypatch, capsys):
        monkeypatch.setattr(pc.pdfplumber, "open", lambda *a, **k: FakePdf([FakePdfPage(None)]))
        s = FakeSession(bytes_by_url={"https://pdf/1.pdf": b"%PDF-fake"})
        assert pc.house_ptr_trades(s, "https://pdf/1.pdf") == []
        assert "no text layer" in capsys.readouterr().out

    def test_delegates_to_parser_on_text(self, monkeypatch):
        monkeypatch.setattr(
            pc.pdfplumber, "open",
            lambda *a, **k: FakePdf([FakePdfPage(
                "SP Apple Inc. (AAPL) [ST] P 09/01/2026 09/05/2026 $1,001 - $15,000")]))
        s = FakeSession(bytes_by_url={"https://pdf/1.pdf": b"%PDF-fake"})
        trades = pc.house_ptr_trades(s, "https://pdf/1.pdf")
        assert len(trades) == 1
        assert trades[0]["ticker"] == "AAPL"


# ---------------------------------------------------------------- db: migrations

class TestRunMigrations:
    def test_applies_in_order_and_tracks(self, monkeypatch, tmp_path):
        d = tmp_path / "migrations"
        d.mkdir()
        (d / "01_first.sql").write_text("CREATE TABLE a (id int);")
        (d / "02_second.sql").write_text("CREATE TABLE b (id int);")
        monkeypatch.setattr(pc, "MIGRATIONS_DIR", str(d))

        cur_create = FakeCursor()
        cur_select = FakeCursor(fetchall_result=[])
        conn = FakeConn(cursors=[cur_create, cur_select])
        assert pc.run_migrations(conn) == 0
        # create schema_version + select applied + 2 migrations = 3 commits
        assert conn.commits == 3
        assert "schema_version" in cur_create.calls[0][0]
        # each migration cursor executed the file SQL then the version insert
        for cur in conn.all_cursors[2:]:
            assert len(cur.calls) == 2
            assert cur.calls[1][0].startswith("INSERT INTO schema_version")
        assert conn.all_cursors[2].calls[0][0] == "CREATE TABLE a (id int);"
        assert conn.all_cursors[3].calls[0][0] == "CREATE TABLE b (id int);"

    def test_skips_applied_migrations(self, monkeypatch, tmp_path):
        d = tmp_path / "migrations"
        d.mkdir()
        (d / "01_first.sql").write_text("CREATE TABLE a (id int);")
        (d / "02_second.sql").write_text("CREATE TABLE b (id int);")
        monkeypatch.setattr(pc, "MIGRATIONS_DIR", str(d))

        cur_select = FakeCursor(fetchall_result=[("01_first.sql",)])
        conn = FakeConn(cursors=[FakeCursor(), cur_select])
        assert pc.run_migrations(conn) == 1
        assert conn.commits == 2  # schema_version + only the un-applied migration
        assert conn.all_cursors[2].calls[0][0] == "CREATE TABLE b (id int);"

    def test_no_migration_files(self, monkeypatch, tmp_path):
        d = tmp_path / "migrations"
        d.mkdir()
        monkeypatch.setattr(pc, "MIGRATIONS_DIR", str(d))
        conn = FakeConn(cursors=[FakeCursor(), FakeCursor(fetchall_result=[])])
        assert pc.run_migrations(conn) == 0


# ---------------------------------------------------------------- run_senate

class TestRunSenate:
    def test_empty_page_breaks(self, monkeypatch):
        data_url = pc.SENATE_BASE + "/search/report/data/"
        fake = FakeSession(text_by_url={data_url: '{"data": []}'})
        monkeypatch.setattr(pc, "senate_session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        conn = FakeConn()
        assert pc.run_senate(conn) == (0, 0)

    def test_row_without_link_skipped(self, monkeypatch):
        data_url = pc.SENATE_BASE + "/search/report/data/"
        fake = FakeSession(text_by_url={
            data_url: json.dumps({"data": [
                ["John", "Smith", "CA", "", "09/01/2026"],
                ["Jane", "Doe", "NY", '<a href="/search/view/ptr/xyz/">View PTR</a>',
                 "09/02/2026"],
            ]})})
        monkeypatch.setattr(pc, "senate_session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        conn = FakeConn(cursors=[FakeCursor(fetchone_result=(True,))])
        nf, nt = pc.run_senate(conn, start=date(2026, 9, 1), end=date(2026, 9, 1))
        assert nf == 1      # only the row with a link is counted
        assert nt == 0      # xyz is a PTR with an empty table -> no trades

    def test_new_filing_pulls_trades(self, monkeypatch):
        data_url = pc.SENATE_BASE + "/search/report/data/"
        ptr_url = pc.SENATE_BASE + "/search/view/ptr/abc/"
        fake = FakeSession(text_by_url={
            data_url: json.dumps({"data": [[
                "John", "Smith", "CA", '<a href="/search/view/ptr/abc/">View PTR</a>',
                "09/01/2026"]]}),
            ptr_url: SENATE_HTML,
        })
        monkeypatch.setattr(pc, "senate_session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        cur_upsert = FakeCursor(fetchone_result=(True,))
        conn = FakeConn(cursors=[cur_upsert])
        nf, nt = pc.run_senate(conn, start=None, end=date(2026, 9, 1))
        assert nf == 1
        assert nt == 2
        # upsert ran, then one INSERT per trade on the same cursor
        assert len(cur_upsert.calls) == 3
        assert cur_upsert.calls[0][0].startswith("INSERT INTO filings")
        assert cur_upsert.calls[1][0].startswith("INSERT INTO trades")
        # raw JSON payload stored with the trade
        assert json.loads(cur_upsert.calls[1][1][7])["ticker"] == "AAPL"

    def test_known_filing_skipped(self, monkeypatch):
        data_url = pc.SENATE_BASE + "/search/report/data/"
        fake = FakeSession(text_by_url={
            data_url: json.dumps({"data": [[
                "John", "Smith", "CA", '<a href="/search/view/ptr/abc/">View PTR</a>',
                "09/01/2026"]]})})
        monkeypatch.setattr(pc, "senate_session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        cur_upsert = FakeCursor(fetchone_result=(False,))  # already in DB
        conn = FakeConn(cursors=[cur_upsert])
        nf, nt = pc.run_senate(conn, start=None, end=date(2026, 9, 1))
        assert nf == 0
        assert nt == 0
        assert len(cur_upsert.calls) == 1  # upsert only, no trade fetch/insert

    def test_paper_filing_logged(self, monkeypatch, capsys):
        data_url = pc.SENATE_BASE + "/search/report/data/"
        fake = FakeSession(text_by_url={
            data_url: json.dumps({"data": [[
                "John", "Smith", "CA", '<a href="/search/view/paper/xyz/">View PTR</a>',
                "09/01/2026"]]}),
            pc.SENATE_BASE + "/search/view/ptr/xyz/": "<html>no table</html>",
        })
        monkeypatch.setattr(pc, "senate_session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        conn = FakeConn(cursors=[FakeCursor(fetchone_result=(True,))])
        nf, nt = pc.run_senate(conn, start=None, end=date(2026, 9, 1))
        assert nf == 1
        assert nt == 0
        assert "paper filing" in capsys.readouterr().out

    def test_pagination_loops_until_short_page(self, monkeypatch):
        data_url = pc.SENATE_BASE + "/search/report/data/"
        row = ["John", "Smith", "CA", '<a href="/search/view/ptr/abc/">View PTR</a>',
               "09/01/2026"]

        def fake_request(url, data=None, headers=None, retries=4):
            payload = data or {}
            if payload.get("start") == "0":
                return json.dumps({"data": [row] * 100}), url
            return json.dumps({"data": [row]}), url

        fake = FakeSession()
        fake.request = fake_request
        monkeypatch.setattr(pc, "senate_session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        conn = FakeConn()  # auto-cursors, upsert always True
        nf, _ = pc.run_senate(conn, start=None, end=date(2026, 9, 1))
        # 100 rows on page 1 (loop continues), 1 row on page 2 (breaks)
        assert nf == 101


# ---------------------------------------------------------------- run_house

class TestRunHouse:
    def test_default_years_and_trade_insert(self, monkeypatch):
        xml = """<?xml version="1.0"?>
<Members>
  <Member><Last>Smith</Last><First>John</First><FilingType>P</FilingType>
    <FilingDate>09/01/2026</FilingDate><DocID>111</DocID></Member>
</Members>
"""
        fake = FakeSession(bytes_by_url={
            pc.HOUSE_PUBLIC + "/financial-pdfs/2026FD.zip": make_fd_zip(2026, xml),
            pc.HOUSE_PUBLIC + "/financial-pdfs/2025FD.zip": make_fd_zip(2025, xml),
            pc.HOUSE_PUBLIC + "/ptr-pdfs/2026/111.pdf": b"%PDF-fake",
            pc.HOUSE_PUBLIC + "/ptr-pdfs/2025/111.pdf": b"%PDF-fake",
        })
        monkeypatch.setattr(pc, "Session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            pc.pdfplumber, "open",
            lambda *a, **k: FakePdf([FakePdfPage(
                "SP Apple Inc. (AAPL) [ST] P 09/01/2026 09/05/2026 $1,001 - $15,000")]))

        class _FakeDate:
            @staticmethod
            def today():
                return date(2026, 9, 7)

        monkeypatch.setattr(pc, "date", _FakeDate)
        conn = FakeConn()  # auto-cursors; upsert always True
        nf, nt = pc.run_house(conn)
        assert nf == 2     # one PTR filing per year
        assert nt == 2     # one parsed trade per PTR PDF
        assert len(fake.request_bytes_calls) == 4  # 2 zips + 2 PDFs

    def test_index_and_ptr_pdf_pull(self, monkeypatch):
        year = 2026
        xml = """<?xml version="1.0"?>
<Members>
  <Member><Last>Smith</Last><First>John</First><FilingType>P</FilingType>
    <FilingDate>09/01/2026</FilingDate><DocID>111</DocID></Member>
  <Member><Last>Jones</Last><First>Jane</First><FilingType>X</FilingType>
    <FilingDate>09/02/2026</FilingDate><DocID>222</DocID></Member>
</Members>
"""
        zip_url = pc.HOUSE_PUBLIC + f"/financial-pdfs/{year}FD.zip"
        pdf_url = pc.HOUSE_PUBLIC + f"/ptr-pdfs/{year}/111.pdf"
        fake = FakeSession(bytes_by_url={
            zip_url: make_fd_zip(year, xml),
            pdf_url: b"not a pdf",  # parse failure -> no trades, but filing still indexed
        })
        monkeypatch.setattr(pc, "Session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)

        cur_done = FakeCursor(fetchall_result=[])
        cur_up1 = FakeCursor(fetchone_result=(True,))
        cur_up2 = FakeCursor(fetchone_result=(True,))
        conn = FakeConn(cursors=[cur_done, cur_up1, cur_up2])
        nf, nt = pc.run_house(conn, years=[year])
        assert nf == 2
        assert nt == 0
        assert fake.request_bytes_calls[0] == zip_url
        assert fake.request_bytes_calls[1] == pdf_url

    def test_existing_ptr_skips_pdf(self, monkeypatch):
        year = 2026
        xml = """<?xml version="1.0"?>
<Members>
  <Member><Last>Smith</Last><First>John</First><FilingType>P</FilingType>
    <FilingDate>09/01/2026</FilingDate><DocID>111</DocID></Member>
</Members>
"""
        zip_url = pc.HOUSE_PUBLIC + f"/financial-pdfs/{year}FD.zip"
        fake = FakeSession(bytes_by_url={zip_url: make_fd_zip(year, xml)})
        monkeypatch.setattr(pc, "Session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)

        cur_done = FakeCursor(fetchall_result=[("house:111",)])
        conn = FakeConn(cursors=[cur_done])
        nf, nt = pc.run_house(conn, years=[year])
        assert nf == 1   # filing still upserted
        assert nt == 0
        assert len(fake.request_bytes_calls) == 1  # zip only, no PDF fetch

    def test_refresh_reparses_existing_ptr(self, monkeypatch):
        year = 2026
        xml = """<?xml version="1.0"?>
<Members>
  <Member><Last>Smith</Last><First>John</First><FilingType>P</FilingType>
    <FilingDate>09/01/2026</FilingDate><DocID>111</DocID></Member>
</Members>
"""
        zip_url = pc.HOUSE_PUBLIC + f"/financial-pdfs/{year}FD.zip"
        pdf_url = pc.HOUSE_PUBLIC + f"/ptr-pdfs/{year}/111.pdf"
        fake = FakeSession(bytes_by_url={
            zip_url: make_fd_zip(year, xml),
            pdf_url: b"%PDF-fake",
        })
        monkeypatch.setattr(pc, "Session", lambda: fake)
        monkeypatch.setattr(pc.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            pc.pdfplumber, "open",
            lambda *a, **k: FakePdf([FakePdfPage(
                "SP Apple Inc. (AAPL) [ST] P 09/01/2026 09/05/2026 $1,001 - $15,000")]))

        # Trades already exist, so a plain run would skip the PDF. refresh=True
        # must ignore `done`, re-fetch the PDF, delete the stale rows, and
        # re-insert freshly parsed trades.
        cur_done = FakeCursor(fetchall_result=[("house:111",)])
        cur_trades = FakeCursor()
        conn = FakeConn(cursors=[cur_done, cur_trades])
        nf, nt = pc.run_house(conn, years=[year], refresh=True)
        assert nf == 1
        assert nt == 1
        assert fake.request_bytes_calls == [zip_url, pdf_url]  # PDF re-fetched
        deletes = [c for c in cur_trades.calls
                   if c[0] == "DELETE FROM trades WHERE filing_id = %s"]
        assert deletes == [("DELETE FROM trades WHERE filing_id = %s", ("house:111",))]


# ---------------------------------------------------------------- db: db_conn

class TestDbConn:
    def test_reads_password_file_and_connects(self, tmp_path, monkeypatch):
        pw_file = tmp_path / "pw"
        pw_file.write_text("test-password\n")
        monkeypatch.setattr(pc, "PG_PASS_FILE", str(pw_file))
        captured = {}

        class FakePsycopg:
            @staticmethod
            def connect(**kw):
                captured.update(kw)
                return "conn"

        monkeypatch.setattr(pc, "psycopg", FakePsycopg)
        assert pc.db_conn() == "conn"
        assert captured["host"] == pc.PG_HOST
        assert captured["port"] == pc.PG_PORT
        assert captured["dbname"] == pc.PG_DB
        assert captured["user"] == pc.PG_USER
        assert captured["password"] == "test-password"
        assert captured["connect_timeout"] == 10


# ---------------------------------------------------------------- main

class TestMain:
    def test_env_dates_passed(self, monkeypatch, capsys):
        monkeypatch.setattr(pc, "db_conn", lambda: FakeConn())
        monkeypatch.setattr(pc, "run_migrations", lambda conn: None)
        seen = {}
        monkeypatch.setattr(pc, "run_senate",
                            lambda conn, start=None, end=None:
                            seen.update(start=start, end=end) or (0, 0))
        monkeypatch.setattr(pc, "run_house", lambda conn, refresh=False: (0, 0))
        monkeypatch.setenv("START_DATE", "2026-08-01")
        monkeypatch.setenv("END_DATE", "2026-09-01")
        pc.main()
        assert seen["start"] == date(2026, 8, 1)
        assert seen["end"] == date(2026, 9, 1)

    def test_success_flow(self, monkeypatch, capsys):
        monkeypatch.setattr(pc, "db_conn", lambda: FakeConn())
        calls = {}
        monkeypatch.setattr(pc, "run_migrations", lambda conn: calls.setdefault("mig", True))
        monkeypatch.setattr(pc, "run_senate",
                            lambda conn, start=None, end=None: calls.setdefault("sen", (1, 2)))
        monkeypatch.setattr(pc, "run_house",
                            lambda conn, refresh=False: calls.setdefault("house", (3, 4)))
        monkeypatch.delenv("START_DATE", raising=False)
        monkeypatch.delenv("END_DATE", raising=False)
        pc.main()
        out = capsys.readouterr().out
        assert "senate done: 1 new filings, 2 new trades" in out
        assert "house done: 3 new filings, 4 new trades" in out
        assert calls == {"mig": True, "sen": (1, 2), "house": (3, 4)}

    def test_refresh_house_env_passed(self, monkeypatch, capsys):
        monkeypatch.setattr(pc, "db_conn", lambda: FakeConn())
        monkeypatch.setattr(pc, "run_migrations", lambda conn: None)
        monkeypatch.setattr(pc, "run_senate", lambda conn, start=None, end=None: (0, 0))
        seen = {}
        monkeypatch.setattr(pc, "run_house",
                            lambda conn, refresh=False: seen.update(refresh=refresh) or (0, 0))
        monkeypatch.delenv("START_DATE", raising=False)
        monkeypatch.delenv("END_DATE", raising=False)
        monkeypatch.delenv("REFRESH_HOUSE", raising=False)
        pc.main()
        assert seen["refresh"] is False

        monkeypatch.setenv("REFRESH_HOUSE", "1")
        pc.main()
        assert seen["refresh"] is True

    def test_error_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(pc, "db_conn", lambda: FakeConn())

        def boom(*a, **k):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(pc, "run_migrations", boom)
        with pytest.raises(SystemExit) as exc:
            pc.main()
        assert exc.value.code == 1
        assert "ERROR" in capsys.readouterr().out