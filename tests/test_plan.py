"""Plans: built without writing, applied in bulk, journalled, reversible."""

from __future__ import annotations

import pytest

from damien.decide import DEFAULT_SPEC
from damien.jev import FakeJudge
from damien.models import Action, Message, Source
from damien.plan import apply_plan, approve, build_plan, sample_plan, undo_run
from damien.rules import Condition, Rule
from damien.store import Store


def messages() -> list[Message]:
    return [
        Message(id="m1", account="me@example.com", subject="Big SALE 50% off", snippet="deals",
                from_addr="deals@shop.example", label_ids=["INBOX"], internal_date=1_700_000_000_000),
        Message(id="m2", account="me@example.com", subject="Weekly digest", snippet="news",
                from_addr="news@list.example", label_ids=["INBOX"], list_unsubscribe=True,
                internal_date=1_700_000_000_000),
        Message(id="m3", account="me@example.com", subject="can we talk Tuesday?", snippet="about the split",
                from_addr="alex@studio.example", label_ids=["INBOX"], internal_date=1_700_000_000_000),
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
