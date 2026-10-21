"""Shared fixtures. Everything runs offline: Gmail is an httpx MockTransport."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from damien.config import Settings
from damien.gmail import Gmail
from damien.store import Store


class FakeGmailState:
    """A tiny stand-in for the Gmail REST surface, in one dict."""

    def __init__(self) -> None:
        self.labels = {"INBOX": "INBOX", "UNREAD": "UNREAD", "TRASH": "TRASH", "Label_1": "Newsletters"}
        self.messages: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []      # (method, path)
        self.mods: list[dict] = []                  # batchModify bodies
        self.queries: list[str] = []                 # every q= that went out
        self.fail_mods = 0                          # fail the next N batchModify calls
        self.fail_status = 400
        self._next_label = 2

    def add(self, mid: str, *, subject: str = "Hello", sender: str = "Alex <alex@example.com>",
            labels: list[str] | None = None, date_ms: int = 1_700_000_000_000, snippet: str = "hi",
            list_unsub: bool = False, attachment: bool = False) -> None:
        headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
                   {"name": "To", "value": "me@example.com"}, {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"}]
        if list_unsub:
            headers.append({"name": "List-Unsubscribe", "value": "<mailto:u@example.com>"})
        parts = [{"filename": "invoice.pdf"}] if attachment else []
        self.messages[mid] = {
            "id": mid, "threadId": f"t-{mid}", "internalDate": str(date_ms), "snippet": snippet,
            "labelIds": labels or ["INBOX", "UNREAD"], "sizeEstimate": 1234,
            "payload": {"headers": headers, "parts": parts},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "me@example.com"})
        if path.endswith("/labels"):
            if method == "POST":
                body = json.loads(request.content or b"{}")
                lid = f"Label_{self._next_label}"
                self._next_label += 1
                self.labels[lid] = body.get("name", lid)
                return httpx.Response(200, json={"id": lid, "name": self.labels[lid]})
            return httpx.Response(200, json={"labels": [{"id": k, "name": v} for k, v in self.labels.items()]})
        if path.endswith("/batchModify"):
            if self.fail_mods > 0:
                self.fail_mods -= 1
                return httpx.Response(self.fail_status, json={"error": {"message": "simulated failure"}})
            body = json.loads(request.content or b"{}")
            self.mods.append(body)
            return httpx.Response(204)
        if path.endswith("/messages"):
            raw_q = request.url.params.get("q", "") or ""
            self.queries.append(raw_q)
            needle = raw_q.split()[0].split(":")[-1].strip().lower()   # 'in:inbox -in:spam' -> 'inbox'
            ids = [mid for mid, m in self.messages.items() if not needle or needle in json.dumps(m).lower()]
            start = int(request.url.params.get("pageToken") or "0")
            max_results = int(request.url.params.get("maxResults", "100"))
            page = ids[start:start + max_results]
            body: dict = {"messages": [{"id": i} for i in page]}
            if start + max_results < len(ids):
                body["nextPageToken"] = str(start + max_results)
            return httpx.Response(200, json=body)
        if "/messages/" in path:
            mid = path.rsplit("/", 1)[-1]
            if mid not in self.messages:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json=self.messages[mid])
        return httpx.Response(404, json={"error": {"message": f"unhandled {method} {path}"}})


class FakeCreds:
    """Stands in for google.oauth2.credentials.Credentials: no network, refreshable."""

    def __init__(self, token: str = "test-token") -> None:
        self.token = token
        self.valid = True
        self.expired = False
        self.refresh_token = "fake-refresh"

    def refresh(self, request: object) -> None:
        self.token = "refreshed-token"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path, db_path=tmp_path / "damien.db", db_url=None, credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "token.json", account="me@example.com", scopes=("x",), judge="fake",
        model="jev-latest", api_key=None, page_size=2, op_batch=2, judge_batch=2,
        confidence_act=0.8, confidence_ask=0.55, protect_labels=("ACTION REQUIRED", "WAITING FOR"),
    )


@pytest.fixture
def fake() -> FakeGmailState:
    return FakeGmailState()


@pytest.fixture
def gmail(settings: Settings, fake: FakeGmailState) -> Gmail:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    g = Gmail(settings, http=client)
    g._creds = FakeCreds()                                              # no OAuth in tests
    return g


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, settings: Settings):
    """Every store test runs against both dialects, so they cannot drift.

    Postgres runs only when DAMIEN_TEST_DB_URL names a scratch database.
    """
    if request.param == "sqlite":
        s = Store(settings.db_path)
        yield s
        s.close()
        return
    url = os.environ.get("DAMIEN_TEST_DB_URL")
    if not url:
        pytest.skip("DAMIEN_TEST_DB_URL is not set — Postgres half of the matrix skipped")
    s = Store(url)
    s.reset()
    yield s
    s.reset()
    s.close()
