"""Plans: built without writing, applied in bulk, journalled, reversible."""

from __future__ import annotations

import pytest

from damien.decide import DEFAULT_SPEC
from damien.jev import FakeJudge
from damien.models import Action, Message, PlanItem, Source
from damien.plan import (
    apply_plan,
    approve,
    build_plan,
    digest_plan,
    group_by_index,
    load_messages,
    plan_from_rules,
    sample_plan,
    undo_run,
)
from damien.rules import Condition, Rule
from damien.store import Store


def messages() -> list[Message]:
    return [
        Message(id="m1", account="me@example.com", subject="Big SALE 50% off", snippet="deals",
                from_addr="deals@shop.example", label_ids=["INBOX", "UNREAD"], internal_date=1_700_000_000_000),
        Message(id="m2", account="me@example.com", subject="Weekly digest", snippet="news",
                from_addr="news@list.example", label_ids=["INBOX", "UNREAD"], list_unsubscribe=True,
                internal_date=1_700_000_000_000),
        Message(id="m3", account="me@example.com", subject="can we talk Tuesday?", snippet="about the split",
                from_addr="alex@studio.example", label_ids=["INBOX", "UNREAD"], internal_date=1_700_000_000_000),
    ]


def build(store: Store, *, rules=(), judge=None, **kw):
    return build_plan(store=store, gmail=None, rules=list(rules), judge=judge, query="in:inbox",
                      messages=messages(), spec=DEFAULT_SPEC, **kw)


def test_build_decides_and_stores_without_touching_gmail(store: Store):
    plan = build(store, judge=FakeJudge())
    assert plan.status == "draft"
    assert set(plan.counts()) <= {"trash", "label", "keep"}
    assert len(plan.items) == 3
    assert store.count_messages() == 3
    assert len(store.decisions_for(["m1", "m2", "m3"])) == 3


def test_rules_carry_their_provenance_into_the_plan(store: Store):
    rule = Rule(id="r-digest", name="digests", action=Action.label, label="Newsletters",
                match=[Condition(field="list_unsubscribe", op="exists")])
    plan = build(store, rules=[rule], judge=FakeJudge())
    item = next(i for i in plan.items if i.message_id == "m2")
    assert item.action is Action.label and item.rule_id == "r-digest" and item.source is Source.rule


def test_an_untranslatable_rule_still_matches_in_python(store: Store):
    rule = Rule(id="r-regex", name="sale", action=Action.trash,
                match=[Condition(field="subject", op="regex", value="^big sale")])
    plan = build(store, rules=[rule], judge=None)
    trashed = [i.message_id for i in plan.items if i.action is Action.trash]
    assert trashed == ["m1"]


def test_sample_is_stratified_not_just_the_first_n(store: Store):
    plan = build(store, judge=FakeJudge())
    rows = sample_plan(plan, store, n=6)
    assert {r.action for r in rows} == set(plan.counts())         # every action represented
    assert all(r.subject for r in rows)


def test_apply_requires_the_token_and_the_approval(store: Store, gmail, fake):
    plan = build(store, judge=FakeJudge())
    with pytest.raises(ValueError, match="approve it first"):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token="x")
    token = approve(store, plan.id)
    with pytest.raises(ValueError, match="wrong approval token"):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token="not-it")
    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    assert run.status == "ok" and run.applied == len(plan.items) - plan.counts().get("keep", 0)
    assert all(m["ids"] for m in fake.mods)


def test_apply_batches_labels_into_one_call_and_creates_the_label(store: Store, gmail, fake):
    rule = Rule(id="r-digest", name="digests", action=Action.label, label="Newsletters",
                match=[Condition(field="list_unsubscribe", op="exists")])
    plan = build(store, rules=[rule], judge=FakeJudge())
    token = approve(store, plan.id)
    apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    label_post = [m for m in fake.mods if m.get("addLabelIds")]
    assert label_post, "a label action must add a label id"
    assert any(m["addLabelIds"] for m in fake.mods if m["ids"] == ["m2"])


def test_reapplying_an_applied_plan_is_refused(store: Store, gmail):
    plan = build(store, judge=FakeJudge())
    token = approve(store, plan.id)
    apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    with pytest.raises(ValueError, match="already applied"):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)


def test_undo_replays_the_journal_backwards(store: Store, gmail, fake):
    plan = build(store, judge=FakeJudge())
    token = approve(store, plan.id)
    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    applied = len(fake.mods)
    undone = undo_run(store=store, gmail=gmail, run_id=run.id)
    assert undone.status == "undone"
    assert len(fake.mods) > applied                                  # inverse ops were sent
    inverses = fake.mods[applied:]
    # whatever was trashed, the inverse puts it back in the inbox
    trash_batches = [b for b in run.batches if b.op == "trash"]
    if trash_batches:
        assert any("INBOX" in m["addLabelIds"] for m in inverses)


