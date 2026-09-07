"""Shared fixtures for congress-tracker unit tests.

pull_congress.py is a script, not a package, so tests import it by inserting
the repo root into sys.path. No network, database, or PDF access happens in
tests: sessions, cursors, and openers are all fakes.
"""

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pull_congress as pc


class FakeCursor:
    """Records execute() calls; fetchone/fetchall return canned values.

    execute() returns self so psycopg-style chaining works
    (cur.execute(sql).fetchall()).
    """

    def __init__(self, fetchone_result=(True,), fetchall_result=None):
        self.calls = []          # [(sql, params), ...]
        self.rowcount = None
        self._fetchone = fetchone_result
        self._fetchall = fetchall_result if fetchall_result is not None else []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.rowcount = 1 if params is not None else 0
        return self

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """Context-manager conn: pops pre-made cursors in order, counts commits.

    Every cursor handed out (pre-seeded or auto-created) is recorded in
    ``all_cursors`` for assertions.
    """

    def __init__(self, cursors=None):
        self.cursors = cursors if cursors is not None else []
        self.all_cursors = []
        self.commits = 0

    def cursor(self):
        cur = self.cursors.pop(0) if self.cursors else FakeCursor()
        self.all_cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_cursor():
    return FakeCursor()


class FakeResponse:
    """Mimics urllib's HTTPResponse enough for Session._fetch."""

    def __init__(self, raw=b"", final_url="https://example.test/"):
        self._raw = raw
        self.final = final_url

    def read(self):
        return self._raw

    def geturl(self):
        return self.final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Opener that yields a sequence of outcomes (response or exception)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, req, timeout=60):
        self.calls.append((req, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSession:
    """Minimal stand-in for pc.Session: canned request/request_bytes.

    text_by_url / bytes_by_url map URL -> body. bytes_errors maps URL -> an
    exception to raise from request_bytes (fetch-failure paths).
    """

    def __init__(self, text_by_url=None, bytes_by_url=None, bytes_errors=None):
        self.text_by_url = text_by_url or {}
        self.bytes_by_url = bytes_by_url or {}
        self.bytes_errors = bytes_errors or {}
        self.cookies = {}   # like pc.Session, a plain dict of cookie names
        self.request_calls = []
        self.request_bytes_calls = []

    def request(self, url, data=None, headers=None, retries=4):
        self.request_calls.append((url, data, headers))
        body = self.text_by_url.get(url, "")
        return body, url

    def request_bytes(self, url, headers=None, retries=4):
        self.request_bytes_calls.append(url)
        if url in self.bytes_errors:
            raise self.bytes_errors[url]
        raw = self.bytes_by_url.get(url, b"")
        return raw, url


def make_fd_zip(year, xml):
    """Build a Clerk YYYYFD.zip in memory containing YYYYFD.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{year}FD.xml", xml)
    return buf.getvalue()