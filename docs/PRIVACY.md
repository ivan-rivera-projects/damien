# Privacy — what stays here, and what does not

The short version: **nothing leaves your machine unless you switch on a remote judge**, and even
then it is headers and a snippet by default. With a `local` judge nothing leaves at all. With
`DAMIEN_READ_BODIES=on_escalation` a remote judge also gets the **newest text** of the messages
the snippet could not settle — quoted replies stripped, HTML flattened, capped at 4,000
characters — and still never an attachment.

There is no server, no account with this project, no telemetry, and no analytics. This
document describes a program you run yourself.

## What stays on your machine

| thing | where |
|---|---|
| the message headers Damien has seen | `~/.damien/damien.db` (or your own Postgres) |
| every decision, its rationale and its confidence | same store |
| the model's answers, cached | same store, so a message is never paid for twice |
| your rules, your profile | `~/.damien/rules.json`, `~/.damien/profile.json` |
| your Google credential and token | `~/.damien/credentials.json`, `token.json` (0600) |

Nothing is uploaded anywhere by default. Delete `~/.damien` and it is all gone. Settings in a
`.env` file are read only from `~/.damien/.env` (or `$DAMIEN_HOME/.env`), never from the
current directory.

## What leaves, and when

**1. Google — always, if you use Gmail.** Every action is a Gmail API call made as you:
list, fetch headers, apply labels. That is the same traffic any mail client makes, and it
goes to Google, not to this project.

**2. A model provider — only if you turn a remote judge on.** With `DAMIEN_JUDGE=openrouter` (or
`typesafe`), what is sent for each message that your rules could not settle is below. With
`DAMIEN_JUDGE=local` and `DAMIEN_JUDGE_URL` pointing at a model on your own machine, the same
request goes there and nothing leaves the machine. Every `damien pass` report names the judge
host and says whether it is local or remote.

```
from, subject, a 600-character snippet, whether it has an attachment,
whether it is a mailing list, Gmail's own category, labels you applied,
plus your profile's `context` (the description of you that you wrote)
```

What is **never** sent: attachments, your account address, message ids, or anything about
your other messages.

Whether the body is sent depends on one setting, because that is the whole difference:

| `DAMIEN_READ_BODIES` | what the judge sees | 
|---|---|
| `off` (default) | from, subject, a 600-character snippet, Gmail's category, your labels |
| `on_escalation` | the above, plus the newest text of messages the snippet could not settle |

Escalation is *narrow on purpose*: it fires only for messages whose answer came back below
`confidence_ask`, never for a protected message or one a person has corrected; it strips quoted
replies and signatures first (the newest words only), it caps the text at 4,000 characters, and
it never reads a MIME part marked as an attachment. What it read is then kept
in your local store (`body_excerpt`) so the same message is not fetched — or sent — twice. The judge answers one
question per message — which bucket — and nothing else.

**3. Nothing else.** No usage statistics, no crash reports, no version check, no banner.

## Running it with no network at all beyond Gmail

Set the judge off and Damien never calls a third party:

```bash
DAMIEN_JUDGE=rules          # deterministic rules only
damien plan build --judge none
```

Anything the rules cannot settle is **kept in the inbox** with a note for you, rather than
guessed at. A rules-only install is a complete install — it is the mode a scheduled pass
should run in once your mailbox has settled.

## The Gmail permission, precisely

One scope: `https://www.googleapis.com/auth/gmail.modify`.

- It can read, label, archive and move to Trash.
- It **cannot** permanently delete: Google's description says `gmail.modify` "does not allow
  immediate, permanent deletion of threads and messages, bypassing the trash". `trash` is a
  label, and Gmail clears it after 30 days — that window is the undo.
- Google's description of the scope also covers *composing and sending*, so the scope can send.
  There is no send *path*: no code in Damien sends, drafts-and-sends, or forwards, and it never
  calls the send API. The token is a credential, so treat `~/.damien/token.json` like one.
- It grants nothing in Drive, Calendar or Tasks — those are separate APIs with separate scopes.
- You own the OAuth client, so you can revoke the grant at
  <https://myaccount.google.com/permissions> whenever you want, and every token Damien holds
  stops working immediately.

## Other people's data

Your store holds the senders, subjects and snippets of mail *other people* sent you. That is
normal for a mail client, and it stays local — but it is the reason to treat `~/.damien`
like your mailbox: back it up accordingly, and do not hand the file to anyone you would not
hand your inbox.

## Two honest caveats

- **A remote judge is a third party.** If you enable one, its provider's terms and retention
  policy apply to the headers and snippets described above. Read them, or use `local` or `rules` mode.
- **Trash is not deletion from Google's point of view.** Messages sit in Trash for 30 days,
  recoverable by you *and* by anyone with access to the mailbox. That is deliberate.
