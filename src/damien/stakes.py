"""The stake guardrail: a floor under the model's judgment.

Not a classifier — a short list of things a person would be angry to lose. A
Choice question has no notion of stakes, so the model can be perfectly confident
and still archive "your domains will expire in 30 days". Three real ones did.

The floor only ever raises an action to `keep`; it never trashes, and it never
touches a rule or a person's decision (those are not guesses). Everything the
model said is still recorded beside it — the guardrail adds a reason, not a veto.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .models import Message

# Money, deadlines, account and legal trouble: if the subject names one of these,
# a model's archive/trash is not allowed to stand. Erring towards `keep` costs an
# inbox row; erring the other way costs a domain or a payment.
CONSEQUENTIAL = re.compile(
    r"\b(invoice|payment|paid|refund|charge|billing|past due|overdue|insufficient|declined|"
    r"expire|expires|expiring|expiry|renew|renewal|deadline|action required|"
    r"verify|verification|suspend|suspended|locked|unauthori[sz]ed|breach|security|"
    r"sign-in|signin|password|one-time|2fa|mfa|"
    r"legal|contract|agreement|lawsuit|cease and desist|tax|irs|wire|transfer)\b",
    re.IGNORECASE,
)

# Software, not a person. Deliberately broad: the point is to avoid *claiming* a
# human wrote something.
MACHINERY = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notification|mailer|bounce|updates?|alerts?|"
    r"newsletter|marketing|automated|system|robot|postmaster|campaign|news@|info@)",
    re.IGNORECASE,
)


# Labels that mean "a human is waiting" — mail a model must not file away. The
# person applied these themselves, so they outrank anything Google or Jev think.
DEFAULT_PROTECTED: tuple[str, ...] = ("ACTION REQUIRED", "WAITING FOR", "Bills")


def protected_label(message: Message, label_names: dict[str, str] | None,
                    protected: Sequence[str] = DEFAULT_PROTECTED) -> str | None:
    """The person's own label on this message, if it is one they protect."""
    names = {(label_names or {}).get(lid, lid).strip().lower() for lid in message.own_label_ids}
    for name in protected:
        if name.strip().lower() in names:
            return name
    return None


def _not_a_bulk_sender(message: Message) -> bool:
    return not message.list_unsubscribe and not message.list_id and not MACHINERY.search(message.from_addr or "")


def _is_a_reply(message: Message) -> bool:
    return (message.subject or "").lstrip().lower().startswith(("re:", "fwd:", "fw:"))


def stake_floor(message: Message, *, machine_sent: float | None = None, importance: float | None = None,
                protected: str | None = None) -> str | None:
    """Why a model must not archive or trash this message, or None.

    Two of the three signals are evidence the request already paid for — the model's
    own P(written by software) and its own importance Score. The third is free and
    deterministic: a reply from someone who is not a bulk sender is a person.

    The importance rule exists because the questions are answered independently: a
    real message ("Re: wearelittlegs + Pinterest") came back rated 2.2 of 3 for
    importance and still filed as trash at 0.66. Reading the signals against each
    other is code's job, not the model's.
    """
    if protected:
        return f"the person labelled it {protected!r} themselves"
    if CONSEQUENTIAL.search(message.subject or ""):
        return "the subject names money, a deadline or an account problem"
    if _not_a_bulk_sender(message):
        if _is_a_reply(message):
            return "it is a reply, from someone who is not a bulk sender"
        if machine_sent is not None and machine_sent < 0.3:
            return "it reads as written by a person, not by software"
    if importance is not None and importance >= 2.0:
        return f"the model rated it important ({importance:.1f} of 3) while filing it away"
    return None
