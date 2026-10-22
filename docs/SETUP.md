# Setup — your own credentials, start to finish

Damien runs against **your accounts, with your keys**. There is no hosted service, no account
to create here, and no key of anyone else's involved — you bring a Gmail credential of your
own and, if you want judgement beyond your rules, a model key of your own. Ten minutes, once.

Nothing is written to your mail until you approve a plan, so you can work through all of this
without risking a single message. If anything is missing, `damien doctor` names it and tells
you the fix.

## What it costs to run

| | |
|---|---|
| rules — and the Gmail queries behind them | **$0** — Gmail evaluates them itself |
| the model, before your rules are tuned | **≈$0.045 per 1,000 messages** (measured: $0.0000449 each) |
| the model, once your mailbox has settled | **$0** — a pass re-checks in seconds from the cache, 0 calls |
| a 7,054-message inbox, first pass | **$0.17**, about three minutes |
| a 100,000-message mailbox, all-in | **≈$2** at the measured rate — and much of it served from cache |
| no model at all (`DAMIEN_JUDGE=rules`) | **$0** forever, and nothing leaves your machine |

No subscription, nothing per seat. Your only costs are your own Google quota (free) and, if
you want it, your own model credits — and the model's share of the work shrinks as your rules
grow, because every class of mail you decide once becomes a rule that costs nothing.

## What you need

| | why | cost |
|---|---|---|
| Python 3.11+ | the tool is a Python CLI | free |
| a Google Cloud OAuth client **of your own** | so the app reads your mail as you, with a client you control | free |
| a judge key (**optional**) | only if you want the model to judge what your rules cannot | ≈$0.05 per 1,000 messages, or **nothing** |

## 1. Install

```bash
uvx --from git+https://github.com/ivan-rivera-projects/damien damien --help
# or, permanently:
pipx install git+https://github.com/ivan-rivera-projects/damien
# or, from a checkout you want to hack on:
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Nothing is written outside `~/.damien` (override with `DAMIEN_HOME`), and no data leaves
your machine unless you switch the judge on.

## 2. Your own Gmail credential (bring your own)

Damien asks Gmail for **one scope**: `https://www.googleapis.com/auth/gmail.modify` — read,
label, archive, trash. It cannot permanently delete, and it grants nothing in Calendar,
Drive or Tasks. You own the client, so you can revoke it in one place whenever you like.

1. **Create a project** at <https://console.cloud.google.com> (any name).
2. **Enable the Gmail API**: *APIs & Services → Library → Gmail API → Enable*.
3. **Consent screen**: *APIs & Services → OAuth consent screen* → **External** → add your
   own address under **Test users**.
4. **Create the credential**: *Credentials → Create credentials → OAuth client ID →
   Desktop app* → **Download JSON**.
5. **Put it where Damien looks**:
   ```bash
   mkdir -p ~/.damien && mv ~/Downloads/client_secret_*.json ~/.damien/credentials.json
   chmod 600 ~/.damien/credentials.json
   ```
6. **Sign in** — this opens one browser tab:
   ```bash
   damien login
   ```
   The token lands in `~/.damien/token.json` (0600). Only the hash of it never exists; the
   token itself never leaves your machine except to Google.

### The one thing that will bite you: test mode expires tokens

While your consent screen is in **Testing**, Google limits you to 100 users and **expires
refresh tokens after 7 days** — so a week later every command fails with an `invalid_token`
error and it looks like the tool broke. When you have finished setting up, publish it:

*OAuth consent screen → **Publish app***. For your own project you do not need Google's
verification: you will see an "unverified app" warning once (click *Advanced → Go to …*),
and your tokens stop expiring.

That trap is not theoretical — it is exactly what broke the previous version of this tool's
own credential after a week.

## 3. The judge — or none at all

Rules run first and cost nothing. The judge only sees what your rules could not settle.

