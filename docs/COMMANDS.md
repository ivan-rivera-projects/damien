# Commands — what each one is for

Read this top to bottom once; after that the five-line pipeline is all you need.

## The pipeline, memorised

```bash
damien rules plan --sample 0 --json        # or: damien plan build -q 'in:inbox newer_than:7d'
damien plan show <plan_id>                 # read the counts and a sample
damien plan approve <plan_id>              # prints a token, once
damien plan apply <plan_id> --token <t> --yes
damien run undo <run_id>                   # if it was wrong
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
| `damien plan build` | decides a query: rules, then the model | the store | ≈$0.05 / 1,000 judged |
| `damien plan show <id>` | counts + a stratified sample of one plan | — | free |
| `damien plan digest <id>` | collapses a plan into review groups | the store | free |
| `damien plan approve <id>` | mints the approval token; prints it once | the store (hash only) | free |
| `damien plan apply <id> --token <t> --yes` | applies an approved plan in batches | **Gmail** | free |
| `damien plan apply-group <id> -g <n> --token <t> --yes` | applies one reviewed group only | **Gmail** | free |
| `damien restore <id>...` | puts a message back in the inbox, and records that you wanted it | **Gmail** + a correction | free |
| `damien correct <id> --action <a>` | records what a person decided about one message | the store | free |
| `damien run list` | plans and their runs | — | free |
| `damien run undo <run_id>` | replays a run's journal backwards | **Gmail** | free |
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
damien plan apply <plan_id> --token <t> --yes --min-confidence 0.9
damien plan apply-group <plan_id> -g 3 --token <t> --yes
```

- `--only <action>` applies exactly one slice (`trash|archive|label|mark_read`). **The
  rest of the plan stays approved** for a later pass — this is how a plan of thousands is
  read and applied a slice at a time.
- `--min-confidence` applies only what the model was sure about. Rules and your own
  decisions always pass, whatever the threshold.
- `--yes` is required and is the point: no command writes to Gmail without it.

An already-applied plan is refused; undo its run first.

## Taking it back

```bash
damien run list
damien run undo <run_id>
damien restore <message_id> --note "this was a real invoice"
```

`run undo` replays the journal backwards — every batch stored its inverse, so this puts
back exactly what that run moved and nothing else. `restore` is the arrow for *one*
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
into a permanent rule. Each one becomes free and deterministic for every future pass.
`from-group` does the same for one reviewed group, with `--dry-run` first.

Run these after applying a plan, and a mailbox stops needing the model at all. This is the
difference between a tool that costs money every week and one that costs money once.
