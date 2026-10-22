<div align="center">

# 📺 DAMIEN 📺

### ★ THE INBOX CLEANER THAT *ASKS FIRST!* ★

*As seen on your terminal!*

[![tests](https://github.com/ivan-rivera-projects/damien/actions/workflows/test.yml/badge.svg)](https://github.com/ivan-rivera-projects/damien/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/damien)](https://pypi.org/project/damien/)
![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.11%2B-blue)

</div>

---

> 🎙️ *\[ANNOUNCER, in a voice that has sold a million steak knives\]*

## ARE YOU TIRED OF THIS?

*\[Black-and-white footage. A person stares at a laptop. A number climbs. They sigh at the camera.\]*

**107,710 UNREAD EMAILS.** Receipts from 2019. Newsletters you never asked for. A **"FINAL HOURS"
SALE** that ended three presidents ago. And somewhere in that mess, buried like treasure:
**the one email from your actual accountant.**

You've tried *"Mark all as read."* 😩
You've tried *declaring email bankruptcy.* 😫
You've tried *one of those AI apps* that wanted your password, your data and your **firstborn.** 😱

**THERE HAS TO BE A BETTER WAY!**

---

## 🌈 INTRODUCING... DAMIEN! 🌈

*\[Full color. Synth stab. A terminal glows.\]*

**Damien is the Gmail triage machine that runs on YOUR computer, with YOUR keys, and
NEVER touches a single message until YOU say so!**

```
$ damien plan build -q 'in:inbox'
plan 8f6ed799980e  (7054 item(s))
  archive    49
  keep     6825
  label      66
  trash     114
  judgment: 3709 call(s), 575 from cache, $0.1713
```

**IT SLICES!** 🔪 Your rules sort mail first, **for free**, inside Gmail itself!

**IT DICES!** 🎲 A small decision model handles what's left, at less than **FIVE CENTS per
THOUSAND messages!**

**IT ASKS PERMISSION!** 🙋 Nothing moves until you read the plan and approve it!

**AND IT PUTS IT ALL BACK!** ↩️ Every change is written to a journal *before* it happens,
so one command undoes the lot!

---

## 📊 THE RESULTS ARE IN... AND THEY'RE REAL!

*\[An actor in a lab coat, holding a clipboard, says "these are real numbers."\]*

> 🗣️ **"My inbox went from 107,710 messages to UNDER 7,000!"**
> *— The author's actual inbox. Measured by the tool, not by an actor.*

| | |
|---|---|
| 📉 **107,710 → under 7,000** | messages in the inbox (94,202 of them from 2024 or older!) |
| 📦 **101,048 messages** | sorted across 29 runs, every single one reversible |
| 💸 **$0.045 per 1,000 messages** | a 7,054-message inbox took **$0.17** and about **three minutes** |
| 🆓 **$0.000000** | once your rules cover it: 233 messages re-sorted in **3 seconds**, zero model calls |
| 🧠 **200 corrections → 68 rules** | correct a kind of email once, it's free **forever** |

**THAT'S RIGHT, FOLKS: THE MORE YOU USE IT, THE LESS IT COSTS!**
Every decision you make becomes a rule, and rules are free. The model is the on-ramp,
not the engine!

---

## 🚨 BUT WAIT... THERE'S MORE! 🚨

*\[The announcer is now SHOUTING.\]*

### 🛡️ IT WON'T TRASH YOUR MONEY MAIL!

Invoices? Bank statements? **Your LLC's annual report?** Damien's guardrail **will not let the
model** file away anything about money, deadlines, accounts or business paperwork. A person
replying to you? **KEPT.** Something you already opened? **KEPT.** Gmail marked it important?
**YOU GUESSED IT. KEPT!**

### 🔒 IT KEEPS YOUR SECRETS!

- **No server.** No account. No middleman. **No telemetry. NONE!**
- The model sees **headers and a snippet**. That's it! It reads more only if YOU switch that on,
  and it **never** opens an attachment.
- Want **nothing** to leave your machine? Run it with **rules only**, or point it at a decision
  model **you host yourself!**

### 🤖 YOUR AI ASSISTANT CAN DRIVE IT!

Got **OpenClaw**? Got **Hermes**? One command and your assistant knows exactly what to do:

```bash
damien skill install --target hermes    # or openclaw
```

Then it runs `damien pass weekly` every Monday, applies **only** what it's sure about, and
**reports the rest to you.** Set it! And! **FORGET IT!** 🍗

### 🧾 AND IT NEVER, EVER SENDS!

There is **no send path in the code.** Damien can't send, forward or reply. Not one email.
Not ever. **WE CHECKED!** 🔍

---

## 📞 OPERATORS ARE STANDING BY!

*\[A phone number flashes on screen. It is actually a terminal command.\]*

### CALL NOW! *(Type now.)*

```bash
uvx damien --help        # TRY IT FREE! No install!
pipx install damien      # KEEP IT FOREVER!
```

**\* Not available in stores. Batteries not included. Gmail account required.**

Then, in three easy steps:

1. **Bring your own Gmail key** 🔑: a free Google OAuth client from your own Cloud project
   (**[docs/SETUP.md](docs/SETUP.md)** walks you through it, click by click).
2. **Pick your judge** ⚖️: a decision model via OpenRouter, TypeSafe, one you host yourself,
   or **none at all.**
3. **Run the doctor** 🩺: `damien init && damien doctor`. It tells you what's missing, and
   changes nothing!

👉 **First time?** **[docs/FIRST-RUN.md](docs/FIRST-RUN.md)** is your first hour, in order.

---

## 📜 THE FINE PRINT

*\[Tiny text scrolls past at an illegal speed. Except this time... it's GOOD NEWS.\]*

<sub>

**NO PERMANENT DELETE.** "Trash" means Gmail's Trash, which holds mail for 30 days. Damien has
no code that can do more. ✦ **NO WRITE WITHOUT APPROVAL.** A plan needs a one-time token, and
only its hash is stored. ✦ **EVERY WRITE IS UNDOABLE.** `damien run undo` puts back the inbox,
unread and label state each message had when the plan was built. ✦ **YOUR CORRECTIONS WIN.**
What you decide outranks every rule and every model, on every future plan. ✦ **NOTHING SENT.**
Damien's Gmail permission (`gmail.modify`) could technically send mail. Damien never calls
it. ✦ Results shown are from one real mailbox. Your inbox may vary. Side effects may include
inbox zero, smug satisfaction and telling people about your email setup at parties.

</sub>

---

## 🔧 FOR THE NERDS WATCHING AT HOME

*\[The music stops. The lights come up. Okay, here's how it actually works.\]*

### The pipeline

```
scan ──► rules ──► (residue) ──► the judge ──► guardrail ──► confidence ──► plan ──► apply ──► journal
         free       one request              a person's      threshold       review   bulk      undoable
         server-    per message              signals win                     groups
         side
```

1. **Rules first, and for free.** A rule that maps onto Gmail search syntax is executed *by
   Gmail*. `to_query()` returns `None` when it cannot be certain, and the rule then runs in
   Python over listed mail. A wrong query acts on the wrong mail, so `None` is the safe
   direction.
2. **Gmail's own work is trusted before ours.** Spam and Trash are never fetched, and Gmail's
   category reaches the model as context.
3. **The person's work is trusted above both.** Labels you name in `DAMIEN_PROTECT_LABELS` are
   a floor the model cannot cross, and your corrections outrank a rule and the model on every
   later plan, in both `plan build` and the free `rules plan`.
4. **The judge sees only the residue**: headers and a snippet (never the body unless
   escalation is on), plus *atomic* questions: which bucket, sent by a machine?, wants a
   reply?, how important? One request per message, and the answers are kept as data.
5. **A guardrail may only raise an action to `keep`.** It fires on money, deadline and
   business-admin subjects, replies from real people, mail you already read, and Gmail's own
   `IMPORTANT`.
6. **A confidence gate.** Below `DAMIEN_CONFIDENCE_ASK` a judgement keeps the message, with
   the reason attached.
7. **Plans are read, then applied**, by group or by action. Every batch is journalled
   `pending` before it lands and `done` after, so a run is resumable and undoable.

### Use

```bash
damien doctor                                        # is everything wired up?
damien scan  -q 'in:inbox older_than:2y'             # how much is there? reads only
damien plan build -q 'in:inbox' -n 200               # decide; writes nothing to Gmail
damien plan digest <plan-id> --min-size 5            # collapse it into review groups (N:fp)
damien plan approve <plan-id>                        # prints a token, once; only its hash is kept
damien plan apply <plan-id> --token <t> --only archive --yes
damien run undo <run-id> --yes                       # take a run back
damien correct <message-id> --action keep --note 'a brand we follow'
damien rules from-senders <plan-id> --yes            # make your decisions permanent
damien rules plan --scope 'in:inbox older_than:2y'   # a rule pass over history, bounded
damien judge eval                                    # calibrate the model's threshold
damien pass weekly                                   # the scheduled pass: build, approve, apply the certain
damien skill install --target openclaw              # give an agent host the Damien skill
```

`--json` works on `status`, `doctor`, `scan`, `plan build|digest|show|approve|apply|apply-group`,
`run list|undo`, `rules list`, `correct`, `restore`, `judge eval` and `pass`. Every command is
safe to run twice: `plan build` never writes to Gmail, an applied plan refuses to re-apply, a
partial run resumes only when its filter matches, and an undone run can't be undone twice.

### The judge

Any decision model that speaks the Jev contract (a state plus typed questions in, a choice
with a calibrated confidence out): `DAMIEN_JUDGE=openrouter` with any model on OpenRouter's
decisions endpoint (e.g. `typesafe/jev-1.14`, `perplexity/pplx-decider-flash-v1.2`),
`typesafe` direct, `local` (`DAMIEN_JUDGE_URL`, so nothing leaves your machine), or `rules`
for no model at all. A backend that returns no confidence is refused.

Confidence means different things on different models, so thresholds are **per model and
host**: `damien judge eval` re-judges the mail you've already settled and picks the lowest bar
that keeps mistakes under 2%, with at least 30 messages behind it. Until a model is
calibrated, `damien pass` applies only rules and your corrections. (`typesafe/jev-1.13` keeps
the 0.9 bar it was tuned at.)

### How the model actually behaved, measured

Across **44,985 judgements** on one real mailbox:

| | | |
|---|---|---|
| **10,735** | 24% | cleared the 0.8 bar and were **acted on unattended** |
| **11,611** | 26% | between the bars: reported, not acted on |
| **22,639** | 50% | **kept**, because it was not sure enough to touch anything |
| **36,775** (82%) | | of what it chose was `keep`; `trash` was only 5.5% of its answers |

Half the model's answers never moved a message, and its instinct is `keep`. The archive-and-
trash volume lives in the **rules**, which are mined from your own corrections and evaluated
by Gmail. You decide the classes; the model handles what's left, and only when it's sure.

### Cleaning a mailbox of a hundred thousand messages

Brute force doesn't work: a Gmail header costs quota, and a first attempt to fetch a whole
inbox throttled after 64 seconds. So the order is **rule passes first, judgement second,
and never both over the same messages.**

| step | how | messages | cost |
|---|---|---|---|
| archive what a rule can describe | `rules plan --scope …` (Gmail evaluates it) | tens of thousands | ~1 min, $0, no headers |
| judge what is left and recent | `plan build -q '<a date range>'` | a few thousand | ~$0.000046 each |
| keep it clean | `rules from-senders` on the recurring senders | — | free |

A rule query is always wrapped in `in:inbox` unless `--scope` says otherwise: a bare
`older_than:365d` once matched 152,881 messages here, because it had escaped the inbox and
was reaching Sent and archived mail too.

### Where state lives

Everything is in `~/.damien`: one SQLite file by default, or your own Postgres via
`DAMIEN_DB_URL` (CI runs the suite against both). The tables are `messages`, `decisions`,
`verdicts` (the judgement cache, keyed on the exact request), `message_embeddings`, `plans`,
`plan_items`, `runs`, `run_batches` and `corrections`. New columns arrive through
`ADDED_COLUMNS` in `store.py`, never by dropping a table.

**Review groups:** senders don't collapse an inbox (200 messages came from 140 senders), but
similarity does. Local embeddings (`fastembed`, no torch) turned 200 messages into 36 review
groups.

### Documentation

| document | read it for |
|---|---|
| [`docs/SETUP.md`](docs/SETUP.md) | your own Gmail credential and judge key, step by step |
| [`docs/FIRST-RUN.md`](docs/FIRST-RUN.md) | the first hour: look, apply, learn rules, schedule |
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | every command: when to use it, what it writes, what it costs |
| [`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md) | the pipeline, the store, and what it refuses to do |
| [`docs/POLICY.md`](docs/POLICY.md) | the layers of authority, the seven buckets, the rule vocabulary |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | the weekly and monthly passes, failure modes, how to undo |
| [`docs/PRIVACY.md`](docs/PRIVACY.md) | exactly what leaves your machine, and when |
| [`docs/SKILL-PROMPT.md`](docs/SKILL-PROMPT.md) | installing the agent skill for OpenClaw or Hermes |
| [`docs/AUDIT-BRIEF.md`](docs/AUDIT-BRIEF.md) | for reviewers: every promise, where it's enforced, where a bug would hide |

### Status

Beta, and in daily use on a 100,000-message mailbox. Interfaces may still move; the safety
properties above will not. MIT licensed.

---

<div align="center">

**📺 DAMIEN 📺**
*Your inbox called. It wants its life back.*

**⭐ STAR THE REPO! TELL A FRIEND! DO IT NOW! ⭐**

</div>
