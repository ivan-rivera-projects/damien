# Commands — what each one is for

Read this top to bottom once; after that the five-line pipeline is all you need.

## The pipeline, memorised

```bash
damien rules plan --sample 0 --json        # or: damien plan build -q 'in:inbox newer_than:7d'
damien plan show <plan_id>                 # read the counts and a sample
damien plan approve <plan_id>              # prints a token, once
damien plan apply <plan_id> --token <t> --yes
damien run undo <run_id> --yes             # if it was wrong
```

Only the fourth line changes anything, and only after the third.

## Every command

| command | does | writes | cost |
|---|---|---|---|
| `damien login` | Gmail OAuth; writes `~/.damien/token.json` | nothing local | free |
| `damien version` | prints the version | — | free |
| `damien status` | config, counts, what has been decided and done | — | free |
| `damien scan -q <query>` | lists and caches headers; **decides nothing** | the store | free |
| `damien rules plan` | a plan from rules Gmail evaluates itself | the store | **free** |
| `damien doctor` | checks the install and names the fix; **changes nothing** | — | free |
| `damien pass weekly\|monthly` | one scheduled pass: builds, approves, applies only what is certain | the store + **Gmail** | ≈$0.05 / 1,000 judged |
| `damien judge eval` | calibrates the model's threshold against what you settled | `calibration.json` | ≈$0.05 / 1,000 judged |
| `damien skill install --target openclaw\|hermes` | installs the agent skill (`SKILL.md`) for a host agent | the host's skills folder | free |
| `damien plan build` | decides a query: rules, then the model | the store | ≈$0.05 / 1,000 judged |
| `damien plan show <id>` | counts + a stratified sample of one plan; never shows the token | — | free |
| `damien plan digest <id>` | collapses a plan into review groups, numbered `N:fp` | the store | free |
| `damien plan approve <id>` | mints the approval token; prints it once | the store (hash only) | free |
| `damien plan apply <id> --token <t> --yes` | applies an approved plan in batches | **Gmail** | free |
| `damien plan apply-group <id> -g <N:fp> --token <t> --yes` | applies one reviewed group only | **Gmail** | free |
| `damien restore <id>...` | puts a message back in the inbox, and records that you wanted it | **Gmail** + a correction | free |
| `damien correct <id> --action <a>` | records what a person decided about one message | the store | free |
| `damien run list` | plans and their runs | — | free |
| `damien run undo <run_id> --yes` | restores each message's state from plan time | **Gmail** | free |
| `damien rules list` | the rules, and whether each is a Gmail query | — | free |
| `damien rules init [--force]` | writes the starter rules file | `rules.json` | free |
| `damien rules from-senders <plan_id> --yes` | a rule per consistently-decided sender | `rules.json` | free |
| `damien rules from-group <plan_id> -g <n> --yes` | expresses an approved group as rules | `rules.json` | free |

## Setting it up

```bash
damien status          # what is configured, what is decided, what has been done
damien login           # once, in a browser; the token lands in ~/.damien
```

`status` is the health check: the account, the judge, the model, the thresholds, the
paths. It is the first command to run when something looks wrong.

## Seeing what is there

```bash
damien scan -q 'in:inbox older_than:30d' --limit 500
damien scan -q 'from:sell.amazon.com' --json
```

`scan` caches headers and decides nothing — useful to count, to check a query, or to warm
the store before a plan. It never writes to Gmail.

## Deciding

```bash
damien rules plan --sample 0 --json                    # every translatable rule, free
damien rules plan --rule sell-amazon --scope 'in:inbox'
damien plan build -q 'in:inbox newer_than:7d' --show 12
```

`rules plan` is the pass to reach for: free, minutes on a huge mailbox, and every match
carries its rule id. `--scope` restricts it to a Gmail query — this is where a weekly or
monthly window belongs. `plan build` is for mail no rule covers: it strips headers,
judges what is left, and costs money in proportion to how much is left.

`--judge auto|fake|none` on `plan build`: `none` decides by rules only (free), `fake` is
a deterministic stand-in for a dry run with no API key.

`DAMIEN_READ_BODIES=on_escalation` makes the judge read the *newest text* of the messages it
could not settle from the headers and snippet, and ask again. It is the only mode where message
text reaches the judge — see `PRIVACY.md` — and the plan reports how many it read:

```
  bodies:  12 read, for the messages the snippet could not settle
```

A pass always says something about bodies, so that "nothing needed reading" and "reading is
off" are never mistaken for each other: `bodies: 0 read — nothing needed it`, or
`bodies: not read (DAMIEN_READ_BODIES=off)`.

## Reading a plan before it acts

