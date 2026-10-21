"""Damien v2 — the vocabulary.

A Message is what Gmail knows. A Decision is what we concluded about it.
A Plan is a list of decisions a person can read before any of them happen.
Nothing here writes anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(UTC)


class Action(str, Enum):
    """What to do with a message. Small on purpose — each one maps to a Gmail label op."""

    keep = "keep"          # leave alone (recorded so the reason is auditable)
    archive = "archive"    # remove INBOX, keep the message
    trash = "trash"        # move to Trash (recoverable for 30 days)
    label = "label"        # add a label (removes INBOX unless keep_in_inbox)
    mark_read = "mark_read"


class Source(str, Enum):
    rule = "rule"          # a deterministic rule matched
    jev = "jev"            # the System One model decided
    person = "person"      # a human decided, in review or after the fact
    escalation = "escalation"  # second, better-informed model pass
    default = "default"    # nothing matched and no judgment was available


class Message(BaseModel):
    id: str
    account: str
    thread_id: str | None = None
    internal_date: int | None = None          # ms since epoch, Gmail's own
    from_addr: str | None = None
    from_name: str | None = None
    to: str | None = None
    subject: str | None = None
    snippet: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    has_attachment: bool = False
    list_unsubscribe: bool = False
    list_id: str | None = None
    size_estimate: int | None = None

    def age_days(self, at: datetime | None = None) -> float | None:
        if not self.internal_date:
            return None
        ref = at or now()
        return (ref.timestamp() * 1000 - self.internal_date) / 86_400_000

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.label_ids

    @property
    def in_inbox(self) -> bool:
        return "INBOX" in self.label_ids

    @property
    def category(self) -> str | None:
        """Gmail's own tab category, lower-cased: 'promotions', 'updates', 'personal'…"""
        for lid in self.label_ids:
            if lid.startswith("CATEGORY_"):
                return lid[len("CATEGORY_"):].lower()
        return None

    @property
    def own_label_ids(self) -> list[str]:
        """Labels the person made, as opposed to Google's: user labels are `Label_<n>`."""
        return [lid for lid in self.label_ids if lid.startswith("Label_")]


class Decision(BaseModel):
    message_id: str
    action: Action
    label: str | None = None
    source: Source = Source.default
    rule_id: str | None = None
    rationale: str | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    model: str | None = None
    keep_in_inbox: bool = False
    at: datetime = Field(default_factory=now)

    def acts_unattended(self, threshold: float) -> bool:
        if self.source in (Source.rule, Source.person):
            return True
        if self.confidence is None:
            return False
        return self.confidence >= threshold


class PlanItem(BaseModel):
    message_id: str
    action: Action
    label: str | None = None
    keep_in_inbox: bool = False       # label it, but leave it in the inbox (the Promotions tab)
    source: Source = Source.default
    rule_id: str | None = None
    confidence: float | None = None


class Plan(BaseModel):
    id: str
    account: str
    query: str
    created_at: datetime = Field(default_factory=now)
    status: Literal["draft", "approved", "partly_applied", "applied", "undone"] = "draft"
    token: str | None = None
    items: list[PlanItem] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.action.value] = out.get(item.action.value, 0) + 1
        return out


class RunBatch(BaseModel):
    index: int
    op: str                    # what was done: 'archive' | 'trash' | 'label' | 'mark_read'
    ids: list[str]
    label: str | None = None
    inverse: dict[str, object] | None = None   # what an undo needs
    at: datetime = Field(default_factory=now)


class Run(BaseModel):
    id: str
    plan_id: str
    account: str
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    status: Literal["running", "ok", "failed", "undone", "partial"] = "running"
    batches: list[RunBatch] = Field(default_factory=list)
    applied: int = 0
    error: str | None = None
