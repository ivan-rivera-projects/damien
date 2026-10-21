"""The store: one SQLite file that remembers everything.

It exists because the old Damien kept jobs in two in-memory dicts and rules in a
JSON file, so a restart lost a run in flight and nothing was auditable. Every
decision, every batch applied, and every correction a person makes lives here.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from .models import (
    Action,
    Decision,
    Message,
    Plan,
    PlanItem,
    Run,
    RunBatch,
    Source,
    now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  thread_id TEXT,
  internal_date INTEGER,
  from_addr TEXT,
  from_name TEXT,
  to_addr TEXT,
  subject TEXT,
  snippet TEXT,
  label_ids TEXT NOT NULL DEFAULT '[]',
  has_attachment INTEGER NOT NULL DEFAULT 0,
  list_unsubscribe INTEGER NOT NULL DEFAULT 0,
  size_estimate INTEGER,
  seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_account_date ON messages(account, internal_date);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  account TEXT NOT NULL,
  source TEXT NOT NULL,
  action TEXT NOT NULL,
  label TEXT,
  rule_id TEXT,
  rationale TEXT,
  confidence REAL,
  probabilities TEXT,
  model TEXT,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_message ON decisions(message_id);

CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  query TEXT NOT NULL,
  status TEXT NOT NULL,
  token TEXT,
  created_at TEXT NOT NULL,
  model TEXT,
  judge TEXT
);

CREATE TABLE IF NOT EXISTS plan_items (
  plan_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  action TEXT NOT NULL,
  label TEXT,
  source TEXT NOT NULL,
  rule_id TEXT,
  confidence REAL,
  PRIMARY KEY (plan_id, message_id)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  account TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  applied INTEGER NOT NULL DEFAULT 0,
  error TEXT
);

CREATE TABLE IF NOT EXISTS run_batches (
  run_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  op TEXT NOT NULL,
  label TEXT,
  ids TEXT NOT NULL,
  inverse TEXT,
  at TEXT NOT NULL,
  PRIMARY KEY (run_id, idx)
);

CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  action TEXT NOT NULL,
  label TEXT,
  note TEXT,
  at TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- messages -----------------------------------------------------
    def upsert_messages(self, messages: Iterable[Message], seen_at: datetime | None = None) -> int:
        stamp = _iso(seen_at or now())
        rows = [
            (
                m.id, m.account, m.thread_id, m.internal_date, m.from_addr, m.from_name, m.to,
                m.subject, m.snippet, json.dumps(m.label_ids), int(m.has_attachment),
                int(m.list_unsubscribe), m.size_estimate, stamp,
            )
            for m in messages
        ]
        self.db.executemany(
            """INSERT INTO messages (id, account, thread_id, internal_date, from_addr, from_name, to_addr,
                                     subject, snippet, label_ids, has_attachment, list_unsubscribe, size_estimate, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 thread_id=excluded.thread_id, internal_date=excluded.internal_date, from_addr=excluded.from_addr,
                 from_name=excluded.from_name, to_addr=excluded.to_addr, subject=excluded.subject,
                 snippet=excluded.snippet, label_ids=excluded.label_ids, has_attachment=excluded.has_attachment,
                 list_unsubscribe=excluded.list_unsubscribe, size_estimate=excluded.size_estimate,
                 seen_at=excluded.seen_at""",
            rows,
        )
        self.db.commit()
        return len(rows)

    def get_messages(self, ids: Sequence[str]) -> list[Message]:
        out: list[Message] = []
        for chunk in _chunks(list(ids), 900):
            q = f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(chunk))})"
            for row in self.db.execute(q, chunk):
                out.append(_message(row))
        return out

    def count_messages(self, account: str | None = None) -> int:
        if account:
            return self.db.execute("SELECT COUNT(*) FROM messages WHERE account=?", (account,)).fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    # ---- decisions ----------------------------------------------------
    def add_decisions(self, decisions: Iterable[Decision], account: str = "") -> int:
        rows = [
            (d.message_id, account, d.source.value, d.action.value, d.label, d.rule_id, d.rationale,
             d.confidence, json.dumps(d.probabilities) if d.probabilities else None, d.model, _iso(d.at))
            for d in decisions
        ]
        self.db.executemany(
            """INSERT INTO decisions (message_id, account, source, action, label, rule_id, rationale,
                                      confidence, probabilities, model, at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.db.commit()
        return len(rows)

    def decisions_for(self, ids: Sequence[str]) -> list[Decision]:
        out: list[Decision] = []
        for chunk in _chunks(list(ids), 900):
            q = f"SELECT * FROM decisions WHERE message_id IN ({','.join('?' * len(chunk))}) ORDER BY id"
            for row in self.db.execute(q, chunk):
                out.append(Decision(
                    message_id=row["message_id"], action=Action(row["action"]), label=row["label"],
                    source=Source(row["source"]), rule_id=row["rule_id"], rationale=row["rationale"],
                    confidence=row["confidence"],
                    probabilities=json.loads(row["probabilities"]) if row["probabilities"] else None,
                    model=row["model"], at=datetime.fromisoformat(row["at"]),
                ))
        return out

    def decision_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    # ---- plans --------------------------------------------------------
    def create_plan(self, account: str, query: str, items: Sequence[PlanItem], *, model: str | None = None,
                    judge: str | None = None) -> Plan:
        plan = Plan(id=uuid.uuid4().hex[:12], account=account, query=query, items=list(items))
        self.db.execute(
            "INSERT INTO plans (id, account, query, status, token, created_at, model, judge) VALUES (?,?,?,?,?,?,?,?)",
            (plan.id, account, query, plan.status, plan.token, _iso(plan.created_at), model, judge),
        )
        self.db.executemany(
            """INSERT INTO plan_items (plan_id, message_id, action, label, source, rule_id, confidence)
               VALUES (?,?,?,?,?,?,?)""",
            [(plan.id, i.message_id, i.action.value, i.label, i.source.value, i.rule_id, i.confidence) for i in plan.items],
        )
        self.db.commit()
        return plan

    def get_plan(self, plan_id: str) -> Plan | None:
        row = self.db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            return None
        items = [
            PlanItem(message_id=r["message_id"], action=Action(r["action"]), label=r["label"],
                     source=Source(r["source"]), rule_id=r["rule_id"], confidence=r["confidence"])
            for r in self.db.execute("SELECT * FROM plan_items WHERE plan_id=? ORDER BY message_id", (plan_id,))
        ]
        return Plan(id=row["id"], account=row["account"], query=row["query"], status=row["status"],
                    token=row["token"], created_at=datetime.fromisoformat(row["created_at"]), items=items)

    def list_plans(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM plans ORDER BY created_at DESC LIMIT ?", (limit,)))

    def approve_plan(self, plan_id: str) -> str:
        token = uuid.uuid4().hex[:16]
        self.db.execute("UPDATE plans SET status='approved', token=? WHERE id=?", (token, plan_id))
        self.db.commit()
        return token

    def set_plan_status(self, plan_id: str, status: str) -> None:
        self.db.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))
        self.db.commit()

    def update_plan_items(self, plan_id: str, items: Sequence[PlanItem]) -> None:
        self.db.execute("DELETE FROM plan_items WHERE plan_id=?", (plan_id,))
        self.db.executemany(
            "INSERT INTO plan_items (plan_id, message_id, action, label, source, rule_id, confidence) VALUES (?,?,?,?,?,?,?)",
            [(plan_id, i.message_id, i.action.value, i.label, i.source.value, i.rule_id, i.confidence) for i in items],
        )
        self.db.commit()

    # ---- runs (the journal) -------------------------------------------
    def start_run(self, plan: Plan) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], plan_id=plan.id, account=plan.account)
        self.db.execute(
            "INSERT INTO runs (id, plan_id, account, status, started_at, applied) VALUES (?,?,?,?,?,0)",
            (run.id, run.plan_id, run.account, run.status, _iso(run.started_at)),
        )
        self.db.commit()
        return run

    def journal_batch(self, run_id: str, batch: RunBatch) -> None:
        self.db.execute(
            "INSERT INTO run_batches (run_id, idx, op, label, ids, inverse, at) VALUES (?,?,?,?,?,?,?)",
            (run_id, batch.index, batch.op, batch.label, json.dumps(batch.ids),
             json.dumps(batch.inverse) if batch.inverse else None, _iso(batch.at)),
        )
        self.db.execute("UPDATE runs SET applied = applied + ? WHERE id=?", (len(batch.ids), run_id))
        self.db.commit()

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?",
                        (status, _iso(now()), error, run_id))
        self.db.commit()

    def get_run(self, run_id: str) -> Run | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        batches = [
            RunBatch(index=r["idx"], op=r["op"], ids=json.loads(r["ids"]), label=r["label"],
                     inverse=json.loads(r["inverse"]) if r["inverse"] else None,
                     at=datetime.fromisoformat(r["at"]))
            for r in self.db.execute("SELECT * FROM run_batches WHERE run_id=? ORDER BY idx", (run_id,))
        ]
        return Run(id=row["id"], plan_id=row["plan_id"], account=row["account"], status=row["status"],
                   started_at=datetime.fromisoformat(row["started_at"]),
                   finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
                   batches=batches, applied=row["applied"], error=row["error"])

    def last_run_for_plan(self, plan_id: str) -> Run | None:
        row = self.db.execute("SELECT id FROM runs WHERE plan_id=? ORDER BY started_at DESC LIMIT 1", (plan_id,)).fetchone()
        return self.get_run(row["id"]) if row else None

    # ---- corrections ---------------------------------------------------
    def correct(self, message_id: str, action: Action, label: str | None = None, note: str | None = None) -> None:
        self.db.execute("INSERT INTO corrections (message_id, action, label, note, at) VALUES (?,?,?,?,?)",
                        (message_id, action.value, label, note, _iso(now())))
        self.db.commit()

    def corrections(self, limit: int = 200) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM corrections ORDER BY at DESC LIMIT ?", (limit,)))

    # ---- reporting -----------------------------------------------------
    def summary(self) -> dict[str, int]:
        q = self.db.execute
        return {
            "messages": q("SELECT COUNT(*) FROM messages").fetchone()[0],
            "decisions": q("SELECT COUNT(*) FROM decisions").fetchone()[0],
            "plans": q("SELECT COUNT(*) FROM plans").fetchone()[0],
            "runs": q("SELECT COUNT(*) FROM runs").fetchone()[0],
            "corrections": q("SELECT COUNT(*) FROM corrections").fetchone()[0],
        }


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"], account=row["account"], thread_id=row["thread_id"], internal_date=row["internal_date"],
        from_addr=row["from_addr"], from_name=row["from_name"], to=row["to_addr"], subject=row["subject"],
        snippet=row["snippet"], label_ids=json.loads(row["label_ids"]),
        has_attachment=bool(row["has_attachment"]), list_unsubscribe=bool(row["list_unsubscribe"]),
        size_estimate=row["size_estimate"],
    )
