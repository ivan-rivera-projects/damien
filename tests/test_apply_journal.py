"""Apply and undo against a fake mailbox whose labels really change.

Each test is a way a run could leave a person's mail different from how it was:
an undo that invents state, a resume that skips work, a crash between the Gmail
call and the journal, a plan that is half-valid.
"""

from __future__ import annotations

import pytest

from damien.models import Action, PlanItem, Source
from damien.plan import apply_plan, approve, undo_run
from damien.store import Store


def item(fake, mid: str, action: Action, label: str | None = None, label_id: str | None = None) -> PlanItem:
    """A plan item that remembers the message's state the way build_plan does."""
    labels = fake.messages[mid]["labelIds"]
    return PlanItem(message_id=mid, action=action, label=label, source=Source.rule,
                    was_in_inbox="INBOX" in labels, was_unread="UNREAD" in labels,
                    had_label=(label_id in labels) if label_id else None)


def labels_of(fake) -> dict[str, set[str]]:
    return {mid: set(m["labelIds"]) for mid, m in fake.messages.items()}


@pytest.fixture
def live(fake):
    fake.live_labels = True
    return fake


@pytest.mark.parametrize("mark_read", [False, True])
def test_undo_restores_each_messages_exact_state(store: Store, gmail, live, mark_read):
    live.add("a1", labels=["INBOX", "UNREAD"])
    live.add("a2", labels=["INBOX"])
    live.add("t1", labels=["INBOX"])                       # read, in the inbox
    live.add("t2", labels=["UNREAD", "CATEGORY_UPDATES"])  # unread, already archived
    live.add("l1", labels=["Label_1"])                     # archived, read, already labelled
    live.add("l2", labels=["INBOX", "UNREAD"])
    live.add("r1", labels=["INBOX", "UNREAD"])
    live.add("r2", labels=["CATEGORY_UPDATES"])            # archived and read
    before = labels_of(live)
    items = [item(live, "a1", Action.archive), item(live, "a2", Action.archive),
             item(live, "t1", Action.trash), item(live, "t2", Action.trash),
             item(live, "l1", Action.label, "Newsletters", "Label_1"),
             item(live, "l2", Action.label, "Newsletters", "Label_1"),
             item(live, "r1", Action.mark_read), item(live, "r2", Action.mark_read)]
    plan = store.create_plan("me@example.com", "q", items)
    token = approve(store, plan.id)

    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, mark_read_when_filed=mark_read)
    assert "TRASH" in live.messages["t1"]["labelIds"] and "INBOX" not in live.messages["a1"]["labelIds"]

    undo_run(store=store, gmail=gmail, run_id=run.id)
    assert labels_of(live) == before
    assert "Label_1" in live.messages["l1"]["labelIds"]   # the person's own label survived the undo
    assert store.get_plan(plan.id).status == "undone"


def test_a_failed_only_trash_does_not_make_only_archive_skip_its_batches(store: Store, gmail, live):
    for mid in ("t1", "t2", "a1", "a2", "a3"):
        live.add(mid, labels=["INBOX"])
    plan = store.create_plan("me@example.com", "q",
                             [item(live, m, Action.trash) for m in ("t1", "t2")]
                             + [item(live, m, Action.archive) for m in ("a1", "a2", "a3")])
    token = approve(store, plan.id)

    live.fail_mods = 1
    with pytest.raises(Exception, match="simulated"):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, only=Action.trash)

    apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, only=Action.archive)
    assert all("INBOX" not in live.messages[m]["labelIds"] for m in ("a1", "a2", "a3"))

    resumed = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, only=Action.trash)
    assert resumed.status == "ok" and resumed.applied == 2
    assert all("TRASH" in live.messages[m]["labelIds"] for m in ("t1", "t2"))


class Died(BaseException):
    """The process going away: not an Exception, so nothing in apply_plan catches it."""


def test_a_batch_that_landed_before_the_process_died_is_undone(store: Store, gmail, live, monkeypatch):
    live.add("m1", labels=["INBOX", "UNREAD"])
    plan = store.create_plan("me@example.com", "q", [item(live, "m1", Action.trash)])
    token = approve(store, plan.id)

    def die(run_id, index):
        raise Died
    monkeypatch.setattr(store, "batch_done", die)
    with pytest.raises(Died):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    monkeypatch.undo()
    assert "TRASH" in live.messages["m1"]["labelIds"]       # Gmail did it; the journal never heard back

    run = store.last_run_for_plan(plan.id)
    assert run.applied == 0 and [b.state for b in run.batches] == ["pending"]
    undo_run(store=store, gmail=gmail, run_id=run.id)
    assert set(live.messages["m1"]["labelIds"]) == {"INBOX", "UNREAD"}


