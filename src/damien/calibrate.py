"""Calibration: the confidence at which *this* model, on *this* host, may act unattended.

A decision model's confidence describes its answer, not its correctness, and two models
(or one model on two hosts) do not mean the same thing by 0.9. So the threshold is measured
against what this inbox already settled — a person's corrections and the rules' applied
work — and stored per model@host. A model without a measurement does not act on its own.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .decide import JudgementSpec, decide_message
from .models import Action

CALIBRATION_FILE = "calibration.json"

# The pass ran jev-1.13 at 0.9 before calibration existed; keep that, on any host.
BASELINE_MODEL = "typesafe/jev-1.13"
BASELINE_ACT = 0.9


def _path(settings: Settings) -> Path:
    return Path(settings.root) / CALIBRATION_FILE


def _entry_key(settings: Settings) -> str:
    return f"{settings.model}@{settings.judge_host() or ''}"


def _load(settings: Settings) -> dict[str, Any]:
    try:
        return json.loads(_path(settings).read_text())
    except (OSError, ValueError):
        return {}


def act_threshold(settings: Settings) -> float | None:
    """The calibrated threshold for the current model+host, the baseline's, or None (uncalibrated)."""
    entry = _load(settings).get(_entry_key(settings))
    if isinstance(entry, dict) and isinstance(entry.get("act"), (int, float)):
        return float(entry["act"])
    if settings.model == BASELINE_MODEL:
        return BASELINE_ACT
    return None


def _truth(store) -> dict[str, tuple[str, str | None]]:
    """message_id -> (action, label) that a person or a rule settled, newest person correction winning."""
    truth: dict[str, tuple[str, str | None]] = {}
    runs = store.conn.execute(
        "SELECT id, plan_id FROM runs WHERE status IN ('ok', 'partial') ORDER BY started_at").fetchall()
    for run in runs:
        applied: set[str] = set()
        for batch in store.conn.execute("SELECT * FROM run_batches WHERE run_id=?", (run["id"],)).fetchall():
            if "state" in batch.keys() and batch["state"] not in (None, "done"):
                continue
            applied.update(json.loads(batch["ids"]))
        if not applied:
            continue
        for item in store.conn.execute(
                "SELECT message_id, action, label FROM plan_items WHERE plan_id=? AND source IN ('rule', 'person')",
                (run["plan_id"],)).fetchall():
            if item["message_id"] in applied:
                truth[item["message_id"]] = (item["action"], item["label"])
    for mid, row in store.latest_corrections().items():
        truth[mid] = (row["action"], row.get("label"))
    return truth


MIN_ACTED = 30          # a bar set on a handful of answers is a guess, not a calibration


def evaluate(*, store, settings: Settings, spec: JudgementSpec, judge, limit: int = 500,
             max_wrong_rate: float = 0.02, min_acted: int = MIN_ACTED) -> dict:
    """Re-judge settled messages (headers only, no rules) and find the lowest safe threshold.

    The full decide_message runs, guardrail included, because that is what would act;
    no confidence floor is applied, so every threshold can be read off one pass.

    "Safe" means: at least `min_acted` messages would have been acted on at that bar, and
    the wrong rate stays within `max_wrong_rate` there *and* at every higher bar with as
    much evidence — so the chosen bar never sits below a worse one.
    """
    truth = _truth(store)
    ids = list(truth)[-limit:] if limit else list(truth)
    messages = [m.model_copy(update={"body": None}) for m in store.get_messages(ids)]
    judged: list[tuple[float, bool]] = []          # (confidence, wrong) for every non-keep action
    criteria = spec.criteria()
    batch = max(1, settings.judge_batch)
    for start in range(0, len(messages), batch):
        chunk: Sequence = messages[start:start + batch]
        verdicts = judge.judge(chunk, criteria, context=spec.context)
        for message, verdict in zip(chunk, verdicts, strict=False):
            if message.id in getattr(judge, "failed", ()):
                continue
            d = decide_message(message, rules=(), verdict=verdict, spec=spec, model=settings.model,
                               protected_labels=settings.protect_labels)
            if d.action is Action.keep:
                continue
            action, label = truth[message.id]
            wrong = d.action.value != action or (d.action is Action.label and d.label != label)
            judged.append((d.confidence or 0.0, wrong))

    thresholds = []
    for step in range(50, 100):
        t = step / 100
        acted = [w for c, w in judged if c >= t]
        thresholds.append({"t": t, "acted": len(acted), "wrong": sum(acted)})
    act = None
    for row in reversed(thresholds):                 # from the strictest bar down
        if row["acted"] < min_acted:
            continue
        if row["wrong"] / row["acted"] > max_wrong_rate:
            break
        act = row["t"]
    return {"n": len(messages), "thresholds": thresholds, "act": act}


def save(settings: Settings, result: dict) -> Path:
    """Record the measurement for this model@host; a result with no safe threshold removes it."""
    data = _load(settings)
    key = _entry_key(settings)
    act = result.get("act")
    if act is None:
        data.pop(key, None)
    else:
        row = next((r for r in result.get("thresholds", []) if r["t"] == act), {})
        data[key] = {"act": act, "n": result.get("n", 0), "wrong": row.get("wrong", 0),
                     "at": datetime.now(UTC).isoformat(timespec="seconds")}
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return path
