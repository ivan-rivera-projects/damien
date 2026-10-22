"""One scheduled pass: rules first, the model on what they left, applied only where it is certain.

This replaces `scripts/damien-pass.sh`. Everything the pass does not apply is REPORTED, never
applied — that split is what makes it safe to run unattended. The pass approves its own plan
in-process, so a scheduler needs no human step and the token never leaves this function.

How certain "certain" is depends on the model: a threshold calibrated for one model means
nothing for another, so it comes from `calibrate.act_threshold`. With no calibration the pass
applies rules and the person's own corrections only, and says so.
"""

from __future__ import annotations

from datetime import date

from . import calibrate
from .config import Settings
from .gmail import Gmail, GmailError
from .jev import make_judge
from .models import Action, Source
from .plan import apply_plan, approve, build_plan
from .profile import load_profile
from .rules import default_rules, load_rules
from .store import VerdictCache, open_store

UNCALIBRATED = 1.01          # above any confidence: only rules and the person pass
NEEDS_REPLY_AT = 0.6
NEEDS_REPLY_MAX = 10


def window(period: str, today: date | None = None) -> str:
    """The Gmail query for a period. Monthly is the calendar month just gone, whatever today is."""
    if period == "weekly":
        return "in:inbox newer_than:7d"
    if period == "monthly":
        today = today or date.today()
        first = today.replace(day=1)
        prev = date(first.year - 1, 12, 1) if first.month == 1 else first.replace(month=first.month - 1)
        return f"in:inbox after:{prev:%Y/%m/%d} before:{first:%Y/%m/%d}"
    raise ValueError(f"unknown period {period!r}: weekly or monthly")


def run_pass(*, settings: Settings, period: str, dry_run: bool, limit: int | None,
             today: date | None = None, gmail: Gmail | None = None) -> dict:
    """Build, approve and apply one pass; return everything a report or an agent needs.

    `ok` is False only on a real failure (`error` says which step). A pass with nothing
    to do is ok.
    """
    query = window(period, today)
    act = calibrate.act_threshold(settings)
    report: dict = {
        "ok": True, "error": None, "period": period, "window": query, "dry_run": dry_run,
        "judge": settings.judge, "judge_host": settings.judge_host(), "judge_local": settings.judge_is_local(),
        "calibrated_act": act, "min_confidence": act if act is not None else UNCALIBRATED,
        "read_bodies": settings.read_bodies,
        "plan": None, "counts": {}, "total": 0, "judge_calls": 0, "judge_cached": 0, "judge_usd": 0.0,
        "judge_failed": 0, "bodies_read": 0, "certain": 0, "waiting": 0, "skipped": [],
        "applied": 0, "run": None, "needs_reply": [],
    }
    gmail = gmail or Gmail(settings)
    rules_path = settings.root / "rules.json"
    rules = load_rules(rules_path) if rules_path.exists() else default_rules()
    spec = load_profile(settings.root).spec()
    stats: dict = {}
    with open_store(settings) as store:
        judge = make_judge(settings, spec, cache=VerdictCache(store))
        try:
            plan = build_plan(store=store, gmail=gmail, rules=rules, judge=judge, query=query, limit=limit,
                              spec=spec, ttl_hours=settings.header_ttl_hours, stats=stats,
                              judge_batch=settings.judge_batch, ask_below=settings.confidence_ask,
                              protected_labels=settings.protect_labels, model=settings.model,
                              read_bodies=settings.read_bodies)
        except (GmailError, ValueError) as exc:
            return {**report, "ok": False, "error": f"the plan did not build: {exc}"}
        counts = plan.counts()
        report.update(plan=plan.id, counts=counts, total=sum(counts.values()),
                      judge_calls=getattr(judge, "calls", 0) or 0, judge_cached=getattr(judge, "cached", 0) or 0,
                      judge_usd=round(getattr(judge, "spent_usd", 0.0) or 0.0, 6),
                      judge_failed=int(stats.get("judge_failed") or 0),
                      bodies_read=int(stats.get("escalated") or 0))
        if not plan.items:
            return report

        threshold = report["min_confidence"]
        taken = {i.message_id for i in plan.items
                 if i.source in (Source.rule, Source.person) or (i.confidence or 0.0) >= threshold}
        left = [i for i in plan.items if i.message_id not in taken and i.action is not Action.keep]
        report.update(certain=len(taken), waiting=len(left), skipped=[i.message_id for i in left])

        kept = [i.message_id for i in plan.items if i.action is Action.keep]
        signals = store.decision_signals(kept) if kept else {}
        wanting = sorted(((mid, s.get("wants_a_reply")) for mid, s in signals.items()
                          if (s.get("wants_a_reply") or 0.0) >= NEEDS_REPLY_AT), key=lambda r: -r[1])
        wanting = wanting[:NEEDS_REPLY_MAX]
        by_id = {m.id: m for m in store.get_messages([mid for mid, _ in wanting])}
        report["needs_reply"] = [{"message_id": mid, "wants_a_reply": round(p, 2),
                                  "subject": (by_id[mid].subject if mid in by_id else "") or "",
                                  "from": (by_id[mid].from_addr if mid in by_id else "") or ""}
                                 for mid, p in wanting]
        if dry_run:
            return report

        try:
            token = approve(store, plan.id)
            run = apply_plan(store=store, gmail=gmail, plan_id=plan.id, token=token, min_confidence=threshold,
                             mark_read_when_filed=settings.mark_read_when_filed)
        except (GmailError, ValueError) as exc:
            return {**report, "ok": False, "error": f"the apply did not run: {exc}"}
        report.update(applied=run.applied, run=run.id)
    return report


