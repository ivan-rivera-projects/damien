"""The CLI surface, offline: typer's CliRunner, the conftest fake Gmail, DAMIEN_HOME in tmp."""

from __future__ import annotations

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from damien import cli, passes
from damien.models import Action, Message, PlanItem, Source
from damien.rules import Condition, Rule, load_rules, save_rules
from damien.store import Store

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch, gmail):
    """A clean install in tmp: fake judge, an uncalibrated model, the fake Gmail behind `cli._gmail`."""
    for key in ("DAMIEN_DB", "DAMIEN_DB_URL", "DAMIEN_JUDGE_URL", "DAMIEN_READ_BODIES", "DAMIEN_CONFIDENCE_ACT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DAMIEN_HOME", str(tmp_path))
    monkeypatch.setenv("DAMIEN_JUDGE", "fake")
    monkeypatch.setenv("DAMIEN_MODEL", "acme/decider-1")
    monkeypatch.setattr(cli, "_gmail", lambda settings: gmail)
    return tmp_path


def _invoke(*args: str):
    return runner.invoke(cli.app, list(args))


def _plan_with(home, rows: list[tuple[str, str, Action]]) -> str:
    """A plan in the store: (message_id, sender, action) per row, decided by the model."""
    with Store(home / "damien.db") as store:
        store.upsert_messages([Message(id=mid, account="me@example.com", from_addr=sender, subject=f"s {mid}")
                               for mid, sender, _ in rows])
        plan = store.create_plan("me@example.com", "in:inbox",
                                 [PlanItem(message_id=mid, action=action, source=Source.jev, confidence=0.95)
                                  for mid, _, action in rows])
    return plan.id


def test_plan_show_json_never_carries_the_token(home):
    plan_id = _plan_with(home, [("m1", "a@shop.example", Action.archive)])
    token = json.loads(_invoke("plan", "approve", plan_id, "--json").stdout)["token"]
    out = _invoke("plan", "show", plan_id, "--json")
    assert out.exit_code == 0
    assert token not in out.stdout and "token" not in json.loads(out.stdout)
    assert token not in _invoke("plan", "show", plan_id).stdout


def test_correct_label_without_a_label_is_a_usage_error(home):
    out = _invoke("correct", "m1", "--action", "label", "--json")
    assert out.exit_code == 2
    assert json.loads(out.stdout)["ok"] is False
    with Store(home / "damien.db") as store:
        assert store.latest_corrections() == {}


def test_from_senders_writes_rules_and_refuses_a_corrected_sender(home):
    rows = [(f"s{i}", f"promo{i}@shop.example", Action.archive) for i in range(5)]
    rows += [(f"c{i}", f"news{i}@client.example", Action.archive) for i in range(5)]
    plan_id = _plan_with(home, rows)
    _invoke("correct", "c0", "--action", "keep")             # the person said: this sender stays

    out = _invoke("rules", "from-senders", plan_id, "--yes")
    assert out.exit_code == 0, out.stdout
    values = {r.match[0].value for r in load_rules(home / "rules.json")}
    assert "shop.example" in values
    assert "client.example" not in values
    assert "refusing client.example" in out.stdout


def test_from_senders_mints_a_freemail_sender_as_the_full_address(home):
    plan_id = _plan_with(home, [(f"g{i}", "Deals.Bot@gmail.com", Action.trash) for i in range(5)])
    assert _invoke("rules", "from-senders", plan_id, "--yes").exit_code == 0
    values = {r.match[0].value for r in load_rules(home / "rules.json")}
    assert values == {"deals.bot@gmail.com"}                # never all of gmail.com


def _seed_mailbox(home, fake):
    save_rules(home / "rules.json", [Rule(id="r-shop", name="shop", action=Action.archive,
                                          match=[Condition(field="from", op="contains", value="shop.example")])])
    fake.add("m1", subject="your weekly letter", sender="News <news@shop.example>")
    fake.add("m2", subject="Big sale 50% off", sender="Deals <deals@other.example>")


def test_pass_dry_run_json_reports_everything_and_writes_nothing(home, fake):
    _seed_mailbox(home, fake)
    out = _invoke("pass", "weekly", "--dry-run", "--json")
    assert out.exit_code == 0, out.stdout
    report = json.loads(out.stdout)
    assert report["plan"] and report["total"] == 2 and report["counts"]
    for key in ("bodies_read", "read_bodies", "judge_host", "judge_local", "calibrated_act", "needs_reply", "skipped"):
        assert key in report
    assert report["calibrated_act"] is None
    assert report["window"] == "in:inbox newer_than:7d"
    assert fake.mods == [] and not any(path.endswith("batchModify") for _m, path in fake.calls)


def test_uncalibrated_pass_applies_rules_but_no_model_decision(home, fake):
    _seed_mailbox(home, fake)
    out = _invoke("pass", "weekly")
    assert out.exit_code == 0, out.stdout
    touched = {mid for body in fake.mods for mid in body["ids"]}
    assert touched == {"m1"}                               # the rule; the model's 0.85 sale is reported
    assert "uncalibrated" in out.stdout and "damien judge eval" in out.stdout
    assert "waiting   1" in out.stdout


def test_monthly_window_is_the_calendar_month_just_gone():
    assert passes.window("monthly", date(2026, 3, 31)) == "in:inbox after:2026/02/01 before:2026/03/01"
    assert passes.window("monthly", date(2026, 1, 15)) == "in:inbox after:2025/12/01 before:2026/01/01"


def test_skill_install_writes_the_packaged_skill(home, tmp_path):
    target = tmp_path / "skills"
    out = _invoke("skill", "install", "--dir", str(target))
    assert out.exit_code == 0, out.stdout
    text = (target / "damien-mail-triage" / "SKILL.md").read_text()
    assert text.startswith("---\nname: damien-mail-triage")
    assert _invoke("skill", "install", "--dir", str(target)).exit_code == 1     # no silent overwrite


def test_apply_group_refuses_a_stale_fingerprint(home, fake):
    fake.add("m1")
    plan_id = _plan_with(home, [("m1", "a@shop.example", Action.archive)])
    token = json.loads(_invoke("plan", "approve", plan_id, "--json").stdout)["token"]
    out = _invoke("plan", "apply-group", plan_id, "--group", "1:000000", "--token", token, "--yes", "--json")
    assert out.exit_code == 1
    assert "no longer" in json.loads(out.stdout)["error"]
    assert fake.mods == []
