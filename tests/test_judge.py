"""The judgment layer: the questions we ask, what we do with the answers, and
the rule that an unsure model never acts."""

from __future__ import annotations

import dataclasses
import json
from typing import ClassVar

import httpx

from damien.decide import (
    DEFAULT_SPEC,
    decide_all,
    enforce_confidence,
)
from damien.jev import FakeJudge, JevJudge, build_questions, message_state, parse_answer
from damien.models import Action, Decision, Message, Source
from damien.rules import Condition, Rule


def a_message(mid: str = "m1", **kw) -> Message:
    base = {"id": mid, "account": "me@example.com", "subject": "Your order confirmation",
            "snippet": "Thanks for your order", "from_addr": "noreply@shop.example",
            "from_name": "Shop", "label_ids": ["INBOX"], "internal_date": 1_700_000_000_000}
    base.update(kw)
    return Message(**base)


def test_state_is_small_and_has_no_body():
    state = message_state(a_message(), "a producer's inbox")
    assert set(state) == {"message", "reader"}
    assert set(state["message"]) == {"from", "subject", "snippet", "has_attachment", "is_mailing_list"}
    assert state["reader"] == "a producer's inbox"
    assert "gmail_category" not in state["message"]      # only when Gmail said something
    assert "your_labels" not in state["message"]         # only when the person did


def test_state_carries_googles_category_and_the_persons_own_labels():
    m = a_message(label_ids=["INBOX", "CATEGORY_PROMOTIONS", "Label_30"])
    state = message_state(m, "", {"Label_30": "Bills"})
    assert state["message"]["gmail_category"] == "promotions"
    assert state["message"]["your_labels"] == ["Bills"]


def test_questions_are_atomic_and_within_the_documented_limits():
    questions = build_questions(DEFAULT_SPEC)
    assert questions["bucket"]["type"] == "choice"
    assert len(questions["bucket"]["criteria"]) <= 255            # Choice cap
    assert questions["importance"]["type"] == "score"
    assert 2 <= len(questions["importance"]["criteria"]) <= 10    # Score cap
    # one judgment per question: no "and" anywhere in an instruction
    for q in questions.values():
        assert " and " not in q["instructions"].lower()


def test_parse_answer_keeps_probabilities_and_notes():
    answers = {
        "bucket": {"type": "choice", "choice": "promotion", "confidence": 0.93,
                   "probabilities": {"promotion": 0.93, "keep": 0.05, "receipt": 0.02}},
        "handled_by_a_machine": {"type": "noul", "noul": 0.97},
        "wants_a_reply": {"type": "noul", "noul": 0.02},
        "about_their_work": {"type": "noul", "noul": 0.21},
        "importance": {"type": "score", "score": 0.4, "confidence": 0.8},
    }
    verdict = parse_answer("m1", answers)
    assert verdict.choice == "promotion"
    assert verdict.confidence == 0.93
    assert verdict.probabilities == {"promotion": 0.93, "keep": 0.05, "receipt": 0.02}
    assert "machine-sent p=0.97" in verdict.rationale
    assert "importance=0.40" in verdict.rationale


def test_parse_answer_survives_a_thin_response():
    verdict = parse_answer("m1", {})
    assert verdict.choice == "unclear" and verdict.confidence is None and verdict.rationale is None


def test_fake_judge_maps_and_refuses_unknown_buckets():
    judge = FakeJudge()
    out = judge.judge([a_message("a", list_unsubscribe=True), a_message("b", subject="Big SALE 50% off"),
                       a_message("c", subject="dinner?", from_addr="alex@studio.example")], DEFAULT_SPEC.criteria())
    assert [v.choice for v in out] == ["newsletter", "promotion", "keep"]
    only_receipts = FakeJudge(lambda m: ("nonsense", 0.99))
    assert only_receipts.judge([a_message()], {"receipt": "x"})[0].choice == "unclear"


