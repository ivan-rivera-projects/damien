"""Turning a raw Gmail payload into the newest words a person actually wrote.

The judge is weakened by irrelevant material — the vendor calls it context rot — so this is
deliberately narrow: the newest text, quoted replies and signatures removed, HTML flattened,
capped. Nothing here ever touches an attachment.

Called only for messages the headers and snippet could not settle (see `read_bodies` in
`plan.build_plan`), so it is allowed to be careful rather than fast.
"""
from __future__ import annotations

import base64
import html
import re
from collections.abc import Sequence
from typing import Any

DEFAULT_LIMIT = 4000

_SCRIPT = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BREAK = re.compile(
    r"<(br|/p|/div|/tr|/li|/h[1-6]|/blockquote|/td|/th|/table|/ul|/ol)\b[^>]*>", re.IGNORECASE
)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# A reply starts here; everything below it is history the person has already read.
_QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*On .{5,160}\bwrote:\s*$", re.MULTILINE),          # On 1 Jan, Alex wrote:
    re.compile(r"^\s*-{2,}\s*(original|forwarded) message\s*-{2,}", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*From:\s.+$", re.MULTILINE),                        # Outlook quote headers
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*>", re.MULTILINE),                                 # a quoted line
)
_SIGNATURE = re.compile(r"^\s*--\s*$", re.MULTILINE)


def html_to_text(markup: str) -> str:
    """Flatten HTML without a dependency: drop scripts, keep line breaks, unescape."""
    text = _SCRIPT.sub(" ", markup)
    text = _BREAK.sub("\n", text)
    # inline tags vanish rather than becoming spaces, or "<b>W-9</b>?" reads as "W-9 ?"
    text = _TAG.sub("", text)
    return html.unescape(text)


def tidy(text: str) -> str:
    """Collapse the whitespace that MIME and HTML leave behind."""
    lines = [_SPACES.sub(" ", line).strip() for line in (text or "").splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def strip_quoted(text: str) -> str:
    """The newest text only: cut at the first line that begins a quoted reply.

    If that leaves nothing (a reply with no new words), return an empty string — the caller
    then treats the message as having no readable body, which is the honest answer.
    """
    cleaned = tidy(text)
    if not cleaned:
        return ""
    cut = len(cleaned)
    for marker in _QUOTE_MARKERS:
        found = marker.search(cleaned)
        if found and found.start() < cut:
            cut = found.start()
    signature = _SIGNATURE.search(cleaned)
    if signature and signature.start() < cut:
        cut = signature.start()
    return cleaned[:cut].strip()


def excerpt(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """The newest text, quoted replies gone, capped — and cut at a word, not mid-word."""
    body = strip_quoted(text)
    if len(body) <= limit:
        return body
    clipped = body[:limit]
    space = clipped.rfind(" ")
    return (clipped[:space] if space > limit * 0.8 else clipped).strip() + " […]"


def payload_text(payload: dict[str, Any], *, limit: int = DEFAULT_LIMIT) -> str:
    """The readable text of one Gmail payload: text/plain if present, else text/html.

    Attachments are never read — a part with a filename is skipped entirely, which also
    keeps a holiday photo out of a judgement about mail.
    """
    plain = _first_part(payload, "text/plain")
    if plain is not None:
        return excerpt(plain, limit)
    markup = _first_part(payload, "text/html")
    if markup is not None:
        return excerpt(html_to_text(markup), limit)
    return ""


def _first_part(payload: dict[str, Any], wanted: str) -> str | None:
    """Depth-first for the first part of a given mime type, decoding it if it has data."""
    stack: list[dict[str, Any]] = [payload]
    while stack:
        part = stack.pop(0)
        if part.get("filename"):
            continue                                   # an attachment: never read
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if mime == wanted and data:
            return _decode(data)
        stack.extend(part.get("parts") or [])
    return None


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def part_texts(parts: Sequence[dict[str, Any]]) -> list[str]:
    """Every readable body in a payload, for tests and for callers that want them all."""
    out: list[str] = []
    for part in parts:
        if part.get("filename"):
            continue
        data = (part.get("body") or {}).get("data")
        if data and (part.get("mimeType") or "").startswith("text/"):
            out.append(_decode(data))
        out.extend(part_texts(part.get("parts") or []))
    return out
