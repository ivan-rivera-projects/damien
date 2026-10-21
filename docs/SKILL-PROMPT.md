# The prompt that creates the Hermes skill

This is the prompt to hand to Hermes. It is written to be *executed*, not read: it tells
Hermes what to build, what to read first, and what the skill must contain.

Copy everything in the block below into Hermes.

---

Create a skill called **`damien-mail-triage`** that lets me run and schedule the Damien
Gmail triage tool. Damien is a local CLI on this Mac — it is not part of CONSPIRE and
must never be wired into it. It talks to Gmail and nothing else.

**Before you write anything, read these four documents. They are the source of truth;
the skill you create must point at them, not copy them, so the two cannot drift:**

- `/Users/riveraix/Downloads/AWS/damien/docs/HOW-IT-WORKS.md` — the pipeline, the store, what Damien will not do
- `/Users/riveraix/Downloads/AWS/damien/docs/COMMANDS.md` — every command, when to use it, what it writes, what it costs
- `/Users/riveraix/Downloads/AWS/damien/docs/POLICY.md` — the three layers of authority, the buckets, the rule vocabulary, and why each rule exists
- `/Users/riveraix/Downloads/AWS/damien/docs/RUNBOOK.md` — the scheduled passes, the failure modes, how to take anything back

Also confirm the tool is installed and working before you write the skill:

```bash
cd /Users/riveraix/Downloads/AWS/damien && damien status
```

If that fails, stop and tell me — do not invent commands.

## What the skill must contain

1. **When it triggers** — "clean up my inbox", "what came in last week", "run Damien",
   or the scheduled passes (below).

2. **The five-line pipeline, as the only safe shape:**
   `rules plan` (free) → `plan show` (read it) → `plan approve` (token) →
   `plan apply --token … --yes` → `run undo <run_id>` if it was wrong.
   Only `apply` writes, and only after `approve`.

3. **The commands, with when to use each** — take the table from `COMMANDS.md` rather than
   restating it, but the skill must make these distinctions explicit:
   - `damien rules plan` is **free** and covers everything a rule matches; use it first, always.
   - `damien plan build` costs **≈$0.000046 a message**; use it only for mail no rule covers.
   - `damien rules from-senders <plan_id> --yes` after applying a plan — it turns consistent
     senders into permanent rules, which is what makes later passes free.
   - `damien restore <id>` for one message, `damien run undo <run_id>` for a whole run.

4. **The pass is one script — call it, do not re-derive it:**
   ```bash
   cd ~/Downloads/AWS/damien
   ./scripts/damien-pass.sh weekly     # Monday: the week just gone
   ./scripts/damien-pass.sh monthly    # the 1st: the month just gone
   ```
   It builds the plan, applies only what is deterministic (rules, my corrections, the model
   at ≥0.9), and prints the report described in item 7. `--dry-run` builds and reports
   without applying anything; `--limit N` caps one pass.

5. **The schedule** — every Monday for the prior week, and on the 1st for the prior month.
   The script's windows are `in:inbox newer_than:7d` and
   `in:inbox after:<1st of last month> before:<1st of this month>`; the monthly window
   overlaps the weeklies on purpose (already-decided mail is cached and free).

6. **The unattended rule: apply what is deterministic, report the rest.** On a scheduled
   run: apply the rules pass, and apply the model pass only at `--min-confidence 0.9`.
   Everything below that is a **report**, with the plan id and the exact apply command —
   never applied by the schedule.

7. **The report format** — one message per run: the window; counts by action; what was
   applied; the plan id; the cost of any model calls; and the command to apply what was
   left. If a pass failed part-way, say so and give `damien run undo <run_id>`.

8. **The never-do list** (verbatim, these are the guardrails):
   - never `--yes` an unreviewed model slice — rules and `--min-confidence 0.9` are the allowance;
   - never edit the buckets in `src/damien/decide.py` — that invalidates the verdict cache and re-judges everything (≈$0.17);
   - never widen a `--scope` past `in:inbox` — a scope without it reaches Sent and Archive;
   - never attempt to send mail — Damien has no send scope and no such command;
   - never involve CONSPIRE, its MCP tools, its ledger or its gateway — this is my personal inbox and nothing about it belongs in a workspace.

9. **The failure modes** — take the table from `RUNBOOK.md` (refused `--yes`, a plan already
   applied, a `GmailError`, a pass that stopped part-way, cost far above expectation) into
   the skill, since these are what you will hit.

## Then schedule it

Once the skill exists and you have confirmed the commands run, set up the two schedules:

- **every Monday**, `./scripts/damien-pass.sh weekly`;
- **on the 1st of each month**, `./scripts/damien-pass.sh monthly`.

Run it from `/Users/riveraix/Downloads/AWS/damien` (the script resolves the CLI and the
store itself). Use your own scheduler. Report the result to me in the format above — and
when something needs a decision, give me the plan id and the command rather than deciding
it yourself. If a run fails, tell me the exit reason and do not retry it more than once.