def test_a_partial_run_stays_resumable_and_replays_nothing(store: Store, gmail, fake):
    plan = build(store, judge=FakeJudge())
    token = approve(store, plan.id)

    fake.fail_mods = 1                                           # the first batch dies
    with pytest.raises(Exception):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)

    assert store.get_plan(plan.id).status == "approved"           # not "applied" — nothing was finished
    run = store.last_run_for_plan(plan.id)
    assert run.status == "partial" and run.error
    journaled = len(run.batches)
    sent_before = len(fake.mods)

    fake.fail_mods = 0                                            # heal, resume
    run2 = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    assert run2.id == run.id and run2.status == "ok"
    assert len(fake.mods) - sent_before == len(run2.batches) - journaled    # only what was missing


def test_a_keep_only_plan_writes_nothing(store: Store, gmail, fake):
    rule = Rule(id="r-keep", name="keep it all", action=Action.keep,
                match=[Condition(field="any", op="exists")])
    plan = build(store, rules=[rule], judge=None)
    assert plan.counts() == {"keep": 3}
    token = approve(store, plan.id)
    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    assert run.applied == 0 and fake.mods == []


def test_a_person_correction_outranks_the_judge_on_the_next_plan(store: Store):
    first = build(store, judge=FakeJudge())
    assert next(i for i in first.items if i.message_id == "m1").action is Action.trash

    store.correct("m1", Action.keep, note="that was a client")

    second = build(store, judge=FakeJudge())
    item = next(i for i in second.items if i.message_id == "m1")
    assert item.action is Action.keep
    assert item.source is Source.person            # the person is named as the decider
    assert item.rule_id is None


# ---- header caching + parallel fetch (#3) ------------------------------------
def test_headers_come_from_the_cache_the_second_time(store: Store, gmail, fake):
    for i in range(4):
        fake.add(f"m{i}")
    ids = gmail.list_ids("in:inbox")
    first, fetched_first = load_messages(store=store, gmail=gmail, ids=ids)
    assert len(first) == 4 and fetched_first == 4

    before = gmail.fetched
    second, fetched_second = load_messages(store=store, gmail=gmail, ids=ids)
    assert fetched_second == 0 and gmail.fetched == before          # nothing was downloaded again
    assert [m.id for m in second] == [m.id for m in first]          # same order, same content
    assert second[0].subject == first[0].subject


def test_a_zero_ttl_always_refetches(store: Store, gmail, fake):
    fake.add("m1")
    load_messages(store=store, gmail=gmail, ids=["m1"])
    before = gmail.fetched
    load_messages(store=store, gmail=gmail, ids=["m1"], ttl_hours=0)
    assert gmail.fetched == before + 1


def test_only_the_missing_ids_are_fetched(store: Store, gmail, fake):
    for i in range(3):
        fake.add(f"m{i}")
    load_messages(store=store, gmail=gmail, ids=["m0"])
    before = gmail.fetched
    messages, fetched = load_messages(store=store, gmail=gmail, ids=["m0", "m1", "m2"])
    assert fetched == 2 and gmail.fetched == before + 2
    assert [m.id for m in messages] == ["m0", "m1", "m2"]           # order preserved across cache+fetch


def test_parallel_fetch_keeps_order_and_gets_everything(gmail, fake):
    for i in range(25):
        fake.add(f"m{i:02d}")
    ids = [f"m{i:02d}" for i in range(25)]
    messages = gmail.fetch(ids, concurrency=8)
    assert [m.id for m in messages] == ids
    assert gmail.fetched == 25


def test_build_plan_reports_what_came_from_cache(store: Store, gmail, fake):
    for i in range(3):
        fake.add(f"m{i}")
    ids = gmail.list_ids("in:inbox")
    messages, _ = load_messages(store=store, gmail=gmail, ids=ids)   # warm the cache
    stats: dict[str, int] = {}
    plan = build_plan(store=store, gmail=gmail, rules=[], judge=FakeJudge(), query="in:inbox",
                      limit=None, stats=stats)
    assert stats == {"messages": 3, "fetched": 0, "cached": 3}
    assert len(plan.items) == 3


def test_applying_only_one_action_leaves_the_rest_of_the_plan(store: Store, gmail, fake):
    plan = build(store, judge=FakeJudge())
    assert plan.counts().get("trash") and plan.counts().get("label")
    token = approve(store, plan.id)

    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, only=Action.trash)
    assert run.status == "ok"
    assert all("TRASH" in m["addLabelIds"] for m in fake.mods)       # nothing but trashing happened
    assert store.get_plan(plan.id).status == "partly_applied"

    sent = len(fake.mods)
    apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    assert len(fake.mods) > sent                                     # the rest followed
    assert store.get_plan(plan.id).status == "applied"


