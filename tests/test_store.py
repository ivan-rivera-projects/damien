"""The store: everything a run needs to be auditable, resumable and reversible."""

from __future__ import annotations

import sqlite3

from damien.models import Action, Decision, Message, Plan, PlanItem, RunBatch, Source
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


def test_signals_are_stored_and_the_newest_non_null_wins(store: Store):
    store.add_decisions([
        Decision(message_id="m1", action=Action.keep, signals={"wants_a_reply": 0.2}),
        Decision(message_id="m1", action=Action.keep, signals={"wants_a_reply": 0.8}),
        Decision(message_id="m1", action=Action.keep),                      # a rule decision: no signals
    ])
    assert store.decision_signals(["m1", "m2"]) == {"m1": {"wants_a_reply": 0.8}}
    assert store.decisions_for(["m1"])[1].signals == {"wants_a_reply": 0.8}


def test_plan_items_remember_the_state_undo_needs(store: Store):
    plan = store.create_plan("me@example.com", "q", [
        PlanItem(message_id="m1", action=Action.label, label="X", was_in_inbox=False, was_unread=True, had_label=True),
        PlanItem(message_id="m2", action=Action.archive),
    ])
    got = {i.message_id: i for i in store.get_plan(plan.id).items}
    assert (got["m1"].was_in_inbox, got["m1"].was_unread, got["m1"].had_label) == (False, True, True)
    assert (got["m2"].was_in_inbox, got["m2"].was_unread, got["m2"].had_label) == (None, None, None)


def test_plan_lifecycle_and_token(store: Store):
    plan = store.create_plan("me@example.com", "in:inbox", [
        PlanItem(message_id="m1", action=Action.archive, source=Source.rule, rule_id="r1"),
        PlanItem(message_id="m2", action=Action.trash, source=Source.jev, confidence=0.9),
    ])
    assert plan.status == "draft" and store.get_plan(plan.id).counts() == {"archive": 1, "trash": 1}
    token = store.approve_plan(plan.id)
    assert len(token) == 16
    stored = store.get_plan(plan.id)
    assert stored.status == "approved"
    assert stored.token != token and token not in str(stored.token)    # a database reader cannot apply it
    assert store.check_token(stored, token)
    assert not store.check_token(stored, stored.token)                   # the hash itself is not a token
    assert not store.check_token(stored, "0" * 16)
    store.set_plan_status(plan.id, "applied")
    assert store.get_plan(plan.id).status == "applied"


def test_journal_records_batches_and_inverse(store: Store):
    plan = store.create_plan("me@example.com", "q", [PlanItem(message_id="m1", action=Action.trash, source=Source.rule)])
    run = store.start_run(plan)
    store.journal_batch(run.id, RunBatch(index=0, op="trash", ids=["m1", "m2"], inverse={"add": ["INBOX"], "remove": ["TRASH"]}))
    again = store.get_run(run.id)
    assert again.applied == 0 and again.batches[0].state == "pending"   # intent, not yet a fact
    assert again.batches[0].inverse == {"add": ["INBOX"], "remove": ["TRASH"]}
    store.batch_done(run.id, 0)
    store.batch_done(run.id, 0)                                          # idempotent
    again = store.get_run(run.id)
    assert again.applied == 2 and again.batches[0].state == "done"
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


def test_latest_corrections_has_every_message_newest_first(store: Store):
    for i in range(250):
        store.correct(f"m{i}", Action.archive)
    store.correct("m0", Action.keep, note="changed my mind")
    latest = store.latest_corrections()
    assert len(latest) == 250                                            # no silent limit
    assert latest["m0"]["action"] == "keep" and latest["m0"]["note"] == "changed my mind"


def test_a_legacy_plaintext_token_is_still_accepted(store: Store):
    plan = store.create_plan("me@example.com", "q", [PlanItem(message_id="m1", action=Action.archive)])
    store.conn.execute("UPDATE plans SET status='approved', token=? WHERE id=?", ("abcdef0123456789", plan.id))
    store.conn.commit()
    assert store.check_token(store.get_plan(plan.id), "abcdef0123456789")


V03_SCHEMA = """
CREATE TABLE plans (id TEXT PRIMARY KEY, account TEXT NOT NULL, query TEXT NOT NULL, status TEXT NOT NULL,
  token TEXT, created_at TEXT NOT NULL, model TEXT, judge TEXT);
CREATE TABLE plan_items (plan_id TEXT NOT NULL, message_id TEXT NOT NULL, action TEXT NOT NULL, label TEXT,
  source TEXT NOT NULL, rule_id TEXT, confidence REAL, PRIMARY KEY (plan_id, message_id));
CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL, account TEXT NOT NULL,
  source TEXT NOT NULL, action TEXT NOT NULL, label TEXT, rule_id TEXT, rationale TEXT, confidence REAL,
  probabilities TEXT, model TEXT, at TEXT NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, account TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, applied INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE run_batches (run_id TEXT NOT NULL, idx INTEGER NOT NULL, op TEXT NOT NULL, label TEXT,
  ids TEXT NOT NULL, inverse TEXT, at TEXT NOT NULL, PRIMARY KEY (run_id, idx));
"""


def test_a_database_from_0_3_opens_and_round_trips_a_plan(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(V03_SCHEMA)
    raw.execute("INSERT INTO runs VALUES ('r0','p0','me','ok','2025-01-01T00:00:00+00:00',NULL,1,NULL)")
    raw.execute("INSERT INTO run_batches VALUES ('r0',0,'archive',NULL,'[\"m1\"]','{\"add\": [\"INBOX\"]}',"
                "'2025-01-01T00:00:00+00:00')")
    raw.commit()
    raw.close()
    with Store(path) as store:
        plan = store.create_plan("me", "q", [PlanItem(message_id="m1", action=Action.archive, was_in_inbox=True)])
        store.add_decisions([Decision(message_id="m1", action=Action.keep, signals={"wants_a_reply": 0.5})])
        got: Plan = store.get_plan(plan.id)
        assert got.items[0].was_in_inbox is True
        token = store.approve_plan(plan.id)
        assert store.check_token(store.get_plan(plan.id), token)
        old = store.get_run("r0")
        assert old.batches[0].state == "done"                           # an old journal row counts as landed
        run = store.start_run(got, "sig")
        assert store.last_run_for_plan(plan.id, "sig").id == run.id


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
