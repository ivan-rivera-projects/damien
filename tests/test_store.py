"""The store: everything a run needs to be auditable, resumable and reversible."""

from __future__ import annotations

from damien.models import Action, Decision, Message, PlanItem, RunBatch, Source
from damien.store import Store


def a_message(mid: str = "m1") -> Message:
    return Message(id=mid, account="me@example.com", thread_id="t1", internal_date=1_700_000_000_000,
                   from_addr="a@b.example", from_name="A", subject="Hi", snippet="hi",
                   label_ids=["INBOX"], has_attachment=False, list_unsubscribe=False, size_estimate=10)


def test_messages_round_trip_and_upsert(store: Store):
    assert store.upsert_messages([a_message("m1")]) == 1
    assert store.upsert_messages([a_message("m1")]) == 1        # upsert, not duplicate
    assert store.count_messages() == 1
    [m] = store.get_messages(["m1"])
    assert m.subject == "Hi" and m.label_ids == ["INBOX"]


def test_decisions_are_append_only_with_provenance(store: Store):
    store.add_decisions([
        Decision(message_id="m1", action=Action.trash, source=Source.jev, confidence=0.91,
                 probabilities={"promotion": 0.91}, model="jev-latest", rationale="it is an ad"),
        Decision(message_id="m1", action=Action.keep, source=Source.person, rationale="person overruled"),
    ], account="me@example.com")
    got = store.decisions_for(["m1"])
    assert [d.action for d in got] == [Action.trash, Action.keep]      # history, not last-write
    assert got[0].confidence == 0.91 and got[0].model == "jev-latest"
    assert got[1].source is Source.person


def test_plan_lifecycle_and_token(store: Store):
    plan = store.create_plan("me@example.com", "in:inbox", [
        PlanItem(message_id="m1", action=Action.archive, source=Source.rule, rule_id="r1"),
        PlanItem(message_id="m2", action=Action.trash, source=Source.jev, confidence=0.9),
    ])
    assert plan.status == "draft" and store.get_plan(plan.id).counts() == {"archive": 1, "trash": 1}
    token = store.approve_plan(plan.id)
    assert len(token) == 16
    assert store.get_plan(plan.id).status == "approved"
    store.set_plan_status(plan.id, "applied")
    assert store.get_plan(plan.id).status == "applied"


def test_journal_records_batches_and_inverse(store: Store):
    plan = store.create_plan("me@example.com", "q", [PlanItem(message_id="m1", action=Action.trash, source=Source.rule)])
    run = store.start_run(plan)
    store.journal_batch(run.id, RunBatch(index=0, op="trash", ids=["m1", "m2"], inverse={"add": ["INBOX"], "remove": ["TRASH"]}))
    again = store.get_run(run.id)
    assert again.applied == 2
    assert again.batches[0].inverse == {"add": ["INBOX"], "remove": ["TRASH"]}
    store.finish_run(run.id, "ok")
    assert store.get_run(run.id).status == "ok"
    assert store.last_run_for_plan(plan.id).id == run.id


def test_missing_plan_is_none_and_unknown_ids_are_empty(store: Store):
    assert store.get_plan("nope") is None
    assert store.get_run("nope") is None
    assert store.get_messages(["nope"]) == []


def test_corrections_are_kept_for_learning(store: Store):
    store.correct("m1", Action.keep, note="that was a client")
    rows = store.corrections()
    assert rows[0]["message_id"] == "m1" and rows[0]["action"] == "keep"


def test_summary_counts_everything(store: Store):
    store.upsert_messages([a_message("m1")])
    store.add_decisions([Decision(message_id="m1", action=Action.keep)], account="me@example.com")
    store.create_plan("me@example.com", "q", [])
    assert store.summary()["messages"] == 1
    assert store.summary()["decisions"] == 1
    assert store.summary()["plans"] == 1


def test_a_connection_string_never_prints_its_password():
    from damien.config import _mask_url
    assert _mask_url("postgres://user:s3cr3t@127.0.0.1:5433/damien") == "postgres://user:***@127.0.0.1:5433/damien"
    assert _mask_url("postgres://user@host/db") == "postgres://user@host/db"
    assert _mask_url(None) is None


def test_sql_with_a_literal_percent_works_on_both_dialects(store: Store):
    """A LIKE pattern must not be mistaken for a placeholder (psycopg reads %v)."""
    store.upsert_messages([a_message("m1")])
    row = store.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE subject LIKE ?", ("%i%",)).fetchone()
    assert row["n"] == 1
    row2 = store.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE subject LIKE ?", ("%zzz%",)).fetchone()
    assert row2["n"] == 0
