"""The judgment layer: rules first, then a System One model, then a person.

The shape is deliberate and it is the whole reason this is cheap at scale:

    rules        — deterministic, free, run over every message, and usually
                   expressible as a Gmail query (no listing, no model call)
    Jev          — one batched call per N messages with a handful of *atomic*
                   questions; the model returns a choice plus a confidence
    escalation   — anything the model is unsure about is asked again with more
                   evidence, or handed to a person as `keep` + a note

Nothing here writes to Gmail. Decisions are data; `plan.py` acts on them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from .models import Action, Decision, Message, Source
from .rules import Rule, first_match
from .stakes import DEFAULT_PROTECTED, protected_label, stake_floor


class Verdict(BaseModel):
    """What a judge returns for one message. Transport-independent on purpose."""

    message_id: str
    choice: str                       # the bucket key the judge picked
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    signals: dict[str, float | None] | None = None   # the Noul/Score answers, kept raw
    rationale: str | None = None


class Judge(Protocol):
    """A judge answers one Choice question per message: which bucket is this?"""

    name: str

    def judge(self, messages: Sequence[Message], buckets: dict[str, str], *, context: str = "",
              label_names: dict[str, str] | None = None) -> list[Verdict]:
        ...


@dataclass(frozen=True)
class Bucket:
    """One answer option, and what Damien does when it is chosen."""

    key: str
    description: str
    action: Action
    label: str | None = None
    keep_in_inbox: bool = False


@dataclass(frozen=True)
class JudgementSpec:
    """The buckets a judge may choose between. Small, closed, and actionable."""

    buckets: tuple[Bucket, ...]
    context: str = ""      # a line describing whose inbox this is, for the state

    def criteria(self) -> dict[str, str]:
        return {b.key: b.description for b in self.buckets}

    def get(self, key: str) -> Bucket | None:
        for b in self.buckets:
            if b.key == key:
                return b
        return None


# The tool's vocabulary: seven buckets, one action each. These descriptions are the
# *default*, neutral ones. Nothing here may name a person, a trade or a taste — a person's
# own framing lives in ~/.damien/profile.json, which `profile.py` overlays on these.
BUCKETS = (
    Bucket("keep", "Belongs in the inbox: written by a person, needs attention, or is one of "
                   "the subjects they follow", Action.keep),
    Bucket("receipt", "An automated receipt, order or confirmation for something already done",
           Action.label, "Receipts"),
    Bucket("newsletter", "A newsletter or digest they subscribed to that is not one of the "
                         "subjects they follow — those belong in the inbox", Action.label,
           "Newsletters"),
    Bucket("promotion", "A discount, sale, pitch or upsell — including marketing from a company "
                        "they already use, which is still marketing, and never news about that "
                        "company's field", Action.trash),
    Bucket("notification", "Automated notification (social, app, security, shipping) with nothing to do",
           Action.archive),
    Bucket("archive", "Once-useful, now done: keep the message, get it out of the way",
           Action.archive),
    Bucket("unclear", "Cannot tell from the headers and snippet whether this matters", Action.keep),
)

DEFAULT_CONTEXT = (
    "The owner's personal Gmail inbox. Mail that belongs in the inbox: anything written by a "
    "person, anything that needs a decision or a payment, and the subjects they follow. "
    "A discount blast from a retailer they do not deal with does not belong in the inbox; "
    "mail from a company they do deal with does, even when it is marketing."
)

DEFAULT_SPEC = JudgementSpec(context=DEFAULT_CONTEXT, buckets=BUCKETS)


class PersonJudge:
    """The fallback judge: never guesses. Everything becomes `keep` with a note."""

    name = "person"

    def judge(self, messages: Sequence[Message], buckets: dict[str, str], *, context: str = "",
              label_names: dict[str, str] | None = None) -> list[Verdict]:
        return [Verdict(message_id=m.id, choice="unclear", confidence=0.0,
                        rationale="no judge configured — left in place for a person to decide") for m in messages]


def decide_message(
    message: Message,
    *,
    rules: Sequence[Rule],
    verdict: Verdict | None,
    spec: JudgementSpec,
    label_names: dict[str, str] | None = None,
    model: str | None = None,
    protected_labels: Sequence[str] = DEFAULT_PROTECTED,
) -> Decision:
    rule = first_match(rules, message, label_names)
    if rule is not None:
        decision = Decision(
            message_id=message.id, action=rule.action, label=rule.label, source=Source.rule,
            rule_id=rule.id, rationale=f"rule: {rule.name}", confidence=1.0,
            keep_in_inbox=rule.keep_in_inbox,
        )
        # A rule the *person* wrote is their instruction and stands. A rule this tool
        # shipped, or minted from a group, is still somebody else's guess — and one of
        # those was filing "Action required: Invoice overdue" as a neutral Receipt.
        if rule.source != "person" and decision.action is not Action.keep:
            held = stake_floor(message, action=decision.action,
                               protected=protected_label(message, label_names, protected_labels))
            if held:
                return decision.model_copy(update={
                    "action": Action.keep, "label": None,
                    "rationale": f"held for a person — {held} (a rule said {rule.action.value}: {rule.id})",
                })
        return decision
    if verdict is None:
        return Decision(message_id=message.id, action=Action.keep, source=Source.default,
                        rationale="no rule matched and no judgment was made")
    bucket = spec.get(verdict.choice)
    if bucket is None:
        return Decision(message_id=message.id, action=Action.keep, source=Source.default,
                        rationale=f"judge returned an unknown bucket ({verdict.choice!r})")
    decision = Decision(
        message_id=message.id, action=bucket.action, label=bucket.label, source=Source.jev,
        rationale=verdict.rationale or bucket.description, confidence=verdict.confidence,
        probabilities=verdict.probabilities, model=model, keep_in_inbox=bucket.keep_in_inbox,
    )
    # The guardrail: the model may not quietly file away money, deadlines or a person.
    if decision.action is not Action.keep:
        signals = verdict.signals or {}
        held = stake_floor(message, machine_sent=signals.get("handled_by_a_machine"),
                           importance=signals.get("importance"), action=decision.action,
                           protected=protected_label(message, label_names, protected_labels))
        if held:
            return decision.model_copy(update={
                "action": Action.keep, "label": None,
                "rationale": f"held for a person — {held} — {decision.rationale or ''}".strip(),
            })
    return decision


def decide_all(
    messages: Sequence[Message],
    *,
    rules: Sequence[Rule],
    judge: Judge | None,
    spec: JudgementSpec = DEFAULT_SPEC,
    batch_size: int = 8,
    ask_below: float = 0.55,
    label_names: dict[str, str] | None = None,
    model: str | None = None,
    protected_labels: Sequence[str] = DEFAULT_PROTECTED,
) -> list[Decision]:
    """Rules for everything they cover; the judge only sees the rest, in batches."""
    decisions: list[Decision] = []
    residue: list[Message] = []

    for message in messages:
        rule = first_match(rules, message, label_names)
        if rule is not None:
            decisions.append(decide_message(message, rules=(rule,), verdict=None, spec=spec, label_names=label_names,
                                            model=model, protected_labels=protected_labels))
        else:
            residue.append(message)

    if judge is None or not residue:
        for message in residue:
            decisions.append(decide_message(message, rules=(), verdict=None, spec=spec, model=model,
                                            protected_labels=protected_labels))
        return decisions

    criteria = spec.criteria()
    for chunk in _chunks(residue, max(1, batch_size)):
        verdicts = judge.judge(chunk, criteria, context=spec.context, label_names=label_names)
        # A judge answers one message per message, in order. Re-keying by the verdict's
        # own message_id looked safer and was not: it silently dropped 2,180 decisions
        # to "no judgment was made" whenever two messages shared a cache key.
        by_position = dict(zip((m.id for m in chunk), verdicts))
        for message in chunk:
            decisions.append(decide_message(message, rules=(), verdict=by_position.get(message.id), spec=spec,
                                            label_names=label_names, model=model, protected_labels=protected_labels))
    return enforce_confidence(decisions, ask_below=ask_below)


def enforce_confidence(decisions: Sequence[Decision], *, ask_below: float) -> list[Decision]:
    """An unsure judgment never acts.

    The model's `confidence` describes its answer, not its correctness — so a low
    one degrades to `keep` with the reason attached, and a person decides. Rules
    and people are exempt: they were never guesses.
    """
    out: list[Decision] = []
    for d in decisions:
        if d.source is Source.jev and (d.confidence is None or d.confidence < ask_below):
            shown = "no confidence reported" if d.confidence is None else f"confidence {d.confidence:.2f}"
            out.append(d.model_copy(update={
                "action": Action.keep, "label": None,
                "rationale": f"left for a person ({shown} below {ask_below:.2f}) — {d.rationale or ''}".strip(),
            }))
        else:
            out.append(d)
    return out


def apply_corrections(decisions: Sequence[Decision], corrections: dict[str, dict]) -> list[Decision]:
    """What a person decided outranks a rule and outranks the model, on every later plan.

    This is what makes `damien correct` a memory rather than a note: the record is
    append-only, so the model's original judgment stays in history beside it.
    """
    if not corrections:
        return list(decisions)
    out: list[Decision] = []
    for d in decisions:
        fixed = corrections.get(d.message_id)
        if fixed is None:
            out.append(d)
            continue
        note = f" — {fixed['note']}" if fixed.get("note") else ""
        out.append(d.model_copy(update={
            "action": Action(fixed["action"]), "label": fixed.get("label"), "source": Source.person,
            "rule_id": None, "confidence": 1.0,
            "rationale": f"a person decided this ({fixed['at']}){note}",
        }))
    return out


def needs_a_person(decision: Decision, *, act_at: float, ask_below: float) -> bool:
    """Between the two thresholds: act, but say so prominently. Below: do nothing."""

    if decision.source in (Source.rule, Source.person):
        return False
    if decision.confidence is None:
        return True
    return decision.confidence < ask_below


def _chunks(items: Sequence[Message], size: int) -> Iterable[list[Message]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])
