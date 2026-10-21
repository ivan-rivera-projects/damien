"""Plans: the only thing that ever touches Gmail.

A plan is built, looked at, approved, applied, and — if it was wrong — undone.
Every batch that reaches Gmail is written to the journal first, so a run that
dies halfway is resumable and reversible.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .decide import DEFAULT_SPEC, Judge, JudgementSpec, apply_corrections, decide_all
from .gmail import Gmail
from .models import Action, Message, Plan, PlanItem, Run, RunBatch
from .rules import Rule
from .stakes import DEFAULT_PROTECTED
from .store import Store


def build_plan(
    *,
    store: Store,
    gmail: Gmail | None,
    rules: Sequence[Rule],
    judge: Judge | None,
    query: str,
    messages: Sequence[Message] | None = None,
    spec: JudgementSpec = DEFAULT_SPEC,
    judge_batch: int = 8,
    ask_below: float = 0.55,
    protected_labels: Sequence[str] = (),
    limit: int | None = None,
    model: str | None = None,
) -> Plan:
    """List, fetch, decide, store. No writes to Gmail.

    `messages` may be supplied instead of `gmail` (tests, or a re-plan from the
    store) — that is what keeps this offline-testable.
    """
    if messages is None:
        if gmail is None:
            raise ValueError("build_plan needs either gmail or messages")
        ids = gmail.list_ids(query, limit=limit)
        messages = gmail.fetch(ids) if ids else []

    if messages:
        store.upsert_messages(messages)

    label_names = gmail.labels() if gmail is not None else None
    decisions = decide_all(messages, rules=rules, judge=judge, spec=spec, batch_size=judge_batch,
                           ask_below=ask_below, label_names=label_names, model=model,
                           protected_labels=protected_labels or DEFAULT_PROTECTED)
    decisions = apply_corrections(decisions, {r["message_id"]: dict(r) for r in store.corrections(limit=10_000)})
    store.add_decisions(decisions, account=(gmail.settings.account if gmail is not None else ""))

    items = [
        PlanItem(message_id=d.message_id, action=d.action, label=d.label, source=d.source,
                 rule_id=d.rule_id, confidence=d.confidence)
        for d in decisions
    ]
    return store.create_plan(
        account=gmail.settings.account if gmail is not None else "",
        query=query, items=items, model=model, judge=(judge.name if judge else None),
    )


@dataclass
class Sample:
    action: str
    label: str | None
    source: str
    confidence: float | None
    subject: str
    from_addr: str
    age_days: float | None
    message_id: str


def sample_plan(plan: Plan, store: Store, n: int = 20, *, seed: int = 7) -> list[Sample]:
    """A stratified sample, so a small read shows the shape of the whole plan."""
    messages = {m.id: m for m in store.get_messages([i.message_id for i in plan.items])}
    by_action: dict[tuple[str, str | None], list[PlanItem]] = {}
    for item in plan.items:
        by_action.setdefault((item.action.value, item.label), []).append(item)

    rng = random.Random(seed)
    out: list[Sample] = []
    groups = sorted(by_action.items())
    per_group = max(1, n // max(1, len(groups)))
    for (action, label), items in groups:
        picked = items if len(items) <= per_group else rng.sample(items, per_group)
        for item in picked:
            m = messages.get(item.message_id)
            out.append(Sample(
                action=action, label=label, source=item.source.value, confidence=item.confidence,
                subject=(m.subject if m else "") or "", from_addr=(m.from_addr if m else "") or "",
                age_days=(m.age_days() if m else None), message_id=item.message_id,
            ))
    return out


def approve(store: Store, plan_id: str) -> str:
    plan = store.get_plan(plan_id)
    if plan is None:
        raise ValueError(f"no such plan: {plan_id}")
    if plan.status == "applied":
        raise ValueError("that plan was already applied — build a new one from the current mailbox")
    return store.approve_plan(plan_id)


def apply_plan(*, store: Store, gmail: Gmail, plan_id: str, token: str, resume: bool = True) -> Run:
    """Execute an approved plan in bulk batches, journaling each one.

    Idempotent by batch index: re-running a half-finished plan with `resume`
    performs only the batches the journal does not already hold.
    """
    plan = store.get_plan(plan_id)
    if plan is None:
        raise ValueError(f"no such plan: {plan_id}")
    if plan.status == "applied":
        raise ValueError(f"plan {plan_id} is already applied — undo its run first (`damien run list`)")
    if plan.status != "approved":
        raise ValueError(f"plan {plan_id} is {plan.status}: approve it first (`damien plan approve {plan_id}`)")
    if not plan.token or plan.token != token:
        raise ValueError("wrong approval token for that plan")

    existing = store.last_run_for_plan(plan.id)
    run = existing if (existing and resume and existing.status in ("running", "partial")) else store.start_run(plan)
    done = {b.index for b in run.batches}

    label_cache = gmail.labels()
    groups = _group_items(plan.items)

    index = 0
    try:
        for (action, label), ids in groups:
            for chunk in _chunks(ids, gmail.settings.op_batch):
                if index in done:
                    index += 1
                    continue
                label_id = None
                if action == Action.label:
                    label_id = label_cache.get(label or "") or gmail.label_id(label or "")
                    if label_id is None and label:
                        label_id = gmail.create_label(label)
                        label_cache[label] = label_id
                op, inverse = _perform(gmail, action, chunk, label_id)
                store.journal_batch(run.id, RunBatch(index=index, op=op, ids=chunk, label=label, inverse=inverse))
                index += 1
    except Exception as exc:                      # journal already holds what landed
        store.finish_run(run.id, "partial", error=str(exc)[:500])
        raise
    store.finish_run(run.id, "ok")
    store.set_plan_status(plan.id, "applied")
    return store.get_run(run.id)  # type: ignore[return-value]


def undo_run(*, store: Store, gmail: Gmail, run_id: str) -> Run:
    """Replay the journal backwards. Anything already applied goes back."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"no such run: {run_id}")
    for batch in sorted(run.batches, key=lambda b: b.index, reverse=True):
        inv = batch.inverse or {}
        add = list(inv.get("add", []))
        remove = list(inv.get("remove", []))
        if add or remove:
            gmail.modify(batch.ids, add=add, remove=remove)
    store.finish_run(run.id, "undone")
    store.set_plan_status(run.plan_id, "undone")
    return store.get_run(run.id)  # type: ignore[return-value]


def _group_items(items: Sequence[PlanItem]) -> list[tuple[tuple[Action, str | None], list[str]]]:
    groups: dict[tuple[Action, str | None], list[str]] = {}
    for item in items:
        if item.action in (Action.keep,):
            continue
        groups.setdefault((item.action, item.label), []).append(item.message_id)
    return [(key, sorted(ids)) for key, ids in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1] or ""))]


def _perform(gmail: Gmail, action: Action, ids: list[str], label_id: str | None) -> tuple[str, dict[str, object]]:
    if action == Action.archive:
        gmail.archive(ids)
        return "archive", {"add": ["INBOX"], "remove": []}
    if action == Action.trash:
        gmail.trash(ids)
        return "trash", {"add": ["INBOX", "UNREAD"], "remove": ["TRASH"]}
    if action == Action.mark_read:
        gmail.mark_read(ids)
        return "mark_read", {"add": ["UNREAD"], "remove": []}
    if action == Action.label and label_id:
        gmail.add_label(ids, label_id)
        return "label", {"add": ["INBOX"], "remove": [label_id]}
    raise ValueError(f"nothing to do for action {action.value}")


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
