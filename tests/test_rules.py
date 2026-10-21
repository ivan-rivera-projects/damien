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


def test_words_operator_does_not_match_inside_a_longer_word():
    """'sale' must not match 'wholesale' — and 'tax' must not match 'syntax'.

    (My first version of this test claimed 'order' was inside 'openrouter'. It is
    not: o-p-e-n-r-o-u-t-e-r has no 'd'. The operator is still right for short
    tokens — 'sale' inside 'wholesale', 'tax' inside 'syntax'.)
    """
    cond = Condition(field="subject", op="words", value="sale")
    matches_rule = Rule(id="r", name="r", action=Action.archive, match=[cond])
    assert matches(matches_rule, msg(subject="Wholesale pricing update")) is False
    assert matches(matches_rule, msg(subject="Big SALE 50% off")) is True
    loose = Condition(field="subject", op="contains", value="sale")
    assert matches(Rule(id="r", name="r", action=Action.archive, match=[loose]),
                   msg(subject="Wholesale pricing update")) is True


def test_words_matches_a_phrase_and_a_hyphen_breaks_it():
    cond = Condition(field="subject", op="words", value="past due")
    rule = Rule(id="r", name="r", action=Action.archive, match=[cond])
    assert matches(rule, msg(subject="Past due: your subscription")) is True
    assert matches(rule, msg(subject="The past-due date")) is False      # a hyphen is not a word break here


def test_words_translates_to_a_gmail_query():
    from damien.rules import to_query
    assert to_query(Rule(id="w", name="w", action=Action.archive,
                         match=[Condition(field="subject", op="words", value="past due")])) == 'subject:"past due"'


def test_list_id_is_a_field_rules_can_use():
    on_list = msg(list_id="Studio List <studio.list.example>")
    assert matches(Rule(id="r", name="r", action=Action.archive,
                        match=[Condition(field="list_id", op="exists")]), on_list) is True
    assert matches(Rule(id="r", name="r", action=Action.archive,
                        match=[Condition(field="list_id", op="contains", value="list.example")]), on_list) is True
    assert matches(Rule(id="r", name="r", action=Action.archive,
                        match=[Condition(field="list_id", op="exists")]), msg()) is False


def test_the_default_rules_no_longer_blanket_label_every_mailing_list():
    from damien.rules import default_rules
    ids = {r.id for r in default_rules()}
    assert "r-newsletters" not in ids, "the rule that swallowed 149 of 200 must be gone"
    for rule in default_rules():
        fields = {c.field for c in rule.match} | {c.field for group in rule.any_of for c in group}
        assert "list_unsubscribe" not in fields or rule.action is Action.keep


def test_gmail_categories_and_the_persons_own_labels_are_visble_on_a_message():
    m = msg(label_ids=["INBOX", "CATEGORY_UPDATES", "Label_30", "Label_1"])
    assert m.category == "updates"
    assert set(m.own_label_ids) == {"Label_30", "Label_1"}
    assert msg(label_ids=["INBOX"]).category is None


def test_the_default_rules_keep_googles_personal_mail_without_asking_a_model():
    from damien.rules import default_rules
    rule = next(r for r in default_rules() if r.id == "r-gmail-personal")
    assert rule.action is Action.keep
    assert matches(rule, msg(label_ids=["INBOX", "CATEGORY_PERSONAL"])) is True
    assert matches(rule, msg(label_ids=["INBOX", "CATEGORY_PROMOTIONS"])) is False


# ---- minting rules from a reviewed group -------------------------------------
def _m(mid, **kw):
    base = {"id": mid, "account": "a", "subject": "x", "from_addr": "a@shop.example", "label_ids": ["INBOX"]}
    base.update(kw)
    return Message(**base)


def test_a_category_dominant_group_becomes_one_category_rule():
    from damien.rules import mint_rules, to_query
    messages = [_m(f"m{i}", label_ids=["INBOX", "CATEGORY_PROMOTIONS"], from_addr=f"s{i}@shop{i}.example")
                for i in range(8)] + [_m("m9")]
    rules, why = mint_rules(messages, Action.trash, None)
    assert len(rules) == 1
    assert rules[0].match[0].value == "CATEGORY_PROMOTIONS"
    assert to_query(rules[0]) == "label:CATEGORY_PROMOTIONS"          # Gmail evaluates it, not us
    assert "Gmail's 'promotions' category" in why


def test_a_few_senders_become_one_rule_each():
    from damien.rules import mint_rules
    messages = [_m("m1", from_addr="a@dsw.com"), _m("m2", from_addr="b@dsw.com"),
                _m("m3", from_addr="c@bestbuy.com")]
    rules, why = mint_rules(messages, Action.trash, None)
    assert {r.match[0].value for r in rules} == {"dsw.com", "bestbuy.com"}
    assert all(r.action is Action.trash for r in rules)
    assert "2 sender(s)" in why


def test_too_many_senders_mints_nothing_and_says_why():
    from damien.rules import mint_rules
    messages = [_m(f"m{i}", from_addr=f"a@shop{i}.example") for i in range(12)]
    rules, why = mint_rules(messages, Action.trash, None)
    assert rules == []
    assert "cannot express" in why and "12 different senders" in why


def test_minted_rules_carry_the_label_when_the_group_had_one():
    from damien.rules import mint_rules
    rules, _ = mint_rules([_m("m1", from_addr="a@news.example")], Action.label, "Newsletters")
    assert rules[0].label == "Newsletters" and rules[0].action is Action.label
    assert rules[0].source == "jev"
