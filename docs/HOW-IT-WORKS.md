# How Damien works

Damien decides what each message in a Gmail mailbox is, writes down why, and lets one
person approve the writes before any of them happen.

It is one person's mailbox on one machine. It talks to Gmail and to a model, and to
nothing else. There is no server, no daemon and no account besides the mailbox itself.

## The pipeline

```mermaid
flowchart LR
  A[scan<br/>headers cached] --> B[rules<br/>deterministic]
  B --> C{matched?}
  C -->|yes| D[decision<br/>source: rule]
  C -->|no| E[judge<br/>one question per bucket]
  E --> F{cached?}
  F -->|yes| D
  F -->|no| G[model call<br/>verdict stored]
  G --> D
  D --> H[plan<br/>a reviewable list]
  H --> I[approve<br/>mints a token]
  I --> J[apply<br/>bulk batches, journalled]
  J --> K[run<br/>undo replays the journal]
```

Six things are worth knowing about that shape:

1. **Nothing is written to Gmail until `apply`.** `plan build` and `rules plan` write to
   Damien's own store. A plan is a document, not a change.
2. **Rules run first, and they are free.** A rule is a deterministic match — a sender, a
   label, a list header, an age. What a rule decides costs nothing and never varies.
3. **The model only sees what the rules left.** It answers one question per message by
   choosing among the buckets (see `POLICY.md`), and every verdict is stored, so the same
   message is never judged twice for the same spec.
4. **A decision records who made it.** `rule`, `person`, `jev` (the model) or `default` —
   and `person` always wins over `rule`, which wins over the model.
5. **Every write is journalled with its inverse**, so a run can be replayed backwards
   message by message. `run undo` is that replay, not a guess.
6. **Confidence gates the automatic path.** A model decision at or above
   `DAMIEN_CONFIDENCE_ACT` (default 0.80) may be applied unattended; below it the message
   is kept and handed to a person. Rules and your own corrections always pass.

## The two passes

There are two ways to build a plan, and they are not variations of each other — they are
for different jobs.

| pass | what it does | cost | when |
|---|---|---|---|
| `damien rules plan` | every rule Gmail can evaluate itself, as one query per rule | **free**, no model | migrating a mailbox; the scheduled pass; anything over ~10k messages |
| `damien plan build` | rules, then the model on everything they left | **≈$0.05 per 1,000 judgments** | a new mailbox, or mail no rule covers yet |

A hundred thousand messages are a hundred Gmail listings and a few bulk writes for the
rules pass — about a minute. The same mailbox through `plan build` is hours and real
money, which is why the rules pass exists and why `rules from-senders` matters: every
sender made permanent is a sender never judged again.

Measured, on this mailbox: 3,709 model calls cost **$0.1713** — about **$0.000046 a
message**. The cache means a second pass over the same mail costs nothing.

## The store

State lives in `~/.damien` (or wherever `DAMIEN_HOME` points). One SQLite file by
default; a Postgres URL instead if `DAMIEN_DB_URL` is set. The tables, and what each is
for:

| table | holds |
|---|---|
| `messages` | the headers Damien has seen: sender, subject, labels, list headers, age |
| `decisions` | one row per message per pass: the action, the source, the rationale, the confidence |
| `verdicts` | the model's answers, **cached** — keyed by the message and the spec, so a spec change re-judges and nothing else does |
| `plans` / `plan_items` | a plan: its query, its status, its token hash, and one row per message with the action decided |
| `runs` / `run_batches` | what was actually applied to Gmail: one row per batch, with the inverse of the write |
| `corrections` | what a person said a message should have been — the training signal |
| `message_embeddings` | vectors, used only to group a plan for review |

Gmail credentials are `credentials.json` (the OAuth desktop client) and `token.json`
(written by `damien login`). The only scope requested is
`https://www.googleapis.com/auth/gmail.modify` — read, label, archive and trash. Damien
never sends, never deletes permanently, and never touches Drive, Calendar or Contacts.

## What Damien will not do

- **Delete anything permanently.** `trash` moves a message to the Trash label, which
  Gmail clears after thirty days. The window is the undo.
- **Send mail.** There is no send scope.
- **Write without a token.** `plan apply` refuses without the token minted by
  `plan approve`, and refuses a second time on a plan already applied.
- **Judge a message twice for the same spec.** The verdict cache is keyed on it.
- **Act on a low-confidence guess.** Below the threshold it keeps the message and says so.
