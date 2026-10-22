# Damien

**A Gmail triage CLI that asks before it acts.** Rules decide what they can, a model judges
the residue, and code performs every write — in bulk, journalled, and reversible.

[![tests](https://github.com/ivan-rivera-projects/damien/actions/workflows/test.yml/badge.svg)](https://github.com/ivan-rivera-projects/damien/actions/workflows/test.yml)
![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.11%2B-blue)

It was built to answer one question honestly: *can a mailbox of a hundred thousand
messages be put in order by software you would actually leave alone with your mail?*

## Why it is different

- **It never acts on mail you have not read.** A plan is a document. Applying one takes an
  approval token, and `--only <action>` applies exactly the slice you looked at.
- **Every write is reversible.** Each batch is journalled with its inverse *before* it
  reaches Gmail, so `run undo` puts back precisely what it moved. `trash` is a label and
  Gmail holds it 30 days; there is no permanent-delete path in the code or the scope.
- **Rules run first, and they are free.** A rule that maps onto Gmail search syntax is
  executed *by Gmail* — no listings, no headers, no model. On a settled mailbox a pass
  costs **$0**.
- **The model only ever sees what your rules could not settle** — headers and a snippet,
  never the body — and every answer is cached, so a message is never paid for twice.
- **Your keys, your credential, nothing in the middle.** No server, no account here, no
  telemetry. Run it with no model at all if you like (`DAMIEN_JUDGE=rules`).
- **It does not send.** There is no send path and no send scope.

## What it looks like

From a real run over the author's mailbox (7,054 messages still in the inbox; 68 rules;
about three minutes):

```
$ damien plan build -q 'in:inbox' --show 12
plan 8f6ed799980e  (7054 item(s))
  archive    49
  keep     6825
  label      66
  trash     114
  judgment: 3709 call(s), 575 from cache, $0.1713
  archive    jev     121d  'Clean up your report, Ivan.' <notifications@mail.creditkarma.com>
  keep       rule    117d  'AI is making your next Apple device cost more' <theneuron@newsletter.theneurondaily.com>
  label      Receipts jev  13d  'You accepted an AWS Marketplace offer' <no-reply@marketplace.aws>
  trash      jev      13d  '💀 You're Not Ready for This Friday…' <support@theduabrand.com>

$ damien plan approve 8f6ed799980e
token: 1f0c…

$ damien plan apply 8f6ed799980e --token 1f0c… --yes --only trash
run 2ba1162fdb34 ok: 114 message(s) in 1 batch(es) (trash slice)
the rest of the plan is still approved
to take it back: damien run undo 2ba1162fdb34
```

Nothing in Gmail changed until that last command, and `run undo 2ba1162fdb34` reverses it.

## Install

```bash
uvx --from git+https://github.com/ivan-rivera-projects/damien damien --help   # try it without installing
pipx install git+https://github.com/ivan-rivera-projects/damien               # or install it
```

From a checkout, if you want to hack on it:

```bash
git clone https://github.com/ivan-rivera-projects/damien && cd damien
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

State lives in `~/.damien` (one SQLite file by default; set `DAMIEN_DB_URL` for Postgres).

## Set it up

Two things are yours, and neither is shared with anyone else:

1. **A Gmail credential of your own** — a Google OAuth *desktop* client from your own
   Cloud project, dropped at `~/.damien/credentials.json`, then `damien login`. One scope
   (`gmail.modify`), and you can revoke it whenever you like.
2. **A judge key, or none** — `openrouter` for the model this is developed against, or
   `DAMIEN_JUDGE=rules` to use no model at all and pay nothing.

Then `damien init` writes your profile and a rules file, and `damien doctor` tells you
whether anything is missing.

→ **[`docs/SETUP.md`](docs/SETUP.md)** has the click-by-click, including the OAuth
testing-mode trap that expires tokens weekly (it caught this project's own first
credential). → **[`docs/FIRST-RUN.md`](docs/FIRST-RUN.md)** is the first hour, in order.

## The pipeline

```
scan ──► rules ──► (residue) ──► the judge ──► guardrail ──► confidence ──► plan ──► apply ──► journal
         free       one request              a person's      threshold       review   bulk      undoable
         server-    per message              signals win                     groups
         side
```

1. **Rules first, and for free.** A rule that maps onto Gmail search syntax is executed *by
   Gmail*. `to_query()` returns `None` when it cannot be certain, and the rule then runs in
   Python over listed mail — a wrong query acts on the wrong mail, so `None` is the safe
   direction.
2. **Gmail's own work is trusted before ours.** Spam is never fetched. `CATEGORY_PERSONAL`
   is kept by a rule with no model call. Gmail's category reaches the model as context.
3. **The person's work is trusted above both.** Labels you name in
   `DAMIEN_PROTECT_LABELS` are a floor the model cannot cross, your labels reach the model
   as `your_labels`, and your own corrections outrank a rule and the model on every later
   plan.
4. **The judge sees only the residue**, as a small state (headers + snippet, never the
   body) and *atomic* questions: which bucket, was it sent by a machine, does it want a
   reply, how important. One request per message. The answers are kept as data, so
   thresholds can be re-tuned without paying again.
5. **A guardrail may only raise an action to `keep`** — never overrule you or a rule. It
   fires on money and deadline subjects, replies from real people, mail you already read,
   and Gmail's own `IMPORTANT`.
6. **A confidence gate.** Below `DAMIEN_CONFIDENCE_ASK` a judgement keeps the message with
   the reason attached: recorded, but not acted on.
7. **Plans are read, then applied** — by group or by action, with every batch journalled
   before it lands, so a run is resumable and undoable.

## Use

```bash
damien doctor                                        # is everything wired up?
damien scan  -q 'in:inbox older_than:2y'             # how much is there? reads only
damien plan build -q 'in:inbox' -n 200               # decide; writes nothing to Gmail
damien plan digest <plan-id> --min-size 5            # collapse it into review groups
damien plan approve <plan-id>                        # prints a token, once
damien plan apply <plan-id> --token <t> --only archive --yes
damien run undo <run-id>                             # take a run back
damien correct <message-id> --action keep --note 'a brand we follow'
damien rules from-senders <plan-id> --yes            # make your decisions permanent
damien rules plan --scope 'in:inbox older_than:2y'   # a rule pass over history, bounded
```

`--json` on `status`, `doctor`, `scan`, `plan build|digest|show`, `run list`, `rules list`.
Safe to run twice: `plan build` never writes to Gmail, an applied plan refuses to re-apply,
and a partial run resumes only the batches its journal does not hold.

## Documentation

| document | read it for |
|---|---|
| [`docs/SETUP.md`](docs/SETUP.md) | your own Gmail credential and judge key, step by step |
| [`docs/FIRST-RUN.md`](docs/FIRST-RUN.md) | the first hour: look, apply, learn rules, schedule |
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | every command — when to use it, what it writes, what it costs |
| [`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md) | the pipeline, the store, and what it refuses to do |
| [`docs/POLICY.md`](docs/POLICY.md) | the layers of authority, the seven buckets, the rule vocabulary |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | the weekly and monthly passes, failure modes, how to undo |
| [`docs/PRIVACY.md`](docs/PRIVACY.md) | exactly what leaves your machine, and when |

