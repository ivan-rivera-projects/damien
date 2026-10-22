"""Top-level commands."""

from __future__ import annotations

import json

import typer

from damien import cli as c
from damien.cli import app

from .. import __version__, passes, version_note
from ..config import write_env
from ..doctor import checks as doctor_checks
from ..gmail import GmailError
from ..models import Action
from ..plan import (
    load_messages,
)
from ..profile import write_profile
from ..rules import (
    default_rules,
    save_rules,
)
from ..store import open_store


@app.command()
def version() -> None:
    """Print the version. A stale editable install is called out rather than hidden."""
    typer.echo(f"damien {__version__}")
    note = version_note()
    if note:
        typer.echo(note)

@app.command()
def login() -> None:
    """Sign in to Gmail (installed-app OAuth; writes a token to ~/.damien)."""
    settings = c._load()
    gmail = c._gmail(settings)
    try:
        who = gmail.login()
    except GmailError as exc:
        typer.secho(f"login failed: {exc}", fg="red")
        raise typer.Exit(1) from exc
    typer.secho(f"signed in as {who or 'unknown'}", fg="green")
    typer.echo(f"token stored at {settings.token_path}")

@app.command()
def status(json_out: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """What is configured, what has been decided, what has been done."""
    settings = c._load()
    with open_store(settings) as store:
        info = {
            "config": settings.redacted(),
            "store": store.summary(),
            "rules_file": str(settings.root / "rules.json"),
        }
    if json_out:
        typer.echo(json.dumps(info, indent=2, default=str))
        return
    typer.secho("damien", bold=True)
    for key, value in info["config"].items():
        typer.echo(f"  {key:<18} {value}")
    typer.echo("  store")
    for key, value in info["store"].items():
        typer.echo(f"    {key:<14} {value}")

@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite what is already there.")) -> None:
    """Set up this install: profile, rules and a config file. Safe to re-run.

    Nothing here is required to run Damien on the defaults — but `profile.json` is where
    you tell it whose inbox this is, which is the difference between a tool that files your
    mail and one that files mail.
    """
    settings = c._load()
    root = settings.root
    root.mkdir(parents=True, exist_ok=True)

    path, written = write_profile(root, force=force)
    typer.secho(f"{'wrote' if written else 'kept '} {path}", fg="green" if written else "yellow")

    rules_path = root / "rules.json"
    if not rules_path.exists() or force:
        save_rules(rules_path, default_rules())
        typer.secho(f"wrote {rules_path}", fg="green")
    else:
        typer.secho(f"kept  {rules_path}", fg="yellow")

    env_path, env_written = write_env(root, force=force)
    typer.secho(f"{'wrote' if env_written else 'kept '} {env_path}", fg="green" if env_written else "yellow")

    typer.echo()
    typer.secho("next:", bold=True)
    typer.echo(f"  1. edit {path} — describe whose inbox this is and what belongs in it")
    typer.echo(f"  2. edit {env_path} — your account, and the judge you want (rules | openrouter | typesafe | local)")
    typer.echo("  3. damien login                      # Gmail, read/write in one browser tab")
    typer.echo("  4. damien plan build -q 'in:inbox' --limit 200 --show 20")
    typer.echo("  5. read it, then: damien plan approve <plan_id>, and damien plan apply <plan_id> --token <t> --yes")
    typer.echo("  6. calibrate the model once: damien judge eval   — then schedule damien pass weekly")

@app.command()
def doctor(json_out: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """Check this install and name the fix for anything missing.

    Changes nothing: it reads your config, profile, rules, credential and store, makes at
    most one live Gmail call, and prints a fix beside each finding. Exits non-zero only when
    something actually blocks you.
    """
    settings = c._load()
    rows = doctor_checks(settings, rules=c._rules(settings, None, seed=False))
    if json_out:
        typer.echo(json.dumps({"checks": [r.__dict__ for r in rows],
                               "ok": not any(r.blocking for r in rows)}, indent=2))
    else:
        colour = {"ok": "green", "warn": "yellow", "fail": "red"}
        mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
        typer.secho("damien doctor", bold=True)
        for r in rows:
            typer.secho(f"  {mark[r.status]}  {r.name:<17} {r.detail}", fg=colour[r.status])
        blocking = sum(1 for r in rows if r.blocking)
        soft = sum(1 for r in rows if r.status == "warn")
        typer.echo()
        typer.echo(f"  {len(rows)} checks: {len(rows) - blocking - soft} ok · "
                   f"{soft} to look at · {blocking} blocking")
        if blocking:
            typer.secho("  the fix for each line above is in docs/SETUP.md", dim=True)
        else:
            typer.secho("  ready: damien plan build -q 'in:inbox' --limit 200 --show 20", dim=True)
    if any(r.blocking for r in rows):
        raise typer.Exit(1)

@app.command()
def scan(
    query: str = typer.Option("in:inbox", "--query", "-q", help="Gmail search query."),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Stop after this many messages."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List and cache headers for a query. Reads only; decides nothing."""
    settings = c._load()
    gmail = c._gmail(settings)
    with open_store(settings) as store:
        try:
            ids = gmail.list_ids(query, limit=limit)
            messages, _fetched = load_messages(store=store, gmail=gmail, ids=ids,
                                               ttl_hours=settings.header_ttl_hours)
        except GmailError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1) from exc
        store.upsert_messages(messages)
        cached = store.count_messages()
    if json_out:
        typer.echo(json.dumps({"query": query, "matched": len(ids), "cached": cached}, indent=2))
        return
    typer.secho(f"{len(ids)} message(s) matched {query!r}; {cached} cached in the store", fg="green")

@app.command("pass")
def pass_(
    period: str = typer.Argument(..., help="weekly (the last 7 days) | monthly (the calendar month just gone)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build and report; write nothing to Gmail."),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Cap how much this pass looks at."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """One scheduled pass: rules first, the model on what they left, applied only where certain.

    It approves its own plan in-process, so a scheduler needs no human step. What it is
    not sure of stays in the plan and is reported, never applied.
    """
    if period not in ("weekly", "monthly"):
        c._fail(f"usage: damien pass weekly|monthly — not {period!r}", json_out, 2)
    settings = c._load()
    report = passes.run_pass(settings=settings, period=period, dry_run=dry_run, limit=limit,
                             gmail=c._gmail(settings))
    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
    else:
        for line in passes.render(report):
            typer.echo(line)
    if not report["ok"]:
        raise typer.Exit(1)

@app.command()
def restore(
    message_id: list[str] = typer.Argument(..., help="One or more message ids."),
    note: str | None = typer.Option(None, "--note"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Put a message back in the inbox, and record that you wanted it there.

    The inverse of trashing one message — because a whole run is a blunt thing to undo
    when the model got exactly one of three hundred wrong. Also records the correction,
    so the next plan agrees with you.
    """
    settings = c._load()
    gmail = c._gmail(settings)
    try:
        n = gmail.untrash(message_id)
    except GmailError as exc:
        c._fail(str(exc), json_out)
    with open_store(settings) as store:
        for mid in message_id:
            store.correct(mid, Action.keep, note=note)
        refreshed = gmail.fetch(message_id)
        store.upsert_messages(refreshed)
    if json_out:
        typer.echo(json.dumps({"ok": True, "restored": n, "message_ids": message_id}))
        return
    typer.secho(f"restored {n} message(s) to the inbox", fg="green")
    typer.secho("recorded as a correction, so the next plan keeps it", dim=True)

@app.command()
def correct(
    message_id: str,
    action: str = typer.Option(..., "--action", help="What it should have been: keep|archive|trash|label|mark_read"),
    label: str | None = typer.Option(None, "--label", help="Required with --action label."),
    note: str | None = typer.Option(None, "--note"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Record what a person decided about one message. This is the training signal."""
    try:
        parsed = Action(action)
    except ValueError:
        c._fail(f"unknown action: {action}", json_out, 2)
    if parsed is Action.label and not (label or "").strip():
        c._fail("--action label needs --label NAME: a label correction without a label cannot be applied", json_out, 2)
    settings = c._load()
    with open_store(settings) as store:
        store.correct(message_id, parsed, label=label, note=note)
    if json_out:
        typer.echo(json.dumps({"ok": True, "message_id": message_id, "action": parsed.value, "label": label}))
        return
    typer.secho(f"recorded correction for {message_id}: {parsed.value}", fg="green")
    typer.secho("it outranks a rule or a model on every later plan", dim=True)
