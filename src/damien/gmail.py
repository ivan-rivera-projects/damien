"""Gmail, over HTTP, with batching that is actually batching.

The old Damien used google-api-python-client and issued one call per message for
mutations. Everything here is built on four facts, which is why a 5,000-message
pass costs about fifteen requests instead of five thousand:

  * `messages.list` pages at 500 ids per call.
  * `messages.batchModify` takes up to 1,000 ids and can add/remove labels —
    including the system labels TRASH, INBOX and UNREAD.
  * So archive = removeLabelIds:[INBOX], trash = addLabelIds:[TRASH],
    mark read = removeLabelIds:[UNREAD]. All bulk, all revertible.
  * `users.messages.get` is the only per-message call, and we only make it for
    messages whose headers we actually need to judge.

`http` is injectable so every test runs offline.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import httpx

from .config import Settings
from .models import Message

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URI = "https://oauth2.googleapis.com/token"
HEADERS = ("From", "To", "Subject", "Date", "List-Unsubscribe", "List-Id")


class GmailError(RuntimeError):
    pass


class Gmail:
    def __init__(self, settings: Settings, http: httpx.Client | None = None, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._http = http or httpx.Client(timeout=60.0, transport=transport)
        self._creds: Any = None
        self._label_names: dict[str, str] | None = None

    # ---- auth ---------------------------------------------------------
    def login(self, open_browser: bool = True) -> str:
        """One-time installed-app flow. Writes the token; prints nothing secret."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.settings.credentials_path.is_file():
            raise GmailError(f"no OAuth client file at {self.settings.credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.settings.credentials_path), list(self.settings.scopes))
        creds = flow.run_local_server(port=0, open_browser=open_browser) if open_browser else flow.run_console()
        self.settings.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.token_path.write_text(creds.to_json())
        self.settings.token_path.chmod(0o600)
        self._creds = creds
        return self.profile().get("emailAddress", "")

    def _credentials(self) -> Any:
        if self._creds is not None:
            return self._creds
        if not self.settings.token_path.is_file():
            raise GmailError("not signed in — run `damien login`")
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(self.settings.token_path), list(self.settings.scopes))
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.settings.token_path.write_text(creds.to_json())
                self.settings.token_path.chmod(0o600)
            else:
                raise GmailError("token is not usable — run `damien login` again")
        self._creds = creds
        return creds

    def _token(self) -> str:
        return self._credentials().token

    def _force_refresh(self) -> None:
        """Refresh the access token in place, keeping the same credentials object."""
        try:
            creds = self._credentials()
        except GmailError:
            return
        if getattr(creds, "refresh_token", None):
            from google.auth.transport.requests import Request

            creds.refresh(Request())
            try:
                self.settings.token_path.write_text(creds.to_json())
                self.settings.token_path.chmod(0o600)
            except Exception:
                pass                                           # a read-only home must not kill the run
        else:
            self._creds = None                                 # next call re-reads the token file

    # ---- transport ----------------------------------------------------
    def _request(self, method: str, url: str, *, params: dict | None = None, json_body: dict | None = None,
                 attempts: int = 5) -> dict:
        delay = 0.5
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._http.request(
                    method, url, params=params, json=json_body,
                    headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:                     # network: retry
                last = exc
            else:
                if resp.status_code == 401 and attempt == 0:
                    self._force_refresh()                       # token aged out mid-run: refresh once, in place
                    continue
                if resp.status_code in (429, 500, 502, 503, 504):  # quota/backoff: retry
                    last = GmailError(f"{resp.status_code}: {resp.text[:200]}")
                elif resp.status_code >= 400:
                    raise GmailError(f"{method} {url} -> {resp.status_code}: {resp.text[:400]}")
                else:
                    return resp.json() if resp.content else {}
            time.sleep(delay + random.random() * 0.2)
            delay = min(delay * 2, 8.0)
        raise GmailError(f"gave up after {attempts} attempts: {last}")

    # ---- reads --------------------------------------------------------
    def profile(self) -> dict:
        return self._request("GET", f"{API}/profile")

    def labels(self, refresh: bool = False) -> dict[str, str]:
        """id -> name for every label; small, cached for the process."""
        if self._label_names is None or refresh:
            data = self._request("GET", f"{API}/labels")
            self._label_names = {l["id"]: l.get("name", l["id"]) for l in data.get("labels", [])}
        return self._label_names

    def label_id(self, name: str) -> str | None:
        for lid, lname in self.labels().items():
            if lname.lower() == name.lower() or lid.lower() == name.lower():
                return lid
        return None

    def create_label(self, name: str) -> str:
        data = self._request("POST", f"{API}/labels", json_body={"name": name, "labelListVisibility": "labelShow",
                                                                 "messageListVisibility": "show"})
        self.labels(refresh=True)
        return data["id"]

    def list_ids(self, query: str, *, limit: int | None = None) -> list[str]:
        """Every matching id, paged. Ids only — no per-message call."""
        ids: list[str] = []
        page = None
        while True:
            params: dict[str, Any] = {"q": query, "maxResults": self.settings.page_size}
            if page:
                params["pageToken"] = page
            data = self._request("GET", f"{API}/messages", params=params)
            ids.extend(m["id"] for m in data.get("messages", []))
            page = data.get("nextPageToken")
            if not page or (limit and len(ids) >= limit):
                break
        return ids[:limit] if limit else ids

    def fetch(self, ids: Sequence[str], *, with_snippet: bool = True) -> list[Message]:
        """Headers for a set of ids. One call per id — so call it once per message, ever."""
        out: list[Message] = []
        for mid in ids:
            data = self._request("GET", f"{API}/messages/{mid}",
                                 params={"format": "metadata", "metadataHeaders": list(HEADERS)})
            payload = data.get("payload", {})
            hdrs = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            parts = payload.get("parts") or []
            out.append(Message(
                id=data["id"], account=self.settings.account or "me", thread_id=data.get("threadId"),
                internal_date=int(data.get("internalDate") or 0) or None,
                from_addr=_address(hdrs.get("from")), from_name=_display(hdrs.get("from")),
                to=hdrs.get("to"), subject=hdrs.get("subject"),
                snippet=data.get("snippet") if with_snippet else None,
                label_ids=list(data.get("labelIds") or []),
                has_attachment=any(p.get("filename") for p in parts),
                list_unsubscribe="list-unsubscribe" in hdrs,
                list_id=hdrs.get("list-id"),
                size_estimate=data.get("sizeEstimate"),
            ))
        return out

    # ---- writes (all bulk) -------------------------------------------
    def modify(self, ids: Sequence[str], *, add: Iterable[str] = (), remove: Iterable[str] = ()) -> int:
        done = 0
        for chunk in _chunks(list(ids), self.settings.op_batch):
            self._request("POST", f"{API}/messages/batchModify",
                          json_body={"ids": chunk, "addLabelIds": list(add), "removeLabelIds": list(remove)})
            done += len(chunk)
        return done

    def archive(self, ids: Sequence[str]) -> int:
        return self.modify(ids, remove=["INBOX"])

    def trash(self, ids: Sequence[str]) -> int:
        # addLabelIds:[TRASH] is the bulk form of messages.trash — same result, 1 call per 1000.
        return self.modify(ids, add=["TRASH"], remove=["INBOX", "UNREAD"])

    def untrash(self, ids: Sequence[str]) -> int:
        return self.modify(ids, add=["INBOX"], remove=["TRASH"])

    def mark_read(self, ids: Sequence[str]) -> int:
        return self.modify(ids, remove=["UNREAD"])

    def add_label(self, ids: Sequence[str], label_id: str, *, keep_in_inbox: bool = False) -> int:
        return self.modify(ids, add=[label_id], remove=[] if keep_in_inbox else ["INBOX"])


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _address(value: str | None) -> str | None:
    if not value:
        return None
    if "<" in value and ">" in value:
        return value[value.rfind("<") + 1:value.rfind(">")].strip().lower()
    return value.strip().lower()


def _display(value: str | None) -> str | None:
    if not value:
        return None
    if "<" in value:
        return value[:value.rfind("<")].strip().strip('"') or None
    return None
