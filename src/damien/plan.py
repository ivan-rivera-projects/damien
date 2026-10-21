"""Plans: the only thing that ever touches Gmail.

A plan is built, looked at, approved, applied, and — if it was wrong — undone.
Every batch that reaches Gmail is written to the journal first, so a run that
dies halfway is resumable and reversible.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .decide import DEFAULT_SPEC, Judge, JudgementSpec, apply_corrections, decide_all
from .embed import DEFAULT_SIMILARITY, cluster
from .gmail import Gmail
from .models import Action, Message, Plan, PlanItem, Run, RunBatch, Source
from .rules import Rule, to_query
from .stakes import DEFAULT_PROTECTED
from .store import Store

DEFAULT_HEADER_TTL_HOURS = 12.0


def load_messages(*, store: Store, gmail: Gmail, ids: Sequence[str],
                  ttl_hours: float = DEFAULT_HEADER_TTL_HOURS) -> tuple[list[Message], int]:
    """Headers for these ids: from the store when they are fresh, from Gmail otherwise.

    Headers do not change; labels do. The TTL is the honesty dial — inside it we
    reuse what we already downloaded (a re-plan costs nothing), and past it we pay
    for a fresh copy. `ttl_hours=0` always refetches.
    """
    ids = list(ids)
    if not ids:
        return [], 0
    cutoff = None if ttl_hours <= 0 else (datetime.now(UTC) - timedelta(hours=ttl_hours)).isoformat()
    fresh = store.seen_recently(ids, cutoff) if cutoff else set()
    missing = [i for i in ids if i not in fresh]
    fetched = gmail.fetch(missing) if missing else []
    if fetched:
        store.upsert_messages(fetched)
    cached = store.get_messages([i for i in ids if i in fresh]) if fresh else []
    by_id = {m.id: m for m in [*cached, *fetched]}
    return [by_id[i] for i in ids if i in by_id], len(fetched)


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
    ttl_hours: float = DEFAULT_HEADER_TTL_HOURS,
    stats: dict[str, int] | None = None,
    limit: int | None = None,
    model: str | None = None,
) -> Plan:
    """List, fetch, decide, store. No writes to Gmail.

    `messages` may be supplied instead of `gmail` (tests, or a re-plan from the
    store) — that is what keeps this offline-testable.
    """
    fetched = 0
    if messages is None:
        if gmail is None:
            raise ValueError("build_plan needs either gmail or messages")
        ids = gmail.list_ids(query, limit=limit)
        messages, fetched = load_messages(store=store, gmail=gmail, ids=ids, ttl_hours=ttl_hours)
    else:
        store.upsert_messages(messages)
    if stats is not None:
        stats["messages"] = len(messages)
        stats["fetched"] = fetched
        stats["cached"] = len(messages) - fetched

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
class Group:
    """One review unit: messages that deserve the same answer, found by similarity."""

    action: str
    label: str | None
    message_ids: list[str]
    domains: str
    sample_subject: str


def digest_plan(plan: Plan, store: Store, *, similarity: float = DEFAULT_SIMILARITY,
                embedder=None) -> list[Group]:
    """Collapse a plan into groups a person can actually review.

    Senders do not do this job — 200 messages came from 140 senders, 102 of them
    with one message. Similarity does: forty shops sending a promotion collapse to
    one decision. Grouping is within an action, so a group is always one answer.
    """
    ids = [i.message_id for i in plan.items]
    messages = {m.id: m for m in store.get_messages(ids)}
    vectors = store.get_embeddings(ids)
    if embedder is not None:
        missing = [m for m in ids if m not in vectors]
        if missing:
            fresh = embedder.embed_messages([messages[mid] for mid in missing if mid in messages])
            rows = [(mid, embedder.name, vec) for mid, vec in zip([m for m in missing if m in messages], fresh)]
            if rows:
                store.put_embeddings(rows, embedder.name)
                vectors.update({mid: vec for mid, _model, vec in rows})

    by_action: dict[tuple[Action, str | None], list[str]] = {}
    for item in plan.items:
        by_action.setdefault((item.action, item.label), []).append(item.message_id)

    groups: list[Group] = []
    for (action, label), group_ids in sorted(by_action.items(), key=lambda kv: (kv[0][0].value, kv[0][1] or "")):
        with_vectors = [(mid, vectors[mid]) for mid in group_ids if mid in vectors]
        without = [mid for mid in group_ids if mid not in vectors]
        clusters = [list(c) for c in cluster(with_vectors, similarity=similarity)] + [[mid] for mid in without]
        for members in clusters:
            present = [messages[m] for m in members if m in messages]
            sample = max(present, key=lambda m: len(m.subject or ""), default=None)
            groups.append(Group(
                action=action.value, label=label, message_ids=members,
                domains=_domains(present), sample_subject=(sample.subject if sample else "") or "",
            ))
    groups.sort(key=lambda g: (g.action, -len(g.message_ids)))
    return groups


def plan_from_rules(*, store: Store, gmail: Gmail, rules: Sequence[Rule], limit: int | None = None,
                    sample: int = 10, scope: str = "in:inbox") -> Plan:
    """A plan whose decisions Gmail itself can carry out — no headers, no model.

    This is how a mailbox of a hundred thousand messages is handled at all: a rule
    that translates to a Gmail query is evaluated *by Gmail*, so the only calls are
    the listings and the bulk writes. A few messages are fetched afterwards, purely
    so the plan can be read before it is applied.

    `scope` matters more than it looks. The rules are written against the inbox —
    "anything over a year old leaves the inbox" — but a translated query on its own
    has no scope at all: `older_than:365d` alone matched 152,881 messages in a
    mailbox whose inbox holds 107,710, because it was also matching Sent and
    archived mail. Every rule query is therefore wrapped in `in:inbox` unless the
    caller says otherwise, and the plan file does not change that.
    """
    if scope:
        rules = [r for r in rules]
    items: dict[str, PlanItem] = {}
    used: list[str] = []
    for rule in rules:
        query = to_query(rule)
        if query is None or not rule.enabled:
            continue
        used.append(rule.id)
        scoped = f"{scope} {query}".strip() if scope else query
        for mid in gmail.list_ids(scoped, limit=limit):
            items.setdefault(mid, PlanItem(message_id=mid, action=rule.action, label=rule.label,
                                           source=Source.rule, rule_id=rule.id))
    plan = store.create_plan(
        account=gmail.settings.account or "", query=f"rules: {', '.join(used) or 'none'}",
        items=list(items.values()), judge="rules",
    )
    if sample and items:
        ids = list(items)[:max(sample, 1)]
        store.upsert_messages(gmail.fetch(ids))
    return plan


def group_by_index(plan: Plan, store: Store, index: int, *, similarity: float = DEFAULT_SIMILARITY,
                   embedder=None) -> Group:
    """The group a person saw at position `index` (1-based) in `plan digest`.

    Indices are stable for a plan and a similarity setting; changing either changes
    them, so the error says to re-run the digest rather than acting on a guess.
    """
    groups = digest_plan(plan, store, similarity=similarity, embedder=embedder)
    if index < 1 or index > len(groups):
        raise ValueError(f"plan {plan.id} has {len(groups)} group(s) at similarity {similarity}; "
                         f"run `damien plan digest {plan.id}` and try again")
    return groups[index - 1]


def _domains(messages: Sequence[Message], limit: int = 3) -> str:
    seen: list[str] = []
    for m in messages:
        domain = (m.from_addr or "").split("@")[-1]
        if domain and domain not in seen:
            seen.append(domain)
    return ", ".join(seen[:limit]) or "—"


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
    groups = sorted(by_action.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
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


def apply_plan(*, store: Store, gmail: Gmail, plan_id: str, token: str, resume: bool = True,
               only: Action | None = None, message_ids: Sequence[str] | None = None,
               min_confidence: float | None = None) -> Run:
    """Execute an approved plan in bulk batches, journaling each one.

    Idempotent by batch index: re-running a half-finished plan with `resume`
    performs only the batches the journal does not already hold.

    `only` applies one action of the plan — the slice a person has actually read —
    and leaves the rest approved for a later pass.

    `min_confidence` keeps only what the model was sure about, which is how a plan of
    twenty thousand messages is worked through without reading all of it: take the
    certain, leave the doubtful.
    """
    plan = store.get_plan(plan_id)
    if plan is None:
        raise ValueError(f"no such plan: {plan_id}")
    if plan.status == "applied":
        raise ValueError(f"plan {plan_id} is already applied — undo its run first (`damien run list`)")
    if plan.status not in ("approved", "partly_applied"):
        raise ValueError(f"plan {plan_id} is {plan.status}: approve it first (`damien plan approve {plan_id}`)")
    if not plan.token or plan.token != token:
        raise ValueError("wrong approval token for that plan")

    existing = store.last_run_for_plan(plan.id)
    run = existing if (existing and resume and existing.status in ("running", "partial")) else store.start_run(plan)
    done = {b.index for b in run.batches}

    label_cache = gmail.labels()
    wanted = set(message_ids) if message_ids is not None else None
    items = [i for i in plan.items
             if (only is None or i.action is only)
             and (wanted is None or i.message_id in wanted)
             and (min_confidence is None or i.source in (Source.rule, Source.person)
                  or (i.confidence or 0.0) >= min_confidence)]
    if (only is not None or wanted is not None) and not items:
        raise ValueError(f"plan {plan_id} has nothing to do for {only.value if only else 'those messages'}")
    groups = _group_items(items)

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
    subset = only is not None or wanted is not None or min_confidence is not None
    store.set_plan_status(plan.id, "partly_applied" if subset else "applied")
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
