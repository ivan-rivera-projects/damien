# Runbook — the passes, and what to do when one goes wrong

This is the operational half: what to run on a schedule, what to check, and how to take
anything back. `COMMANDS.md` says what each command is; this says when it runs.

## The two scheduled passes

Both are one command, and it is the same command:

```bash
cd ~/Downloads/AWS/damien
./scripts/damien-pass.sh weekly            # Monday: the week just gone
./scripts/damien-pass.sh monthly           # the 1st: the month just gone
./scripts/damien-pass.sh weekly --dry-run  # build and report; apply nothing
```

`damien-pass.sh` builds the plan, applies **only what is deterministic** — rules, your own
corrections, and the model at ≥0.9 — and prints a report: the window, the counts by
action, the cost, what it applied, what is waiting, and the command that applies the rest.
`--limit N` caps how much one pass looks at. Exit 0 means a pass ran (or there was nothing
to do); exit 1 means it failed, with the reason.

With `DAMIEN_MARK_READ_WHEN_FILED=1`, everything a pass files away is also marked read, so the
unread count becomes a real list of what you have not dealt with yet. Kept mail is never
marked read — it is never written to at all — and `damien run undo <run_id>` puts the unread
state back from the journal.

The windows it uses, if you would rather run the pieces by hand:

| pass | when | window |
|---|---|---|
| weekly | Monday | `in:inbox newer_than:7d` |
| monthly | the 1st | `in:inbox after:<1st of last month> before:<1st of this month>` |

The model is only asked about mail no rule covers, which on a settled mailbox is almost
nothing — a measured weekly pass over 221 messages cost **$0.0001** because 116 verdicts
were already cached.

The monthly window **overlaps the four weekly ones on purpose**. A message already
decided is already in the store and its verdict is cached, so the second look is free —
and it is what catches a week the schedule missed or a message a window cut in half.

`--scope` is the Gmail query the rules run inside. Keep `in:inbox` in it: a bare
`older_than:365d` once matched Sent and archived mail too — 152,881 messages in a mailbox
whose inbox held 107,710.

## Propose or apply

For a scheduled, unattended run the rule is: **apply what is deterministic, report the
rest.** `damien-pass.sh` is exactly that rule, written down — so use it, and read the next
few lines only to know what it does:

```bash
PLAN=$(damien rules plan --scope 'in:inbox newer_than:7d' --sample 0 --json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["plan"])')
TOKEN=$(damien plan approve "$PLAN" | grep -oE '[0-9a-f]{16}' | head -1)
damien plan apply "$PLAN" --token "$TOKEN" --yes                    # the rules pass: certain
damien plan apply "$PLAN" --token "$TOKEN" --yes --min-confidence 0.9   # the model, where it was sure
```

What that leaves for a person: everything the model judged below 0.9. Report it with the
plan id and the exact apply command, and the run stays useful without anyone watching.

- **Rules and your own corrections always pass** whatever `--min-confidence` says.
- `--only <action>` applies one slice, and **the rest of the plan stays approved** for a
  later pass. That is how a plan of thousands is read a slice at a time.

## What the run should report

One message per run, containing: the window; the counts by action; the cost of the model
calls; the plan id; what was applied; and the command to apply what was not. `--json` on
`plan build` carries the counts, the calls, the cache hits and the cost; `plan show --json`
carries what was recorded for any plan; `damien status` totals them.

> Cost is recorded **on the plan** at build time, so `damien status` totals every pass:
> `judged`, `from_cache` and `spent_usd`. Only the plans built after 2026-10-21 carry it —
> earlier ones read as zero, because nothing stored it at the time.

## When something goes wrong

| symptom | what it means | what to do |
|---|---|---|
| `this writes to Gmail` / a refusal about `--yes` | the command needs the explicit flag | add `--yes`, having read the plan |
| `approve it first` | no token for that plan | `damien plan approve <plan_id>` |
| `already applied — undo its run first` | the plan ran once; Damien refuses to run it twice | `damien run list`, then the id you want |
| a red `GmailError` and a non-zero exit | Gmail refused: rate limit, auth or a bad page token | nothing was half-written that the journal cannot undo; re-run the pass, or `damien login` if auth |
| a pass that stops part-way | a later batch failed; earlier batches are applied and journalled | `damien run list` shows it; `damien run undo <run_id>` puts back exactly the batches that landed |
| the model cost far more than expected | the spec changed (cache invalidated), or the rules were not run first | check `git log src/damien/decide.py`; run `rules plan` before `plan build` |
| mail filed that should not have been | a rule is too broad, or two rules overlap | `damien restore <id>` for the instance; `damien rules list` for shadowed rules; fix the rule for the class |

Nothing is irreversible: `trash` is a label, so the thirty days Gmail holds it are a slow
undo, and `run undo` is the fast one. There is no permanent delete anywhere in Damien.

## Cost, and the one rule about it

- `rules plan` — **free**, always. It is Gmail evaluating queries.
- `plan build` — about **$0.000046 a message**, so ≈$0.05 per 1,000 and ≈$0.19 for a full
  re-judge of this mailbox. Cached messages cost nothing.
- **A change to the buckets re-judges everything.** The verdict cache is keyed on the
  spec, so editing `DEFAULT_SPEC` costs a full pass. A scheduled run must never change it.
- A rule change is **free** and applies on the next pass.

## Health check

```bash
damien status            # account, judge, model, thresholds, paths, and the store's counts
damien run list          # what has been applied, and the id to undo it
damien scan -q 'in:inbox' --limit 1   # is the token still good?
damien rules list        # how many rules, how many are Gmail-evaluable, and any shadowed rule
```

## What an unattended run must never do

1. **Never apply an unreviewed model slice.** Rules and `--min-confidence 0.9` are the
   unattended allowance; the rest is a report.
2. **Never edit the spec or the buckets.** That is a deliberate, expensive, by-hand change.
3. **Never widen a `--scope` silently.** A scope that loses `in:inbox` reaches Sent and
   Archive.
4. **Never send mail.** No code path sends and no command does — but the token's scope
   permits composing and sending, so treat `~/.damien/token.json` as a live credential and
   never widen the scope.
5. **Never `--yes` without a plan id you just built.** The token belongs to one plan.

## Steps

| step | state |
|---|---|
| (a) this documentation set | **written** — four documents plus this runbook |
| (b) the Hermes skill prompt, generated from these docs | **written** — `SKILL-PROMPT.md` |
| (c) the schedule | the pass is **built and tested** (`scripts/damien-pass.sh`); installing the Monday / 1st schedule is Hermes's |
| (d) cleanup of the old tool and its AWS resources | last; inventory read-only first |
