"""The CLI. One binary, complete operations, nothing running in the background.

    damien login                                  sign in to Gmail once
    damien doctor                                 check the install; names the fix
    damien pass weekly|monthly [--dry-run]        one scheduled pass: build, approve, apply the certain
    damien scan    --query 'in:inbox older_than:1y'
    damien plan build --query ... [--limit N]      decide, write nothing
    damien plan show  <id> [--sample 20]           read the plan before it happens
    damien plan approve <id>                       mint the token (printed once)
    damien plan apply <id> --token T --yes         do it, in bulk, journaled
    damien run list | run undo <run-id> --yes
    damien rules init | rules list
    damien judge eval                             calibrate the model's act threshold
    damien skill install --target openclaw         install the agent skill
    damien correct <message-id> --action keep --note "that was a client"

Everything prints plain text unless `--json` is given, and every command is safe
to run twice. `plan build` never writes to Gmail; only `plan apply` does.
"""


from __future__ import annotations

import hashlib
import json
from pathlib import Path

import typer

from ..config import Settings, load_settings
from ..gmail import Gmail
from ..jev import FakeJudge, make_judge
from ..plan import group_by_index
from ..rules import default_rules, load_rules, save_rules, sender_key

app = typer.Typer(add_completion=False, help="Damien — Gmail triage: rules first, a System One model for judgment, code for every write.")
plan_app = typer.Typer(help="Build, review and apply a plan. Only `apply` writes.")
run_app = typer.Typer(help="Runs that were applied, and how to take them back.")
rules_app = typer.Typer(help="The deterministic rules.")
judge_app = typer.Typer(help="The decision model: how far its confidence can be trusted.")
skill_app = typer.Typer(help="The agent skill that teaches a host agent to run Damien.")
app.add_typer(plan_app, name="plan")
app.add_typer(run_app, name="run")
app.add_typer(rules_app, name="rules")
app.add_typer(judge_app, name="judge")
app.add_typer(skill_app, name="skill")

SEEDED = "damien-seed"
SKILL_NAME = "damien-mail-triage"


def _load() -> Settings:
    return load_settings()


def _rules(settings: Settings, path: Path | None, *, seed: bool = True) -> list:
    """The rules in force. `seed=False` is for read-only callers (doctor): no file is written."""
    target = path or settings.root / "rules.json"
    rules = load_rules(target)
    if not rules and not target.exists():
        rules = default_rules()
        if seed:
            save_rules(target, rules)
    return rules


def _gmail(settings: Settings) -> Gmail:
    return Gmail(settings)


def _judge(settings: Settings, mode: str, spec, cache=None):
    if mode == "none":
        return None
    if mode == "fake":
        return FakeJudge()
    return make_judge(settings, spec, cache=cache)


def _fail(message: str, json_out: bool, code: int = 1):
    """One way to refuse: plain red text, or `{"ok": false, "error": ...}` for an agent."""
    if json_out:
        typer.echo(json.dumps({"ok": False, "error": message}))
    else:
        typer.secho(message, fg="red" if code == 1 else "yellow")
    raise typer.Exit(code)


def _fingerprint(ids: list[str]) -> str:
    """Six hex of the group's sorted ids: a digest number that names the wrong group is refused."""
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:6]


def _parse_group(raw: str) -> tuple[int, str | None]:
    number, _, fp = raw.partition(":")
    try:
        return int(number), (fp.strip().lower() or None)
    except ValueError:
        raise typer.BadParameter(f"--group takes N or N:fp from `plan digest`, not {raw!r}") from None


def _pick_group(plan, store, raw: str, similarity: float):
    """The group a person read in the digest — refused when its fingerprint no longer matches."""
    index, fp = _parse_group(raw)
    found = group_by_index(plan, store, index, similarity=similarity)
    if fp is not None and _fingerprint(found.message_ids) != fp:
        raise ValueError(f"group {index} is no longer {index}:{fp} (it is {index}:{_fingerprint(found.message_ids)}) — "
                         f"run `damien plan digest {plan.id}` again and pick from what it shows now")
    return found


def _corrected_against(store, messages, action: str, ids: set[str] | None = None) -> list[tuple[str, dict]]:
    """Person corrections that a rule for these senders would contradict: (subject, correction)."""
    keys = {sender_key(m.from_addr) for m in messages if m.from_addr}
    latest = store.latest_corrections()
    if not latest:
        return []
    by_id = {m.id: m for m in store.get_messages(list(latest))}
    out = []
    for mid, c in latest.items():
        m = by_id.get(mid)
        hit = (ids is not None and mid in ids) or (m is not None and sender_key(m.from_addr) in keys)
        if hit and c["action"] != action:
            out.append(((m.subject if m else "") or "", c))
    return out


def main() -> None:
    app()


from . import core, plans, rules_cmds, runs  # noqa: E402,F401