def test_an_undone_plan_cannot_be_reapproved_or_undone_twice(store: Store, gmail, live):
    live.add("m1", labels=["INBOX"])
    plan = store.create_plan("me@example.com", "q", [item(live, "m1", Action.archive)])
    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=approve(store, plan.id))
    undo_run(store=store, gmail=gmail, run_id=run.id)
    sent = len(live.mods)
    with pytest.raises(ValueError, match="undone"):
        approve(store, plan.id)
    with pytest.raises(ValueError, match="already undone"):
        undo_run(store=store, gmail=gmail, run_id=run.id)
    assert len(live.mods) == sent


def test_undoing_one_slice_leaves_the_plan_partly_applied(store: Store, gmail, live):
    live.add("t1", labels=["INBOX"])
    live.add("a1", labels=["INBOX"])
    plan = store.create_plan("me@example.com", "q", [item(live, "t1", Action.trash), item(live, "a1", Action.archive)])
    token = approve(store, plan.id)
    trash_run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, only=Action.trash)
    archive_run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, only=Action.archive)

    undo_run(store=store, gmail=gmail, run_id=archive_run.id)
    assert store.get_plan(plan.id).status == "partly_applied"   # the trash run still stands
    new_token = approve(store, plan.id)                          # re-approving does not reset that
    assert new_token != token and store.get_plan(plan.id).status == "partly_applied"
    undo_run(store=store, gmail=gmail, run_id=trash_run.id)
    assert store.get_plan(plan.id).status == "undone"


@pytest.mark.parametrize("label", [None, "Inbox", "TRASH"])
def test_a_plan_with_an_unusable_label_writes_nothing(store: Store, gmail, live, label):
    live.add("m1", labels=["INBOX"])
    live.add("m2", labels=["INBOX"])
    plan = store.create_plan("me@example.com", "q", [
        item(live, "m1", Action.archive),
        PlanItem(message_id="m2", action=Action.label, label=label, source=Source.rule),
    ])
    token = approve(store, plan.id)
    with pytest.raises(ValueError, match="m2" if label is None else "system label"):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token)
    assert live.mods == []
    assert store.last_run_for_plan(plan.id) is None                # no run left 'running'
    assert store.get_plan(plan.id).status == "approved"


def test_the_stored_token_is_not_the_one_that_applies(store: Store, gmail, live):
    live.add("m1", labels=["INBOX"])
    plan = store.create_plan("me@example.com", "q", [item(live, "m1", Action.archive)])
    token = approve(store, plan.id)
    stored = store.get_plan(plan.id).token
    assert stored and token not in stored
    with pytest.raises(ValueError, match="token"):
        apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=stored)
    assert live.mods == []


def test_a_plan_approved_before_0_4_applies_with_its_plaintext_token(store: Store, gmail, live):
    live.add("m1", labels=["INBOX"])
    plan = store.create_plan("me@example.com", "q", [item(live, "m1", Action.archive)])
    store.conn.execute("UPDATE plans SET status='approved', token='0123456789abcdef' WHERE id=?", (plan.id,))
    store.conn.commit()
    run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token="0123456789abcdef")
    assert run.status == "ok" and "INBOX" not in live.messages["m1"]["labelIds"]


def test_undo_still_reads_a_legacy_journal(store: Store, gmail, live):
    from damien.models import RunBatch
    live.add("m1", labels=["UNREAD"])
    plan = store.create_plan("me@example.com", "q", [PlanItem(message_id="m1", action=Action.archive)])
    run = store.start_run(plan)
    store.journal_batch(run.id, RunBatch(index=0, op="archive", ids=["m1"], inverse={"add": ["INBOX"], "remove": []}))
    store.batch_done(run.id, 0)
    undo_run(store=store, gmail=gmail, run_id=run.id)
    assert set(live.messages["m1"]["labelIds"]) == {"INBOX", "UNREAD"}
