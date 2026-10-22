"""`damien plan ...` commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from damien import cli as c
from damien.cli import plan_app

from ..embed import DEFAULT_SIMILARITY
from ..gmail import GmailError
from ..models import Action
from ..plan import (
    apply_plan,
    approve,
    build_plan,
    digest_plan,
    sample_plan,
)
from ..profile import load_profile
from ..store import VerdictCache, open_store


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
    settings = c._load()
    gmail = c._gmail(settings)
    rules = c._rules(settings, rules_path)
    stats: dict[str, int] = {}
    with open_store(settings) as store:
        spec = load_profile(settings.root).spec()
        judge = c._judge(settings, judge_mode, spec, cache=VerdictCache(store))
        try:
            plan = build_plan(store=store, gmail=gmail, rules=rules, judge=judge, query=query, limit=limit,
                              spec=spec,
                              ttl_hours=settings.header_ttl_hours, stats=stats,
                              judge_batch=settings.judge_batch, ask_below=settings.confidence_ask,
                              protected_labels=settings.protect_labels, model=settings.model,
                              read_bodies=settings.read_bodies)
        except GmailError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1) from exc
        counts = plan.counts()
        rows = sample_plan(plan, store, n=show)
        spent = getattr(judge, "spent_usd", 0.0)
        calls = getattr(judge, "calls", 0)
        cached_calls = getattr(judge, "cached", 0)

    if json_out:
        typer.echo(json.dumps({"plan": plan.id, "counts": counts,
                               "sample": [r.__dict__ for r in rows],
                               "judge_calls": calls, "judge_cached": cached_calls,
                               "judge_usd": round(spent, 6),
                               # what the pass did besides judging: messages read for
                               # escalation, headers from cache, bodies fetched
                               "stats": stats}, indent=2, default=str))
        return

    typer.secho(f"plan {plan.id}  ({len(plan.items)} item(s))", bold=True)
    if stats:
        typer.echo(f"  headers: {stats['cached']} from cache · {stats['fetched']} fetched from Gmail")
        if stats.get("escalated"):
            typer.echo(f"  bodies:  {stats['escalated']} read, for the messages the snippet could not settle")
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
    group: str = typer.Option(..., "--group", "-g", help="N or N:fp, as printed by `plan digest`."),
    token: str = typer.Option(..., "--token"),
    yes: bool = typer.Option(False, "--yes", help="Required: this writes to Gmail."),
    similarity: float = typer.Option(DEFAULT_SIMILARITY, "--similarity"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Apply one reviewed group — and only its messages."""
    if not yes:
        c._fail("refusing to write without --yes", json_out, 2)
    settings = c._load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            c._fail(f"no such plan: {plan_id}", json_out)
        try:
            found = c._pick_group(plan, store, group, similarity)
        except ValueError as exc:
            c._fail(str(exc), json_out)
        if not json_out:
            typer.secho(f"group {group}: {len(found.message_ids)} message(s) -> "
                        f"{found.action}{' ' + found.label if found.label else ''}  ({found.domains})", bold=True)
            for m in store.get_messages(found.message_ids[:5]):
                typer.echo(f"    {(m.subject or '')[:70]!r}")
    gmail = c._gmail(settings)
    with open_store(settings) as store:
        try:
            run = apply_plan(store=store, gmail=gmail, plan_id=plan_id, token=token,
                             message_ids=found.message_ids)
        except (ValueError, GmailError) as exc:
            c._fail(str(exc), json_out)
    if json_out:
        typer.echo(json.dumps({"ok": True, "run": run.id, "status": run.status, "applied": run.applied,
                               "batches": len(run.batches)}))
        return
    typer.secho(f"run {run.id} {run.status}: {run.applied} message(s) in {len(run.batches)} batch(es)", fg="green")
    typer.secho(f"to take it back: damien run undo {run.id} --yes", dim=True)

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
    Each group prints as `N:fp`; passing that back to `apply-group`/`from-group`
    refuses if the group has changed since you read it.
    """
    settings = c._load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            c._fail(f"no such plan: {plan_id}", json_out)
        embedder = None
        if embed_missing and store.missing_embeddings([i.message_id for i in plan.items]):
            from ..embed import Embedder
            try:
                embedder = Embedder()
            except ImportError:
                if not json_out:
                    typer.secho("(fastembed is not installed — grouping on what is already embedded)", dim=True)
        groups = digest_plan(plan, store, similarity=similarity, embedder=embedder)

    if json_out:
        typer.echo(json.dumps({"plan": plan.id, "messages": len(plan.items),
                               "groups": [{"group": f"{n}:{c._fingerprint(g.message_ids)}", **g.__dict__}
                                          for n, g in enumerate(groups, 1)]}, indent=2))
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
        typer.echo(f"  {n:>3}:{c._fingerprint(group.message_ids)} {len(group.message_ids):>4}  {where}{group.domains}")
        typer.echo(f"          e.g. {group.sample_subject[:70]!r}")
    typer.secho(f"\n{len(plan.items)} message(s) → {len(groups)} decision(s)", dim=True)
    typer.secho(f"apply one: damien plan apply-group {plan.id} --group <n:fp> --token <t> --yes", dim=True)
    typer.secho(f"keep it clean: damien rules from-group {plan.id} --group <n:fp> --yes", dim=True)

@plan_app.command("show")
def plan_show(plan_id: str, sample: int = typer.Option(20, "--sample"), json_out: bool = typer.Option(False, "--json")) -> None:
    """Read one plan — counts and a stratified sample. Never shows the approval token."""
    settings = c._load()
    with open_store(settings) as store:
        plan = store.get_plan(plan_id)
        if plan is None:
            c._fail(f"no such plan: {plan_id}", json_out)
        rows = sample_plan(plan, store, n=sample)
    if json_out:
        typer.echo(json.dumps({"id": plan.id, "status": plan.status, "query": plan.query,
                               "counts": plan.counts(), "approved": plan.token is not None,
                               "judge_usd": plan.judge_usd, "judge_calls": plan.judge_calls,
                               "judge_cached": plan.judge_cached,
                               "sample": [r.__dict__ for r in rows]}, indent=2, default=str))
        return
    typer.secho(f"plan {plan.id}  status={plan.status}  query={plan.query!r}", bold=True)
    for action, n in sorted(plan.counts().items()):
        typer.echo(f"  {action:<10} {n}")
    for row in rows:
        typer.echo(f"  {row.action:<9} {(row.label or ''):<12} {row.source:<6}   {row.subject[:60]!r} <{row.from_addr}>")

@plan_app.command("approve")
def plan_approve(plan_id: str, json_out: bool = typer.Option(False, "--json")) -> None:
    """Mint the approval token for a plan. Prints it once — only its hash is stored."""
    settings = c._load()
    with open_store(settings) as store:
        try:
            token = approve(store, plan_id)
        except ValueError as exc:
            c._fail(str(exc), json_out)
    if json_out:
        typer.echo(json.dumps({"ok": True, "plan": plan_id, "token": token}))
        return
    typer.secho(f"token: {token}", bold=True)
    typer.echo(f"  damien plan apply {plan_id} --token {token} --yes")
    typer.secho("  (shown once — approve again for a fresh one)", dim=True)

@plan_app.command("apply")
def plan_apply(
    plan_id: str,
    token: str = typer.Option(..., "--token"),
    yes: bool = typer.Option(False, "--yes", help="Required: this writes to Gmail."),
    only: str | None = typer.Option(None, "--only", help="Apply one action of the plan: trash|archive|label|mark_read."),
    min_confidence: float | None = typer.Option(None, "--min-confidence",
                                               help="Apply only what the model was sure about (rules and your own decisions always pass)."),
    mark_read_when_filed: bool | None = typer.Option(
        None, "--mark-read-when-filed/--no-mark-read-when-filed",
        help="Filing a message (archiving, labelling it out of the inbox) also marks it read, "
             "so unread keeps meaning 'not looked at yet'. Defaults to DAMIEN_MARK_READ_WHEN_FILED."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Apply an approved plan, in bulk batches, journalling every one.

    `--only trash` applies exactly the slice you read — the rest of the plan stays
    approved for a later pass.
    """
    if not yes:
        c._fail("refusing to write without --yes", json_out, 2)
    parsed_only: Action | None = None
    if only is not None:
        try:
            parsed_only = Action(only)
        except ValueError:
            c._fail(f"unknown action for --only: {only}", json_out, 2)
    settings = c._load()
    gmail = c._gmail(settings)
    filing_marks_read = (settings.mark_read_when_filed if mark_read_when_filed is None
                         else mark_read_when_filed)
    with open_store(settings) as store:
        try:
            run = apply_plan(store=store, gmail=gmail, plan_id=plan_id, token=token, only=parsed_only,
                             min_confidence=min_confidence, mark_read_when_filed=filing_marks_read)
        except (ValueError, GmailError) as exc:
            c._fail(str(exc), json_out)
    if json_out:
        typer.echo(json.dumps({"ok": True, "run": run.id, "status": run.status, "applied": run.applied,
                               "batches": len(run.batches)}))
        return
    slice_note = f" ({parsed_only.value} slice)" if parsed_only else ""
    if min_confidence is not None:
        slice_note += f" at confidence >= {min_confidence}"
    if filing_marks_read:
        slice_note += " · filing marks read"
    typer.secho(f"run {run.id} {run.status}: {run.applied} message(s) in {len(run.batches)} batch(es){slice_note}", fg="green")
    if parsed_only:
        typer.secho("the rest of the plan is still approved", dim=True)
    typer.secho(f"to take it back: damien run undo {run.id} --yes", dim=True)
