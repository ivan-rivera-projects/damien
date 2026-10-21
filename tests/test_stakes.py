"""The stake guardrail — the floor that stopped three real archives in a real inbox."""

from __future__ import annotations

from damien.decide import DEFAULT_SPEC, Verdict, decide_message
from damien.models import Action, Message, Source
from damien.rules import Condition, Rule
from damien.stakes import protected_label, stake_floor


def a_message(subject: str, **kw) -> Message:
    base = {"id": "m1", "account": "me@example.com", "subject": subject, "snippet": "", "from_addr": "a@b.example",
            "label_ids": ["INBOX", "UNREAD"], "internal_date": 1_700_000_000_000}
    base.update(kw)
    return Message(**base)


def verdict(choice: str, *, machine_sent: float | None = None, confidence: float = 0.95) -> Verdict:
    return Verdict(message_id="m1", choice=choice, confidence=confidence, probabilities={choice: confidence},
                   signals={"handled_by_a_machine": machine_sent})


# ---- the subjects that actually failed on the real run ----------------------
def test_the_three_real_misses_are_now_held():
    for subject in ("Ivan, your domains will expire in 30 days - renew now",
                    "Your account may have insufficient funds",
                    "A bill payment failed for Little Giants"):
        assert stake_floor(a_message(subject)) is not None, subject


def test_money_deadlines_accounts_and_legal_are_all_held():
    for subject in ("Invoice 4471 from Acme", "Past due: your subscription",
                    "Your card was declined", "Security alert: new sign-in",
                    "Action required: verify your account", "Contract renewal notice",
                    "Your verification code", "IRS notice"):
        assert stake_floor(a_message(subject)) is not None, subject


def test_ordinary_mail_is_not_floored():
    for subject in ("Weekly digest: 3 new articles", "Big SALE 50% off",
                    "You have 1 new invitation", "TBPN", "New customer message"):
        assert stake_floor(a_message(subject)) is None, subject


def test_a_person_is_held_and_machinery_is_not():
    human = a_message("Questions about the split", from_addr="viviantang@pinterest.com")
    assert stake_floor(human, machine_sent=0.05) is not None
    # the model's own reading says a machine sent it, and nothing else says otherwise
    assert stake_floor(human, machine_sent=0.7) is None
    # a robot address is never treated as a person, however low the probability
    robot = a_message("hello", from_addr="noreply@shop.example")
    assert stake_floor(robot, machine_sent=0.02) is None
    # a mailing list is not a person either
    listed = a_message("hello", from_addr="news@list.example", list_id="List <x.list.example>")
    assert stake_floor(listed, machine_sent=0.02) is None


# ---- and how it behaves inside a decision ------------------------------------
def test_a_confident_model_archive_is_held_and_the_reason_is_kept():
    decision = decide_message(a_message("Your domains will expire in 30 days"), rules=(), spec=DEFAULT_SPEC,
                              verdict=verdict("notification", machine_sent=0.97))
    assert decision.action is Action.keep
    assert decision.source is Source.jev                       # still the model's judgment, recorded
    assert "held for a person" in decision.rationale
    assert "notification" in (decision.probabilities or {})   # what it wanted is still visible


def test_a_person_wins_the_model_floors():
    decision = decide_message(a_message("Your domains will expire"), rules=(), spec=DEFAULT_SPEC,
                              verdict=verdict("keep"))
    assert decision.action is Action.keep


def test_a_rule_is_never_second_guessed_by_the_guardrail():
    # a person's own rule says trash; the guardrail exists to catch models, not people
    rule = Rule(id="r1", name="trash it", action=Action.trash,
                match=[Condition(field="subject", op="words", value="expire")])
    decision = decide_message(a_message("Your domains will expire"), rules=[rule], spec=DEFAULT_SPEC, verdict=None)
    assert decision.action is Action.trash
    assert decision.source is Source.rule