A `docs/SKILL-PROMPT.md` is also in there: a prompt that has an AI agent (Hermes,
OpenClaw, anything that runs commands) create its own skill for driving this, including
the scheduled passes below.

## Cleaning a mailbox of a hundred thousand messages

Brute force does not work, and the numbers say why: a Gmail header costs quota, and a
first attempt to fetch a whole inbox throttled after 64 seconds. A paced pass over 107,710
messages would take ~90 minutes of wall time before any judgement happened.

```
107,710  in:inbox
 94,202  2024 and older          <- 87% of it, historical records
  2,256  Jan – Oct 2025
  1,717  Oct 21 – Dec 2025
  9,536  2026
```

So the order is: **rule passes first, judgement second, and never both over the same
messages.**

| step | how | messages | cost |
|---|---|---|---|
| archive what a rule can describe | `rules plan --scope …` — Gmail evaluates it | tens of thousands | ~1 min, $0, no headers |
| judge what is left and recent | `plan build -q '<a date range>'` | a few thousand | ~$0.000046 each |
| keep it clean | `rules from-senders` on the recurring senders | — | free |

The rule query is always wrapped in `in:inbox` unless `--scope` says otherwise: a bare
`older_than:365d` matched 152,881 messages in this mailbox because it had escaped the inbox
and was reaching Sent and archived mail too.

