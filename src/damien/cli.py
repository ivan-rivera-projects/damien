"""The CLI. One binary, complete operations, nothing running in the background.

    damien login                                  sign in to Gmail once
    damien scan    --query 'in:inbox older_than:1y'
    damien plan build --query ... [--limit N]      decide, write nothing
    damien plan show  <id> [--sample 20]           read the plan before it happens
    damien plan approve <id>                       mint the token
    damien plan apply <id> --token T --yes         do it, in bulk, journaled
    damien run list | run undo <run-id>
    damien rules init | rules list
    damien correct <message-id> --action keep --note "that was a client"

Everything prints plain text unless `--json` is given, and every command is safe
to run twice. `plan build` never writes to Gmail; only `plan apply` does.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import __version__
from .config import Settings, load_settings, write_env
from .decide import DEFAULT_SPEC
from .doctor import checks as doctor_checks
from .profile import load_profile, write_profile
from .embed import DEFAULT_SIMILARITY
from .gmail import Gmail, GmailError
from .jev import FakeJudge, make_judge
from .models import Action
from .plan import (
    apply_plan,
    approve,
    build_plan,
    digest_plan,
    group_by_index,
    load_messages,
    plan_from_rules,
    sample_plan,
    undo_run,
)
from .rules import (
    default_rules,
    load_rules,
    mint_rules,
    mint_rules_by_sender,
    save_rules,
)
from .store import VerdictCache, open_store

app = typer.Typer(add_completion=False, help="Damien — Gmail triage: rules first, a System One model for judgment, code for every write.")
plan_app = typer.Typer(help="Build, review and apply a plan. Only `apply` writes.")
run_app = typer.Typer(help="Runs that were applied, and how to take them back.")
rules_app = typer.Typer(help="The deterministic rules.")
app.add_typer(plan_app, name="plan")
app.add_typer(run_app, name="run")
app.add_typer(rules_app, name="rules")

SEEDED = "damien-seed"


def _load() -> Settings:
    return load_settings()


def _rules(settings: Settings, path: Path | None) -> list:
    target = path or settings.root / "rules.json"
    rules = load_rules(target)
    if not rules and not target.exists():
        rules = default_rules()
        save_rules(target, rules)
    return rules


def _gmail(settings: Settings) -> Gmail:
    return Gmail(settings)


def _judge(settings: Settings, mode: str, cache=None):
    if mode == "none":
        return None
    if mode == "fake":
        return FakeJudge()
    return make_judge(settings, DEFAULT_SPEC, cache=cache)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f"damien {__version__}")


@app.command()
def login() -> None:
    """Sign in to Gmail (installed-app OAuth; writes a token to ~/.damien)."""
    settings = _load()
    gmail = _gmail(settings)
    try:
        who = gmail.login()
    except GmailError as exc:
        typer.secho(f"login failed: {exc}", fg="red")
        raise typer.Exit(1)
    typer.secho(f"signed in as {who or 'unknown'}", fg="green")
    typer.echo(f"token stored at {settings.token_path}")


@app.command()
def status(json_out: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """What is configured, what has been decided, what has been done."""
    settings = _load()
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
    settings = _load()
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
    typer.echo(f"  2. edit {env_path} — your account, and the judge you want (rules | openrouter | fake)")
    typer.echo("  3. damien login                      # Gmail, read/write in one browser tab")
    typer.echo("  4. damien plan build -q 'in:inbox' --limit 200 --show 20")
    typer.echo("  5. read it, then: damien plan approve <plan_id> && damien plan apply <plan_id> --token <t> --yes")


@app.command()
def doctor(json_out: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """Check this install and name the fix for anything missing.

    Changes nothing: it reads your config, profile, rules, credential and store, makes at
    most one live Gmail call, and prints a fix beside each finding. Exits non-zero only when
    something actually blocks you.
    """
    settings = _load()
    rows = doctor_checks(settings, rules=_rules(settings, None))
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
    settings = _load()
    gmail = _gmail(settings)
    with open_store(settings) as store:
        try:
            ids = gmail.list_ids(query, limit=limit)
            messages, _fetched = load_messages(store=store, gmail=gmail, ids=ids,
                                               ttl_hours=settings.header_ttl_hours)
        except GmailError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
        store.upsert_messages(messages)
        cached = store.count_messages()
    if json_out:
        typer.echo(json.dumps({"query": query, "matched": len(ids), "cached": cached}, indent=2))
        return
    typer.secho(f"{len(ids)} message(s) matched {query!r}; {cached} cached in the store", fg="green")


@plan_app.command("build")
def plan_build(
    query: str = typer.Option("in:inbox", "--query", "-q"),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Cap the number of messages this pass looks at."),
    judge_mode: str = typer.Option("auto", "--judge", help="auto | fake | none"),
    rules_path: Path | None = typer.Option(None, "--rules", help="Rules file (default ~/.damien/rules.json)."),
    show: int = typer.Option(12, "--show", help="Print this many sample items."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Decide what to do with a query. Writes to the store, never to Gmail."""
    settings = _load()
    gmail = _gmail(settings)
    rules = _rules(settings, rules_path)
    stats: dict[str, int] = {}
    with open_store(settings) as store:
        judge = _judge(settings, judge_mode, cache=VerdictCache(store))
        try:
            plan = build_plan(store=store, gmail=gmail, rules=rules, judge=judge, query=query, limit=limit,
                              spec=load_profile(settings.root).spec(),
                              ttl_hours=settings.header_ttl_hours, stats=stats,
                              judge_batch=settings.judge_batch, ask_below=settings.confidence_ask,
                              protected_labels=settings.protect_labels, model=settings.model)
        except GmailError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
        counts = plan.counts()
        rows = sample_plan(plan, store, n=show)
        spent = getattr(judge, "spent_usd", 0.0)
        calls = getattr(judge, "calls", 0)
        cached_calls = getattr(judge, "cached", 0)

    if json_out:
        typer.echo(json.dumps({"plan": plan.id, "counts": counts,
                               "sample": [r.__dict__ for r in rows],
                               "judge_calls": calls, "judge_cached": cached_calls,
                               "judge_usd": round(spent, 6)}, indent=2, default=str))
        return

    typer.secho(f"plan {plan.id}  ({len(plan.items)} item(s))", bold=True)
    if stats:
        typer.echo(f"  headers: {stats['cached']} from cache · {stats['fetched']} fetched from Gmail")
    for action, n in sorted(counts.items()):
        typer.echo(f"  {action:<10} {n}")
    if calls or cached_calls:
        typer.echo(f"  judgment: {calls} call(s), {cached_calls} from cache, ${spent:.4f}")
    typer.echo()
    typer.secho("sample", bold=True)
    for row in rows:
        age = f"{row.age_days:.0f}d" if row.age_days is not None else "  ?"
        typer.echo(f"  {row.action:<9} {(row.label or ''):<12} {row.source:<6} {age:>5}  {row.subject[:60]!r} <{row.from_addr}>")
    typer.echo()
    typer.secho("nothing has changed in Gmail. review, then:", dim=True)
    typer.echo(f"  damien plan approve {plan.id}")


