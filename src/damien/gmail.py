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
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from .config import Settings
from .mime import DEFAULT_LIMIT, payload_text
from .models import Message

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URI = "https://oauth2.googleapis.com/token"
HEADERS = ("From", "To", "Subject", "Date", "List-Unsubscribe", "List-Id")


class GmailError(RuntimeError):
    pass


class RateLimiter:
    """A token bucket.

    Gmail's quota is charged per unit per minute and a burst is what trips it —
    eight parallel metadata calls are 40 units/second, fine, but not when another
    pass is paging at the same time. This keeps the whole process under a rate we
    choose instead of discovering the ceiling with a 403.
    """

    FLOOR = 5.0            # never crawl: even throttled, 5/s is 1,500 units/minute
    RECOVER_AFTER = 200    # successes before it stops being punished for one bad moment

    def __init__(self, rate_per_second: float):
        self.ceiling = max(0.1, rate_per_second)
        self.rate = self.ceiling
        self.floor = min(self.FLOOR, self.rate)
        self.capacity = max(1.0, self.rate)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._ok = 0
        self._lock = threading.Lock()

    def slow_down(self, factor: float = 0.5) -> None:
        """Gmail told us we are too fast. Slower — but never into a crawl.

        Halving with no floor is how a run that should take three minutes took
        eighty: one throttled moment drove the rate to its minimum and everything
        after it inherited that. Five a second is still 1,500 units a minute, well
        under any ceiling, and slow enough to let a quota window clear.
        """
        with self._lock:
            self.rate = max(self.floor, self.rate * factor)
            self.capacity = max(1.0, self.rate)
            self._ok = 0

    def note_success(self) -> None:
        """Sustained success earns the rate back, so a bad moment is not permanent."""
        with self._lock:
            self._ok += 1
            if self._ok >= self.RECOVER_AFTER:
                self._ok = 0
                self.rate = min(self.ceiling, self.rate * 1.5)
                self.capacity = max(1.0, self.rate)

    def take(self) -> float:
        """Block until one request may go. Returns the seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return waited
                sleep_for = (1 - self._tokens) / self.rate
            time.sleep(min(sleep_for, 0.2))
            waited += min(sleep_for, 0.2)


class Gmail:
    def __init__(self, settings: Settings, http: httpx.Client | None = None, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._http = http or httpx.Client(timeout=60.0, transport=transport)
        self._creds: Any = None
        self._label_names: dict[str, str] | None = None
        self.fetched = 0                                  # messages actually downloaded this run
        self.bodies = 0                                   # bodies read (the escalation set only)
        self.throttled = 0                                # times Gmail said "too fast"
        self.limiter = RateLimiter(getattr(settings, "max_rps", 20.0))

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
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(self.settings.token_path), list(self.settings.scopes))
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                self._refresh(creds)
            else:
                raise GmailError("token is not usable — run `damien login` again")
        self._creds = creds
        return creds

    def _refresh(self, creds: Any) -> None:
        """Refresh an access token, and turn the two ways that fails into something a person
        can act on — both are common, and neither should arrive as a Python traceback.

        A token expires (an OAuth app left in Google's "Testing" status expires refresh
        tokens after seven days) or a client is deleted from the Cloud console.
        """
        from google.auth.transport.requests import Request

        try:
            creds.refresh(Request())
        except Exception as exc:                        # google-auth raises several shapes
            text = str(exc).lower()
            if "deleted_client" in text or "deleted client" in text:
                raise GmailError(
                    "the OAuth client this sign-in was made with has been deleted. Create a new "
                    "desktop client (docs/SETUP.md step 2), save it as credentials.json, then "
                    "run `damien login`."
                ) from exc
            if "invalid_grant" in text or "invalid_token" in text or "expired" in text or "revoked" in text:
                raise GmailError(
                    "the sign-in has expired or was revoked. Run `damien login`. If this keeps "
                    "happening weekly, publish your OAuth consent screen: an app in Google's "
                    "\"Testing\" status expires refresh tokens after seven days (docs/SETUP.md)."
                ) from exc
            raise GmailError(f"could not refresh the Gmail sign-in: {exc}") from exc
        try:
            self.settings.token_path.write_text(creds.to_json())
            self.settings.token_path.chmod(0o600)
        except OSError:
            pass                                        # a read-only home must not break a read

    def _token(self) -> str:
        return self._credentials().token

    def _force_refresh(self) -> None:
        """Refresh the access token in place, keeping the same credentials object."""
        try:
            creds = self._credentials()
        except GmailError:
            return
        if getattr(creds, "refresh_token", None):
            try:
                self._refresh(creds)
            except GmailError:
                return
            except Exception:
                pass                                           # a read-only home must not kill the run
        else:
            self._creds = None                                 # next call re-reads the token file

    # ---- transport ----------------------------------------------------
    def _request(self, method: str, url: str, *, params: dict | None = None, json_body: dict | None = None,
                 attempts: int = 4) -> dict:
        """One HTTP call, with a retry policy per failure type.

        A quota ceiling is worth waiting out — it clears, and it clears sooner if we
        stop pushing. A broken endpoint is not: waiting four minutes per request
        across a few thousand requests is how a run never ends. So a plain server
        error gets four tries inside a minute, and a throttle switches this request
        to ten tries with a minute's patience.
        """
        delay = 0.5
        cap = 8.0
        max_attempts = attempts
        last: Exception | None = None
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            self.limiter.take()
            resp: httpx.Response | None = None
            try:
                resp = self._http.request(
                    method, url, params=params, json=json_body,
                    headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:                     # network: retry
                last = exc
            else:
                if resp.status_code == 401 and attempt == 1:
                    self._force_refresh()                       # token aged out mid-run: refresh once, in place
                    continue
                if resp.status_code in (429, 500, 502, 503, 504, 529) or _is_throttle(resp):
                    # Gmail reports a quota breach as 403, not 429. A 403 that is not a
                    # throttle is a real refusal and still raises.
                    if resp.status_code == 429 or _is_throttle(resp):
                        self.throttled += 1
                        self.limiter.slow_down()          # the ceiling is real: back off for the rest of the run
                        max_attempts, cap = 10, 60.0      # this one is worth waiting out
                        delay = max(delay, 3.0)
                    last = GmailError(f"{resp.status_code}: {resp.text[:200]}")
                elif resp.status_code >= 400:
                    raise GmailError(f"{method} {url} -> {resp.status_code}: {resp.text[:400]}")
                else:
                    self.limiter.note_success()
                    return resp.json() if resp.content else {}
            wait = _retry_after(resp) if resp is not None else None
            time.sleep(wait if wait is not None else delay + random.random() * 0.2)
            delay = min(delay * 2, cap)
        raise GmailError(f"gave up after {max_attempts} attempts: {last}")

    # ---- reads --------------------------------------------------------
    def profile(self) -> dict:
        return self._request("GET", f"{API}/profile")

    def labels(self, refresh: bool = False) -> dict[str, str]:
        """id -> name for every label; small, cached for the process."""
        if self._label_names is None or refresh:
            data = self._request("GET", f"{API}/labels")
            self._label_names = {label["id"]: label.get("name", label["id"]) for label in data.get("labels", [])}
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
        """Every matching id, paged. Ids only — no per-message call.

        Spam and Trash are excluded unless the query names them: Google's spam
        filter is better than anything this tool could do, so it is trusted by
        default rather than second-guessed.
        """
        query = _exclude_spam_and_trash(query)
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

    def fetch(self, ids: Sequence[str], *, with_snippet: bool = True, concurrency: int | None = None) -> list[Message]:
        """Headers for a set of ids. One call per id — so call it once per message, ever.

        Parallel by default: 200 sequential calls were ~30 of the ~40 seconds a real
        200-message pass took. Order is preserved.
        """
        workers = max(1, concurrency or self.settings.fetch_concurrency)
        total = len(ids)
        if total >= 250:
            import sys
            print(f"  fetching {total} messages ({workers} at a time)…", file=sys.stderr, flush=True)
        if total > 1 and workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                return list(pool.map(lambda mid: self._fetch_one(mid, with_snippet), ids))
        return [self._fetch_one(mid, with_snippet) for mid in ids]

    def _fetch_one(self, mid: str, with_snippet: bool) -> Message:
        self.fetched += 1
        data = self._request("GET", f"{API}/messages/{mid}",
                             params={"format": "metadata", "metadataHeaders": list(HEADERS)})
        payload = data.get("payload", {})
        hdrs = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        parts = payload.get("parts") or []
        return Message(
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
        )

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

    def add_label(self, ids: Sequence[str], label_id: str, *, keep_in_inbox: bool = False,
                  mark_read: bool = False) -> int:
        """Label in bulk. `keep_in_inbox` leaves it in the inbox; `mark_read` drops UNREAD
        (they are mutually exclusive in practice: something you keep unread is not filed)."""
        remove: list[str] = [] if keep_in_inbox else ["INBOX"]
        if mark_read and not keep_in_inbox:
            remove.append("UNREAD")
        return self.modify(ids, add=[label_id], remove=remove)

    def fetch_bodies(self, ids: Sequence[str], *, concurrency: int | None = None,
                     limit: int = DEFAULT_LIMIT) -> dict[str, str]:
        """The newest words of each message — only ever called for the escalation set.

        `format=full` is a far bigger response than the metadata call the rest of the tool
        makes, so this is for the messages the snippet could not settle and no others. Only
        ids with readable text come back: a message whose new words are all quoted has none,
        and is left to be decided on its headers.
        """
        if not ids:
            return {}
        workers = max(1, concurrency or self.settings.fetch_concurrency)
        if len(ids) > 1 and workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pairs = list(pool.map(lambda mid: (mid, self._fetch_body(mid, limit)), ids))
        else:
            pairs = [(mid, self._fetch_body(mid, limit)) for mid in ids]
        return {mid: text for mid, text in pairs if text}

    def _fetch_body(self, mid: str, limit: int) -> str:
        self.bodies += 1
        data = self._request("GET", f"{API}/messages/{mid}", params={"format": "full"})
        return payload_text(data.get("payload") or {}, limit=limit)


THROTTLE_MARKS = ("Quota exceeded", "rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED",
                  "Too many requests", "Rate Limit")


def _is_throttle(resp: httpx.Response) -> bool:
    """A 403 that means 'slow down' rather than 'not allowed'."""
    if resp.status_code != 403:
        return False
    return any(mark in resp.text for mark in THROTTLE_MARKS)


def _retry_after(resp: httpx.Response | None) -> float | None:
    if resp is None:
        return None
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), 120.0)
    except ValueError:
        return None


def _exclude_spam_and_trash(query: str) -> str:
    q = query or "in:inbox"
    if not any(token in q for token in ("in:spam", "in:trash", "in:anywhere")):
        q = f"{q} -in:spam -in:trash"
    return q


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
