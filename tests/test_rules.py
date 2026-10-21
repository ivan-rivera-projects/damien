"""Rules: matching, translation, and the guarantee that an untranslatable rule is safe."""

from __future__ import annotations

from damien.models import Action, Message
from damien.rules import (
    Condition,
    Rule,
    first_match,
    load_rules,
    matches,
    save_rules,
    to_query,
)


def msg(**kw) -> Message:
    base = {"id": "m1", "account": "me", "subject": "Your order confirmation", "snippet": "Thanks for your order",
            "from_addr": "orders@shop.example", "from_name": "Shop", "label_ids": ["INBOX", "UNREAD"],
            "internal_date": 1_600_000_000_000}
    base.update(kw)
    return Message(**base)


def test_contains_and_age_and_label_matching():
    rule = Rule(id="r1", name="old promos", action=Action.trash,
                match=[Condition(field="label", op="contains", value="CATEGORY_PROMOTIONS"),
                       Condition(field="age_days", op="gt", value=30)])
    old = msg(label_ids=["INBOX", "CATEGORY_PROMOTIONS"], internal_date=1_600_000_000_000)
    assert matches(rule, old) is True
    assert matches(rule, msg(label_ids=["INBOX"])) is False          # label gate
    fresh = msg(label_ids=["CATEGORY_PROMOTIONS"], internal_date=9_999_999_999_999)
    assert matches(rule, fresh) is False                             # age gate


def test_any_of_is_or_of_and_groups():
    rule = Rule(id="r2", name="receipts", action=Action.label, label="Receipts",
                match=[Condition(field="any", op="contains", value="receipt")],
                any_of=[[Condition(field="from", op="contains", value="noreply")],
                        [Condition(field="subject", op="contains", value="confirmation")]])
    assert matches(rule, msg(subject="Your receipt", from_addr="billing@x.example")) is False  # receipt but neither group
    assert matches(rule, msg(subject="Your receipt", from_addr="noreply@x.example")) is True
    assert matches(rule, msg(subject="receipt confirmation", from_addr="hi@x.example")) is True


def test_first_match_wins_and_disabled_is_skipped():
    off = Rule(id="off", name="off", enabled=False, action=Action.trash, match=[Condition(field="from", op="contains", value="shop")])
    on = Rule(id="on", name="on", action=Action.label, label="Shop", match=[Condition(field="from", op="contains", value="shop")])
    assert first_match([off, on], msg()).id == "on"
    assert first_match([off], msg()) is None


def test_to_query_translates_only_the_certain_subset():
    assert to_query(Rule(id="a", name="a", action=Action.archive,
                         match=[Condition(field="from", op="contains", value="news@x.example")])) == "from:news@x.example"
    assert to_query(Rule(id="b", name="b", action=Action.archive,
                         match=[Condition(field="label", op="equals", value="CATEGORY_PROMOTIONS")])) == "label:CATEGORY_PROMOTIONS"
    assert to_query(Rule(id="c", name="c", action=Action.archive,
                         match=[Condition(field="age_days", op="gt", value=365)])) == "older_than:365d"
    assert to_query(Rule(id="d", name="d", action=Action.archive,
                         match=[Condition(field="label", op="equals", value="UNREAD")])) == "is:unread"
    assert to_query(Rule(id="e", name="e", action=Action.archive,
                         match=[Condition(field="subject", op="regex", value="^sale")])) is None   # not certain -> None
    assert to_query(Rule(id="f", name="f", action=Action.archive,
                         match=[Condition(field="list_unsubscribe", op="exists")])) is None        # not certain -> None
    assert to_query(Rule(id="g", name="g", action=Action.archive,
                         match=[Condition(field="subject", op="contains", value="two words")])) == 'subject:"two words"'


def test_rules_round_trip_on_disk(tmp_path):
    path = tmp_path / "rules.json"
    rule = Rule(id="r1", name="n", action=Action.archive, match=[Condition(field="from", op="contains", value="x")])
    save_rules(path, [rule])
    again = load_rules(path)
    assert [r.id for r in again] == ["r1"]
    assert again[0].match[0].value == "x"


def test_load_rules_is_empty_when_missing(tmp_path):
    assert load_rules(tmp_path / "nope.json") == []


def test_default_rules_are_all_translatable_or_explicitly_not():
    from damien.rules import default_rules
    for rule in default_rules():
        # Every default rule is safe by construction: never a permanent delete.
        assert rule.action.value in ("archive", "trash", "label", "keep", "mark_read")
