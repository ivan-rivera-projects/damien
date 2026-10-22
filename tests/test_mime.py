"""The extractor decides whether reading a body helps or hurts: the newest text, nothing
else. The vendor's own finding is that irrelevant material costs accuracy."""
from __future__ import annotations

import base64

from damien.mime import DEFAULT_LIMIT, excerpt, html_to_text, payload_text, strip_quoted, tidy


def part(mime: str, text: str, *, filename: str | None = None) -> dict:
    data = base64.urlsafe_b64encode(text.encode()).decode()
    return {"mimeType": mime, "filename": filename or "", "body": {"data": data}}


def test_the_newest_words_survive_and_the_quote_does_not():
    body = ("Tuesday at 3 works. Bring the split sheet.\n"
            "\n"
            "On Mon, 6 Oct 2026 at 09:12, Alex <alex@studio.example> wrote:\n"
            "> can we talk tuesday?\n"
            "> i have the masters\n")
    assert strip_quoted(body) == "Tuesday at 3 works. Bring the split sheet."


def test_outlook_and_signature_styles_also_stop_the_text():
    assert strip_quoted("Here is the invoice.\n-----Original Message-----\nfrom a client") == "Here is the invoice."
    assert strip_quoted("Approved.\n--\nIvan\nya label") == "Approved."
    assert strip_quoted("Sounds good.\n\n> quoted reply only") == "Sounds good."


def test_a_reply_with_no_new_words_has_no_body():
    """Honest answer: if all that is new is the quote, the body tells us nothing extra."""
    assert strip_quoted("On Mon, Alex wrote:\n> the whole thing") == ""
    assert excerpt("On Mon, Alex wrote:\n> the whole thing") == ""


def test_html_is_flattened_without_a_dependency():
    markup = "<html><style>p{color:red}</style><body><p>Hi Ivan,</p><p>Can you send the <b>W-9</b>?</p><br>Thanks</body></html>"
    assert html_to_text(markup).split() == ["Hi", "Ivan,", "Can", "you", "send", "the", "W-9?", "Thanks"]


def test_attachments_are_never_read():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "filename": "holiday.jpg", "body": {"data": "cGhvdG8="}},
        part("text/plain", "The invoice is attached."),
    ]}
    assert payload_text(payload) == "The invoice is attached."


def test_plain_text_wins_over_html():
    payload = {"mimeType": "multipart/alternative", "parts": [
        part("text/html", "<p>HTML version with <a href='x'>links</a></p>"),
        part("text/plain", "plain version, no links"),
    ]}
    assert payload_text(payload) == "plain version, no links"


def test_html_alone_still_gives_text():
    payload = part("text/html", "<div>Just <b>HTML</b> here</div>")
    assert payload_text(payload) == "Just HTML here"


def test_a_long_body_is_capped_at_a_word_boundary():
    body = " ".join(f"word{i}" for i in range(3000))
    got = excerpt(body)
    assert len(got) <= DEFAULT_LIMIT + 6
    assert got.endswith("[…]") and "word" in got
    assert not got.rstrip("[…]").endswith("wor")          # cut at a space, not mid-word


def test_tidy_keeps_paragraphs_but_not_rivers_of_blank_lines():
    assert tidy("a\r\n\r\n\r\n\r\nb   \n") == "a\n\nb"


def test_an_empty_payload_is_empty_not_an_error():
    assert payload_text({}) == ""
    assert payload_text({"mimeType": "text/plain"}) == ""


def test_an_attached_or_forwarded_text_is_not_the_body():
    """S2: an unnamed `Content-Disposition: attachment` part, or a forwarded message, is
    not what the sender wrote."""
    attached = {**part("text/plain", "attached contract text"),
                "headers": [{"name": "Content-Disposition", "value": "attachment"}]}
    forwarded = {"mimeType": "message/rfc822", "filename": "",
                 "parts": [part("text/plain", "forwarded words")]}
    payload = {"mimeType": "multipart/mixed", "parts": [attached, forwarded, part("text/plain", "my own words")]}
    assert payload_text(payload) == "my own words"