| `DAMIEN_JUDGE` | what it does | what you need |
|---|---|---|
| `rules` | **deterministic only** — no model, no network, no key | nothing |
| `openrouter` | Jev through OpenRouter (what this repo is developed and measured against) | `OPENROUTER_API_KEY` |
| `typesafe` | Jev direct from TypeSafe | `TYPESAFE_API_KEY` |
| `fake` | a deterministic stand-in: same shape, no network, for a demo | nothing |

- **Nothing:** `DAMIEN_JUDGE=rules` and `damien plan build --judge none`. A complete install
  that never calls a model — everything unresolved is **kept in the inbox** with a note
  rather than guessed at.
- **OpenRouter:** get a key at <https://openrouter.ai/keys>, then:
  ```bash
  DAMIEN_JUDGE=openrouter
  DAMIEN_MODEL=typesafe/jev-1.13      # pinned on purpose (see below)
  OPENROUTER_API_KEY=sk-or-v1-…
  ```
  Measured cost of judging the residue on a real mailbox: **≈$0.000046 a message**, so
  about **$0.05 per 1,000**, and **$0** for anything already judged (verdicts are cached).
- **Why the model is pinned:** the confidence thresholds were tuned against one version's
  behaviour. Changing `DAMIEN_MODEL` re-judges everything and may change how much is acted
  on unattended. Change it deliberately. What the vendor claims about the model, and what
  this tool relies on, is listed in the README section *Model facts this is built on*.

Put it in `~/.damien/.env` (0600 — it holds a key):

```bash
DAMIEN_ACCOUNT=you@gmail.com
DAMIEN_JUDGE=rules
DAMIEN_MODEL=typesafe/jev-1.13
OPENROUTER_API_KEY=
DAMIEN_MARK_READ_WHEN_FILED=0     # 1 = filing also marks read, so unread means "not looked at"
DAMIEN_READ_BODIES=off            # on_escalation = read the text where the snippet failed
```

## 4. Set up your own policy

```bash
damien init      # writes profile.json, a starter rules file, and a .env
```

Then edit `~/.damien/profile.json`. **This is the single most valuable thing you will
write**, because it is what the model is told about you before it judges anything:

```json
{
  "context": "A film editor's inbox. Work: clients, producers, footage transfers, invoices. \
Personal: school mail about my kids, and nothing else. A discount blast from a shop I do \
not use is noise; a mail from a studio I work with is not.",
  "buckets": {
    "keep": "Belongs in the inbox: a person wrote it, or it is about a production I am on"
  }
}
```

Rules ship **empty on purpose** — a rule is your decision, and yours will not match anyone
else's. You will have a working set within an hour of first use, from your own corrections:
see [`FIRST-RUN.md`](FIRST-RUN.md).

## 5. Prove it, then use it

```bash
damien doctor                              # config, credentials, key, store — and the fix for anything wrong
damien scan -q 'in:inbox' --limit 20       # reads only, decides nothing
damien plan build -q 'in:inbox' --limit 200 --show 20
```

## When something goes wrong

| symptom | cause | fix |
|---|---|---|
| `invalid_token`, `invalid_grant` | the Google token expired (test mode expires them weekly) | publish the consent screen (step 2), then `damien login` |
| `insufficientPermissions` / 403 | the project has no Gmail API, or the token predates the scope | enable the API, `rm ~/.damien/token.json`, `damien login` |
| `credentials.json` missing | step 5 not done, or saved elsewhere | `damien doctor` prints the exact path it wants |
| `is not a usable profile: …` | `profile.json` is not valid JSON, or names a bucket that does not exist | the message names the offending key; delete it or fix the spelling |
| `401`/`402` from the judge | no key, or no credit at your provider | switch to `DAMIEN_JUDGE=rules` and it works with no key at all |
| a rule stopped firing | a broader rule shadows it | `damien rules list` marks shadowed rules in yellow |

Every command and what it costs is in [`COMMANDS.md`](COMMANDS.md). What leaves your machine
is in [`PRIVACY.md`](PRIVACY.md).
