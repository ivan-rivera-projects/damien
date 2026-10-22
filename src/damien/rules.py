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
from collections.abc import Iterable, Sequence
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
}
# Not here on purpose: `snippet` and `any`. A bare Gmail term searches the whole body,
# attachments' names and more, so it matches mail the Python rule would not (L4).


def label_token(name: str) -> str:
    """The Gmail search token for a label: system labels by their operator, user labels
    the way Gmail spells them in a query (lower case, spaces and `/` as `-`). An
    unquoted `label:Action Required` would search for `label:action` AND the word `required`."""
    system = SYSTEM_LABELS.get(name.upper())
    if system:
        return system
    return "label:" + re.sub(r"[\s/]+", "-", name.strip()).lower()


def _group_query(conds: Sequence[Condition]) -> str | None:
    parts: list[str] = []
    for cond in conds:
        if cond.field == "age_days" and cond.op == "gt" and isinstance(cond.value, (int, float)):
            parts.append(f"older_than:{int(cond.value)}d")
            continue
        if cond.field == "label" and cond.op in ("equals", "contains"):
            name = str(cond.value or "")
            if not name:
                return None
            parts.append(label_token(name))
            continue
        key = _SAFE.get((cond.field, cond.op))
        if key is None or not cond.value:
            return None
        token = str(cond.value)
        if re.search(r'[\s"(){}]', token):
            token = '"' + token.replace('"', "") + '"'
        parts.append(f"{key}:{token}")
    return " ".join(parts) or None


def to_query(rule: Rule) -> str | None:
    """The Gmail search string for this rule, or None when it is not certain.

    None means: run the rule in Python over listed messages instead. That is the
    safe direction — a wrong query acts on the wrong mail.
    """
    parts: list[str] = []
    if rule.match:
        base = _group_query(rule.match)
        if base is None:
            return None
        parts.append(base)
    if rule.any_of:
        groups = [_group_query(g) for g in rule.any_of]
        if not groups or any(g is None for g in groups):
            return None
        parts.append("{" + " ".join(f"({g})" for g in groups) + "}")
    return " ".join(parts) or None


_FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com",
             "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com"}
_FREEMAIL_FAMILIES = ("yahoo.", "gmx.", "yandex.")


def sender_key(addr: str | None) -> str | None:
    """What a sender rule should match: the domain — except on freemail, where the domain
    is millions of unrelated people and only the full address names one sender (L3)."""
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return None
    domain = addr.rsplit("@", 1)[1]
    if not domain:
        return None
    if domain in _FREEMAIL or domain.startswith(_FREEMAIL_FAMILIES):
        return addr
    return domain


def action_query(action: Action, label: str | None) -> str | None:
    """Some actions are expressible as a query alone; return the label token for the action."""
    return label if action == Action.label else None


def mint_rules(messages: Iterable[Message], action: Action, label: str | None, *,
               max_domains: int = 5, category_share: float = 0.8, min_for_category: int = 5,
               ) -> tuple[list[Rule], str]:
    """Turn an approved group into rules that catch the same mail next time.

    A rule cannot say "messages that are semantically alike", so this looks for the
    strongest *deterministic* description of the group and says plainly when there
    isn't one:

      * mostly one Gmail category, and enough messages to mean it -> a category rule,
        which Gmail evaluates itself and which keeps working for senders you have
        never seen. Never for `keep`: "keep everything Gmail calls Updates" would
        silently switch triage off for a whole category, and a one-message group
        would mint it from nothing.
      * otherwise, a handful of senders -> one rule per sender domain
      * neither -> no rule, and an explanation, because a wrong rule acts on mail
        the person never approved
    """
    messages = list(messages)
    if not messages:
        return [], "the group is empty"

    categories: dict[str, int] = {}
    for m in messages:
        if m.category:
            categories[m.category] = categories.get(m.category, 0) + 1
    if categories and len(messages) >= min_for_category and action is not Action.keep:
        top, count = max(categories.items(), key=lambda kv: kv[1])
        if count / len(messages) >= category_share:
            rule_id = f"auto-{top}-{action.value}"
            return ([Rule(
                id=rule_id,
                name=f"Gmail {top} -> {action.value}" + (f" '{label}'" if label else ""),
                match=[Condition(field="label", op="contains", value=f"CATEGORY_{top.upper()}")],
                action=action, label=label, source="jev",
                note="minted from a reviewed plan group",
            )], f"{count}/{len(messages)} carry Gmail's {top!r} category, so the rule is Google's own classification")

    domains: list[str] = []
    for m in messages:
        domain = (m.from_addr or "").split("@")[-1]
        if domain and domain not in domains:
            domains.append(domain)
    if domains and len(domains) <= max_domains:
        rules = [Rule(
            id=f"auto-from-{domain.replace('.', '-')}",
            name=f"from {domain} -> {action.value}" + (f" '{label}'" if label else ""),
            match=[Condition(field="from", op="contains", value=domain)],
            action=action, label=label, source="jev",
            note="minted from a reviewed plan group",
        ) for domain in domains]
        return rules, f"the group comes from {len(domains)} sender(s): {', '.join(domains)}"

    return [], (f"this group spans {len(domains)} different senders "
                f"({', '.join(domains[:6])}...), and a rule cannot express 'messages like these' "
                f"— apply the group, or name the senders you want a rule for")


