"""`damien run`, `judge`, `skill` commands."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import typer

from damien import cli as c
from damien.cli import judge_app, run_app, skill_app

from ..gmail import GmailError
from ..jev import make_judge
from ..plan import (
    undo_run,
)
from ..profile import load_profile
from ..store import VerdictCache, open_store


@run_app.command("list")
def run_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """Plans and their runs."""
    settings = c._load()
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
def run_undo(run_id: str, yes: bool = typer.Option(False, "--yes"), json_out: bool = typer.Option(False, "--json")) -> None:
    """Put back the state each message had when the plan was built (INBOX, UNREAD, label)."""
    if not yes:
        c._fail("refusing to write without --yes", json_out, 2)
    settings = c._load()
    gmail = c._gmail(settings)
    with open_store(settings) as store:
        try:
            run = undo_run(store=store, gmail=gmail, run_id=run_id)
        except (ValueError, GmailError) as exc:
            c._fail(str(exc), json_out)
    if json_out:
        typer.echo(json.dumps({"ok": True, "run": run.id, "status": run.status}))
        return
    typer.secho(f"run {run.id} {run.status}", fg="green")

@judge_app.command("eval")
def judge_eval(
    limit: int = typer.Option(500, "--limit", "-n", help="Judge at most this many known answers."),
    max_wrong: float = typer.Option(0.02, "--max-wrong", help="The wrong-rate the pass may accept at its threshold."),
    save: bool = typer.Option(True, "--save/--no-save", help="Write the threshold to calibration.json for `damien pass`."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Measure where this model's confidence can be trusted, against what you decided.

    Truth is your own corrections and the runs you kept. A threshold belongs to one model
    at one host: change either and the pass falls back to rules + corrections until this
    is run again.
    """
    from .. import calibrate
    settings = c._load()
    spec = load_profile(settings.root).spec()
    with open_store(settings) as store:
        judge = make_judge(settings, spec, cache=VerdictCache(store))
        if judge is None:
            c._fail(f"no decision model to calibrate (DAMIEN_JUDGE={settings.judge}) — set it and its key, then re-run",
                  json_out)
        result = calibrate.evaluate(store=store, settings=settings, spec=spec, judge=judge,
                                    limit=limit, max_wrong_rate=max_wrong)
    if save:                     # a result with no safe threshold also clears a stale one
        calibrate.save(settings, result)
    spent = round(getattr(judge, "spent_usd", 0.0) or 0.0, 6)
    if json_out:
        typer.echo(json.dumps({"ok": True, **result, "saved": save,
                               "judge_usd": spent}, indent=2, default=str))
        return
    typer.secho(f"{settings.model} @ {settings.judge_host() or settings.judge}: {result['n']} known answer(s)", bold=True)
    typer.echo(f"  {'t':>5}  {'acted':>6}  {'wrong':>6}")
    marks = {0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.99, result.get("act")}
    for row in result["thresholds"]:
        if round(row["t"], 2) in marks:
            typer.echo(f"  {row['t']:>5.2f}  {row['acted']:>6}  {row['wrong']:>6}")
    if result.get("act") is None:
        typer.secho(f"  no threshold with {calibrate.MIN_ACTED}+ acted messages keeps wrong <= {max_wrong:.0%} — "
                    "the pass applies rules and corrections only (correct more mail, or apply more runs, then re-run)",
                    fg="yellow")
    else:
        saved = " (saved — the next `damien pass` uses it)" if save else " (not saved)"
        typer.secho(f"  act at >= {result['act']}{saved}", fg="green")
    typer.echo(f"  cost so far ${spent}")

@skill_app.command("install")
def skill_install(
    target: str = typer.Option("openclaw", "--target", help="openclaw | hermes"),
    directory: Path | None = typer.Option(None, "--dir", help="A skills folder to install into instead of the host's "
                                                              "(e.g. a workspace `skills/`)."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing SKILL.md."),
) -> None:
    """Install the agent skill (SKILL.md) for a host agent."""
    if target not in ("openclaw", "hermes"):
        c._fail(f"unknown --target {target!r}: openclaw or hermes", False, 2)
    dest = (directory or Path.home() / f".{target}" / "skills").expanduser() / c.SKILL_NAME / "SKILL.md"
    if dest.exists() and not force:
        c._fail(f"{dest} already exists (use --force to overwrite)", False)
    text = resources.files("damien").joinpath("skill/SKILL.md").read_text(encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    typer.secho(f"wrote {dest}", fg="green")
    typer.secho("schedule it with the host: `damien pass weekly` on Mondays, `damien pass monthly` on the 1st", dim=True)