@plan_app.command("apply-group")
def plan_apply_group(
    plan_id: str,
    group: int = typer.Option(..., "--group", "-g", help="The group number printed by `plan digest`."),
    token: str = typer.Option(..., "--token"),
    yes: bool = typer.Option(False, "--yes", help="Required: this writes to Gmail."),
    similarity: float = typer.Option(DEFAULT_SIMILARITY, "--similarity"),
) -> None:
    """Apply one reviewed group — and only its messages."""
    if not yes:
        typer.secho("refusing to write without --yes", fg="yellow")
        raise typer.Exit(2)
    settings = _load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            typer.secho(f"no such plan: {plan_id}", fg="red")
            raise typer.Exit(1)
        try:
            found = group_by_index(plan, store, group, similarity=similarity)
        except ValueError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
        typer.secho(f"group {group}: {len(found.message_ids)} message(s) -> "
                    f"{found.action}{' ' + found.label if found.label else ''}  ({found.domains})", bold=True)
        for mid in found.message_ids[:5]:
            [m] = store.get_messages([mid])
            typer.echo(f"    {(m.subject or '')[:70]!r}")
    gmail = _gmail(settings)
    with open_store(settings) as store:
        try:
            run = apply_plan(store=store, gmail=gmail, plan_id=plan_id, token=token,
                             message_ids=found.message_ids)
        except (ValueError, GmailError) as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
    typer.secho(f"run {run.id} {run.status}: {run.applied} message(s) in {len(run.batches)} batch(es)", fg="green")
    typer.secho(f"to take it back: damien run undo {run.id}", dim=True)


