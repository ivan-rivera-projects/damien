# Runbook — the passes, and what to do when one goes wrong

This is the operational half: what to run on a schedule, what to check, and how to take
anything back. `COMMANDS.md` says what each command is; this says when it runs.

## The two scheduled passes

Both are one command, and it is the same command:

```bash
damien pass weekly             # Monday: the week just gone
damien pass monthly            # the 1st: the month just gone
damien pass weekly --dry-run   # build and report; apply nothing
```

`damien pass` builds the plan, approves it in-process, applies **only what is deterministic**
— rules, your own corrections, and the model at or above its calibrated threshold — and prints
a report: the window, the judge (and whether it is local), the counts, the cost, the bodies
read, what may need a reply, what it applied, what is waiting, and how to undo it.
`--limit N` caps how much one pass looks at; `--json` returns the same report for an agent.
Exit 0 means a pass ran (or there was nothing to do); exit 1 means it failed, with the reason.
Have the host's own scheduler run `damien pass weekly` every Monday and `damien pass monthly`
on the 1st.

Every pass ends with the step that closes the loop:

```bash
damien rules from-senders <plan_id> --yes     # once you have reviewed the pass
```

It mints a rule per sender the pass decided consistently, which is what stops a mailbox being
re-judged every week — `rules plan` is free and `plan build` is not. The pass **reports** it
rather than running it, on purpose: minting rules from a pass nobody has read would bake model
decisions into policy.

With `DAMIEN_READ_BODIES=on_escalation`, a pass also reads the newest text of the messages it
could not settle, and reports `bodies: N read`. That is the one mode where message text reaches
the judge, it reads only the undecided mail, and what it read is kept locally so a re-plan costs
no Gmail call. See `PRIVACY.md` before turning it on.

With `DAMIEN_MARK_READ_WHEN_FILED=1`, everything a pass files away is also marked read, so the
unread count becomes a real list of what you have not dealt with yet. Kept mail is never
marked read — it is never written to at all — and `damien run undo <run_id> --yes` puts the
unread state back from the journal.

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
rest.** `damien pass` is exactly that rule, written down:

```bash
damien pass weekly --dry-run    # propose: build and report, write nothing to Gmail
damien pass weekly              # apply: the certain part, and report the rest
damien pass weekly --json       # the same, as one JSON object for an agent
```

What it applies: rules, your own corrections (which outrank rules and the model), and model
decisions at or above the threshold calibrated for that model and host. What it leaves for a
person is reported as **waiting**, with the plan id and the exact apply command; in `--json`
the ids are in `skipped`. Failed judgments are never applied and never cached.

Until the model is calibrated, a pass applies **rules and corrections only** and says so.
Calibrate it against what you have already settled:

```bash
damien judge eval               # prints a threshold table and the chosen act; saves calibration.json
```

The baseline `typesafe/jev-1.13` with no calibration entry uses 0.9.

- **Rules and your own corrections always pass** whatever `--min-confidence` says.
- By hand, `plan apply --only <action>` applies one slice, and **the rest of the plan stays
  approved** for a later pass. That is how a plan of thousands is read a slice at a time.

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
| a pass that stops part-way | a later batch failed; each batch is journalled `pending` before the Gmail call and `done` after | `damien run list` shows it; `damien run undo <run_id> --yes` replays both pending and done batches |
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

1. **Never apply an unreviewed model slice.** Rules, corrections and the calibrated
   threshold are the unattended allowance; the rest is a report.
2. **Never edit the spec or the buckets.** That is a deliberate, expensive, by-hand change.
3. **Never widen a `--scope` silently.** A scope that loses `in:inbox` reaches Sent and
   Archive.
4. **Never send mail.** There is no send path: no code in Damien sends, drafts-and-sends, or
   forwards. The `gmail.modify` scope technically permits sending — Damien never calls it — so
   treat `~/.damien/token.json` as a live credential and never widen the scope.
5. **Never `--yes` without a plan id you just built.** The token belongs to one plan.

## Steps

| step | state |
|---|---|
| (a) this documentation set | **written** — four documents plus this runbook |
| (b) the Hermes skill prompt, generated from these docs | **written** — `SKILL-PROMPT.md` |
| (c) the schedule | the pass is **built and tested** (`damien pass weekly`, `damien pass monthly`); installing the Monday / 1st schedule is the host's |
| (d) cleanup of the old tool and its AWS resources | last; inventory read-only first |