def test_rules_win_and_the_judge_only_sees_the_residue():
    rules = [Rule(id="r1", name="receipts", action=Action.label, label="Receipts",
                  match=[Condition(field="subject", op="contains", value="order confirmation")])]

    class Counting(FakeJudge):
        seen: ClassVar[list[str]] = []

        def judge(self, messages, buckets, *, context="", label_names=None):
            Counting.seen = [m.id for m in messages]
            return super().judge(messages, buckets, context=context, label_names=label_names)

    decisions = decide_all([a_message("m1"), a_message("m2", subject="Big SALE 50% off")],
                           rules=rules, judge=Counting())
    by_id = {d.message_id: d for d in decisions}
    assert by_id["m1"].source is Source.rule and by_id["m1"].label == "Receipts"
    assert Counting.seen == ["m2"]


def test_an_unsure_judgment_never_acts():
    unsure = Decision(message_id="m1", action=Action.trash, source=Source.jev, confidence=0.41,
                      rationale="importance=0.2")
    sure = Decision(message_id="m2", action=Action.trash, source=Source.jev, confidence=0.95)
    by_rule = Decision(message_id="m3", action=Action.trash, source=Source.rule, confidence=1.0)
    out = enforce_confidence([unsure, sure, by_rule], ask_below=0.55)
    assert out[0].action is Action.keep and "left for a person" in out[0].rationale
    assert out[0].source is Source.jev                      # the judgment is still recorded
    assert out[1].action is Action.trash
    assert out[2].action is Action.trash                    # rules are never second-guessed


def test_missing_confidence_also_waits_for_a_person():
    vague = Decision(message_id="m1", action=Action.archive, source=Source.jev, confidence=None)
    assert enforce_confidence([vague], ask_below=0.55)[0].action is Action.keep


def test_one_request_per_message_pooled_and_cost_tracked(settings):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        mid = body["state"]["message"]["from"]
        return httpx.Response(200, json={
            "model": body["model"], "provider": "typesafe",
            "answers": {"bucket": {"type": "choice", "choice": "newsletter", "confidence": 0.9,
                                   "probabilities": {"newsletter": 0.9, "keep": 0.1}},
                        "importance": {"type": "score", "score": 0.2, "confidence": 0.7}},
            "usage": {"input_tokens": 400, "output_tokens": 0, "cost": 0.00002, "_from": mid},
        })

    live = dataclasses.replace(settings, judge="openrouter", api_key="test-key", model="typesafe/jev-1.13")
    judge = JevJudge(live, DEFAULT_SPEC, transport=httpx.MockTransport(handler))
    messages = [a_message(f"m{i}", from_addr=f"news{i}@list.example") for i in range(3)]
    verdicts = judge.judge(messages, DEFAULT_SPEC.criteria(), context="a producer's inbox")

    assert len(seen) == 3                                   # one request per message
    assert all(list(b["questions"]) == list(build_questions(DEFAULT_SPEC)) for b in seen)   # all questions together
    assert all(b["model"] == "typesafe/jev-1.13" for b in seen)
    assert all(set(b["state"]) == {"message", "reader"} for b in seen)                      # no batch of emails in state
    assert all(b["session_id"] == "damien" for b in seen)
    assert judge.calls == 3 and round(judge.spent_usd, 8) == round(0.00006, 8)
    assert [v.choice for v in verdicts] == ["newsletter"] * 3


def test_a_failing_message_becomes_unclear_not_a_crash(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"loc": ["body", "questions"], "msg": "bad"}]})

    live = dataclasses.replace(settings, judge="openrouter", api_key="test-key")
    judge = JevJudge(live, DEFAULT_SPEC, transport=httpx.MockTransport(handler))
    [verdict] = judge.judge([a_message()], DEFAULT_SPEC.criteria())
    assert verdict.choice == "unclear" and verdict.confidence == 0.0
    assert "judgment failed" in verdict.rationale


def test_spec_buckets_all_map_to_an_action():
    for bucket in DEFAULT_SPEC.buckets:
        assert isinstance(bucket.action, Action)
        if bucket.action is Action.label:
            assert bucket.label, f"{bucket.key} labels but names no label"


