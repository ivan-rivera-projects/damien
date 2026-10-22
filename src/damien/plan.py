"""Plans: the only thing that ever touches Gmail.

A plan is built, looked at, approved, applied, and — if it was wrong — undone.
Every batch is written to the journal as `pending` before it is sent to Gmail and
marked `done` after, so a run that dies halfway is resumable and reversible —
including the batch that was in flight.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import stakes
from .decide import DEFAULT_SPEC, Judge, JudgementSpec, apply_corrections, decide_all
from .embed import DEFAULT_SIMILARITY, cluster
from .gmail import Gmail
from .models import Action, Message, Plan, PlanItem, Run, RunBatch, Source
from .rules import Rule, label_token, to_query
from .stakes import DEFAULT_PROTECTED
from .store import Store

DEFAULT_HEADER_TTL_HOURS = 12.0


FETCH_WINDOW = 250


def load_messages(*, store: Store, gmail: Gmail, ids: Sequence[str],
                  ttl_hours: float = DEFAULT_HEADER_TTL_HOURS,
                  window: int = FETCH_WINDOW) -> tuple[list[Message], int]:
    """Headers for these ids: from the store when they are fresh, from Gmail otherwise.

    Headers do not change; labels do. The TTL is the honesty dial — inside it we
    reuse what we already downloaded (a re-plan costs nothing), and past it we pay
    for a fresh copy. `ttl_hours=0` always refetches.

    Downloads are written in windows. A four-thousand-message fetch takes minutes
    under Gmail's quota, so it says where it is and keeps what it has: a pass that
    dies at 3,900 keeps 3,750 of them, and the next one starts there.
    """
    ids = list(ids)
    if not ids:
        return [], 0
    cutoff = None if ttl_hours <= 0 else (datetime.now(UTC) - timedelta(hours=ttl_hours)).isoformat()
    fresh = store.seen_recently(ids, cutoff) if cutoff else set()
    missing = [i for i in ids if i not in fresh]
    # A refetch is for labels; the body does not change, and reading it again would
    # mean another full-message call for every escalated message after the TTL (L11).
    stored_bodies = {m.id: m.body for m in store.get_messages(missing) if m.body is not None} if missing else {}
    fetched: list[Message] = []
    for start in range(0, len(missing), max(1, window)):
        window_ids = missing[start:start + max(1, window)]
        got = gmail.fetch(window_ids)
        for m in got:
            if m.body is None and m.id in stored_bodies:
                m.body = stored_bodies[m.id]
        fetched.extend(got)
        store.upsert_messages(got)
        if len(missing) > window:
            import sys
            print(f"  fetched {len(fetched):,}/{len(missing):,} new message(s)…", file=sys.stderr, flush=True)
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
    read_bodies: str = "off",
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
        # recorded whether or not escalation runs: a report must not have to infer it
        stats["read_bodies"] = read_bodies

    label_names = gmail.labels() if gmail is not None else None
    protected = protected_labels or DEFAULT_PROTECTED
    # The first pass judges headers and snippet only: a body stored by an earlier
    # escalation must not ride along to every later judgment (C3).
    headers_only = [m.model_copy(update={"body": None}) for m in messages]
    decisions = decide_all(headers_only, rules=rules, judge=judge, spec=spec, batch_size=judge_batch,
                           ask_below=ask_below, label_names=label_names, model=model,
                           protected_labels=protected)
    corrections = store.latest_corrections()

    # Escalation: for the messages the snippet could not settle, read the newest words and
    # ask again. Off unless `DAMIEN_READ_BODIES=on_escalation`, because it is the one mode
    # where message text reaches the judge — see docs/PRIVACY.md.
    if read_bodies == "on_escalation" and judge is not None and gmail is not None:
        by_id = {m.id: m for m in messages}
        failed = set(getattr(judge, "failed", ()) or ())
        # Not worth a body read: mail the person protects or already decided — the answer
        # is settled whatever the words say — and mail the judge could not answer at all (L10).
        unsure = [d.message_id for d in decisions
                  if d.source is Source.jev and (d.confidence or 0.0) < ask_below
                  and d.message_id in by_id and d.message_id not in corrections and d.message_id not in failed
                  and stakes.protected_label(by_id[d.message_id], label_names, protected) is None]
        # `None` means we have not looked; `""` means we looked and the new words were all
        # quoted. Both are remembered, so a body is read once and never re-read for nothing.
        unknown = [mid for mid in unsure if by_id[mid].body is None]
        if unknown:
            for mid, text in gmail.fetch_bodies(unknown).items():
                by_id[mid].body = text
            for mid in unknown:
                if by_id[mid].body is None:
                    by_id[mid].body = ""
            store.upsert_messages([by_id[mid] for mid in unknown])
        escalating = [by_id[mid] for mid in unsure if by_id[mid].body]
        if escalating:
            again = decide_all(escalating, rules=(), judge=judge, spec=spec, batch_size=judge_batch,
                               ask_below=ask_below, label_names=label_names, model=model,
                               protected_labels=protected)
            replaced = {d.message_id: d for d in again}
            decisions = [replaced.get(d.message_id, d) for d in decisions]
            if stats is not None:
                stats["escalated"] = len(escalating)
    if stats is not None:
        stats["judge_failed"] = len(getattr(judge, "failed", ()) or ())

    decisions = apply_corrections(decisions, corrections)
    store.add_decisions(decisions, account=(gmail.settings.account if gmail is not None else ""))

    by_msg = {m.id: m for m in messages}
    label_ids = {name.lower(): lid for lid, name in label_names.items()} if label_names is not None else None

    def had_label(d) -> bool | None:
        m = by_msg.get(d.message_id)
        if m is None or not d.label or label_ids is None:
            return None
        lid = label_ids.get(d.label.lower())
        return lid is not None and lid in m.label_ids

    items = [
        PlanItem(message_id=d.message_id, action=d.action, label=d.label, source=d.source,
                 rule_id=d.rule_id, confidence=d.confidence, keep_in_inbox=d.keep_in_inbox,
                 was_in_inbox=(by_msg[d.message_id].in_inbox if d.message_id in by_msg else None),
                 was_unread=(by_msg[d.message_id].is_unread if d.message_id in by_msg else None),
                 had_label=had_label(d))
        for d in decisions
    ]
    return store.create_plan(
        account=gmail.settings.account if gmail is not None else "",
        query=query, items=items, model=model, judge=(judge.name if judge else None),
        judge_usd=getattr(judge, "spent_usd", 0.0) or 0.0,
        judge_calls=getattr(judge, "calls", 0) or 0,
        judge_cached=getattr(judge, "cached", 0) or 0,
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
            rows = [(mid, embedder.name, vec) for mid, vec in zip([m for m in missing if m in messages], fresh, strict=True)]
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


def _or_group(words: Sequence[str]) -> str:
    return "{" + " ".join(w if w.isalnum() else f'"{w}"' for w in words) + "}"


def _stake_exclusions(rule: Rule, protected_labels: Sequence[str]) -> str:
    """`stakes.stake_floor` as Gmail search terms, for rules the person did not write.

    `plan build` would refuse to file these messages away; a plan Gmail evaluates
    must not do it either (C1). A person's own rule is never second-guessed.
    """
    if rule.source == "person":
        return ""
    parts = [f"-{label_token(p)}" for p in protected_labels]
    parts.append(f"-subject:{_or_group(stakes.ALARM_TERMS)}")
    parts.append("-subject:re -subject:fwd -subject:fw")
    if rule.action in (Action.archive, Action.trash):
        parts.append(f"is:unread -is:important -subject:{_or_group(stakes.CONSEQUENTIAL_TERMS)}")
    return " ".join(parts)


def plan_from_rules(*, store: Store, gmail: Gmail, rules: Sequence[Rule], limit: int | None = None,
                    sample: int = 10, scope: str = "in:inbox",
                    protected_labels: Sequence[str] = DEFAULT_PROTECTED,
                    stats: dict | None = None) -> Plan:
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

    It must never do what `plan build` would refuse: the stake floor rides along in
    the query, a person's correction outranks the rule, and a rule that only Python can
    evaluate keeps its place in the order — a later rule that disagrees with it is
    skipped (`stats["skipped_rules"]`) rather than allowed to claim its mail first.
    """
    items: dict[str, PlanItem] = {}
    used: list[str] = []
    skipped: list[tuple[str, str]] = []
    python_only: list[Rule] = []
    for rule in rules:
        if not rule.enabled:
            continue
        query = to_query(rule)
        if query is None:
            python_only.append(rule)
            continue
        blocker = next((r for r in python_only if (r.action, r.label) != (rule.action, rule.label)), None)
        if blocker is not None:
            skipped.append((rule.id, f"an earlier rule {blocker.id} runs in Python and could claim the same "
                                     f"mail — use plan build"))
            continue
        used.append(rule.id)
        scoped = " ".join(p for p in (scope, query, _stake_exclusions(rule, protected_labels)) if p)
        ids = [mid for mid in gmail.list_ids(scoped, limit=limit) if mid not in items]
        if not ids:
            continue
        # The state each message is in now, so undo can put back exactly that — ids only.
        out_of_inbox = set(gmail.list_ids(f"{scoped} -in:inbox"))
        read = set(gmail.list_ids(f"{scoped} -is:unread"))
        labelled = (set(gmail.list_ids(f"{scoped} {label_token(rule.label)}"))
                    if rule.action == Action.label and rule.label else None)
        for mid in ids:
            items[mid] = PlanItem(message_id=mid, action=rule.action, label=rule.label,
                                  source=Source.rule, rule_id=rule.id, keep_in_inbox=rule.keep_in_inbox,
                                  was_in_inbox=mid not in out_of_inbox, was_unread=mid not in read,
                                  had_label=(mid in labelled) if labelled is not None else None)
    corrections = store.latest_corrections()
    for mid, fixed in corrections.items():
        if mid in items:
            items[mid] = items[mid].model_copy(update={
                "action": Action(fixed["action"]), "label": fixed.get("label"), "source": Source.person,
                "rule_id": None, "confidence": 1.0})
    if stats is not None:
        stats["skipped_rules"] = skipped
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
    """Approve a plan and return its token (shown once; only a hash is stored).

    A plan that was partly applied stays `partly_applied` — re-approving it only issues a
    new token, so the rest can still be applied without pretending nothing happened."""
    plan = store.get_plan(plan_id)
    if plan is None:
        raise ValueError(f"no such plan: {plan_id}")
    if plan.status == "applied":
        raise ValueError("that plan was already applied — build a new one from the current mailbox")
    if plan.status == "undone":
        raise ValueError("that plan was undone — build a new one from the current mailbox")
    return store.approve_plan(plan_id, "partly_applied" if plan.status == "partly_applied" else "approved")


