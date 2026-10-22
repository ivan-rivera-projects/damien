"""Reading the body, but only where the snippet was not enough.

This is the one mode where message text reaches the judge, so the tests that matter are:
it is off unless asked for, it reads only the messages it could not decide, what it read is
kept locally so a re-plan costs nothing, and the escalated answer is cached apart from the
snippet-only answer.
"""
from __future__ import annotations

from damien.decide import DEFAULT_SPEC, Verdict
from damien.jev import JevJudge
from damien.plan import build_plan
from damien.store import Store


class BodyAwareJudge:
    """Answers `unclear` from a snippet, and settles when it can read the text — the exact
    situation escalation exists for."""

    name = "body-aware"

    def __init__(self) -> None:
        self.seen: list[tuple[str, bool]] = []

    def judge(self, messages, buckets, *, context="", label_names=None):
        out = []
        for m in messages:
            self.seen.append((m.id, bool(m.body)))
            if m.body and "invoice" in m.body.lower():
                out.append(Verdict(message_id=m.id, choice="receipt", confidence=0.95,
                                   rationale="the text says invoice"))
            else:
                out.append(Verdict(message_id=m.id, choice="unclear", confidence=0.10,
                                   rationale="the snippet was not enough"))
        return out


def a_plan(store: Store, gmail, fake, judge, *, read_bodies: str):
    fake.add("m1", subject="quick question", sender="a@client.example", body="Hi Ivan, invoice attached.")
    fake.add("m2", subject="newsletter", sender="news@list.example", snippet="weekly",
             list_unsub=True)
    stats: dict[str, int] = {}
    plan = build_plan(store=store, gmail=gmail, rules=[], judge=judge, query="in:inbox",
                      spec=DEFAULT_SPEC, stats=stats, read_bodies=read_bodies)
    return plan, stats


def test_escalation_is_off_unless_asked_for(store: Store, gmail, fake):
    judge = BodyAwareJudge()
    plan, stats = a_plan(store, gmail, fake, judge, read_bodies="off")
    assert all(not body for _, body in judge.seen)          # no body ever reached the judge
    assert stats.get("escalated") is None
    assert all(i.action.value == "keep" for i in plan.items)   # unclear -> keep


def test_escalation_reads_only_the_undecided_mail_and_then_settles_it(store: Store, gmail, fake):
    judge = BodyAwareJudge()
    plan, stats = a_plan(store, gmail, fake, judge, read_bodies="on_escalation")
    assert stats["escalated"] == 1                          # m1 only: m2 is a rule-free newsletter
    assert [(mid, body) for mid, body in judge.seen] == [("m1", False), ("m2", False), ("m1", True)]
    decided = {i.message_id: i for i in plan.items}
    assert decided["m1"].label == "Receipts"                # settled by reading the text
    assert decided["m1"].source.value == "jev"
    assert (decided["m1"].confidence or 0) >= 0.9


def test_what_it_read_is_kept_locally_so_a_re_plan_costs_no_gmail_call(store: Store, gmail, fake):
    judge = BodyAwareJudge()
    a_plan(store, gmail, fake, judge, read_bodies="on_escalation")
    # Two messages were unsure, so two texts were attempted — you cannot know a body is
    # empty until you fetch it. Only the one with readable text was re-judged.
    assert gmail.bodies == 2
    a_plan(store, gmail, fake, judge, read_bodies="on_escalation")
    assert gmail.bodies == 2         # the second pass read none: even "there was no text" is kept
    assert store.get_messages(["m1"])[0].body and "invoice" in store.get_messages(["m1"])[0].body


def test_the_escalated_answer_is_a_different_cache_key(settings):
    """A snippet-only judgement and one made with the text are different answers, and must
    never be served for each other."""
    from damien.models import Message

    judge = JevJudge(settings, DEFAULT_SPEC, cache=None)
    plain = Message(id="m1", account="a", subject="s", snippet="snip")
    with_body = Message(id="m1", account="a", subject="s", snippet="snip", body="the text")
    assert judge.verdict_key(plain, "", None) != judge.verdict_key(with_body, "", None)


def test_the_plan_records_which_mode_it_read_bodies_in(store: Store, gmail, fake):
    """So a wrapper never has to infer it: 'we read nothing' and 'reading is off' must not
    look alike in a report."""
    judge = BodyAwareJudge()
    _, off = a_plan(store, gmail, fake, judge, read_bodies="off")
    assert off["read_bodies"] == "off"
    _, on = a_plan(store, gmail, fake, judge, read_bodies="on_escalation")
    assert on["read_bodies"] == "on_escalation"