def render(report: dict) -> list[str]:
    """The text report — the same line labels the old shell script printed, so it reads the same."""
    plan = report["plan"]
    act = report["calibrated_act"]
    lines = [f"Damien pass — {report['period']}", f"  window    {report['window']}"]
    if act is not None:
        lines.append(f"  certain   rules, your own corrections, and the model at >= {act}")
    else:
        lines.append("  certain   rules and your own corrections only — this model is uncalibrated "
                     "(run: damien judge eval)")
    host = report["judge_host"]
    if host:
        where = "local — nothing leaves this machine" if report["judge_local"] else "remote"
        lines.append(f"  judge     {host} ({where})")
    else:
        lines.append(f"  judge     {report['judge']} (no model call)")
    if not report["ok"] and plan is None:
        lines.append(f"  FAILED    {report['error']}")
        return lines
    counts = " · ".join(f"{k} {v:,}" for k, v in sorted(report["counts"].items())) or "nothing"
    lines.append(f"  plan      {plan}")
    lines.append(f"  found     {report['total']} message(s) — {counts}")
    lines.append(f"  judgment  {report['judge_calls']} call(s), {report['judge_cached']} from cache, ${report['judge_usd']}")
    if report["judge_failed"]:
        lines.append(f"  failed    {report['judge_failed']} judgment(s) failed — left for a person, not cached")
    # Always say something about bodies: "nothing was read" and "reading is off" must not look alike.
    if report["read_bodies"] == "on_escalation":
        n = report["bodies_read"]
        lines.append(f"  bodies    {n} read, for the messages the snippet could not settle" if n
                     else "  bodies    0 read — nothing needed it")
    else:
        lines.append("  bodies    not read (DAMIEN_READ_BODIES=off)")
    if not report["total"]:
        lines += ["", "nothing to do — the window is empty."]
        return lines
    for row in report["needs_reply"]:
        lines.append(f"  needs reply  {row['subject'][:60]!r} <{row['from']}>  p={row['wants_a_reply']}")
    if report["dry_run"]:
        lines += ["", "DRY RUN — nothing was written to Gmail.",
                  f"  certain   {report['certain']} would be decided without you (keep is decided, not written)",
                  f"  waiting   {report['waiting']} would be reported for a person",
                  f"  damien plan show {plan} --sample 20",
                  f"  close it  damien rules from-senders {plan} --yes    # once you have reviewed this pass"]
        return lines
    if not report["ok"]:
        lines += [f"  FAILED    {report['error']}",
                  "  nothing partial is untraceable: damien run list   (then: damien run undo <run_id>)"]
        return lines
    lines += ["", f"applied   {report['applied']} message(s)   run {report['run']}"]
    if report["waiting"]:
        lines += [f"waiting   {report['waiting']} message(s) the model was not sure of — still approved, so:",
                  f"            damien plan show {plan} --sample 20",
                  f"            damien plan approve {plan}    # prints a fresh token once",
                  f"            damien plan apply {plan} --token <t> --yes"]
    lines.append(f"undo      damien run undo {report['run']} --yes")
    # Minting writes rules, so it is reported rather than run: minting from a pass nobody has
    # read would bake model decisions into policy.
    lines += [f"close it  damien rules from-senders {plan} --yes    # once you have reviewed this pass:",
              "            turns each sender it decided consistently into a rule, so next week judges less"]
    return lines