def test_nothing_held_is_ever_made_worse():
    """The floor only raises to keep — it never turns keep into trash."""
    decision = decide_message(a_message("Weekly digest"), rules=(), spec=DEFAULT_SPEC,
                              verdict=verdict("keep", machine_sent=0.9))
    assert decision.action is Action.keep


# ---- the exact message that survived the first guardrail ---------------------
def test_the_real_pinterest_reply_is_now_held():
    """importance 2.2/3 and machine-sent 0.34: two signals the first version ignored."""
    m = a_message("Re: wearelittlegs + Pinterest", from_addr="viviantang@pinterest.com")
    assert stake_floor(m, machine_sent=0.34, importance=2.20) is not None
    # each signal alone is enough
    assert stake_floor(m, machine_sent=0.34) is not None          # a reply, not a bulk sender
    assert stake_floor(m, machine_sent=0.9, importance=2.20) is not None


def test_a_reply_from_a_bulk_sender_is_not_treated_as_a_person():
    m = a_message("Re: your order", from_addr="news@list.example", list_id="List <x.example>")
    assert stake_floor(m, machine_sent=0.05, importance=0.4) is None


def test_importance_alone_holds_anything_the_model_valued():
    m = a_message("Low stock alerts at the store")
    assert stake_floor(m, machine_sent=0.99, importance=2.4) is not None
    assert stake_floor(m, machine_sent=0.99, importance=1.2) is None


# ---- the person's own labels -------------------------------------------------
def test_a_label_the_person_gave_it_outranks_the_model():
    m = a_message("Weekly roundup", label_ids=["INBOX", "Label_59"])
    names = {"Label_59": "ACTION REQUIRED"}
    assert protected_label(m, names, ("ACTION REQUIRED", "WAITING FOR")) == "ACTION REQUIRED"
    assert stake_floor(m, machine_sent=0.99, importance=0.1, protected="ACTION REQUIRED") is not None
    # a label they do not protect does not hold anything
    assert protected_label(a_message("x", label_ids=["INBOX", "Label_30"]), {"Label_30": "Notes"},
                           ("ACTION REQUIRED",)) is None


def test_protected_labels_hold_a_decision_end_to_end():
    m = a_message("Weekly roundup", label_ids=["INBOX", "Label_59"])
    decision = decide_message(m, rules=(), spec=DEFAULT_SPEC, verdict=verdict("promotion"),
                              label_names={"Label_59": "WAITING FOR"},
                              protected_labels=("WAITING FOR",))
    assert decision.action is Action.keep
    assert "WAITING FOR" in decision.rationale


def test_a_rule_may_not_contradict_a_correction_from_the_same_sender():
    """Minting is permanent, so it must not archive a sender the person keeps.
    Exercised through the store the way the CLI does it."""
    import tempfile
    from pathlib import Path

    from damien.store import Store

    with Store(Path(tempfile.mkdtemp()) / "x.db") as store:
        store.upsert_messages([
            Message(id="m1", account="a", subject="Low stock alerts at LG BK", from_addr="no-reply@squareup.com"),
            Message(id="m2", account="a", subject="Low stock alerts at LG LA", from_addr="no-reply@squareup.com"),
        ])
        store.correct("m1", Action.keep, note="store inventory alert")

        domains = {"squareup.com"}
        rows = list(store.conn.execute(
            "SELECT c.message_id, c.action, m.from_addr FROM corrections c JOIN messages m ON m.id = c.message_id"))
        conflicts = [r for r in rows
                     if (r["from_addr"] or "").split("@")[-1] in domains and r["action"] != Action.archive.value]
        assert len(conflicts) == 1                       # so `rules from-group` would refuse


# ---- read and Gmail-important: mail he engaged with does not leave the inbox ---
def test_mail_the_person_read_is_not_filed_away():
    read = a_message("production", from_addr="supplier@fashion.example", label_ids=["INBOX"])
    assert stake_floor(read, action=Action.trash) is not None
    assert stake_floor(read, action=Action.archive) is not None
    assert stake_floor(read, action=Action.label) is None        # filing it under a name is not losing it
    unread = a_message("production", label_ids=["INBOX", "UNREAD"])
    assert stake_floor(unread, action=Action.trash) is None