SYSTEM_LABELS = frozenset({"INBOX", "TRASH", "SPAM", "UNREAD", "SENT", "DRAFT"})


def _filter_signature(only: Action | None, message_ids: Sequence[str] | None,
                      min_confidence: float | None, mark_read_when_filed: bool) -> str:
    """What a run selected. A run is resumed only by a call that selects the same thing:
    `--only archive` must not skip batch 0 because `--only trash` journaled a batch 0."""
    key = json.dumps([only.value if only else None, sorted(message_ids) if message_ids is not None else None,
                      min_confidence, bool(mark_read_when_filed)])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def apply_plan(*, store: Store, gmail: Gmail, plan_id: str, token: str, resume: bool = True,
               only: Action | None = None, message_ids: Sequence[str] | None = None,
               min_confidence: float | None = None, mark_read_when_filed: bool = False) -> Run:
    """Execute an approved plan in bulk batches, journaling each one.

    Idempotent by batch index: re-running a half-finished plan with `resume` and the same
    selection performs only the batches the journal does not hold as `done`.

    `only` applies one action of the plan — the slice a person has actually read —
    and leaves the rest approved for a later pass.

    `min_confidence` keeps only what the model was sure about, which is how a plan of
    twenty thousand messages is worked through without reading all of it: take the
    certain, leave the doubtful.

    Every selected item is checked before anything is written: a plan that cannot be
    carried out whole must not be carried out halfway.
    """
    plan = store.get_plan(plan_id)
    if plan is None:
        raise ValueError(f"no such plan: {plan_id}")
    if plan.status == "applied":
        raise ValueError(f"plan {plan_id} is already applied — undo its run first (`damien run list`)")
    if plan.status not in ("approved", "partly_applied"):
        raise ValueError(f"plan {plan_id} is {plan.status}: approve it first (`damien plan approve {plan_id}`)")
    if not store.check_token(plan, token):
        raise ValueError("wrong approval token for that plan")

    wanted = set(message_ids) if message_ids is not None else None
    items = [i for i in plan.items
             if (only is None or i.action is only)
             and (wanted is None or i.message_id in wanted)
             and (min_confidence is None or i.source in (Source.rule, Source.person)
                  or (i.confidence or 0.0) >= min_confidence)]
    if (only is not None or wanted is not None) and not items:
        raise ValueError(f"plan {plan_id} has nothing to do for {only.value if only else 'those messages'}")
    unlabelled = [i.message_id for i in items if i.action is Action.label and not i.label]
    if unlabelled:
        raise ValueError(f"label action with no label for: {', '.join(unlabelled[:10])}"
                         f"{' …' if len(unlabelled) > 10 else ''}")
    for name in sorted({i.label for i in items if i.action is Action.label and i.label}):
        if (gmail.label_id(name) or name).upper() in SYSTEM_LABELS:
            raise ValueError(f"label {name!r} is a Gmail system label — use archive/trash/mark_read instead")
    groups = _group_items(items)

    sig = _filter_signature(only, message_ids, min_confidence, mark_read_when_filed)
    existing = store.last_run_for_plan(plan.id, sig)
    run = existing if (existing and resume and existing.status in ("running", "partial")) else store.start_run(plan, sig)
    done = {b.index for b in run.batches if b.state == "done"}

    label_cache = {name: lid for lid, name in gmail.labels().items()}
    index = 0
    try:
        for (action, label, keep_in_inbox), group in groups:
            for chunk in _chunks(group, gmail.settings.op_batch):
                if index in done:
                    index += 1
                    continue
                label_id = None
                if action is Action.label and label:
                    label_id = gmail.label_id(label) or label_cache.get(label)
                    if label_id is None:
                        label_id = gmail.create_label(label)
                        label_cache[label] = label_id
                inverse = _inverse(action, chunk, label_id, keep_in_inbox, mark_read_when_filed)
                ids = [i.message_id for i in chunk]
                store.journal_batch(run.id, RunBatch(index=index, op=action.value, ids=ids, label=label,
                                                     inverse=inverse, state="pending"))
                _perform(gmail, action, ids, label_id, keep_in_inbox, mark_read_when_filed)
                store.batch_done(run.id, index)
                index += 1
    except Exception as exc:                      # journal already holds what landed (and what may have)
        store.finish_run(run.id, "partial", error=str(exc)[:500])
        raise
    store.finish_run(run.id, "ok")
    subset = only is not None or wanted is not None or min_confidence is not None
    store.set_plan_status(plan.id, "partly_applied" if subset else "applied")
    return store.get_run(run.id)  # type: ignore[return-value]


