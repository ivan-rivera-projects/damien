# Damien

A Gmail triage CLI. Rules decide what they can, a **System One model** (TypeSafe's Jev,
via OpenRouter) judges the rest, and **code performs every write** — in bulk, journaled,
reversible.

This is the rebuild of the original Damien Email Wrestler: same problem, none of the
platform. That one was four services, three MCP front-ends, two tool registries and
forty-nine tools, with keyword lists standing in for judgment and jobs living in two
in-memory dicts. This one is one package, one binary, and one SQLite file or Postgres.

**The original was torn down on 2026-10-21.** Its AWS resources — three Lambdas, four
DynamoDB tables, an IAM role and its policies, all in account `954976299507` (a different
account from CONSPIRE's, which it never touched) — were deleted; ~744 MB of local copies
went, plus a whole second copy that was sitting in iCloud Drive (the platform, its design
documents, the editable Pages originals of them, and the logo artwork); and every client
still pointing at it was cleaned out — Claude Desktop's MCP config, which launched its
server on **every start**, plus Kiro's and Cursor's references and the old MCP logs. Its
code remains on GitHub at `ivan-rivera-projects/{Damien-Email-Wrestler,damien-cli-project,
damien_mcp_server,reference-servers}`; nothing of it runs, and no archive of it is kept.

Measured on a real 200-message inbox: **0.99 s and $0.0000 to re-plan unchanged mail**,
**~10 s and $0.0075 to judge it for the first time**, 200 messages collapsed to ~36
review groups, and every write applied in bulk (1,000 ids per call) and undoable.

## Documentation

| document | read it for |
|---|---|
| [`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md) | the pipeline, the store, and what Damien will not do |
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | every command — when to use it, what it writes, what it costs |
| [`docs/POLICY.md`](docs/POLICY.md) | the three layers of authority, the buckets, the rule vocabulary, and **why** each rule exists |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | the weekly and monthly passes, the failure modes, and how to take anything back |

## The pipeline

```
scan ──► rules ──► (residue) ──► Jev ──► guardrail ──► confidence ──► plan ──► apply ──► journal
         free       one request     atomic          a person's      review    bulk      undoable
         server-    per message     questions,      signals win     groups
         side                       typed answers
```

1. **Rules first, and for free.** A rule that maps onto Gmail search syntax is executed
   *by Gmail* — no listing, no model call. `to_query()` returns `None` when it cannot be
   certain, and then the rule runs in Python over listed mail; a wrong query acts on the
   wrong mail, so `None` is the safe direction.
2. **Gmail's own work is trusted before ours.** `SPAM` is never fetched (`-in:spam
   -in:trash` unless the query names them). `CATEGORY_PERSONAL` is kept by a rule with no
   model call. `CATEGORY_*` is passed to the model as `gmail_category`.
3. **The person's work is trusted above both.** `ACTION REQUIRED` / `WAITING FOR` /
   `Bills` (`DAMIEN_PROTECT_LABELS`) are a floor the model cannot cross, their labels
   reach the model as `your_labels`, and `damien correct` outranks a rule and a model on
   every later plan.
4. **Jev** sees only the residue, as a small state (headers + snippet, never the body) and
   **atomic** questions: which bucket (Choice), machine-sent / wants-a-reply /
   about-their-work (Nouls), importance (Score). One request per message, up to 8 in
   flight. The answers are kept as data, so thresholds can be re-tuned without paying
   again.
5. **The guardrail** may only raise an action to `keep`, and never overrules a rule or a
   person. It fires on: money/deadlines/account subjects; a reply from someone who is not
   a bulk sender; the model's own `machine_sent < 0.3`; its own importance Score ≥ 2 of 3;
   a label the person gave it; **mail already read** (16 of 16 in a real inbox needed no
   action) and **Gmail's per-user `IMPORTANT`** (33 of 34).
6. **The confidence gate**: below `DAMIEN_CONFIDENCE_ASK` a judgment becomes `keep` with
   the reason attached. It is still recorded; it just does not act.
7. **Plans are read, then applied.** `plan digest` collapses the plan into review groups
   with local embeddings; `plan apply --only <action>` or `plan apply-group --group N`
   applies exactly what was read; every batch is journaled *before* it reaches Gmail, so a
   run is resumable and `run undo` replays the journal backwards.

## Install

```bash
cd /Users/riveraix/Downloads/AWS/damien
uv venv && uv pip install -e '.[dev,pg,vectors]'      # or, for a bare `damien` on PATH:
uv tool install --editable '.[pg,vectors]'            # the tool env is separate from .venv
```

`~/.damien/.env` (chmod 600 — it holds an API key and, with Postgres, a password):

```bash
DAMIEN_ACCOUNT=you@gmail.com
DAMIEN_JUDGE=openrouter          # rules | openrouter | typesafe | fake
DAMIEN_MODEL=typesafe/jev-1.13   # pinned: thresholds are tuned against one version
OPENROUTER_API_KEY=…
DAMIEN_DB_URL=postgres://user:pass@127.0.0.1:5433/damien    # unset = SQLite
DAMIEN_PROTECT_LABELS=ACTION REQUIRED,WAITING FOR,Bills
```

Then once: put a Google OAuth **desktop** client at `~/.damien/credentials.json` and

```bash
damien login
```

*If refresh tokens keep dying every 7 days, the OAuth app is in Google's "Testing"
status — set Publishing status to "In production" (unverified is fine for your own
account).*

## Use

```bash
damien scan  -q 'in:inbox older_than:2y'            # how much is there?  reads only
damien plan build -q 'in:inbox' -n 200              # decide; writes nothing to Gmail
damien plan digest <plan-id>                        # collapse it into review groups
damien plan apply-group <plan-id> --group 3 --token <t> --yes   # one reviewed group
damien plan apply <plan-id> --token <t> --only archive --yes    # one action, in bulk
damien rules from-group <plan-id> --group 3 --dry-run           # make it permanent
damien rules plan --scope 'in:inbox after:2025/01/01 before:2026/01/01'   # a rule pass, bounded
damien plan apply <id> --token <t> --only trash --min-confidence 0.85 --yes
damien run list | run undo <run-id> --yes
damien correct <message-id> --action keep --note 'a brand we follow'
damien status | rules list | rules init
```

`--json` on `status`, `scan`, `plan build|digest|show`, `run list`, `rules list`.

Safe to run twice: `plan build` never writes to Gmail, an applied plan refuses to
re-apply, a partial run resumes only the batches its journal does not hold, and
`--only`/`--group` leave the rest of the plan approved (status `partly_applied`).

## Cleaning a mailbox of a hundred thousand messages

Brute force does not work and the numbers say why. Gmail's quota is charged per unit
per minute, a header costs five units, and the first attempt to fetch a whole inbox
throttled after 64 seconds. A paced pass over 107,710 messages would take about 90
minutes of wall time before any judgment happened.

What the mailbox actually looked like:

```
107,710  in:inbox
 94,202  2024 and older          <- 87% of it, historical records
  2,256  Jan - Oct 2025          <- just over a year
  1,717  Oct 21 - Dec 2025
  9,536  2026
```

So the order is: **rule passes first, judgment second, and never both over the same
messages.**

| step | how | messages | cost |
|---|---|---|---|
| archive what a rule can describe | `rules plan --scope …` — Gmail evaluates it | tens of thousands | ~1 min, $0, no headers |
| judge what is left and recent | `plan build -q '<a date range>'` | a few thousand | ~$0.0000256 each |
| keep it clean | `rules from-group` on the recurring senders | — | free |

`rules plan` is the important one: a rule that translates to a search is carried out
*by Gmail*, so the only calls are the listings and the bulk writes — no headers, no
model, no verdicts. It is why a 96,458-message pass takes a minute instead of an
afternoon. The rule query is always wrapped in `in:inbox` unless `--scope` says
otherwise: a bare `older_than:365d` matched 152,881 messages in this mailbox, because
it had escaped the inbox and was reaching Sent and archived mail too.

## Where state lives

| | SQLite (default) | Postgres (`DAMIEN_DB_URL`) |
|---|---|---|
| setup | none — one file | one env line |
| vectors | text column, clustered in Python | `message_embeddings` + `pgvector` for distance queries |
| tested | ✅ | ✅ **the same 129 tests, both dialects** |

Tables: `messages`, `decisions`, `verdicts` (the judgment cache), `message_embeddings`,
`plans`, `plan_items`, `runs`, `run_batches`, `corrections`. A column added after release
is applied by `ADDED_COLUMNS` in `store.py`, not by dropping the table.

## Judgment cost and speed

| | wall time | model calls | cost |
|---|---|---|---|
| cold (every judgment bought) | ~10 s | 183 | $0.0075 |
| warm (nothing changed) | ~1.0 s | 0 | $0.0000 |
| headers cold, fetched in parallel | ~12.6 s | — | — |
| headers warm (`DAMIEN_HEADER_TTL_HOURS=12`) | — | — | 200 from cache, 0 fetched |

The cache key is the request: the model, the questions as written, and the state the
model was shown. Reword a question, change the model, or add a label the person gave —
invalidated. Nothing else.

## Review groups (the digest)

Senders do not collapse an inbox: 200 messages came from 140 senders, 102 with one
message. Similarity does — forty shops sending a promotion become one decision. Local
embeddings (`fastembed`, a ~130 MB ONNX model, no torch), greedy nearest-centroid, and a
threshold chosen from the data:

```
0.80 → 178 groups (no collapse)   0.70 → 75   0.65 → 36 ✓   0.60 → 10
```
0.60 dissolves the useful "4 finance offers" group into the marketing blob, so 0.65. The
result on the real plan: 200 messages → 36 groups (archive 22→6, label 49→15, trash 46→6).

## What this deliberately does not do

- **No MCP door.** A ~6-tool wrapper over these functions is the obvious next step if a
  host that cannot shell out needs it; the CLI costs zero tool schemas in a context.
- **No bodies, no attachments.** Judgment uses headers + snippet. Reading bodies is a
  deliberate later decision.
- **No scheduling.** Run it; it exits.
- **No permanent delete.** `trash` is recoverable for 30 days.

## Model facts this is built on (all from the vendor docs)

- One request per message with *every* question about it: questions in a call are
  independent and parallel ("adding questions barely changes the response time"), while
  many emails in one `state` costs accuracy ("Jev suffers from context rot").
- `confidence` (0–1) describes how peaked the answer is, **not** whether it is right —
  hence conservative bands and a person as last resort.
- $0.042/Mtok input, output free; the published measurement is ≈$0.0256 per 1,000 items.
- A Choice always picks an option even when none fit, so a companion Noul and an
  `unclear` bucket guard it.
- Never ask it to count, order or compute — that is code's job.