def test_an_empty_slice_is_refused(store: Store, gmail):
    rule = Rule(id="r-keep", name="keep all", action=Action.keep,
                match=[Condition(field="any", op="exists")])
    plan = build(store, rules=[rule], judge=None)
    token = approve(store, plan.id)
    with pytest.raises(ValueError, match="nothing to do"):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, only=Action.trash)


def test_sampling_a_plan_that_mixes_labelled_and_unlabelled_items(store: Store):
    """A plan has both `label`-with-a-name and `trash`-with-no-label; sorting those
    keys together is a TypeError unless None is normalised."""
    plan = store.create_plan("me@example.com", "q", [
        PlanItem(message_id="m1", action=Action.trash, source=Source.jev),
        PlanItem(message_id="m2", action=Action.label, label="Receipts", source=Source.jev),
        PlanItem(message_id="m3", action=Action.keep, source=Source.jev),
    ])
    store.upsert_messages([Message(id=f"m{i}", account="me@example.com", subject=f"s{i}")
                           for i in (1, 2, 3)])
    rows = sample_plan(plan, store, n=10)
    assert {r.action for r in rows} == {"trash", "label", "keep"}


# ---- the digest: collapsing a plan into review groups ------------------------
def test_similar_messages_collapse_into_one_group(store: Store):
    store.upsert_messages([
        Message(id="m1", account="a", subject="Ugg Classic Slipper only $99.96", from_addr="dsw@e.dsw.com"),
        Message(id="m2", account="a", subject="These Ugg slippers are just $99.96", from_addr="dsw@e.dsw.com"),
        Message(id="m3", account="a", subject="Your receipt from OpenRouter", from_addr="invoice@openrouter.ai"),
        Message(id="m4", account="a", subject="Your receipt from OpenCode", from_addr="receipts@anoma.ly"),
    ])
    # m1/m2 are near-identical promos; m3/m4 are near-identical receipts
    store.put_embeddings([("m1", "t", [1.0, 0.0, 0.0]), ("m2", "t", [0.99, 0.02, 0.0]),
                          ("m3", "t", [0.0, 1.0, 0.0]), ("m4", "t", [0.0, 0.99, 0.02])], "t")
    plan = store.create_plan("a", "q", [
        PlanItem(message_id="m1", action=Action.trash, source=Source.jev),
        PlanItem(message_id="m2", action=Action.trash, source=Source.jev),
        PlanItem(message_id="m3", action=Action.label, label="Receipts", source=Source.jev),
        PlanItem(message_id="m4", action=Action.label, label="Receipts", source=Source.jev),
    ])
    groups = digest_plan(plan, store, similarity=0.9)
    assert len(groups) == 2                                     # not 4: two kinds of message
    trashes = [g for g in groups if g.action == "trash"]
    assert len(trashes) == 1 and len(trashes[0].message_ids) == 2
    assert "dsw.com" in trashes[0].domains
    receipts = [g for g in groups if g.action == "label"][0]
    assert receipts.label == "Receipts" and len(receipts.message_ids) == 2


def test_a_dissimilar_message_keeps_its_own_group(store: Store):
    store.upsert_messages([
        Message(id="m1", account="a", subject="promo one", from_addr="a@x.com"),
        Message(id="m2", account="a", subject="something else", from_addr="b@y.com"),
    ])
    store.put_embeddings([("m1", "t", [1.0, 0.0]), ("m2", "t", [0.0, 1.0])], "t")
    plan = store.create_plan("a", "q", [
        PlanItem(message_id="m1", action=Action.trash, source=Source.jev),
        PlanItem(message_id="m2", action=Action.trash, source=Source.jev),
    ])
    assert len(digest_plan(plan, store, similarity=0.8)) == 2


def test_the_digest_never_mixes_actions_or_labels(store: Store):
    store.upsert_messages([
        Message(id="m1", account="a", subject="identical", from_addr="a@x.com"),
        Message(id="m2", account="a", subject="identical", from_addr="a@x.com"),
    ])
    store.put_embeddings([("m1", "t", [1.0, 0.0]), ("m2", "t", [1.0, 0.0])], "t")
    plan = store.create_plan("a", "q", [
        PlanItem(message_id="m1", action=Action.trash, source=Source.jev),
        PlanItem(message_id="m2", action=Action.keep, source=Source.jev),
    ])
    groups = digest_plan(plan, store, similarity=0.9)
    assert len(groups) == 2                       # identical text, different answers: never merged
    assert {g.action for g in groups} == {"trash", "keep"}