```bash
damien plan show <plan_id> --sample 30
damien plan digest <plan_id> --min-size 5
```

`show` prints the counts and a sample drawn from every action, so the sample cannot lie
by showing only the easy ones. `digest` collapses thousands of items into groups — one
decision per group instead of per message, biggest first.

## Acting, and acting partially

```bash
damien plan approve <plan_id>
damien plan apply <plan_id> --token <t> --yes --only trash
damien plan apply <plan_id> --token <t> --yes --min-confidence 0.95
damien plan apply <plan_id> --token <t> --yes --mark-read-when-filed
damien plan apply-group <plan_id> -g 3:a1b2c3 --token <t> --yes
```

- `--only <action>` applies exactly one slice (`trash|archive|label|mark_read`). **The
  rest of the plan stays approved** for a later pass — this is how a plan of thousands is
  read and applied a slice at a time.
- `--min-confidence` applies only what the model was sure about. Rules and your own
  decisions always pass, whatever the threshold.
- `--mark-read-when-filed` (or `DAMIEN_MARK_READ_WHEN_FILED=1`) makes *filing* a message —
  archiving it, or labelling it out of the inbox — also drop `UNREAD`, so "unread" keeps
  meaning *you have not looked at this yet*. Mail that is **kept** is never touched by it,
  because a keep is a decision, not a write. The journal carries `UNREAD` back, so `run undo`
  restores the unread state exactly.
- `--yes` is required and is the point: no command writes to Gmail without it.

An already-applied plan is refused; undo its run first. `plan show` never prints the token —
only its hash is stored. Approving again mints a fresh one.

## Agents: `--json` and exit codes

`--json` on `plan approve` (which returns the token, once), `plan apply`, `plan apply-group`,
`run undo`, `correct` and `restore` prints one JSON object. Errors print
`{"ok": false, "error": "..."}` and exit 1; exit 2 is a usage error — a missing `--yes`, or
`correct --action label` without `--label`.

## The scheduled pass

```bash
damien pass weekly --dry-run       # in:inbox newer_than:7d; build and report, write nothing
damien pass monthly --limit 2000   # the 1st of last month to the 1st of this one
damien pass weekly --json          # the same report as one object, for an agent
```

A pass builds a plan, approves it in-process, and applies only rules, your corrections, and
model decisions at or above the calibrated threshold; everything else is reported as waiting.
Exit 0 is a pass that ran or had nothing to do; exit 1 is a failure.

```bash
damien judge eval                      # re-judge what you settled; print thresholds and the act
damien judge eval --max-wrong 0.01 --no-save --json
```

`judge eval` re-judges messages whose answer you settled (corrections and applied rule or
person items), prints a threshold table, the chosen `act` and the cost, and saves it per
model and host to `~/.damien/calibration.json`. A threshold needs at least 30 messages the
model would have acted on, and the wrong rate must hold at every stricter bar too; with less
evidence nothing is saved. Uncalibrated, `damien pass` applies rules and corrections only.

```bash
damien skill install --target hermes   # or openclaw; --dir <skills folder>, --force
```

Copies the packaged `SKILL.md` to `~/.openclaw/skills/damien-mail-triage/` or
`~/.hermes/skills/damien-mail-triage/`.

## Taking it back

```bash
damien run list
damien run undo <run_id> --yes
damien restore <message_id> --note "this was a real invoice"
```

`run undo` puts back the state each message had when the plan was built — whether it was in
the inbox, unread, and labelled — and replays both `pending` and `done` journal batches, so a
run that failed mid-batch is covered. A run cannot be undone twice. `restore` is the arrow for *one*
message, and it does two things: puts it back **and records the correction**, so the next
plan agrees with you.

`correct` records the same signal without touching Gmail:

```bash
damien correct <message_id> --action archive --note "vendor marketing, still valuable"
```

## Keeping it clean — the step that pays for itself

```bash
damien rules from-senders <plan_id> --min-count 5 --agreement 0.9 --max 80 --yes
damien rules from-group <plan_id> -g 3 --yes
damien rules list
```

`from-senders` turns every sender the plan decided the same way at least 90% of the time
into a permanent rule. A sender is the domain, except freemail (gmail.com, outlook.com,
yahoo.*, icloud.com, …) where it is the full address; senders whose mail a person corrected
to something else are refused. Each rule becomes free and deterministic for every future pass.
`from-group` does the same for one reviewed group (`-g N` or `-g N:fp`; it refuses if the
group changed), with `--dry-run` first.

Run these after applying a plan, and a mailbox stops needing the model at all. This is the
difference between a tool that costs money every week and one that costs money once.
