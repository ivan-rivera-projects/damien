"""The preflight must name the *fix*, not just the fault — and must not call a
rules-only install broken, because it isn't."""
from __future__ import annotations

import dataclasses
import json

from damien.config import Settings
from damien.doctor import FAIL, OK, WARN, checks


def by_name(rows):
    return {r.name: r for r in rows}


def without(settings: Settings, **overrides) -> Settings:
    return dataclasses.replace(settings, **overrides)


def test_a_bare_install_says_exactly_what_is_missing(settings, tmp_path):
    rows = checks(settings, rules=[])
    got = by_name(rows)
    assert got["gmail credential"].status == FAIL
    assert "SETUP.md" in got["gmail credential"].detail          # the fix, not just the fault
    assert got["profile"].status == WARN                         # still the neutral default
    assert got["rules"].status == WARN
    assert got["judge"].status == OK                             # fake needs no key
    assert got["store"].status == OK                             # sqlite, no infrastructure
    assert any(r.blocking for r in rows)                         # and it is honestly not ready


def test_a_finished_install_is_ready(settings, gmail, fake, tmp_path):
    (tmp_path / "credentials.json").write_text("{}")
    (tmp_path / "token.json").write_text("{}")
    (tmp_path / "profile.json").write_text(json.dumps({"context": "A tour manager's inbox."}))
    fake.add("m1")
    got = by_name(checks(settings, gmail=gmail, rules=[]))
    assert got["profile"].status == OK
    assert got["gmail credential"].status == OK
    assert got["gmail token"].status == OK
    assert got["gmail reachable"].status == OK
    assert "me@example.com" in got["gmail reachable"].detail      # the live call, via the fake
    assert not any(r.blocking for r in got.values())


def test_a_model_judge_with_no_key_warns_and_names_the_way_out(settings):
    got = by_name(checks(without(settings, judge="openrouter", api_key=None), rules=[]))
    assert got["judge"].status == WARN
    assert "OPENROUTER_API_KEY" in got["judge"].detail
    assert "DAMIEN_JUDGE=rules" in got["judge"].detail            # the alternative, named

    with_key = by_name(checks(without(settings, judge="openrouter", api_key="sk-or-v1-x"), rules=[]))
    assert with_key["judge"].status == OK
    assert "sk-or-v1-x" not in with_key["judge"].detail           # a key is never printed


def test_a_rules_only_install_is_reported_as_whole(settings):
    got = by_name(checks(without(settings, judge="rules"), rules=[]))
    assert got["judge"].status == OK
    assert "third party" in got["judge"].detail


def test_a_broken_profile_blocks_and_says_which_file(settings, tmp_path):
    (tmp_path / "profile.json").write_text("{not json")
    got = by_name(checks(settings, rules=[]))
    assert got["profile"].status == FAIL
    assert "profile.json" in got["profile"].detail


def test_a_local_judge_without_a_url_warns(settings):
    got = by_name(checks(without(settings, judge="local", judge_url=None), rules=[]))
    assert got["judge"].status == WARN and "DAMIEN_JUDGE_URL" in got["judge"].detail


def test_a_local_judge_says_nothing_leaves(settings):
    got = by_name(checks(without(settings, judge="local", judge_url="http://localhost:8080/x",
                                 model="typesafe/jev-1.13"), rules=[]))
    assert "nothing leaves this machine" in got["judge"].detail
    assert got["calibration"].status == OK and "0.90" in got["calibration"].detail


def test_an_uncalibrated_model_warns_and_names_the_eval(settings):
    got = by_name(checks(without(settings, judge="openrouter", api_key="k", model="perplexity/pplx-decider-flash-v1.2"),
                         rules=[]))
    assert got["judge"].status == OK and "openrouter.ai" in got["judge"].detail
    assert got["calibration"].status == WARN and "damien judge eval" in got["calibration"].detail