@plan_app.command("digest")
def plan_digest(
    plan_id: str,
    similarity: float = typer.Option(DEFAULT_SIMILARITY, "--similarity",
                                     help="How alike two messages must be to share a group."),
    embed_missing: bool = typer.Option(True, "--embed/--no-embed", help="Embed messages that have no vector yet."),
    min_size: int = typer.Option(1, "--min-size", help="Hide groups smaller than this."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Collapse a plan into review groups — one decision per group, not per message.

    `--min-size` is how a plan of thousands gets read: the big groups are the
    decisions, and the singletons are what `plan apply` handles by confidence.
    """
    settings = _load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            typer.secho(f"no such plan: {plan_id}", fg="red")
            raise typer.Exit(1)
        embedder = None
        if embed_missing and store.missing_embeddings([i.message_id for i in plan.items]):
            from .embed import Embedder
            try:
                embedder = Embedder()
            except ImportError:
                typer.secho("(fastembed is not installed — grouping on what is already embedded)", dim=True)
        groups = digest_plan(plan, store, similarity=similarity, embedder=embedder)

    if json_out:
        typer.echo(json.dumps({"plan": plan.id, "messages": len(plan.items),
                               "groups": [g.__dict__ for g in groups]}, indent=2))
        return

    shown = [g for g in groups if len(g.message_ids) >= min_size]
    hidden = [g for g in groups if len(g.message_ids) < min_size]
    typer.secho(f"plan {plan.id} — {len(plan.items)} message(s) in {len(groups)} group(s)", bold=True)
    if hidden:
        n = sum(len(g.message_ids) for g in hidden)
        typer.secho(f"  ({len(hidden)} group(s) of fewer than {min_size} hidden — {n} message(s))", dim=True)
    current = None
    for group in shown:
        if group.action != current:
            current = group.action
            typer.secho(f"\n{current}", bold=True)
        n = groups.index(group) + 1
        where = f"{group.label} · " if group.label else ""
        typer.echo(f"  {n:>3}. {len(group.message_ids):>4}  {where}{group.domains}")
        typer.echo(f"          e.g. {group.sample_subject[:70]!r}")
    typer.secho(f"\n{len(plan.items)} message(s) → {len(groups)} decision(s)", dim=True)
    typer.secho(f"apply one: damien plan apply-group {plan.id} --group <n> --token <t> --yes", dim=True)
    typer.secho(f"keep it clean: damien rules from-group {plan.id} --group <n> --yes", dim=True)


@plan_app.command("show")
def plan_show(plan_id: str, sample: int = typer.Option(20, "--sample"), json_out: bool = typer.Option(False, "--json")) -> None:
    """Read one plan — counts and a stratified sample."""
    settings = _load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            typer.secho(f"no such plan: {plan_id}", fg="red")
            raise typer.Exit(1)
        rows = sample_plan(plan, store, n=sample)
    if json_out:
        typer.echo(json.dumps({"id": plan.id, "status": plan.status, "query": plan.query,
                               "counts": plan.counts(), "token": plan.token,
                               "judge_usd": plan.judge_usd, "judge_calls": plan.judge_calls,
                               "judge_cached": plan.judge_cached,
                               "sample": [r.__dict__ for r in rows]}, indent=2, default=str))
        return
    typer.secho(f"plan {plan.id}  status={plan.status}  query={plan.query!r}  token={plan.token or '—'}", bold=True)
    for action, n in sorted(plan.counts().items()):
        typer.echo(f"  {action:<10} {n}")
    for row in rows:
        typer.echo(f"  {row.action:<9} {(row.label or ''):<12} {row.source:<6}   {row.subject[:60]!r} <{row.from_addr}>")


@plan_app.command("approve")
def plan_approve(plan_id: str) -> None:
    """Mint the approval token for a plan. Prints it once."""
    settings = _load()
    with open_store(settings) as store:
        try:
            token = approve(store, plan_id)
        except ValueError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
    typer.secho(f"token: {token}", bold=True)
    typer.echo(f"  damien plan apply {plan_id} --token {token} --yes")


@plan_app.command("apply")
def plan_apply(
    plan_id: str,
    token: str = typer.Option(..., "--token"),
    yes: bool = typer.Option(False, "--yes", help="Required: this writes to Gmail."),
    only: str | None = typer.Option(None, "--only", help="Apply one action of the plan: trash|archive|label|mark_read."),
    min_confidence: float | None = typer.Option(None, "--min-confidence",
                                               help="Apply only what the model was sure about (rules and your own decisions always pass)."),
) -> None:
    """Apply an approved plan, in bulk batches, journalling every one.

    `--only trash` applies exactly the slice you read — the rest of the plan stays
    approved for a later pass.
    """
    if not yes:
        typer.secho("refusing to write without --yes", fg="yellow")
        raise typer.Exit(2)
    parsed_only: Action | None = None
    if only is not None:
        try:
            parsed_only = Action(only)
        except ValueError:
            typer.secho(f"unknown action for --only: {only}", fg="red")
            raise typer.Exit(2)
    settings = _load()
    gmail = _gmail(settings)
    with open_store(settings) as store:
        try:
            run = apply_plan(store=store, gmail=gmail, plan_id=plan_id, token=token, only=parsed_only,
                             min_confidence=min_confidence)
        except (ValueError, GmailError) as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
    slice_note = f" ({parsed_only.value} slice)" if parsed_only else ""
    if min_confidence is not None:
        slice_note += f" at confidence >= {min_confidence}"
    typer.secho(f"run {run.id} {run.status}: {run.applied} message(s) in {len(run.batches)} batch(es){slice_note}", fg="green")
    if parsed_only:
        typer.secho("the rest of the plan is still approved", dim=True)
    typer.secho(f"to take it back: damien run undo {run.id}", dim=True)


@run_app.command("list")
def run_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Plans and their runs."""
    settings = _load()
    rows: list[dict] = []
    with open_store(settings) as store:
        for p in store.list_plans(20):
            run = store.last_run_for_plan(p["id"])
            rows.append({"plan": p["id"], "status": p["status"], "query": p["query"],
                         "run": run.id if run else None, "applied": run.applied if run else 0})
    if json_out:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    for row in rows:
        typer.echo(f"  {row['plan']}  {row['status']:<8} applied={row['applied']:<5} {row['query'][:40]!r}")


@run_app.command("undo")
def run_undo(run_id: str, yes: bool = typer.Option(False, "--yes")) -> None:
    """Replay a run's journal backwards."""
    if not yes:
        typer.secho("refusing to write without --yes", fg="yellow")
        raise typer.Exit(2)
    settings = _load()
    gmail = _gmail(settings)
    with open_store(settings) as store:
        try:
            run = undo_run(store=store, gmail=gmail, run_id=run_id)
        except (ValueError, GmailError) as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
    typer.secho(f"run {run.id} {run.status}", fg="green")


@rules_app.command("init")
def rules_init(force: bool = typer.Option(False, "--force")) -> None:
    """Write the starter rules file."""
    settings = _load()
    path = settings.root / "rules.json"
    if path.exists() and not force:
        typer.secho(f"{path} already exists (use --force to overwrite)", fg="yellow")
        raise typer.Exit(1)
    save_rules(path, default_rules())
    typer.secho(f"wrote {path}", fg="green")


@rules_app.command("plan")
def rules_plan(
    rule: list[str] = typer.Option(None, "--rule", "-r", help="Which rule(s). Omit for every translatable rule."),
    rules_path: Path | None = typer.Option(None, "--rules", help="A candidate rules file, instead of the live one."),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Cap the matches per rule."),
    sample: int = typer.Option(10, "--sample", help="Messages to fetch so the plan can be read."),
    scope: str = typer.Option("in:inbox", "--scope",
                              help="Gmail query the rules run inside, e.g. \"in:inbox after:2025/01/01 before:2026/01/01\". Empty = the whole mailbox."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Build a plan from rules Gmail can evaluate itself — no headers, no model.

    For a mailbox of this size this is the only pass that finishes: a hundred
    thousand messages cost a hundred listings and a few bulk writes.
    """
    settings = _load()
    gmail = _gmail(settings)
    rules = _rules(settings, rules_path)
    if rule:
        wanted = set(rule)
        rules = [r for r in rules if r.id in wanted or r.name in wanted]
        if not rules:
            typer.secho(f"no such rule: {', '.join(rule)}", fg="red")
            raise typer.Exit(1)
    with open_store(settings) as store:
        try:
            plan = plan_from_rules(store=store, gmail=gmail, rules=rules, limit=limit, sample=sample,
                                   scope=scope)
        except GmailError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
        counts = plan.counts()
        picked = {i.message_id: i for i in plan.items[:max(sample, 1)]}
        rows = [(picked[m.id], m) for m in store.get_messages(list(picked))]

    if json_out:
        typer.echo(json.dumps({"plan": plan.id, "counts": counts,
                               "sample": [{"message_id": i.message_id, "action": i.action.value,
                                           "label": i.label, "rule_id": i.rule_id,
                                           "keep_in_inbox": i.keep_in_inbox,
                                           "subject": m.subject, "from": m.from_addr} for i, m in rows]},
                              indent=2, default=str))
        return
    typer.secho(f"plan {plan.id}  ({len(plan.items):,} item(s))  from {plan.query}", bold=True)
    for action, n in sorted(counts.items()):
        typer.echo(f"  {action:<10} {n:,}")
    typer.echo()
    for item, m in rows:
        typer.echo(f"  {item.action.value:<9} {(m.subject or '')[:56]!r} <{m.from_addr}>")
    typer.secho("\nnothing has changed in Gmail. review, then:", dim=True)
    typer.echo(f"  damien plan approve {plan.id}")
    typer.echo(f"  damien plan apply {plan.id} --token <t> --yes")


@rules_app.command("from-senders")
def rules_from_senders(
    plan_id: str,
    min_count: int = typer.Option(5, "--min-count", help="Ignore senders with fewer messages than this."),
    agreement: float = typer.Option(0.9, "--agreement", help="How one-way a sender has to be."),
    max_rules: int = typer.Option(80, "--max", help="Stop after this many rules."),
    yes: bool = typer.Option(False, "--yes", help="Required: this writes rules.json."),
) -> None:
    """Make every consistently-decided sender permanent — the step that keeps it clean.

    A rule per sender is what stops a mailbox being re-judged every pass, and what
    stops one sender's mail landing in two different places.
    """
    settings = _load()
    with open_store(settings) as store:
        rows = list(store.conn.execute("""
            SELECT split_part(m.from_addr, '@', 2) AS domain, i.action, i.label, count(*) AS n
              FROM plan_items i JOIN messages m ON m.id = i.message_id
             WHERE i.plan_id = ? AND m.from_addr LIKE '%@%'
             GROUP BY 1, 2, 3""", (plan_id,)))
        if not rows:
            typer.secho(f"no plan items with senders for {plan_id}", fg="red")
            raise typer.Exit(1)

    counts = [(r["domain"], Action(r["action"]), r["label"], r["n"]) for r in rows]
    minted, why = mint_rules_by_sender(counts, min_count=min_count, agreement=agreement, max_rules=max_rules)
    typer.secho(why, bold=True)
    if not minted:
        typer.secho("nothing to mint", fg="yellow")
        return
    from .rules import to_query
    by_action: dict[str, list] = {}
    for rule in minted:
        by_action.setdefault(rule.action.value, []).append(rule)
    for action in sorted(by_action):
        typer.secho(f"\n{action}", bold=True)
        for rule in by_action[action]:
            typer.echo(f"  {rule.match[0].value:<32} {to_query(rule) or ''}")
    if not yes:
        typer.secho(f"\n{len(minted)} rule(s) not written — re-run with --yes", fg="yellow")
        return
    path = settings.root / "rules.json"
    existing = {r.id: r for r in load_rules(path)}
    for rule in minted:
        existing[rule.id] = rule
    save_rules(path, list(existing.values()))
    typer.secho(f"\nwrote {len(minted)} rule(s) to {path} ({len(existing)} total)", fg="green")


@rules_app.command("from-group")
def rules_from_group(
    plan_id: str,
    group: int = typer.Option(..., "--group", "-g"),
    similarity: float = typer.Option(DEFAULT_SIMILARITY, "--similarity"),
    yes: bool = typer.Option(False, "--yes", help="Required: this writes rules.json."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Express an approved group as rules, so this mail never needs judging again."""
    settings = _load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            typer.secho(f"no such plan: {plan_id}", fg="red")
            raise typer.Exit(1)
        try:
            found = group_by_index(plan, store, group, similarity=similarity)
        except ValueError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1)
        messages = store.get_messages(found.message_ids)
        # A rule makes a decision permanent, so it may not contradict the person —
        # not in this group, and not among the senders it would capture.
        domains = {(m.from_addr or "").split("@")[-1] for m in messages if m.from_addr}
        rows = list(store.conn.execute(
            """SELECT c.message_id, c.action, m.subject, m.from_addr
                 FROM corrections c JOIN messages m ON m.id = c.message_id"""))
        conflicts = [r for r in rows
                     if (r["message_id"] in set(found.message_ids) or (r["from_addr"] or "").split("@")[-1] in domains)
                     and r["action"] != found.action]
        if conflicts:
            shown = "; ".join(f"{(r['subject'] or '')[:40]!r} -> {r['action']}" for r in conflicts[:3])
            typer.secho(
                f"refusing: {len(conflicts)} message(s) from the same sender(s) were corrected to "
                f"{conflicts[0]['action']!r}, which is not what this group says ({found.action}) — {shown}", fg="red")
            typer.secho(
                "a rule is permanent, so it waits until the plan agrees with you: rebuild, then mint.", dim=True)
            raise typer.Exit(1)

    minted, why = mint_rules(messages, Action(found.action), found.label)
    typer.secho(f"group {group}: {len(found.message_ids)} message(s) -> {found.action}", bold=True)
    typer.secho(f"  why: {why}", dim=True)
    if not minted:
        typer.secho("  no rule minted — nothing to write", fg="yellow")
        raise typer.Exit(0)
    for rule in minted:
        from .rules import to_query
        typer.echo(f"  + {rule.id:<28} {rule.action.value:<9} {to_query(rule) or '(matched in Python)'}")
    if dry_run:
        typer.secho("(dry run — nothing written)", dim=True)
        return
    if not yes:
        typer.secho("refusing to write without --yes", fg="yellow")
        raise typer.Exit(2)

    path = settings.root / "rules.json"
    existing = load_rules(path)
    by_id = {r.id: r for r in existing}
    for rule in minted:
        by_id[rule.id] = rule
    save_rules(path, list(by_id.values()))
    typer.secho(f"wrote {len(minted)} rule(s) to {path}", fg="green")


@rules_app.command("list")
def rules_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Show the rules, and whether each can run as a Gmail query."""
    from .rules import to_query
    settings = _load()
    rules = _rules(settings, None)
    rows = [{"id": r.id, "name": r.name, "action": r.action.value, "label": r.label,
             "gmail_query": to_query(r), "enabled": r.enabled} for r in rules]
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        server = r["gmail_query"] or "(runs in Python over listed mail)"
        typer.echo(f"  {r['id']:<22} {r['action']:<9} {server}")
    from .rules import shadowed
    clashes = shadowed(rules)
    if clashes:
        typer.secho(f"\n{len(clashes)} rule(s) can never fire — a broader one runs first:", fg="yellow")
        for broad, narrow in clashes:
            typer.secho(f"  {narrow.id} ({narrow.match[0].value} -> {narrow.action.value}) "
                        f"is shadowed by {broad.id} ({broad.match[0].value} -> {broad.action.value})", fg="yellow")


@app.command()
def restore(
    message_id: list[str] = typer.Argument(..., help="One or more message ids."),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Put a message back in the inbox, and record that you wanted it there.

    The inverse of trashing one message — because a whole run is a blunt thing to undo
    when the model got exactly one of three hundred wrong. Also records the correction,
    so the next plan agrees with you.
    """
    settings = _load()
    gmail = _gmail(settings)
    try:
        n = gmail.untrash(message_id)
    except GmailError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1)
    with open_store(settings) as store:
        for mid in message_id:
            store.correct(mid, Action.keep, note=note)
        refreshed = gmail.fetch(message_id)
        store.upsert_messages(refreshed)
    typer.secho(f"restored {n} message(s) to the inbox", fg="green")
    typer.secho("recorded as a correction, so the next plan keeps it", dim=True)


@app.command()
def correct(
    message_id: str,
    action: str = typer.Option(..., "--action", help="What it should have been: keep|archive|trash|label|mark_read"),
    label: str | None = typer.Option(None, "--label"),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Record what a person decided about one message. This is the training signal."""
    settings = _load()
    try:
        parsed = Action(action)
    except ValueError:
        typer.secho(f"unknown action: {action}", fg="red")
        raise typer.Exit(1)
    with open_store(settings) as store:
        store.correct(message_id, parsed, label=label, note=note)
    typer.secho(f"recorded correction for {message_id}: {parsed.value}", fg="green")
    typer.secho("it outranks a rule or a model on every later plan", dim=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
