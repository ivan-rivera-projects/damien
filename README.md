# Damien

A Gmail triage CLI. Rules decide what they can, a **System One model** (TypeSafe's Jev,
via OpenRouter) judges the rest, and **code performs every write** — in bulk, journaled,
reversible.

This is the rebuild of the original Damien Email Wrestler: same problem, none of the
platform. That one was four services, three MCP front-ends, two tool registries and
forty-nine tools, with keyword lists standing in for judgment and jobs living in two
in-memory dicts. This one is one package, one SQLite file, one binary.

## Why a CLI and not a connector

Cleaning 5,000 messages is mostly mechanical, and mechanical work should not go through
a chat one message at a time:

| | through a chat connector | through this CLI |
|---|---|---|
| list 5,000 ids | ~10 paged calls | ~10 paged calls |
| trash/archive 5,000 | one call per message | `messages.batchModify`, 1,000 ids a call — **~5 calls** |
| decide what to do | a model turn per message | rules for the bulk, **one model call per message only for the residue** |
| cost | context spent on tool schemas and turns | **~$0.03 per 1,000 messages judged** |

`messages.batchModify` also writes the system labels, so **trash = add `TRASH`**,
**archive = remove `INBOX`**, **mark read = remove `UNREAD`** — all bulk, all revertible.

## The pipeline

```
scan ──► rules ──► (residue) ──► Jev ──► confidence gate ──► plan ──► apply ──► journal
        free       one request     typed answers       < 0.55 → a person    bulk      undoable
        server-    per message,    (choice + p +       sees it, nothing
        side       all questions   confidence)         happens
```

1. **Rules** run first and run for free. A rule that maps onto Gmail search syntax is
   executed *by Gmail* — no listing, no model call. A rule that cannot be certain of its
   query falls back to matching in Python over listed mail (`to_query()` returns `None`;
   that is the safe direction, because a wrong query acts on the wrong mail).
2. **Jev** sees only what the rules did not cover — as a small state (headers + snippet,
   never the body) and a handful of **atomic** questions: which bucket (Choice), is it
   machine-sent / does it expect a reply / is it about your work (Nouls), how much does it
   matter (Score). One request per message, up to 8 in flight.
3. **The confidence gate** is the safety property: a judgment below `DAMIEN_CONFIDENCE_ASK`
   becomes `keep` with the reason attached — it is still recorded, it just does not act.
   Rules and people are never second-guessed.
4. **The plan** is a list a person reads before anything happens: counts, plus a
   stratified sample (every action represented, not just the first twenty).
5. **Apply** groups the plan by action, batches it, and writes each batch to the journal
   *before* it reaches Gmail. A run that dies halfway is resumable; `run undo` replays the
   inverse ops backwards.

## Install

```bash
cd /Users/riveraix/Downloads/AWS/damien
uv venv && uv pip install -e '.[dev]'
```

Point it at your judgment model in `~/.damien/.env`:

```bash
DAMIEN_ACCOUNT=you@gmail.com
DAMIEN_JUDGE=openrouter          # rules | openrouter | typesafe | fake
DAMIEN_MODEL=typesafe/jev-1.13   # pinned: thresholds are tuned against one version
OPENROUTER_API_KEY=…
```

Then, once: put your Google OAuth desktop client at `~/.damien/credentials.json` and

```bash
.venv/bin/damien login
```

## Use

```bash
damien rules init                                  # write the starter rules
damien scan  -q 'in:inbox older_than:2y'           # how much is there?  reads only
damien plan build -q 'in:inbox older_than:2y'      # decide; writes nothing to Gmail
damien plan show  <plan-id> --sample 30            # read it before it happens
damien plan approve <plan-id>                      # mint the token
damien plan apply <plan-id> --token <t> --yes      # do it, in bulk, journaled
damien run undo  <run-id> --yes                    # take it back
damien correct <message-id> --action keep --note 'that was a client'   # the training signal
```

`--json` on `status`, `scan`, `plan build|show`, `run list` and `rules list` for scripts.

Safe to run twice: `plan build` never writes to Gmail, an applied plan refuses to re-apply,
and a partial run resumes only the batches its journal does not hold.

## What this deliberately does not do (yet)

- **No MCP door.** A ~6-tool wrapper over these same functions is the obvious next step if
  a host that cannot shell out needs it; the CLI is the interface, and it costs zero tool
  schemas in an agent's context.
- **No bodies, no attachments.** Judgment uses headers + snippet. Reading bodies is a
  deliberate later decision, not an oversight.
- **No scheduling, no daemon.** Run it; it exits.
- **No delete.** `trash` is recoverable for 30 days; permanent deletion stays a human act.

## Model facts this is built on (all from the vendor docs)

- One request per message with *every* question about it: questions in a call are
  independent and parallel, so adding them "barely changes the response time". Many emails
  in one `state` would cost accuracy ("Jev suffers from context rot").
- `confidence` (0–1) describes how peaked the answer's distribution is, **not** whether it
  is right — hence the conservative bands and the person as last resort.
- Pricing is $0.042/Mtok input, output free; the published measurement is ≈$0.0256 per
  1,000 items at ~610 input tokens each.
- Choice answers always pick an option even when none fit, so a companion Noul and an
  `unclear` bucket do the guarding.
- Never ask it to count, order or compute — that is code's job.
