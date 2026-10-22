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
                         match=[Condition(field="label", op="equals", value="CATEGORY_PROMOTIONS")])) == "label:category_promotions"
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


def test_gmail_categories_and_the_persons_own_labels_are_visible_on_a_message():
    m = msg(label_ids=["INBOX", "CATEGORY_UPDATES", "Label_30", "Label_1"])
    assert m.category == "updates"
    assert set(m.own_label_ids) == {"Label_30", "Label_1"}
    assert msg(label_ids=["INBOX"]).category is None


def test_the_default_rules_do_not_short_circuit_on_a_gmail_category():
    """There was a "CATEGORY_PERSONAL -> keep" rule. Gmail's Personal category includes
    marketing from anyone you have transacted with (engagement is its strongest signal),
    so the rule filed 45 Alibaba blasts unjudged - and 826 of one plan's 7,543 messages
    never reached the model. A shortcut that runs before judgment must be certain."""
    from damien.rules import default_rules
    for rule in default_rules():
        fields = {c.field for c in rule.match}
        assert not (fields & {"label"} and rule.action is Action.keep), rule.id


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
    assert to_query(rules[0]) == "label:category_promotions"          # Gmail evaluates it, not us
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


def test_a_one_message_group_never_becomes_a_category_rule():
    """A single message carries one category, so a share test would trivially pass —
    and `CATEGORY_UPDATES -> keep` would switch triage off for every update."""
    from damien.rules import mint_rules
    rules, _why = mint_rules([_m("m1", label_ids=["INBOX", "CATEGORY_UPDATES"],
                                from_addr="no-reply@amazonaws.com")], Action.keep, None)
    assert len(rules) == 1
    assert rules[0].match[0].field == "from"                  # the sender, not the category
    assert rules[0].match[0].value == "amazonaws.com"
    assert "CATEGORY" not in str([r.model_dump() for r in rules])


def test_keep_never_mints_a_category_rule_however_big_the_group():
    from damien.rules import mint_rules
    messages = [_m(f"m{i}", label_ids=["INBOX", "CATEGORY_UPDATES"], from_addr=f"a@x{i}.example")
                for i in range(20)]
    rules, _ = mint_rules(messages, Action.keep, None)
    assert rules == [] or all(r.match[0].field == "from" for r in rules)


def test_a_big_category_group_can_still_mint_a_category_rule_for_an_action():
    from damien.rules import mint_rules
    messages = [_m(f"m{i}", label_ids=["INBOX", "CATEGORY_PROMOTIONS"], from_addr=f"a@s{i}.example")
                for i in range(9)]
    rules, _ = mint_rules(messages, Action.label, "Promos")
    assert len(rules) == 1 and rules[0].match[0].value == "CATEGORY_PROMOTIONS"


# ---- minting from sender consistency ----------------------------------------
def test_a_sender_that_agrees_with_itself_becomes_a_rule():
    from damien.rules import mint_rules_by_sender
    counts = [("shop.example", Action.keep, None, 30),
              ("dsw.example", Action.trash, None, 36), ("dsw.example", Action.keep, None, 1),
              ("nocafe.example", Action.label, "Newsletters", 12)]
    rules, why = mint_rules_by_sender(counts, min_count=5, agreement=0.9)
    by_id = {r.id: r for r in rules}
    assert set(by_id) == {"auto-from-shop-example", "auto-from-dsw-example", "auto-from-nocafe-example"}
    assert by_id["auto-from-dsw-example"].action is Action.trash      # 36/37 still counts as one-way
    assert by_id["auto-from-nocafe-example"].label == "Newsletters"
    assert "agreed with themselves" in why


def test_a_sender_split_between_two_answers_gets_no_rule():
    """linkedin.com in a real plan: 87 keep, 32 archive. Flattening it with a rule
    would be worse than leaving it to judgment."""
    from damien.rules import mint_rules_by_sender
    counts = [("linkedin.com", Action.keep, None, 87), ("linkedin.com", Action.archive, None, 32)]
    rules, why = mint_rules_by_sender(counts, min_count=5, agreement=0.9)
    assert rules == []
    assert "split between actions" in why and "linkedin.com" in why


def test_a_rare_sender_is_left_alone():
    from damien.rules import mint_rules_by_sender
    rules, _ = mint_rules_by_sender([("rare.example", Action.trash, None, 2)], min_count=5)
    assert rules == []


def test_minting_never_exceeds_its_own_cap():
    from damien.rules import mint_rules_by_sender
    counts = [(f"s{i}.example", Action.trash, None, 9) for i in range(30)]
    rules, _ = mint_rules_by_sender(counts, min_count=5, max_rules=10)
    assert len(rules) == 10


def test_a_broader_rule_that_runs_first_can_shadow_a_narrower_one():
    """The Alibaba shape: one domain rule where a per-address distinction was needed."""
    from damien.rules import shadowed
    broad = Rule(id="b", name="b", action=Action.keep,
                 match=[Condition(field="from", op="contains", value="notice.alibaba.com")])
    narrow = Rule(id="n", name="n", action=Action.trash,
                  match=[Condition(field="from", op="contains", value="sales@notice.alibaba.com")])
    assert shadowed([broad, narrow]) == [(broad, narrow)]
    assert shadowed([narrow, broad]) == []          # specific first: correct, not shadowed
    same_action = Rule(id="s", name="s", action=Action.keep,
                       match=[Condition(field="from", op="contains", value="sales@notice.alibaba.com")])
    assert shadowed([broad, same_action]) == []      # agreeing is harmless


def test_snippet_and_any_text_do_not_translate():
    """L4: a bare Gmail term searches the whole body, so it matches mail the rule would not."""
    for field in ("snippet", "any"):
        rule = Rule(id="s", name="s", action=Action.trash, match=[Condition(field=field, op="contains", value="sale")])
        assert to_query(rule) is None


def test_a_label_with_spaces_is_one_query_token():
    rule = Rule(id="l", name="l", action=Action.archive,
                match=[Condition(field="label", op="equals", value="Action Required/Bills")])
    assert to_query(rule) == "label:action-required-bills"


def test_any_of_translates_to_an_or_group_only_when_every_group_does():
    from damien.rules import default_rules
    rules = {r.id: r for r in default_rules()}
    assert to_query(rules["r-receipts"]) == ('{(subject:receipt) (subject:invoice) '
                                              '(subject:"order confirmation") (subject:"payment received")}')
    mixed = Rule(id="m", name="m", action=Action.archive,
                 any_of=[[Condition(field="from", op="contains", value="a.example")],
                         [Condition(field="subject", op="regex", value="^x")]])
    assert to_query(mixed) is None


def test_sender_key_is_the_domain_except_on_freemail():
    from damien.rules import sender_key
    assert sender_key("Billing@Shop.example") == "shop.example"
    assert sender_key("alex@gmail.com") == "alex@gmail.com"
    assert sender_key("x@yahoo.co.uk") == "x@yahoo.co.uk"
    assert sender_key(None) is None
