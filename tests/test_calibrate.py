"""Calibration: a model acts unattended only at a threshold measured on this inbox."""
from __future__ import annotations

import dataclasses

from damien import calibrate
from damien.decide import DEFAULT_SPEC
from damien.jev import FakeJudge
from damien.models import Action, Message, PlanItem, RunBatch, Source


def bulk(mid: str) -> Message:
    # Unread, bulk, neutral subject: nothing the guardrail would hold.
    return Message(id=mid, account="me@example.com", subject=f"Weekly digest {mid}", snippet="",
                   from_addr="news@list.example", label_ids=["INBOX", "UNREAD"],
                   list_unsubscribe=True, internal_date=1_700_000_000_000,
                   body="secret text")


def test_evaluate_picks_the_lowest_threshold_the_wrong_rate_allows(store, settings):
    ids = [f"m{i}" for i in range(11)]
    store.upsert_messages([bulk(m) for m in ids])
    for m in ids[:5]:
        store.correct(m, Action.trash)                 # the model will agree, at 0.95
    store.correct("m5", Action.keep)                   # the model will trash it, at 0.70: wrong
    for m in ids[6:10]:
        store.correct(m, Action.archive)               # the model will agree, at 0.80
    # m10 was settled by an applied rule, not a correction
    plan = store.create_plan("me@example.com", "q", [PlanItem(message_id="m10", action=Action.archive,
                                                              source=Source.rule, rule_id="r1")])
    run = store.start_run(plan)
    store.journal_batch(run.id, RunBatch(index=0, op="archive", ids=["m10"]))
    store.batch_done(run.id, 0)
    store.finish_run(run.id, "ok")

    seen: list[Message] = []

    def mapping(m: Message):
        seen.append(m)
        if m.id == "m5":
            return "promotion", 0.70
        if m.id in ids[:5]:
            return "promotion", 0.95
        return "notification", 0.80

    judge = FakeJudge(mapping)
    # eleven answers are too few to trust a bar at all
    assert calibrate.evaluate(store=store, settings=settings, spec=DEFAULT_SPEC, judge=judge)["act"] is None
    seen.clear()
    result = calibrate.evaluate(store=store, settings=settings, spec=DEFAULT_SPEC, judge=judge, min_acted=5)
    assert result["n"] == 11
    assert all(m.body is None for m in seen)           # headers only
    at = {r["t"]: r for r in result["thresholds"]}
    assert at[0.7] == {"t": 0.7, "acted": 11, "wrong": 1}
    assert result["act"] == 0.71                       # first threshold that drops the wrong trash

    path = calibrate.save(settings, result)
    assert path.name == calibrate.CALIBRATION_FILE
    assert calibrate.act_threshold(settings) == 0.71


def test_act_threshold_baseline_and_uncalibrated(settings):
    jev = dataclasses.replace(settings, judge="openrouter", model="typesafe/jev-1.13")
    assert calibrate.act_threshold(jev) == 0.9
    other = dataclasses.replace(settings, judge="openrouter", model="perplexity/pplx-decider-flash-v1.2")
    assert calibrate.act_threshold(other) is None
