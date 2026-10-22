"""The seam that makes this shippable: what is personal lives in a file, not in the code.

An install with no profile must be usable by a stranger — on the neutral default — and an
install with one must get its owner's framing. That is the difference between a tool that
files mail and one that files *your* mail.
"""
from __future__ import annotations

import json

import pytest

from damien.decide import DEFAULT_SPEC
from damien.profile import Profile, load_profile, write_profile


def test_without_a_profile_an_install_gets_the_neutral_default(tmp_path):
    profile = load_profile(tmp_path)
    assert profile.context == DEFAULT_SPEC.context
    assert profile.buckets == {}
    assert profile.spec().criteria() == DEFAULT_SPEC.criteria()


def test_a_profile_says_whose_inbox_this_is(tmp_path):
    (tmp_path / "profile.json").write_text(json.dumps({
        "context": "A touring drummer's inbox. Gig offers in, nothing else.",
        "buckets": {"keep": "Belongs in the inbox: gig offers, contracts and money"},
    }))
    spec = load_profile(tmp_path).spec()
    assert "touring drummer" in spec.context
    keep = next(b for b in spec.buckets if b.key == "keep")
    assert keep.description.startswith("Belongs in the inbox: gig offers")
    assert keep.action is DEFAULT_SPEC.get("keep").action       # a profile changes words, not actions
    assert len(spec.buckets) == 7                                # every other bucket keeps its default
    assert spec.get("promotion").description == DEFAULT_SPEC.get("promotion").description


def test_an_unknown_bucket_is_refused_rather_than_ignored(tmp_path):
    """A typo in a profile must be loud: silently ignoring it means the person believes
    they changed a bucket that is still running on the default."""
    (tmp_path / "profile.json").write_text(json.dumps({"buckets": {"promos": "a typo"}}))
    with pytest.raises(ValueError, match="unknown bucket"):
        load_profile(tmp_path).spec()


def test_a_broken_profile_says_so(tmp_path):
    (tmp_path / "profile.json").write_text("{not json")
    with pytest.raises(ValueError, match="not a usable profile"):
        load_profile(tmp_path)


def test_a_profile_is_what_the_judge_is_told(tmp_path):
    """The context is the one line the model reads before it judges anything."""
    profile = Profile(context="A chef's inbox. Suppliers, invoices and menus only.")
    assert profile.spec().context == "A chef's inbox. Suppliers, invoices and menus only."


def test_init_writes_a_template_and_never_clobbers_it(tmp_path):
    path, written = write_profile(tmp_path)
    assert written and path.exists()
    body = json.loads(path.read_text())
    assert body["buckets"] == {} and body["context"] == DEFAULT_SPEC.context
    assert "_readme" in body                                  # the file explains itself to a stranger

    path.write_text(json.dumps({"context": "mine"}))
    same, written_again = write_profile(tmp_path)
    assert written_again is False
    assert json.loads(same.read_text())["context"] == "mine"  # kept, not overwritten

    _, forced = write_profile(tmp_path, force=True)
    assert forced is True                                     # --force is the way to replace it
