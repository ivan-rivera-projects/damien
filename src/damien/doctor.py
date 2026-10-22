"""An install check: what is configured, what is missing, and the fix for each.

Written as a plain function returning plain rows, so the CLI is only the printer and the
behaviour can be tested. Every row carries the *fix*, because "credentials.json missing" is
not useful without "and here is where it goes".
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .decide import DEFAULT_SPEC
from .gmail import Gmail, GmailError
from .profile import load_profile
from .rules import Rule, shadowed
from .store import open_store

OK, WARN, FAIL = "ok", "warn", "fail"

KEY_FOR = {"openrouter": "OPENROUTER_API_KEY", "typesafe": "TYPESAFE_API_KEY"}


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


def checks(settings: Settings, *, gmail: Gmail | None = None, store=None,
           rules: list[Rule] | None = None) -> list[Check]:
    """Every check, in the order a person would ask them. Nothing is fetched by default."""
    out: list[Check] = []
    root = Path(settings.root)

    out.append(Check(OK, "root", str(root)))
    env_file = root / ".env"
    out.append(Check(OK if env_file.exists() else WARN, ".env",
                     str(env_file) if env_file.exists() else "not written yet — `damien init` makes one"))
    out.append(Check(OK if settings.account else WARN, "account",
                     settings.account or "DAMIEN_ACCOUNT is blank (only used to label stored rows)"))

    # the profile: the difference between filing mail and filing *your* mail
    try:
        profile = load_profile(root).spec()
        generic = profile.criteria() == DEFAULT_SPEC.criteria() and profile.context == DEFAULT_SPEC.context
        out.append(Check(WARN if generic else OK, "profile",
                         "still the neutral default — describe whose inbox this is in profile.json"
                         if generic else f"yours ({len(profile.context)} characters of context)"))
    except ValueError as exc:
        out.append(Check(FAIL, "profile", f"{exc} — fix or delete profile.json"))

    # rules
    if rules is not None:
        clashes = shadowed(rules)
        detail = f"{len(rules)} rule(s)"
        if clashes:
            detail += f", {len(clashes)} shadowed by a broader one (`damien rules list`)"
        if not rules:
            detail += " — none yet: your first pass can mint them (`damien rules from-senders <plan_id>`)"
        out.append(Check(OK if rules else WARN, "rules", detail))

    # the judge: with no key it still works, it just keeps everything
    mode = settings.judge
    if mode == "rules":
        out.append(Check(OK, "judge", "rules only — no model, no key, nothing sent to a third party"))
    elif mode in KEY_FOR:
        if settings.api_key:
            out.append(Check(OK, "judge", f"{mode} · model {settings.model} · key present (not shown)"))
        else:
            out.append(Check(WARN, "judge",
                             f"{mode} selected with no key: unresolved mail will be kept, not judged. "
                             f"Set {KEY_FOR[mode]} in {root / '.env'}, or use DAMIEN_JUDGE=rules"))
    else:
        out.append(Check(OK, "judge", mode))

    # Gmail: the credential, the token, and one live call
    credential = Path(settings.credentials_path)
    out.append(Check(OK if credential.exists() else FAIL, "gmail credential",
                     str(credential) if credential.exists()
                     else f"missing {credential} — docs/SETUP.md step 2 makes one"))
    token = Path(settings.token_path)
    if not credential.exists():
        pass                                          # nothing below can work without it
    elif not token.exists():
        out.append(Check(WARN, "gmail token", f"not signed in — `damien login` (writes {token})"))
    else:
        out.append(Check(OK, "gmail token", str(token)))
        probe = gmail
        if probe is None:
            try:
                probe = Gmail(settings)
            except Exception as exc:
                out.append(Check(FAIL, "gmail reachable", f"{type(exc).__name__}: {exc}"))
                probe = None
        if probe is not None:
            try:
                who = probe.profile()
                out.append(Check(OK, "gmail reachable", f"signed in as {who.get('emailAddress', '?')}"))
            except GmailError as exc:
                hint = (" — an expired token looks like this: publish your consent screen, then `damien login`"
                        if "invalid" in str(exc).lower() else "")
                out.append(Check(FAIL, "gmail reachable", f"{exc}{hint}"))

    # the store
    try:
        if store is not None:
            summary = store.summary()
        else:
            with open_store(settings) as opened:
                summary = opened.summary()
        out.append(Check(OK, "store", " · ".join(f"{k} {v}" for k, v in summary.items())))
    except Exception as exc:
        out.append(Check(FAIL, "store", f"{type(exc).__name__}: {exc}"))

    return out