# ---- the verdict cache -------------------------------------------------------
def test_the_cache_key_is_the_request(settings):
    live = dataclasses.replace(settings, judge="openrouter", api_key="k", model="typesafe/jev-1.13")
    judge = JevJudge(live, DEFAULT_SPEC)
    m = a_message("Your receipt")
    key = judge.verdict_key(m, "ctx", None)
    assert key == judge.verdict_key(a_message("Your receipt"), "ctx", None)      # same state: same key
    assert key != judge.verdict_key(a_message("Your receipt", snippet="other"), "ctx", None)
    assert key != judge.verdict_key(m, "another reader", None)
    # a label the person put on it is part of the state — and so is its name
    labelled = a_message("Your receipt", label_ids=["INBOX", "Label_1"])
    assert judge.verdict_key(labelled, "ctx", None) != judge.verdict_key(labelled, "ctx", {"Label_1": "Bills"})
    assert judge.verdict_key(labelled, "ctx", {"Label_1": "Bills"}) != judge.verdict_key(labelled, "ctx", {})
    other_model = dataclasses.replace(live, model="typesafe/jev-1.14")
    assert key != JevJudge(other_model, DEFAULT_SPEC).verdict_key(m, "ctx", None)


def test_an_unchanged_message_is_never_judged_twice(settings, tmp_path):
    from damien.store import Store, VerdictCache

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["state"]["message"]["subject"])
        return httpx.Response(200, json={
            "model": body["model"], "provider": "typesafe",
            "answers": {"bucket": {"type": "choice", "choice": "receipt", "confidence": 0.9,
                                   "probabilities": {"receipt": 0.9}},
                        "importance": {"type": "score", "score": 1.0, "confidence": 0.7}},
            "usage": {"input_tokens": 100, "output_tokens": 0, "cost": 0.00001},
        })

    live = dataclasses.replace(settings, judge="openrouter", api_key="k")
    store = Store(tmp_path / "c.db")
    try:
        judge = JevJudge(live, DEFAULT_SPEC, transport=httpx.MockTransport(handler),
                         cache=VerdictCache(store))
        messages = [a_message(f"m{i}", subject=f"Your receipt {i}") for i in range(3)]
        first = judge.judge(messages, DEFAULT_SPEC.criteria())
        assert judge.calls == 3 and judge.cached == 0 and len(seen) == 3

        judge2 = JevJudge(live, DEFAULT_SPEC, transport=httpx.MockTransport(handler),
                          cache=VerdictCache(store))
        second = judge2.judge(messages, DEFAULT_SPEC.criteria())
        assert judge2.calls == 0 and judge2.cached == 3 and len(seen) == 3   # nothing was sent
        assert [v.model_dump() for v in second] == [v.model_dump() for v in first]
        assert [v.choice for v in second] == ["receipt"] * 3
    finally:
        store.close()


def test_a_changed_message_is_judged_again(settings, tmp_path):
    from damien.store import Store, VerdictCache

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={
            "model": "typesafe/jev-1.13", "provider": "typesafe",
            "answers": {"bucket": {"type": "choice", "choice": "keep", "confidence": 0.9}},
            "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0},
        })

    live = dataclasses.replace(settings, judge="openrouter", api_key="k")
    store = Store(tmp_path / "c2.db")
    try:
        cache = VerdictCache(store)
        JevJudge(live, DEFAULT_SPEC, transport=httpx.MockTransport(handler), cache=cache).judge(
            [a_message("m1", subject="before")], DEFAULT_SPEC.criteria())
        JevJudge(live, DEFAULT_SPEC, transport=httpx.MockTransport(handler), cache=cache).judge(
            [a_message("m1", subject="after")], DEFAULT_SPEC.criteria())
        assert calls["n"] == 2                       # the state changed, so it was bought again
    finally:
        store.close()


def test_the_spec_names_the_persons_world_and_policy():
    spec = DEFAULT_SPEC
    text = spec.context.lower()
    for word in ("streetwear", "fashion", "music", "artificial intelligence", "aws"):
        assert word in text, word
    keep = next(b for b in spec.buckets if b.key == "keep")
    assert "fashion" in keep.description.lower()
    assert "ai" in keep.description.lower()                       # AI and technology stay in the inbox
    promo = next(b for b in spec.buckets if b.key == "promotion")
    assert "brand he follows" in promo.description.lower()       # a brand he follows is never a promo
    newsletter = next(b for b in spec.buckets if b.key == "newsletter")
    assert "those belong in the inbox" in newsletter.description.lower()