## Where state lives

| | SQLite (default) | Postgres (`DAMIEN_DB_URL`) |
|---|---|---|
| setup | none — one file | one env line |
| vectors | text column, clustered in Python | `message_embeddings` + `pgvector` |
| tested | ✅ | ✅ the same suite runs against both dialects |

Tables: `messages`, `decisions`, `verdicts` (the judgement cache), `message_embeddings`,
`plans`, `plan_items`, `runs`, `run_batches`, `corrections`. A column added after release is
applied by `ADDED_COLUMNS` in `store.py` — never by dropping the table.

## Cost and speed, measured

| | wall time | model calls | cost |
|---|---|---|---|
| cold (every judgement bought) | ~10 s | 183 | $0.0075 |
| warm (nothing changed) | ~1.0 s | 0 | $0.0000 |
| a weekly pass over 233 messages, settled | ~3 s | 0 | $0.000000 |

The cache key is the request: the model, the questions as written, and the state the model
was shown. Reword a question, change the model, or add a label yourself — invalidated.
Nothing else.

## Review groups (the digest)

Senders do not collapse an inbox: 200 messages came from 140 senders, 102 with a single
message. Similarity does. Local embeddings (`fastembed`, no torch), greedy
nearest-centroid, and a threshold chosen from the data — 0.65 on this mailbox, where 0.70
left 75 groups and 0.60 dissolved the useful "4 finance offers" group into the marketing
blob. Result: 200 messages → 36 review groups.

## What this deliberately does not do

- **No bodies, no attachments.** Judgement uses headers and a snippet. Reading bodies is a
  deliberate later decision.
- **No scheduler.** Run it, or schedule it (see the RUNBOOK); the tool exits either way.
- **No permanent delete.** `trash` is recoverable for 30 days, and the code cannot do more.
- **No sending.** No send path, no send scope.
- **No telemetry.** Nothing is reported anywhere.
- **No MCP door.** A small tool wrapper over these functions is the obvious next step for a
  host that cannot shell out; the CLI costs zero tool schemas in a context.

## Model facts this is built on (all from the vendor docs)

- One request per message with *every* question about it: questions in a call are
  independent and parallel ("adding questions barely changes the response time"), while
  many emails in one `state` costs accuracy ("Jev suffers from context rot").
- `confidence` (0–1) describes how peaked the answer is, **not** whether it is right —
  hence conservative bands and a person as the last resort.
- $0.042/Mtok input, output free; the vendor's published measurement is ≈$0.0256 per 1,000
  items, and this tool measured ≈$0.000046 per message on a real mailbox.
- A Choice always picks an option even when none fit, so a companion Noul and an `unclear`
  bucket guard it.
- Never ask it to count, order or compute — that is code's job.

## Status

Beta, and in daily use on a 100,000-message mailbox. The test suite runs on Linux and macOS
against both store dialects. Interfaces may still move; the safety properties above will
not.

MIT licensed. The predecessor project — a four-service platform that this replaced — was
torn down in October 2026; its repositories remain on GitHub for history and nothing of it
runs.