def shadowed(rules: Iterable[Rule]) -> list[tuple[Rule, Rule]]:
    """Pairs where a broader sender rule runs first and disagrees with a narrower one.

    `from amazon.com -> keep` placed before `from business.amazon.com -> trash` makes
    the second rule dead code, silently, forever. Minting rules by sender produced
    exactly this shape for Alibaba: one domain rule (wrong on its own terms) sitting
    where a per-address distinction was needed. A rule file is read as an ordered list,
    so the check has to understand order, not just membership.
    """
    ordered = list(rules)
    sender = [(i, r) for i, r in enumerate(ordered)
              if r.enabled and r.match and r.match[0].field == "from" and r.match[0].op == "contains"]
    out: list[tuple[Rule, Rule]] = []
    for pos, (_i, broad) in enumerate(sender):
        for _j, narrow in sender[pos + 1:]:
            b, n = str(broad.match[0].value).lower(), str(narrow.match[0].value).lower()
            if b != n and n.endswith(b) and broad.action is not narrow.action:
                out.append((broad, narrow))
    return out


def mint_rules_by_sender(counts: Iterable[tuple[str, Action, str | None, int]], *,
                         min_count: int = 5, agreement: float = 0.9,
                         max_rules: int = 80) -> tuple[list[Rule], str]:
    """A rule for every sender a plan decided the same way, over and over.

    This is what keeps a large mailbox cheap: a sender that earned a decision should
    not be re-judged next time. `counts` is (domain, action, label, n) per decision,
    as a plan records it.

    Only senders that agree with themselves qualify — a sender split 87/32 across two
    actions is exactly the one a rule must *not* flatten.
    """
    per: dict[str, list[tuple[Action, str | None, int]]] = {}
    for domain, action, label, n in counts:
        if domain:
            per.setdefault(domain, []).append((action, label, n))

    rules: list[Rule] = []
    skipped: list[str] = []
    for domain, entries in sorted(per.items(), key=lambda kv: -sum(e[2] for e in kv[1])):
        total = sum(e[2] for e in entries)
        top_action, top_label, top_n = max(entries, key=lambda e: e[2])
        if total < min_count or top_n / total < agreement:
            if total >= min_count:
                skipped.append(domain)
            continue
        if len(rules) >= max_rules:
            break
        rules.append(Rule(
            id=f"auto-from-{domain.replace('.', '-')}",
            name=f"from {domain} -> {top_action.value}" + (f" '{top_label}'" if top_label else ""),
            match=[Condition(field="from", op="contains", value=domain)],
            action=top_action, label=top_label, source="jev",
            note=f"minted from a plan: {top_n}/{total} of this sender's mail was decided this way",
        ))
    why = (f"{len(rules)} sender(s) agreed with themselves on {min_count}+ messages "
           f"(at least {agreement:.0%} one way)")
    if skipped:
        why += f"; {len(skipped)} split between actions, left alone: {', '.join(skipped[:5])}"
    return rules, why


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

    # source="seed": shipped, not written by the person — so the stake floor applies
    # to them. A rule the person writes is source="person" and is never second-guessed.
    return [
        # There is deliberately no "Gmail calls it personal -> keep" rule here. There was,
        # and it was wrong: Gmail's own account of the Personal category is that engagement
        # is its strongest signal, so a sender you have ever transacted with has its
        # *marketing* filed as Personal too. Ivan's Alibaba suppliers are exactly that -
        # 45 marketing blasts sat unjudged behind it, and 826 of one plan's 7,543 messages
        # never reached the model or any other rule. A shortcut that runs before judgment
        # has to be certain, and a tab guess is not.
        Rule(id="r-receipts", name="File receipts, invoices and order confirmations",
             any_of=[[c("subject", "words", "receipt")],
                     [c("subject", "words", "invoice")],
                     [c("subject", "words", "order confirmation")],
                     [c("subject", "words", "payment received")]],
             action=Action.label, label="Receipts", source="seed"),
        Rule(id="r-old-promotions", name="Trash promotions older than a year",
             match=[c("label", "contains", "CATEGORY_PROMOTIONS"), c("age_days", "gt", 365)],
             action=Action.trash, source="seed"),
        Rule(id="r-old-social", name="Archive social notifications older than 6 months",
             match=[c("label", "contains", "CATEGORY_SOCIAL"), c("age_days", "gt", 182)],
             action=Action.archive, source="seed"),
    ]
