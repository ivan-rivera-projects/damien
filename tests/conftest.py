"""Shared fixtures. Everything runs offline: Gmail is an httpx MockTransport."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest

from damien.config import Settings
from damien.gmail import Gmail
from damien.store import Store

_TOKEN = re.compile(r'-?(?:\w+:)?(?:\{[^}]*\}|\([^)]*\)|"[^"]*"|[^\s{}()]+)')


def _matches(message: dict, query: str, label_names: dict[str, str] | None = None) -> bool:
    """Gmail's search semantics, in miniature: every token must hold.

    Good enough that a test can tell `in:inbox label:X` from a bare `label:X` —
    which is how the fake caught a rule query that had lost its scope. `{a b}` is an
    OR group, `(a b)` an AND group, and a field before either applies to every term.
    """
    return all(_token_holds(message, token, label_names or {}) for token in _TOKEN.findall(query or ""))


def _token_holds(message: dict, token: str, label_names: dict[str, str], field_default: str = "") -> bool:
    labels = message.get("labelIds", [])
    blob = json.dumps(message).lower()
    negative = token.startswith("-")
    word = token[1:] if negative else token
    field, sep, value = word.partition(":")
    if not sep or field.startswith(('"', "{", "(")):
        field, value = field_default, word
    if value[:1] in "{(" and value:
        inner = [t if ":" in t.split('"')[0] or not field else f"{field}:{t}" for t in _TOKEN.findall(value[1:-1])]
        results = (_token_holds(message, t, label_names) for t in inner)
        hit = any(results) if value[0] == "{" else all(results)
        return hit != negative
    value = value.strip('"')
    if field == "in":
        hit = ("INBOX" in labels) if value == "inbox" else (value.upper() in labels)
    elif field == "label":
        names = {re.sub(r"[\s/]+", "-", label_names.get(lid, lid)).lower() for lid in labels}
        hit = value in labels or value.lower() in names or value.lower() in blob
    elif field == "is":
        hit = value.upper() in labels
    elif field in ("older_than", "newer_than"):
        hit = True                                  # time is not modelled by the fake
    elif field == "subject":
        subject = next((h["value"] for h in message.get("payload", {}).get("headers", [])
                        if h["name"] == "Subject"), "")
        hit = re.search(rf"(?<![0-9a-z]){re.escape(value.lower())}(?![0-9a-z])", subject.lower()) is not None
    elif field in ("from", "to"):
        hit = value.lower() in blob
    else:
        hit = value.lower() in blob
    return hit != negative


class FakeGmailState:
    """A tiny stand-in for the Gmail REST surface, in one dict."""

    def __init__(self) -> None:
        self.labels = {"INBOX": "INBOX", "UNREAD": "UNREAD", "TRASH": "TRASH", "Label_1": "Newsletters"}
        self.messages: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []      # (method, path)
        self.mods: list[dict] = []                  # batchModify bodies
        self.queries: list[str] = []                 # every q= that went out
        self.fail_mods = 0                          # fail the next N batchModify calls
        self.live_labels = False                    # apply batchModify label changes to messages
        self.fail_status = 400
        self.result_estimate = 0
        self._next_label = 2

    def add(self, mid: str, *, subject: str = "Hello", sender: str = "Alex <alex@example.com>",
            labels: list[str] | None = None, date_ms: int = 1_700_000_000_000, snippet: str = "hi",
            list_unsub: bool = False, attachment: bool = False, body: str | None = None) -> None:
        headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
                   {"name": "To", "value": "me@example.com"}, {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"}]
        if list_unsub:
            headers.append({"name": "List-Unsubscribe", "value": "<mailto:u@example.com>"})
        parts: list[dict] = [{"filename": "invoice.pdf"}] if attachment else []
        if body is not None:
            import base64
            parts.append({"mimeType": "text/plain", "filename": "",
                          "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}})
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
            if self.live_labels:
                for mid in body.get("ids", []):
                    if mid in self.messages:
                        have = self.messages[mid]["labelIds"]
                        have[:] = [x for x in have if x not in body.get("removeLabelIds", [])]
                        have.extend(x for x in body.get("addLabelIds", []) if x not in have)
            return httpx.Response(204)
        if path.endswith("/messages"):
            raw_q = request.url.params.get("q", "") or ""
            self.queries.append(raw_q)
            ids = [mid for mid, m in self.messages.items() if _matches(m, raw_q, self.labels)]
            if int(request.url.params.get("maxResults", "100")) == 1 and self.result_estimate:
                return httpx.Response(200, json={"messages": [{"id": next(iter(self.messages), "x")}],
                                                 "resultSizeEstimate": self.result_estimate})
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
        header_ttl_hours=12.0, fetch_concurrency=4, max_rps=1000.0,
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