def test_gmail_important_protects_from_trash_and_archive_only():
    flagged = a_message("Weekly roundup", label_ids=["INBOX", "UNREAD", "IMPORTANT"])
    assert stake_floor(flagged, action=Action.trash) is not None
    assert stake_floor(flagged, action=Action.archive) is not None
    assert stake_floor(flagged, action=Action.label) is None


def test_a_read_marketing_blast_the_model_would_trash_is_held():
    """The one false keep this costs: a Shopify-app blast Gmail called important."""
    m = a_message("FREE LIMITED-EDITION TEE!", label_ids=["INBOX", "UNREAD", "IMPORTANT"],
                  from_addr="no-reply@marsello.com")
    decision = decide_message(m, rules=(), spec=DEFAULT_SPEC, verdict=verdict("promotion", machine_sent=0.99))
    assert decision.action is Action.keep
    assert "important" in decision.rationale.lower()


# ---- the floor guards the tool's own rules, not just the model ----------------
def test_a_seeded_rule_may_not_file_an_alarm():
    """r-receipts matched "Action required: Invoice overdue" and labelled it Receipts,
    because a rule's decision used to return before the floor ran."""
    seeded = Rule(id="r-receipts", name="receipts", action=Action.label, label="Receipts", source="seed",
                  any_of=[[Condition(field="subject", op="words", value="invoice")]])
    decision = decide_message(a_message("Action required: Invoice overdue"), rules=[seeded], verdict=None,
                              spec=DEFAULT_SPEC)
    assert decision.action is Action.keep
    assert "held for a person" in decision.rationale
    assert "r-receipts" in decision.rationale                     # which rule is named


def test_a_rule_the_person_wrote_is_never_second_guessed():
    mine = Rule(id="r-mine", name="file my overdue invoices", action=Action.archive, source="person",
                match=[Condition(field="subject", op="words", value="invoice")])
    decision = decide_message(a_message("Action required: Invoice overdue"), rules=[mine], verdict=None,
                              spec=DEFAULT_SPEC)
    assert decision.action is Action.archive
    assert decision.source is Source.rule


def test_an_ordinary_receipt_still_gets_labelled_by_the_seeded_rule():
    seeded = Rule(id="r-receipts", name="receipts", action=Action.label, label="Receipts", source="seed",
                  any_of=[[Condition(field="subject", op="words", value="invoice")]])
    decision = decide_message(a_message("Your invoice from Acme #1234"), rules=[seeded], verdict=None,
                              spec=DEFAULT_SPEC)
    assert decision.action is Action.label and decision.label == "Receipts"


def test_the_shipped_rules_are_marked_as_seeded():
    from damien.rules import default_rules
    assert {r.source for r in default_rules()} == {"seed"}


def test_an_ordinary_invoice_may_still_be_labelled_a_receipt():
    """"Invoice overdue" is an alarm; "Your invoice from Acme #1234" is a receipt.
    One list for both is how the second stopped being filed at all."""
    from damien.stakes import ALARM, CONSEQUENTIAL
    assert ALARM.search("Action required: Invoice overdue")
    assert ALARM.search("[Action required] Payment failed for invoice #IN-835")
    assert not ALARM.search("Your invoice from Acme #1234")
    assert CONSEQUENTIAL.search("Your invoice from Acme #1234")


def test_an_alarm_is_held_under_every_action_but_money_only_when_it_leaves():
    alarm = a_message("Action required: Invoice overdue")
    receipt = a_message("Your invoice from Acme #1234")
    for action in (Action.trash, Action.archive, Action.label):
        assert stake_floor(alarm, action=action) is not None
    assert stake_floor(receipt, action=Action.label) is None       # filing it is the point
    assert stake_floor(receipt, action=Action.trash) is not None   # but it does not get thrown out
    assert stake_floor(receipt, action=Action.archive) is not None
