"""Rules: the deterministic half of the judgment.

A rule is a small, readable predicate plus an action. Rules are the cheapest and
most certain thing Damien can do, so they run first and they run for the whole
account — `to_query()` lets a rule execute server-side in Gmail with no listing
and no model call at all.

Three safety properties, on purpose:
  * `to_query()` returns None whenever any condition is not certain to map onto
    Gmail search syntax. A wrong query trashes the wrong mail; a None is safe.
  * Matching is plain Python over fields we stored; no regex engine surprises.
  * A rule never deletes permanently. `trash` is recoverable for 30 days.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .models import Action, Message

Field_ = Literal["from", "to", "subject", "snippet", "any", "label", "age_days", "attachment", "list_unsubscribe",
                 "list_id"]
Op = Literal["contains", "not_contains", "equals", "not_equals", "starts_with", "ends_with", "regex", "words",
             "gt", "lt", "exists", "not_exists"]

SYSTEM_LABELS = {
    "INBOX": "in:inbox", "UNREAD": "is:unread", "STARRED": "is:starred", "IMPORTANT": "is:important",
    "SPAM": "in:spam", "TRASH": "in:trash", "SENT": "in:sent", "DRAFT": "in:drafts",
}


class Condition(BaseModel):
    field: Field_
    op: Op
    value: str | float | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _numeric(cls, v: object) -> object:
        return v


class Rule(BaseModel):
    id: str
    name: str
    enabled: bool = True
    match: list[Condition] = Field(default_factory=list)          # AND
    any_of: list[list[Condition]] = Field(default_factory=list)   # OR of AND-groups (optional)
    action: Action
    label: str | None = None
    keep_in_inbox: bool = False
    source: Literal["person", "jev", "seed"] = "person"
    note: str | None = None

    model_config = {"extra": "forbid"}


def _text(message: Message, field: str, label_names: dict[str, str] | None) -> str:
    if field == "from":
        return f"{message.from_name or ''} {message.from_addr or ''}".strip().lower()
    if field == "to":
        return (message.to or "").lower()
    if field == "subject":
        return (message.subject or "").lower()
    if field == "snippet":
        return (message.snippet or "").lower()
    if field == "any":
        return f"{message.subject or ''} {message.snippet or ''} {message.from_addr or ''} {message.from_name or ''}".lower()
    if field == "label":
        names = [f"{lid} {(label_names or {}).get(lid, '')}".strip() for lid in message.label_ids]
        return " ".join(names).lower()
    if field == "list_id":
        return (message.list_id or "").lower()
    return ""


def _words(hay: str, needle: str) -> bool:
    """Whole-word match: 'order' must not match 'openrouter'."""
    if not needle:
        return False
    return re.search(rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])", hay) is not None


def _match_condition(cond: Condition, message: Message, label_names: dict[str, str] | None) -> bool:
    op = cond.op
    if cond.field == "attachment":
        return message.has_attachment if op == "exists" else (not message.has_attachment if op == "not_exists" else False)
    if cond.field == "list_unsubscribe":
        return message.list_unsubscribe if op == "exists" else (not message.list_unsubscribe if op == "not_exists" else False)
    if cond.field == "list_id":
        has = bool(message.list_id)
        if op == "exists":
            return has
        if op == "not_exists":
            return not has
        return _match_text(op, (message.list_id or "").lower(), str(cond.value or "").lower())
    if cond.field == "age_days":
        age = message.age_days()
        if age is None or not isinstance(cond.value, (int, float)):
            return False
        return age > float(cond.value) if op == "gt" else (age < float(cond.value) if op == "lt" else False)

    hay = _text(message, cond.field, label_names)
    return _match_text(op, hay, str(cond.value or "").lower())


def _match_text(op: str, hay: str, needle: str) -> bool:
    if op == "exists":
        return bool(hay.strip())
    if op == "not_exists":
        return not hay.strip()
    if op == "contains":
        return needle in hay
    if op == "words":
        return _words(hay, needle)
    if op == "not_contains":
        return needle not in hay
    if op == "equals":
        return hay.strip() == needle
    if op == "not_equals":
        return hay.strip() != needle
    if op == "starts_with":
        return hay.strip().startswith(needle)
    if op == "ends_with":
        return hay.strip().endswith(needle)
    if op == "regex":
        try:
            return re.search(needle, hay, re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def matches(rule: Rule, message: Message, label_names: dict[str, str] | None = None) -> bool:
    if not rule.enabled:
        return False
    if not all(_match_condition(c, message, label_names) for c in rule.match):
        return False
    if rule.any_of:
        return any(all(_match_condition(c, message, label_names) for c in group) for group in rule.any_of)
    return True


def first_match(rules: Iterable[Rule], message: Message, label_names: dict[str, str] | None = None) -> Rule | None:
    for rule in rules:
        if matches(rule, message, label_names):
            return rule
    return None


_SAFE: dict[tuple[str, str], str] = {
    ("from", "contains"): "from",
    ("from", "words"): "from",
    ("to", "contains"): "to",
    ("subject", "contains"): "subject",
    ("subject", "words"): "subject",
    ("snippet", "contains"): "",
    ("any", "contains"): "",
    ("any", "words"): "",
}


def to_query(rule: Rule) -> str | None:
    """The Gmail search string for this rule, or None when it is not certain.

    None means: run the rule in Python over listed messages instead. That is the
    safe direction — a wrong query acts on the wrong mail.
    """
    if rule.any_of:
        return None
    parts: list[str] = []
    for cond in rule.match:
        if cond.field == "age_days" and cond.op == "gt" and isinstance(cond.value, (int, float)):
            parts.append(f"older_than:{int(cond.value)}d")
            continue
        if cond.field == "label" and cond.op in ("equals", "contains"):
            name = str(cond.value or "")
            if not name:
                return None
            parts.append(SYSTEM_LABELS.get(name.upper(), f"label:{name}"))
            continue
        key = _SAFE.get((cond.field, cond.op))
        if key is None or not cond.value:
            return None
        token = str(cond.value)
        if re.search(r'[\s"()]', token):
            token = f'"{token}"'
        parts.append(f"{key}:{token}" if key else token)
    if not parts:
        return None
    return " ".join(parts)


def action_query(action: Action, label: str | None) -> str | None:
    """Some actions are expressible as a query alone; return the label token for the action."""
    return label if action == Action.label else None


# ---- files ------------------------------------------------------------
def load_rules(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text() or "[]")
    if isinstance(raw, dict):
        raw = raw.get("rules", [])
    return [Rule.model_validate(r) for r in raw]


def save_rules(path: Path, rules: list[Rule]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.model_dump(mode="json") for r in rules], indent=2) + "\n")


def default_rules() -> list[Rule]:
    """A starting set — deliberately small, and only where a rule is *certain*.

    The first version of this shipped `has an unsubscribe link -> Newsletters`,
    which swallowed 149 of 200 messages in a real inbox: unsubscribe links are on
    marketing blasts too. Rules are for the unambiguous; everything else is a
    judgment, and that is what the model is for. Nothing here deletes.
    """
    def c(field: str, op: str, value: object = None) -> Condition:
        return Condition(field=field, op=op, value=value)  # type: ignore[arg-type]

    return [
        # Gmail's own tab classification, trusted: it already decided this is a real
        # person's mail, so no model call is spent asking again.
        Rule(id="r-gmail-personal", name="Keep what Gmail calls personal",
             match=[c("label", "contains", "CATEGORY_PERSONAL")], action=Action.keep),
        Rule(id="r-receipts", name="File receipts, invoices and order confirmations",
             any_of=[[c("subject", "words", "receipt")],
                     [c("subject", "words", "invoice")],
                     [c("subject", "words", "order confirmation")],
                     [c("subject", "words", "payment received")]],
             action=Action.label, label="Receipts"),
        Rule(id="r-old-promotions", name="Trash promotions older than a year",
             match=[c("label", "contains", "CATEGORY_PROMOTIONS"), c("age_days", "gt", 365)],
             action=Action.trash),
        Rule(id="r-old-social", name="Archive social notifications older than 6 months",
             match=[c("label", "contains", "CATEGORY_SOCIAL"), c("age_days", "gt", 182)],
             action=Action.archive),
    ]
