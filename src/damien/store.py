"""The store: one place that remembers everything — on SQLite or on Postgres.

Why both: a bare checkout has to work with zero infrastructure (SQLite, one file),
and Damien should also be able to live in the Postgres you already run, next to
everything else, where pgvector and real backups are. The same tests run against
both, so the two never drift.

`Store(path)` for a file, `Store("postgres://…")` for a server. The SQL is written
once with `?` placeholders and translated for Postgres.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Self

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

TABLES = ("corrections", "verdicts", "message_embeddings", "run_batches", "runs", "plan_items", "plans", "decisions", "messages")

SCHEMA_SQLITE = """
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
  list_id TEXT,
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
  judge TEXT,
  judge_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  judge_calls INTEGER NOT NULL DEFAULT 0,
  judge_cached INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS verdicts (
  cache_key TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  model TEXT,
  choice TEXT NOT NULL,
  confidence DOUBLE PRECISION,
  probabilities TEXT,
  signals TEXT,
  rationale TEXT,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_embeddings (
  message_id TEXT PRIMARY KEY,
  model TEXT,
  vec TEXT NOT NULL,
  at TEXT NOT NULL
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

# Same shape, Postgres types. JSON is kept as text in both so the two dialects
# cannot drift in behaviour; these tables are read by this program, not by analysts.
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  thread_id TEXT,
  internal_date BIGINT,
  from_addr TEXT,
  from_name TEXT,
  to_addr TEXT,
  subject TEXT,
  snippet TEXT,
  label_ids TEXT NOT NULL DEFAULT '[]',
  has_attachment INTEGER NOT NULL DEFAULT 0,
  list_unsubscribe INTEGER NOT NULL DEFAULT 0,
  list_id TEXT,
  size_estimate INTEGER,
  seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_account_date ON messages(account, internal_date);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id TEXT NOT NULL,
  account TEXT NOT NULL,
  source TEXT NOT NULL,
  action TEXT NOT NULL,
  label TEXT,
  rule_id TEXT,
  rationale TEXT,
  confidence DOUBLE PRECISION,
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
  judge TEXT,
  judge_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  judge_calls INTEGER NOT NULL DEFAULT 0,
  judge_cached INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plan_items (
  plan_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  action TEXT NOT NULL,
  label TEXT,
  source TEXT NOT NULL,
  rule_id TEXT,
  confidence DOUBLE PRECISION,
  keep_in_inbox INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS verdicts (
  cache_key TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  model TEXT,
  choice TEXT NOT NULL,
  confidence DOUBLE PRECISION,
  probabilities TEXT,
  signals TEXT,
  rationale TEXT,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_embeddings (
  message_id TEXT PRIMARY KEY,
  model TEXT,
  vec TEXT NOT NULL,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id TEXT NOT NULL,
  action TEXT NOT NULL,
  label TEXT,
  note TEXT,
  at TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def is_url(target: str | Path) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


class _SqliteConn:
    """The sqlite3 surface, narrowed to what Store uses."""

    dialect = "sqlite"

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.raw = sqlite3.connect(str(path))
        self.raw.row_factory = sqlite3.Row

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        return self.raw.execute(sql, tuple(params or ()))

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self.raw.executemany(sql, [tuple(r) for r in rows])

    def script(self, ddl: str) -> None:
        self.raw.executescript(ddl)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


class _PgConn:
    """The same surface over psycopg. `?` placeholders are translated once."""

    dialect = "postgres"

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:                       # the other environment, missing an extra
            raise RuntimeError(
                "DAMIEN_DB_URL points at Postgres, but psycopg is not installed in this environment.\n"
                "  fix: uv pip install 'psycopg[binary]'   (or: uv tool install --editable '.[pg]' --force)"
            ) from exc

        self.raw = psycopg.connect(url, row_factory=dict_row)

    @staticmethod
    def _q(sql: str) -> str:
        # psycopg reads % in SQL as a placeholder, so a literal one (e.g. LIKE '%x%')
        # must be doubled. Escape first, then insert our own %s.
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        return self.raw.execute(self._q(sql), tuple(params or ()))

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self.raw.cursor() as cur:
            cur.executemany(self._q(sql), [tuple(r) for r in rows])

    def script(self, ddl: str) -> None:
        with self.raw.cursor() as cur:
            cur.execute(ddl)                       # psycopg sends a multi-statement string in one round trip

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


class Store:
    """Everything a run needs to be auditable, resumable and reversible."""

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS cannot add a
    # column to an existing table, and this tool must survive its own schema changes.
    ADDED_COLUMNS: ClassVar[dict[str, dict[str, str]]] = {
        "messages": {"list_id": "TEXT"},
        "plan_items": {"keep_in_inbox": "INTEGER NOT NULL DEFAULT 0"},
        "plans": {
            "judge_usd": "DOUBLE PRECISION NOT NULL DEFAULT 0",
            "judge_calls": "INTEGER NOT NULL DEFAULT 0",
            "judge_cached": "INTEGER NOT NULL DEFAULT 0",
        },
    }

    def __init__(self, target: Path | str):
        self.target = str(target)
        self.conn: _SqliteConn | _PgConn = _PgConn(self.target) if is_url(target) else _SqliteConn(Path(target))
        self.dialect = self.conn.dialect
        self._pgvector: bool | None = None
        self.conn.script(SCHEMA_PG if self.dialect == "postgres" else SCHEMA_SQLITE)
        self._migrate()
        self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        if self.dialect == "postgres":
            rows = self.conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))
            return {r["column_name"] for r in rows}
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        for table, columns in self.ADDED_COLUMNS.items():
            have = self._columns(table)
            for name, sql_type in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def rollback(self) -> None:
        self.conn.rollback()

    def reset(self) -> None:
        """Empty every table. For tests and for starting over on purpose."""
        for table in TABLES:
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    # ---- messages -----------------------------------------------------
    def upsert_messages(self, messages: Iterable[Message], seen_at: datetime | None = None) -> int:
        stamp = _iso(seen_at or now())
        rows = [
            (
                m.id, m.account, m.thread_id, m.internal_date, m.from_addr, m.from_name, m.to,
                m.subject, m.snippet, json.dumps(m.label_ids), int(m.has_attachment),
                int(m.list_unsubscribe), m.list_id, m.size_estimate, stamp,
            )
            for m in messages
        ]
        self.conn.executemany(
            """INSERT INTO messages (id, account, thread_id, internal_date, from_addr, from_name, to_addr,
                                     subject, snippet, label_ids, has_attachment, list_unsubscribe, list_id, size_estimate, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 thread_id=EXCLUDED.thread_id, internal_date=EXCLUDED.internal_date, from_addr=EXCLUDED.from_addr,
                 from_name=EXCLUDED.from_name, to_addr=EXCLUDED.to_addr, subject=EXCLUDED.subject,
                 snippet=EXCLUDED.snippet, label_ids=EXCLUDED.label_ids, has_attachment=EXCLUDED.has_attachment,
                 list_unsubscribe=EXCLUDED.list_unsubscribe, list_id=EXCLUDED.list_id,
                 size_estimate=EXCLUDED.size_estimate,
                 seen_at=EXCLUDED.seen_at""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_messages(self, ids: Sequence[str]) -> list[Message]:
        out: list[Message] = []
        for chunk in _chunks(list(ids), 900):
            q = f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(chunk))})"
            for row in self.conn.execute(q, chunk):
                out.append(_message(row))
        return out

    def seen_recently(self, ids: Sequence[str], since_iso: str) -> set[str]:
        """Of these ids, the ones already fetched at or after `since_iso` (ISO-8601 sorts as text)."""
        out: set[str] = set()
        for chunk in _chunks(list(ids), 900):
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT id FROM messages WHERE id IN ({placeholders}) AND seen_at >= ?", (*chunk, since_iso))
            out.update(r["id"] for r in rows)
        return out

    def count_messages(self, account: str | None = None) -> int:
        if account:
            return self.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE account=?", (account,)).fetchone()["n"]
        return self.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]

    # ---- decisions ----------------------------------------------------
    def add_decisions(self, decisions: Iterable[Decision], account: str = "") -> int:
        rows = [
            (d.message_id, account, d.source.value, d.action.value, d.label, d.rule_id, d.rationale,
             d.confidence, json.dumps(d.probabilities) if d.probabilities else None, d.model, _iso(d.at))
            for d in decisions
        ]
        self.conn.executemany(
            """INSERT INTO decisions (message_id, account, source, action, label, rule_id, rationale,
                                      confidence, probabilities, model, at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def decisions_for(self, ids: Sequence[str]) -> list[Decision]:
        out: list[Decision] = []
        for chunk in _chunks(list(ids), 900):
            q = f"SELECT * FROM decisions WHERE message_id IN ({','.join('?' * len(chunk))}) ORDER BY id"
            for row in self.conn.execute(q, chunk):
                out.append(Decision(
                    message_id=row["message_id"], action=Action(row["action"]), label=row["label"],
                    source=Source(row["source"]), rule_id=row["rule_id"], rationale=row["rationale"],
                    confidence=row["confidence"],
                    probabilities=json.loads(row["probabilities"]) if row["probabilities"] else None,
                    model=row["model"], at=datetime.fromisoformat(row["at"]),
                ))
        return out

    def decision_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]

    # ---- plans --------------------------------------------------------
    def create_plan(self, account: str, query: str, items: Sequence[PlanItem], *, model: str | None = None,
                    judge: str | None = None, judge_usd: float = 0.0, judge_calls: int = 0,
                    judge_cached: int = 0) -> Plan:
        plan = Plan(id=uuid.uuid4().hex[:12], account=account, query=query, items=list(items))
        self.conn.execute(
            "INSERT INTO plans (id, account, query, status, token, created_at, model, judge,"
            " judge_usd, judge_calls, judge_cached) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (plan.id, account, query, plan.status, plan.token, _iso(plan.created_at), model, judge,
             float(judge_usd), int(judge_calls), int(judge_cached)),
        )
        self.conn.executemany(
            """INSERT INTO plan_items (plan_id, message_id, action, label, source, rule_id, confidence, keep_in_inbox)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(plan.id, i.message_id, i.action.value, i.label, i.source.value, i.rule_id, i.confidence,
              int(i.keep_in_inbox)) for i in plan.items],
        )
        self.conn.commit()
        return plan

    def get_plan(self, plan_id: str) -> Plan | None:
        row = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            return None
        items = [
            PlanItem(message_id=r["message_id"], action=Action(r["action"]), label=r["label"],
                     source=Source(r["source"]), rule_id=r["rule_id"], confidence=r["confidence"],
                     keep_in_inbox=bool(r["keep_in_inbox"]))
            for r in self.conn.execute("SELECT * FROM plan_items WHERE plan_id=? ORDER BY message_id", (plan_id,))
        ]
        return Plan(id=row["id"], account=row["account"], query=row["query"], status=row["status"],
                    token=row["token"], created_at=datetime.fromisoformat(row["created_at"]), items=items,
                    model=row["model"], judge=row["judge"], judge_usd=float(row["judge_usd"] or 0.0),
                    judge_calls=int(row["judge_calls"] or 0), judge_cached=int(row["judge_cached"] or 0))

    def list_plans(self, limit: int = 20) -> list[Any]:
        return list(self.conn.execute("SELECT * FROM plans ORDER BY created_at DESC LIMIT ?", (limit,)))

    def approve_plan(self, plan_id: str) -> str:
        token = uuid.uuid4().hex[:16]
        self.conn.execute("UPDATE plans SET status='approved', token=? WHERE id=?", (token, plan_id))
        self.conn.commit()
        return token

    def set_plan_status(self, plan_id: str, status: str) -> None:
        self.conn.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))
        self.conn.commit()

    def update_plan_items(self, plan_id: str, items: Sequence[PlanItem]) -> None:
        self.conn.execute("DELETE FROM plan_items WHERE plan_id=?", (plan_id,))
        self.conn.executemany(
            "INSERT INTO plan_items (plan_id, message_id, action, label, source, rule_id, confidence, keep_in_inbox)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(plan_id, i.message_id, i.action.value, i.label, i.source.value, i.rule_id, i.confidence,
              int(i.keep_in_inbox)) for i in items],
        )
        self.conn.commit()

    # ---- runs (the journal) -------------------------------------------
    def start_run(self, plan: Plan) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], plan_id=plan.id, account=plan.account)
        self.conn.execute(
            "INSERT INTO runs (id, plan_id, account, status, started_at, applied) VALUES (?,?,?,?,?,0)",
            (run.id, run.plan_id, run.account, run.status, _iso(run.started_at)),
        )
        self.conn.commit()
        return run

    def journal_batch(self, run_id: str, batch: RunBatch) -> None:
        self.conn.execute(
            "INSERT INTO run_batches (run_id, idx, op, label, ids, inverse, at) VALUES (?,?,?,?,?,?,?)",
            (run_id, batch.index, batch.op, batch.label, json.dumps(batch.ids),
             json.dumps(batch.inverse) if batch.inverse else None, _iso(batch.at)),
        )
        self.conn.execute("UPDATE runs SET applied = applied + ? WHERE id=?", (len(batch.ids), run_id))
        self.conn.commit()

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        self.conn.execute("UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?",
                          (status, _iso(now()), error, run_id))
        self.conn.commit()

    def get_run(self, run_id: str) -> Run | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        batches = [
            RunBatch(index=r["idx"], op=r["op"], ids=json.loads(r["ids"]), label=r["label"],
                     inverse=json.loads(r["inverse"]) if r["inverse"] else None,
                     at=datetime.fromisoformat(r["at"]))
            for r in self.conn.execute("SELECT * FROM run_batches WHERE run_id=? ORDER BY idx", (run_id,))
        ]
        return Run(id=row["id"], plan_id=row["plan_id"], account=row["account"], status=row["status"],
                   started_at=datetime.fromisoformat(row["started_at"]),
                   finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
                   batches=batches, applied=row["applied"], error=row["error"])

    def last_run_for_plan(self, plan_id: str) -> Run | None:
        row = self.conn.execute("SELECT id FROM runs WHERE plan_id=? ORDER BY started_at DESC LIMIT 1", (plan_id,)).fetchone()
        return self.get_run(row["id"]) if row else None

    # ---- verdict cache (so an unchanged message is never judged twice) ---
    def get_verdict(self, cache_key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM verdicts WHERE cache_key = ?", (cache_key,)).fetchone()
        if not row:
            return None
        return {
            "message_id": row["message_id"], "choice": row["choice"], "confidence": row["confidence"],
            "probabilities": json.loads(row["probabilities"]) if row["probabilities"] else None,
            "signals": json.loads(row["signals"]) if row["signals"] else None,
            "rationale": row["rationale"],
        }

    def put_verdict(self, cache_key: str, message_id: str, model: str | None, verdict: dict) -> None:
        self.conn.execute(
            """INSERT INTO verdicts (cache_key, message_id, model, choice, confidence, probabilities, signals, rationale, at)
               VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
                 choice=EXCLUDED.choice, confidence=EXCLUDED.confidence, probabilities=EXCLUDED.probabilities,
                 signals=EXCLUDED.signals, rationale=EXCLUDED.rationale, at=EXCLUDED.at""",
            (cache_key, message_id, model, verdict.get("choice"), verdict.get("confidence"),
             json.dumps(verdict.get("probabilities")) if verdict.get("probabilities") else None,
             json.dumps(verdict.get("signals")) if verdict.get("signals") else None,
             verdict.get("rationale"), _iso(now())))
        self.conn.commit()

    def verdict_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM verdicts").fetchone()["n"]

    # ---- embeddings (pgvector where it is available) --------------------
    def has_pgvector(self) -> bool:
        """Turn on the extension if this is Postgres and we may. The column stays text,
        so nothing breaks without it — only the distance queries need the cast."""
        if self.dialect != "postgres" or self._pgvector is not None:
            return bool(self._pgvector)
        try:
            self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self.conn.commit()
            self._pgvector = True
        except Exception:
            self.conn.rollback()
            self._pgvector = False
        return bool(self._pgvector)

    def put_embeddings(self, rows: Sequence[tuple[str, str, Sequence[float]]], model: str) -> int:
        self.conn.executemany(
            """INSERT INTO message_embeddings (message_id, model, vec, at) VALUES (?,?,?,?)
               ON CONFLICT(message_id) DO UPDATE SET model=EXCLUDED.model, vec=EXCLUDED.vec, at=EXCLUDED.at""",
            [(mid, model, json.dumps([round(float(x), 6) for x in vec]), _iso(now())) for mid, _model, vec in rows],
        )
        self.conn.commit()
        return len(rows)

    def get_embeddings(self, ids: Sequence[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for chunk in _chunks(list(ids), 900):
            q = f"SELECT message_id, vec FROM message_embeddings WHERE message_id IN ({','.join('?' * len(chunk))})"
            for row in self.conn.execute(q, chunk):
                out[row["message_id"]] = json.loads(row["vec"])
        return out

    def missing_embeddings(self, ids: Sequence[str]) -> list[str]:
        have = set(self.get_embeddings(ids))
        return [i for i in ids if i not in have]

    def similar_to(self, message_id: str, limit: int = 10) -> list[tuple[str, float]]:
        """Nearest messages by cosine distance. Postgres + pgvector only."""
        if not self.has_pgvector():
            raise RuntimeError("similar_to needs Postgres with pgvector")
        rows = self.conn.execute(
            """SELECT message_id, 1 - (vec::vector <=> (SELECT vec::vector FROM message_embeddings WHERE message_id = ?)) AS sim
                 FROM message_embeddings WHERE message_id <> ?
                ORDER BY vec::vector <=> (SELECT vec::vector FROM message_embeddings WHERE message_id = ?)
                LIMIT ?""",
            (message_id, message_id, message_id, limit))
        return [(r["message_id"], float(r["sim"])) for r in rows]

    def embedding_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM message_embeddings").fetchone()["n"]

    # ---- corrections ---------------------------------------------------
    def correct(self, message_id: str, action: Action, label: str | None = None, note: str | None = None) -> None:
        self.conn.execute("INSERT INTO corrections (message_id, action, label, note, at) VALUES (?,?,?,?,?)",
                          (message_id, action.value, label, note, _iso(now())))
        self.conn.commit()

    def corrections(self, limit: int = 200) -> list[Any]:
        return list(self.conn.execute("SELECT * FROM corrections ORDER BY at DESC LIMIT ?", (limit,)))

    # ---- reporting -----------------------------------------------------
    def summary(self) -> dict[str, int]:
        q = self.conn.execute
        spend = q("SELECT COALESCE(SUM(judge_usd), 0) AS usd, COALESCE(SUM(judge_calls), 0) AS calls,"
                  " COALESCE(SUM(judge_cached), 0) AS cached FROM plans").fetchone()
        return {
            "messages": q("SELECT COUNT(*) AS n FROM messages").fetchone()["n"],
            "decisions": q("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"],
            "plans": q("SELECT COUNT(*) AS n FROM plans").fetchone()["n"],
            "runs": q("SELECT COUNT(*) AS n FROM runs").fetchone()["n"],
            "corrections": q("SELECT COUNT(*) AS n FROM corrections").fetchone()["n"],
            "judged": int(spend["calls"] or 0),
            "from_cache": int(spend["cached"] or 0),
            "spent_usd": f"${float(spend['usd'] or 0):.4f}",
        }


class VerdictCache:
    """The store, narrowed to what a judge needs: remember an answer, look it up."""

    def __init__(self, store: Store):
        self.store = store

    def get(self, cache_key: str) -> dict | None:
        return self.store.get_verdict(cache_key)

    def put(self, cache_key: str, message_id: str, model: str | None, verdict: dict) -> None:
        self.store.put_verdict(cache_key, message_id, model, verdict)


def open_store(settings: Any) -> Store:
    """The one place that decides where state lives: a Postgres URL if given, else the file."""
    url = getattr(settings, "db_url", None)
    return Store(url) if url else Store(settings.db_path)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _message(row: Any) -> Message:
    return Message(
        id=row["id"], account=row["account"], thread_id=row["thread_id"], internal_date=row["internal_date"],
        from_addr=row["from_addr"], from_name=row["from_name"], to=row["to_addr"], subject=row["subject"],
        snippet=row["snippet"], label_ids=json.loads(row["label_ids"]),
        has_attachment=bool(row["has_attachment"]), list_unsubscribe=bool(row["list_unsubscribe"]),
        list_id=row["list_id"], size_estimate=row["size_estimate"],
    )
