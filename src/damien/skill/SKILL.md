---
name: damien-mail-triage
description: Triage a Gmail inbox with the Damien CLI — rules first, a decision model for the rest, every write journalled and reversible. Use when asked to "clean up my inbox", "triage my email", "run the weekly mail pass", "what needs a reply", "undo the last mail run", or "put that email back".
---

# Damien mail triage

Damien is a command-line tool. You run its commands; Damien does every Gmail write itself,
in bulk, journalled, and undoable. You never touch Gmail directly.

## Before anything else

Run `damien doctor --json`. If `ok` is false, report each failing check and its fix to the
person and stop. Do not try to repair the install yourself.

## Always

- Add `--json` to every command that supports it and parse the JSON. Never parse the text
  output — its wording changes.
- A JSON result with `"ok": false` means the command refused or failed; report its `error`
  verbatim. Exit code 1 = refused/failed, 2 = usage.

## Scheduled use

Run these from the host's own scheduler — Damien runs nothing in the background:

- every Monday: `damien pass weekly`
- on the 1st of each month: `damien pass monthly`

Report the pass to the person **verbatim**, every line, including the `bodies` line (whether
message text was read) and the `judge` line (which host judged, and whether it was local).
If you used `--json`, report `plan`, `counts`, `applied`, `waiting`, `bodies_read`,
`read_bodies`, `judge_host`, `judge_local`, `calibrated_act` and every `needs_reply` entry.
If `calibrated_act` is null, say the model is uncalibrated and only rules and the person's
own corrections were applied.

## When the person asks, you may run

- `damien plan build -q 'in:inbox' --json`, `damien plan show <plan> --json`,
  `damien plan digest <plan> --json` — read-only.
- `damien pass weekly|monthly [--dry-run] --json`.
- `damien correct <message-id> --action keep|archive|trash|label|mark_read [--label NAME] --json` —
  record what the person decided.
- `damien restore <message-id> --json` — put a message back in the inbox.
- `damien run undo <run-id> --yes --json` — take a run back.
- `damien rules from-senders <plan> --yes` — **only after the person has reviewed that plan**
  and asked for it.

## Never

- Never edit `rules.json`, `profile.json` or `.env`, and never change `DAMIEN_READ_BODIES`
  or any `DAMIEN_JUDGE*` setting.
- Never widen `--scope` beyond `in:inbox`.
- Never approve and apply a plan the person has not asked you to apply; `damien pass` is
  the only unattended write.
