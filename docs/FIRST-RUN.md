# First run — the first hour

You have installed it and signed in (see [`SETUP.md`](SETUP.md)). This is the order that
gets you from a full inbox to a mailbox that stays clean, without ever acting on a batch you
have not read.

## 0. Point it at yourself

```bash
damien init
$EDITOR ~/.damien/profile.json     # whose inbox is this, and what belongs in it
damien doctor                      # every missing piece, named
```

If you only do one thing before judging thousands of messages, do this one. `context` is
what the model reads before it sees any mail; a generic description produces generic
judgements.

## 1. Look at 200 messages, decide nothing

```bash
damien plan build -q 'in:inbox' --limit 200 --show 20
```

Read the sample. This is the moment to fix your `profile.json` — if the sample is wrong in a
way that is about *you* (say, "AI newsletters belong in the inbox" when you consider them
noise), fix the profile and rebuild; it costs cents and saves the whole run.

Nothing has been written to Gmail at this point. Nothing will be, until you approve it.

## 2. Apply what you have read

```bash
damien plan show <plan_id> --sample 30      # the counts and a sample drawn from every action
damien plan digest <plan_id> --min-size 5   # the same plan as a handful of decisions
damien plan approve <plan_id>               # prints a token, once
damien plan apply <plan_id> --token <t> --yes --only trash
```

`--only` is how a plan of thousands is read: apply one action, look at the result in Gmail,
then apply the next slice **with the same token** — the rest of the plan stays approved.

If one message was wrong, put that one back *and teach it*:

```bash
damien restore <message_id> --note "a real invoice, not marketing"
```

## 3. Turn what you decided into rules — the step that pays for itself

```bash
damien rules from-senders <plan_id> --min-count 5 --agreement 0.9 --yes
damien rules list
```

Every sender the plan decided the same way at least 90% of the time becomes a **rule**: free
from then on, deterministic, no model call, no drift. This is the difference between a tool
that costs money every week and one that costs money once.

For a group you reviewed by hand, express it as rules directly:

```bash
damien plan digest <plan_id> --min-size 5      # note the group number
damien rules from-group <plan_id> -g 3 --dry-run
```

## 4. Check the arithmetic

```bash
damien status        # messages, decisions, plans, runs, spent
damien run list      # every applied run, and the id to undo it
```

On a mailbox that is already settled, a pass should cost **$0** — the answers are cached and
the rules carry it. If it is not, you have senders worth turning into rules (step 3).

## 5. Then let it run itself

```bash
./scripts/damien-pass.sh weekly --dry-run     # builds and reports; writes nothing
./scripts/damien-pass.sh weekly               # applies what is deterministic, reports the rest
```

Schedule it — **every Monday for the week just gone, and on the 1st for the month just
gone** — with cron, launchd, or your agent of choice. The monthly window overlaps the
weeklies on purpose: already-decided mail is cached and free, and the second look catches a
week you skipped.

The script applies **only what is deterministic**: your rules, your own corrections, and the
model where it was at least 0.9 sure. Everything below that is a report with a plan id and
the exact command to apply it — never applied for you. That is the whole safety argument of
running it unattended, and it is in [`RUNBOOK.md`](RUNBOOK.md) in full.

## What you should never see

- mail **sent** by Damien — there is no send path and no send scope;
- anything **permanently deleted** — `trash` is a label, and Gmail holds it for 30 days;
- a write you did not approve — `plan apply` refuses without a token, and refuses twice.
