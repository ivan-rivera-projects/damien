"""The Gmail client: parsing, paging, and the batching that makes scale affordable."""

from __future__ import annotations

from damien.gmail import Gmail


def test_list_ids_pages_through_everything(gmail: Gmail, fake):
    for i in range(5):
        fake.add(f"m{i}")
    ids = gmail.list_ids("", limit=None)
    assert ids == [f"m{i}" for i in range(5)]
    # page_size is 2 in the fixture: 5 ids must take 3 list calls
    assert sum(1 for m, p in fake.calls if m == "GET" and p.endswith("/messages")) == 3


def test_list_ids_honours_a_limit(gmail: Gmail, fake):
    for i in range(5):
        fake.add(f"m{i}")
    assert len(gmail.list_ids("", limit=3)) == 3


def test_fetch_parses_headers_and_flags(gmail: Gmail, fake):
    fake.add("m1", subject="Your receipt", sender='"Shop Billing" <orders@shop.example>',
             list_unsub=True, attachment=True)
    [message] = gmail.fetch(["m1"])
    assert message.subject == "Your receipt"
    assert message.from_addr == "orders@shop.example"
    assert message.from_name == "Shop Billing"
    assert message.list_unsubscribe is True
    assert message.has_attachment is True
    assert message.in_inbox and message.is_unread


def test_batch_ops_are_batched_at_the_configured_size(gmail: Gmail, fake):
    ids = [f"m{i}" for i in range(5)]
    assert gmail.archive(ids) == 5
    assert len(fake.mods) == 3                       # op_batch == 2 in the fixture
    assert fake.mods[0] == {"ids": ["m0", "m1"], "addLabelIds": [], "removeLabelIds": ["INBOX"]}
    assert fake.mods[-1]["ids"] == ["m4"]


def test_trash_is_bulk_and_never_per_message(gmail: Gmail, fake):
    gmail.trash([f"m{i}" for i in range(4)])
    assert len(fake.mods) == 2
    assert fake.mods[0]["addLabelIds"] == ["TRASH"]
    assert fake.mods[0]["removeLabelIds"] == ["INBOX", "UNREAD"]
    assert all("TRASH" in m["addLabelIds"] for m in fake.mods)
    assert not [c for c in fake.calls if "/trash" in c[1]]        # never a per-message trash call


def test_label_lookup_uses_names_and_creates_when_missing(gmail: Gmail, fake):
    assert gmail.label_id("Newsletters") == "Label_1"
    assert gmail.label_id("Nope") is None
    new_id = gmail.create_label("Receipts")
    assert fake.labels[new_id] == "Receipts"
    assert gmail.label_id("Receipts") == new_id


def test_undo_ops_are_the_inverse(gmail: Gmail, fake):
    gmail.untrash(["m0"])
    assert fake.mods[0] == {"ids": ["m0"], "addLabelIds": ["INBOX"], "removeLabelIds": ["TRASH"]}
    gmail.mark_read(["m0"])
    assert fake.mods[1]["removeLabelIds"] == ["UNREAD"]


def test_a_401_refreshes_once_then_succeeds(settings):
    import httpx
    from conftest import FakeCreds

    calls = {"n": 0}
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        seen_tokens.append(request.headers.get("Authorization", ""))
        if calls["n"] == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"emailAddress": "me@example.com"})

    g = Gmail(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    g._creds = FakeCreds()
    assert g.profile()["emailAddress"] == "me@example.com"
    assert calls["n"] == 2
    assert seen_tokens[0].endswith("test-token") and seen_tokens[1].endswith("refreshed-token")


def test_spam_and_trash_are_excluded_unless_asked_for(gmail: Gmail, fake):
    fake.add("m1")
    gmail.list_ids("in:inbox")
    assert fake.queries[0] == "in:inbox -in:spam -in:trash"

    gmail.list_ids("in:anywhere")
    assert fake.queries[1] == "in:anywhere"          # asked for it: untouched

    gmail.list_ids("in:spam")
    assert fake.queries[2] == "in:spam"


def test_an_empty_query_still_means_the_inbox(gmail: Gmail, fake):
    fake.add("m1")
    gmail.list_ids("")
    assert fake.queries[0] == "in:inbox -in:spam -in:trash"


# ---- throttling: Gmail says "too fast" with a 403, not a 429 --------------------
def test_a_403_quota_error_is_retried_not_fatal(settings):
    import httpx
    from conftest import FakeCreds

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(403, json={"error": {"code": 403, "message":
                        "Quota exceeded for quota metric 'Total Query Cost' and limit "
                        "'Units per minute per user' of service 'gmail.googleapis.com'"}})
        return httpx.Response(200, json={"emailAddress": "me@example.com"})

    g = Gmail(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    g._creds = FakeCreds()
    from damien.gmail import RateLimiter
    g.limiter = RateLimiter(rate_per_second=10_000)                   # no pacing in the test
    assert g.profile()["emailAddress"] == "me@example.com"
    assert calls["n"] == 3 and g.throttled == 2


def test_a_403_that_is_not_a_throttle_still_raises(settings):
    import httpx
    import pytest
    from conftest import FakeCreds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": 403, "message": "Insufficient Permission"}})

    g = Gmail(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    g._creds = FakeCreds()
    from damien.gmail import RateLimiter
    g.limiter = RateLimiter(rate_per_second=10_000)
    with pytest.raises(Exception, match="Insufficient Permission"):
        g.profile()


def test_retry_after_header_is_honoured():
    import httpx

    from damien.gmail import _is_throttle, _retry_after
    throttled = httpx.Response(403, headers={"Retry-After": "7"}, text="Quota exceeded")
    assert _is_throttle(throttled) is True
    assert _retry_after(throttled) == 7.0
    assert _is_throttle(httpx.Response(403, text="Insufficient Permission")) is False
    assert _retry_after(httpx.Response(200)) is None


def test_the_rate_limiter_paces(settings):
    from damien.gmail import RateLimiter
    limiter = RateLimiter(rate_per_second=50)
    t0 = __import__("time").monotonic()
    for _ in range(10):
        limiter.take()
    assert __import__("time").monotonic() - t0 < 1.0        # a burst inside the bucket is free


def test_a_throttle_slows_the_client_down_for_the_rest_of_the_run(settings):
    import httpx
    from conftest import FakeCreds

    from damien.gmail import Gmail

    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(403, headers={"Retry-After": "1"}, text="Quota exceeded for quota metric")
        return httpx.Response(200, json={"emailAddress": "me@example.com"})

    g = Gmail(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    g._creds = FakeCreds()
    before = g.limiter.rate
    assert g.profile()["emailAddress"] == "me@example.com"
    assert g.limiter.rate == before / 2               # it learned
    assert g.throttled == 1
