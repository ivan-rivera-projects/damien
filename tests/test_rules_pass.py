"""`rules plan`: a plan Gmail evaluates must never do what `plan build` would refuse."""

from __future__ import annotations

from damien.decide import DEFAULT_SPEC
from damien.models import Action, Source
from damien.plan import build_plan, plan_from_rules
from damien.rules import Condition, Rule, default_rules
from damien.store import Store


def shop_rule(source: str, action: Action = Action.trash) -> Rule:
    return Rule(id="r-shop", name="shop", action=action, source=source,  # type: ignore[arg-type]
                match=[Condition(field="from", op="contains", value="shop.example")])


def test_a_minted_trash_rule_spares_what_the_stake_floor_protects(store: Store, gmail, fake):
    """R8: a jev-minted trash rule over a whole sender used to trash the person's own
    ACTION REQUIRED mail, a 'Payment failed' alarm, a reply and a message they corrected."""
    fake.labels["Label_7"] = "ACTION REQUIRED"
    fake.add("protected", subject="hello", sender="a@shop.example", labels=["INBOX", "UNREAD", "Label_7"])
    fake.add("alarm", subject="Payment failed", sender="a@shop.example")
    fake.add("reply", subject="Re: my order", sender="a@shop.example")
    fake.add("corrected", subject="Your weekly deals", sender="a@shop.example")
    fake.add("promo", subject="Big sale today", sender="a@shop.example")
    store.correct("corrected", Action.keep, note="I read these")

    plan = plan_from_rules(store=store, gmail=gmail, rules=[shop_rule("jev")], sample=0)

    query = fake.queries[0]
    assert "-label:action-required" in query
    assert "-subject:{" in query and "failed" in query and '"past due"' in query
    assert "-subject:re -subject:fwd -subject:fw" in query
    assert "is:unread -is:important" in query
    items = {i.message_id: i for i in plan.items}
    assert set(items) == {"corrected", "promo"}
    assert items["promo"].action is Action.trash
    assert items["corrected"].action is Action.keep and items["corrected"].source is Source.person


def test_a_rule_the_person_wrote_is_not_second_guessed(store: Store, gmail, fake):
    fake.add("alarm", subject="Payment failed", sender="a@shop.example")
    plan = plan_from_rules(store=store, gmail=gmail, rules=[shop_rule("person")], sample=0)
    assert fake.queries[0] == "in:inbox from:shop.example -in:spam -in:trash"
    assert [i.message_id for i in plan.items] == ["alarm"]


def test_the_seed_rules_run_in_gmail_including_the_receipts_or_group(store: Store, gmail, fake):
    fake.add("r1", subject="Your receipt from Shop", sender="a@shop.example")
    fake.add("r2", subject="Order confirmation #12", sender="a@shop.example")
    fake.add("x", subject="hello there", sender="a@shop.example")
    stats: dict = {}
    plan = plan_from_rules(store=store, gmail=gmail, rules=default_rules(), sample=0, stats=stats)
    assert stats["skipped_rules"] == []
    assert all(rid in plan.query for rid in ("r-receipts", "r-old-promotions", "r-old-social"))
    assert {i.message_id: i.label for i in plan.items} == {"r1": "Receipts", "r2": "Receipts"}


def test_a_rule_only_python_can_evaluate_keeps_its_place_in_the_order(store: Store, gmail, fake):
    """The Python-only rule would have claimed this mail first in `plan build`; letting the
    later rule trash it here would invert the person's order."""
    fake.add("m1", subject="newsletter", sender="a@shop.example")
    rules = [
        Rule(id="r-py", name="python only", action=Action.label, label="Shop",
             match=[Condition(field="subject", op="regex", value="^news")]),
        shop_rule("person"),
        Rule(id="r-same", name="same answer", action=Action.label, label="Shop",
             match=[Condition(field="from", op="contains", value="other.example")]),
    ]
    stats: dict = {}
    plan = plan_from_rules(store=store, gmail=gmail, rules=rules, sample=0, stats=stats)
    assert plan.items == []
    assert [rid for rid, _ in stats["skipped_rules"]] == ["r-shop"]
    assert "r-py runs in Python" in stats["skipped_rules"][0][1]
    assert "r-same" in plan.query                     # agrees with the Python rule: safe to run


def test_rules_plan_records_the_state_undo_must_restore(store: Store, gmail, fake):
    fake.add("inbox", sender="a@news.example", labels=["INBOX", "UNREAD"])
    fake.add("filed", sender="a@news.example", labels=["Label_1"])          # Label_1 = Newsletters, read
    rule = Rule(id="r-news", name="news", action=Action.label, label="Newsletters",
                match=[Condition(field="from", op="contains", value="news.example")])
    plan = plan_from_rules(store=store, gmail=gmail, rules=[rule], sample=0, scope="")
    items = {i.message_id: i for i in plan.items}
    assert (items["inbox"].was_in_inbox, items["inbox"].was_unread, items["inbox"].had_label) == (True, True, False)
    assert (items["filed"].was_in_inbox, items["filed"].was_unread, items["filed"].had_label) == (False, False, True)
    assert gmail.fetched == 0                                              # ids only


def test_build_plan_records_the_state_undo_must_restore(store: Store, gmail, fake):
    fake.add("m1", sender="a@news.example", labels=["INBOX", "Label_1"])
    fake.add("m2", sender="b@news.example", labels=["INBOX", "UNREAD"])
    rule = Rule(id="r-news", name="news", action=Action.label, label="newsletters",
                match=[Condition(field="from", op="contains", value="news.example")])
    plan = build_plan(store=store, gmail=gmail, rules=[rule], judge=None, query="in:inbox", spec=DEFAULT_SPEC)
    items = {i.message_id: i for i in plan.items}
    assert (items["m1"].was_in_inbox, items["m1"].was_unread, items["m1"].had_label) == (True, False, True)
    assert (items["m2"].was_in_inbox, items["m2"].was_unread, items["m2"].had_label) == (True, True, False)