def undo_run(*, store: Store, gmail: Gmail, run_id: str) -> Run:
    """Replay the journal backwards — `done` batches and `pending` ones, whose Gmail call may
    have landed before the process died. Each message goes back to the state it had."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"no such run: {run_id}")
    if run.status == "undone":
        raise ValueError(f"run {run_id} was already undone")
    for batch in sorted(run.batches, key=lambda b: b.index, reverse=True):
        inv = batch.inverse or {}
        ops = inv.get("ops") if "ops" in inv else [{"ids": batch.ids, "add": inv.get("add", []),
                                                     "remove": inv.get("remove", [])}]
        for op in ops:                                                # type: ignore[union-attr]
            add, remove = list(op.get("add", [])), list(op.get("remove", []))
            if add or remove:
                gmail.modify(op["ids"], add=add, remove=remove)
    store.finish_run(run.id, "undone")
    still_applied = any(r.id != run.id and r.status in ("ok", "partial", "running") and r.batches
                        for r in store.runs_for_plan(run.plan_id))
    store.set_plan_status(run.plan_id, "partly_applied" if still_applied else "undone")
    return store.get_run(run.id)  # type: ignore[return-value]


def _group_items(items: Sequence[PlanItem]) -> list[tuple[tuple[Action, str | None, bool], list[PlanItem]]]:
    """One group per (action, label, keep_in_inbox): a group is one call, and two items
    that differ on whether INBOX is removed cannot share one."""
    groups: dict[tuple[Action, str | None, bool], list[PlanItem]] = {}
    for item in items:
        if item.action in (Action.keep,):
            continue
        groups.setdefault((item.action, item.label, item.keep_in_inbox), []).append(item)
    return [(key, sorted(group, key=lambda i: i.message_id))
            for key, group in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1] or "", kv[0][2]))]


def _inverse(action: Action, items: Sequence[PlanItem], label_id: str | None,
             keep_in_inbox: bool = False, mark_read: bool = False) -> dict[str, object]:
    """What undoes this batch, per message: put back only what was there and was taken away.

    A fixed inverse would put an archived message into the inbox, mark a read one unread,
    and strip a label the person had applied long before. Each item carries its state from
    plan time; where that is unknown (`None`, an older plan) the old fixed inverse is used.
    Ids with the same inverse share one op, so undo stays a handful of bulk calls.
    """
    ops: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
    for item in items:
        inbox = item.was_in_inbox is not False
        unread = item.was_unread is not False
        add: list[str] = []
        remove: list[str] = []
        if action is Action.archive:
            add = (["INBOX"] if inbox else []) + (["UNREAD"] if mark_read and unread else [])
        elif action is Action.trash:                       # trashing drops INBOX and UNREAD
            add = (["INBOX"] if inbox else []) + (["UNREAD"] if unread else [])
            remove = ["TRASH"]
        elif action is Action.mark_read:
            add = ["UNREAD"] if unread else []
        elif action is Action.label:
            filed = not keep_in_inbox
            add = (["INBOX"] if filed and inbox else []) + (["UNREAD"] if filed and mark_read and unread else [])
            remove = [label_id] if label_id and item.had_label is not True else []
        ops.setdefault((tuple(add), tuple(remove)), []).append(item.message_id)
    return {"ops": [{"ids": ids, "add": list(add), "remove": list(remove)}
                    for (add, remove), ids in ops.items() if add or remove]}


def _perform(gmail: Gmail, action: Action, ids: list[str], label_id: str | None,
             keep_in_inbox: bool = False, mark_read: bool = False) -> None:
    """Carry out one batch. Its inverse comes from `_inverse`, journaled before this runs.

    `mark_read` (from DAMIEN_MARK_READ_WHEN_FILED) is for the actions that *file* a message
    away: archiving or labelling it out of the inbox means you have dealt with it, so it
    stops claiming to be unread. Mail that is kept never reaches here at all — a keep is a
    decision, not a write — so kept mail stays unread by construction.
    """
    if action == Action.archive:
        gmail.modify(ids, remove=["INBOX", "UNREAD"] if mark_read else ["INBOX"])
    elif action == Action.trash:
        gmail.trash(ids)                                  # trashing already drops UNREAD
    elif action == Action.mark_read:
        gmail.mark_read(ids)
    elif action == Action.label and label_id:
        gmail.add_label(ids, label_id, keep_in_inbox=keep_in_inbox,
                        mark_read=mark_read and not keep_in_inbox)
    else:
        raise ValueError(f"nothing to do for action {action.value}")


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
