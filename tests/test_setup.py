"""The two ways a fresh install goes wrong, and the two files that must not drift."""
from __future__ import annotations

import pathlib

import pytest

from damien.config import ENV_TEMPLATE, write_env
from damien.gmail import GmailError


def test_the_env_example_a_contributor_reads_is_the_template_an_install_writes():
    """`init` writes ENV_TEMPLATE; a checkout also carries .env.example. If they drift, the
    file someone reads stops describing the file their install actually gets."""
    example = pathlib.Path(".env.example")
    if not example.exists():
        pytest.skip("not running from a checkout")
    assert example.read_text() == ENV_TEMPLATE


def test_write_env_is_0600_and_never_clobbers(tmp_path):
    path, wrote = write_env(tmp_path)
    assert wrote and path.read_text() == ENV_TEMPLATE
    assert path.stat().st_mode & 0o777 == 0o600           # it is where a key will live

    path.write_text("DAMIEN_JUDGE=rules\n")
    same, wrote_again = write_env(tmp_path)
    assert wrote_again is False
    assert "DAMIEN_JUDGE=rules" in same.read_text()       # kept, not overwritten

    _, forced = write_env(tmp_path, force=True)
    assert forced is True


class _DeadCreds:
    """A credential whose refresh fails exactly as a deleted OAuth client does."""

    def __init__(self, message: str) -> None:
        self.message = message

    def refresh(self, request) -> None:
        raise Exception(self.message)                     # noqa: TRY002 — mimicking google-auth


def test_a_deleted_oauth_client_says_what_to_do_rather_than_tracebacking(gmail):
    with pytest.raises(GmailError) as caught:
        gmail._refresh(_DeadCreds("deleted_client: The OAuth client was deleted."))
    message = str(caught.value)
    assert "deleted" in message.lower()
    assert "damien login" in message                      # the fix, named
    assert "SETUP.md" in message


def test_an_expired_sign_in_explains_the_seven_day_trap(gmail):
    with pytest.raises(GmailError) as caught:
        gmail._refresh(_DeadCreds("invalid_grant: Token has been expired or revoked."))
    message = str(caught.value)
    assert "damien login" in message
    assert "seven days" in message                        # testing-mode OAuth expires weekly


def test_any_other_refresh_failure_is_still_a_readable_error(gmail):
    with pytest.raises(GmailError, match="could not refresh"):
        gmail._refresh(_DeadCreds("something new from google"))


def test_the_version_it_reports_is_the_version_that_is_installed():
    """A hardcoded __version__ once disagreed with the package: the tag said v0.1.1 and the
    CLI said 0.1.0. It reads the installed metadata now, so it cannot drift."""
    import importlib.metadata as md

    import damien

    try:
        installed = md.version("damien")
    except md.PackageNotFoundError:
        pytest.skip("not installed (running from a bare checkout)")
    assert damien.__version__ == installed