# ---- addressing and applying a single group ---------------------------------
def test_applying_a_group_touches_only_that_group(store: Store, gmail, fake):
    store.upsert_messages([
        Message(id="m1", account="a", subject="promo A", from_addr="a@shop.example"),
        Message(id="m2", account="a", subject="promo B (same kind)", from_addr="b@shop.example"),
        Message(id="m3", account="a", subject="totally different", from_addr="c@other.example"),
    ])
    store.put_embeddings([("m1", "t", [1.0, 0.0]), ("m2", "t", [1.0, 0.0]), ("m3", "t", [0.0, 1.0])], "t")
    plan = store.create_plan("a", "q", [
        PlanItem(message_id="m1", action=Action.trash, source=Source.jev),
        PlanItem(message_id="m2", action=Action.trash, source=Source.jev),
        PlanItem(message_id="m3", action=Action.trash, source=Source.jev),
    ])
    token = approve(store, plan.id)
    group = group_by_index(plan, store, 1, similarity=0.9)
    assert group.message_ids == ["m1", "m2"]

    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, message_ids=group.message_ids)
    assert run.applied == 2
    assert fake.mods[0]["ids"] == ["m1", "m2"]
    assert store.get_plan(plan.id).status == "partly_applied"


def test_a_group_number_that_does_not_exist_says_so(store: Store):
    plan = store.create_plan("a", "q", [PlanItem(message_id="m1", action=Action.trash, source=Source.jev)])
    store.upsert_messages([Message(id="m1", account="a", subject="x")])
    with pytest.raises(ValueError, match="run `damien plan digest"):
        group_by_index(plan, store, 99)


def test_a_confidence_floor_takes_the_certain_and_leaves_the_doubtful(store: Store, gmail, fake):
    """At 20,000 messages nobody reads every group: take the sure ones, leave the rest."""
    store.upsert_messages([Message(id=f"m{i}", account="a", subject=f"s{i}", label_ids=["INBOX", "UNREAD"])
                           for i in range(4)])
    plan = store.create_plan("a", "q", [
        PlanItem(message_id="m0", action=Action.trash, source=Source.jev, confidence=0.95),
        PlanItem(message_id="m1", action=Action.trash, source=Source.jev, confidence=0.72),
        PlanItem(message_id="m2", action=Action.trash, source=Source.jev, confidence=0.61),
        PlanItem(message_id="m3", action=Action.trash, source=Source.rule, rule_id="r1"),
    ])
    token = approve(store, plan.id)
    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, min_confidence=0.9)
    assert run.applied == 2                                  # 0.95 and the rule, not 0.72 or 0.61
    assert set(fake.mods[0]["ids"]) == {"m0", "m3"}
    assert store.get_plan(plan.id).status == "partly_applied"


def test_a_person_decision_always_passes_the_confidence_floor(store: Store, gmail, fake):
    plan = store.create_plan("a", "q", [
        PlanItem(message_id="m1", action=Action.trash, source=Source.person, confidence=1.0),
        PlanItem(message_id="m2", action=Action.trash, source=Source.jev, confidence=0.4),
    ])
    token = approve(store, plan.id)
    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, min_confidence=0.9)
    assert run.applied == 1 and fake.mods[0]["ids"] == ["m1"]


# ---- a rule pass: no headers, no model ---------------------------------------
def test_plan_from_rules_lists_and_does_not_fetch_everything(store: Store, gmail, fake):
    for i in range(6):
        fake.add(f"m{i}", subject=f"message {i}", labels=["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"])
    fake.add("keep1", subject="a real letter", labels=["INBOX", "UNREAD"])
    rules = [
        Rule(id="r-promos", name="old promos", action=Action.trash,
             match=[Condition(field="label", op="contains", value="CATEGORY_PROMOTIONS")]),
        Rule(id="r-regex", name="cannot be translated", action=Action.archive,
             match=[Condition(field="subject", op="regex", value="^message")]),
    ]
    plan = plan_from_rules(store=store, gmail=gmail, rules=rules, sample=2)
    assert plan.counts() == {"trash": 6}                       # only the translatable rule ran
    assert {i.rule_id for i in plan.items} == {"r-promos"}
    assert gmail.fetched == 2                                  # only the sample was fetched
    assert "r-promos" in plan.query and "r-regex" not in plan.query


def test_a_message_matching_two_rules_takes_the_first(store: Store, gmail, fake):
    fake.add("m1", subject="promo", labels=["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"])
    rules = [
        Rule(id="r-first", name="first", action=Action.label, label="Promos",
             match=[Condition(field="label", op="contains", value="CATEGORY_PROMOTIONS")]),
        Rule(id="r-second", name="second", action=Action.trash,
             match=[Condition(field="label", op="contains", value="CATEGORY_PROMOTIONS")]),
    ]
    plan = plan_from_rules(store=store, gmail=gmail, rules=rules, sample=0)
    assert len(plan.items) == 1 and plan.items[0].rule_id == "r-first"
