"""`damien rules ...` commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from damien import cli as c
from damien.cli import rules_app

from ..embed import DEFAULT_SIMILARITY
from ..gmail import GmailError
from ..models import Action
from ..plan import (
    plan_from_rules,
)
from ..rules import (
    default_rules,
    load_rules,
    mint_rules,
    mint_rules_by_sender,
    save_rules,
    sender_key,
)
from ..store import open_store


@rules_app.command("init")
def rules_init(force: bool = typer.Option(False, "--force")) -> None:
    """Write the starter rules file."""
    settings = c._load()
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
    settings = c._load()
    gmail = c._gmail(settings)
    rules = c._rules(settings, rules_path)
    if rule:
        wanted = set(rule)
        rules = [r for r in rules if r.id in wanted or r.name in wanted]
        if not rules:
            c._fail(f"no such rule: {', '.join(rule)}", json_out)
    stats: dict = {}
    with open_store(settings) as store:
        try:
            plan = plan_from_rules(store=store, gmail=gmail, rules=rules, limit=limit, sample=sample,
                                   scope=scope, protected_labels=settings.protect_labels, stats=stats)
        except GmailError as exc:
            c._fail(str(exc), json_out)
        counts = plan.counts()
        picked = {i.message_id: i for i in plan.items[:max(sample, 1)]}
        rows = [(picked[m.id], m) for m in store.get_messages(list(picked))]
    skipped = list(stats.get("skipped_rules") or [])

    if json_out:
        typer.echo(json.dumps({"plan": plan.id, "counts": counts,
                               "skipped_rules": [{"rule": r, "reason": why} for r, why in skipped],
                               "sample": [{"message_id": i.message_id, "action": i.action.value,
                                           "label": i.label, "rule_id": i.rule_id,
                                           "keep_in_inbox": i.keep_in_inbox,
                                           "subject": m.subject, "from": m.from_addr} for i, m in rows]},
                              indent=2, default=str))
        return
    typer.secho(f"plan {plan.id}  ({len(plan.items):,} item(s))  from {plan.query}", bold=True)
    for action, n in sorted(counts.items()):
        typer.echo(f"  {action:<10} {n:,}")
    if skipped:
        typer.secho(f"\nskipped {len(skipped)} rule(s):", fg="yellow")
        for rule_id, why in skipped:
            typer.secho(f"  {rule_id:<24} {why}", fg="yellow")
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
    stops one sender's mail landing in two different places. A sender is the domain,
    except on freemail (gmail.com, outlook.com, ...) where it is the whole address —
    a rule for all of gmail.com would file every person who writes from it.
    """
    settings = c._load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            c._fail(f"no such plan: {plan_id}", False)
        by_id = {m.id: m for m in store.get_messages([i.message_id for i in plan.items])}
        tally: dict[tuple[str, Action, str | None], int] = {}
        for item in plan.items:
            m = by_id.get(item.message_id)
            key = sender_key(m.from_addr) if m else None
            if key:
                tally[(key, item.action, item.label)] = tally.get((key, item.action, item.label), 0) + 1
        if not tally:
            c._fail(f"no plan items with senders for {plan_id}", False)
        counts = [(key, action, label, n) for (key, action, label), n in tally.items()]
        minted, why = mint_rules_by_sender(counts, min_count=min_count, agreement=agreement, max_rules=max_rules)
        # A rule is permanent, so it may not contradict a person: drop any sender whose
        # mail was corrected to something else.
        refused: dict[str, list] = {}
        for rule in minted:
            senders = [m for m in by_id.values() if sender_key(m.from_addr) == rule.match[0].value]
            clash = c._corrected_against(store, senders, rule.action.value)
            if clash:
                refused[rule.match[0].value] = clash
    minted = [r for r in minted if r.match[0].value not in refused]
    typer.secho(why, bold=True)
    for sender, clash in refused.items():
        subject, corr = clash[0]
        typer.secho(f"refusing {sender}: {len(clash)} message(s) corrected to {corr['action']!r} — e.g. {subject[:40]!r}",
                    fg="red")
    if not minted:
        typer.secho("nothing to mint", fg="yellow")
        if refused:
            raise typer.Exit(1)
        return
    from ..rules import to_query
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
    group: str = typer.Option(..., "--group", "-g", help="N or N:fp, as printed by `plan digest`."),
    similarity: float = typer.Option(DEFAULT_SIMILARITY, "--similarity"),
    yes: bool = typer.Option(False, "--yes", help="Required: this writes rules.json."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Express an approved group as rules, so this mail never needs judging again."""
    settings = c._load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            c._fail(f"no such plan: {plan_id}", False)
        try:
            found = c._pick_group(plan, store, group, similarity)
        except ValueError as exc:
            c._fail(str(exc), False)
        messages = store.get_messages(found.message_ids)
        # A rule makes a decision permanent, so it may not contradict the person —
        # not in this group, and not among the senders it would capture.
        conflicts = c._corrected_against(store, messages, found.action, set(found.message_ids))
        if conflicts:
            shown = "; ".join(f"{subject[:40]!r} -> {c['action']}" for subject, c in conflicts[:3])
            typer.secho(
                f"refusing: {len(conflicts)} message(s) from the same sender(s) were corrected to "
                f"{conflicts[0][1]['action']!r}, which is not what this group says ({found.action}) — {shown}", fg="red")
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
        from ..rules import to_query
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
    from ..rules import to_query
    settings = c._load()
    rules = c._rules(settings, None)
    rows = [{"id": r.id, "name": r.name, "action": r.action.value, "label": r.label,
             "gmail_query": to_query(r), "enabled": r.enabled} for r in rules]
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        server = r["gmail_query"] or "(runs in Python over listed mail)"
        typer.echo(f"  {r['id']:<22} {r['action']:<9} {server}")
    from ..rules import shadowed
    clashes = shadowed(rules)
    if clashes:
        typer.secho(f"\n{len(clashes)} rule(s) can never fire — a broader one runs first:", fg="yellow")
        for broad, narrow in clashes:
            typer.secho(f"  {narrow.id} ({narrow.match[0].value} -> {narrow.action.value}) "
                        f"is shadowed by {broad.id} ({broad.match[0].value} -> {broad.action.value})", fg="yellow")
