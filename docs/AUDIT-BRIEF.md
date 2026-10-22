# Brief for an independent pre-release review

This is a brief for someone reviewing this codebase for the first time, with no context from
the session that built it. Everything below is either an **invariant** the tool promises, or a
**surface where a defect would be invisible until it mattered**.

The instruction is deliberately narrow: I am not looking for style opinions, formatting, type
annotations, dependency choices or architecture taste. `ruff` already gates the lint, `pytest`
gates the behaviour, and CI runs both on two operating systems.

**What I am looking for:** the one place where this tool does something its documentation says
it does not, or where a defect would damage a person's mail, privacy or trust without being
noticed.

If you are an agent: read `README.md`, `docs/HOW-IT-WORKS.md` and `docs/PRIVACY.md` first, then
attack the list below. Report file:line, the scenario that triggers it, and what you would
change. Do not report style, naming or "consider adding…" items unless they hide a defect.

---

## 1. The invariants — try to break each one

| # | the promise | where it should be enforced | how I would try to break it |
|---|---|---|---|
| 1 | **No permanent delete.** `trash` is a label; the code cannot bypass the trash. | `gmail.py` (`trash`, `modify`), the scope in `config.py` | grep for any permanent-delete or `messages.delete` path; check what the scope actually allows (Google: `gmail.modify` cannot permanently delete — verify that claim holds for every call the tool makes) |
| 2 | **Nothing is sent.** No send path exists. | the whole of `src/damien` | grep for `send`, `messages/send`, `drafts`; note that the *scope* permits sending, so check no code path could reach it, including via a crafted rule or plan |
| 3 | **No write without approval.** `plan apply` refuses without the token minted by `plan approve`, and refuses a plan already applied. | `plan.apply_plan`, `store.approve_plan` | forge a token; re-apply a `partly_applied` plan; apply with `--only` twice; apply an undone run's plan |
| 4 | **Every write is journalled with its inverse before it lands**, so `run undo` restores exactly what was moved. | `plan._perform` (inverse per action), `store.journal_batch`, `run.undo_run` | check every `Action` has a correct inverse (especially `label` with `keep_in_inbox` and `mark_read_when_filed`, and `trash`); kill a run mid-batch and undo; undo twice |
| 5 | **A keep is never written to.** A `keep` decision is not performed at all. | `plan._group_items` (skips keep), `apply_plan` | a plan that is all keeps; a keep with `mark_read_when_filed=1`; a keep with a label |
| 6 | **Kept mail is never marked read; filed mail may be.** | `plan._perform` (`mark_read`), `gmail.add_label` | a label that keeps a message in the inbox with `mark_read` set |
| 7 | **The body is never sent to the judge unless `DAMIEN_READ_BODIES=on_escalation`**, and then only for messages the snippet could not settle, newest text only, attachments never opened. | `plan.build_plan` (the escalation block), `mime.payload_text` | a part with a `filename`; a multipart with an inline text part; check what `message_state` actually contains and when |
| 8 | **A rules-only install sends nothing to a third party.** | `jev.make_judge`, `config.load_settings` | an unrecognised `DAMIEN_JUDGE`; a judge configured with no key; `--judge none` |
| 9 | **The verdict cache is keyed on the request** (model + questions + state), so an answer is never served for a different question. | `jev.verdict_key`, `message_state`, `build_questions` | two messages with identical state; an escalated vs un-escalated judgement of one message; a profile edit |
| 10 | **Rule scope cannot silently widen.** A rule query is wrapped in `in:inbox` unless `--scope` says otherwise. | `rules.to_query`, `plan.plan_from_rules`, `rules_plan` | a rule whose query escapes the inbox; an empty scope |
| 11 | **The protected labels are a floor the model cannot cross.** | `decide.decide_message`, `DEFAULT_PROTECTED` | a protected label + a trash verdict; a protected label on an escalated message |
| 12 | **A profile cannot change an action, only wording.** | `profile.Profile.spec` | a profile that names an unknown bucket (should raise); a profile with a bucket action smuggled in |

## 2. Claims in the documentation — check each one against the code

Every one of these is a factual claim a user will rely on. A false one is the most damaging
defect this project can ship, because it is a promise about someone's private mail.

- `README.md` — "It never acts on mail you have not read", "Every write is reversible",
  "Rules run first, and they are free", "The model only ever sees what your rules could not
  settle", "Your keys, your credential, nothing in the middle", "It does not send",
  "No bodies unless you ask", "No permanent delete", "No telemetry".
- `docs/PRIVACY.md` — the table of what each `DAMIEN_READ_BODIES` mode sends; "never an
  attachment"; "your account address, message ids … never sent" — **verify against
  `message_state` line by line**; "`trash` is a label, and Gmail clears it after 30 days".
- `docs/SETUP.md` — the scope and what it cannot do; the testing-mode expiry claim.
- `docs/RUNBOOK.md` — what an unattended pass applies (`rules`, corrections, model at ≥0.9);
  "the rest of the plan is still approved".
- `docs/COMMANDS.md` — what each command writes and what it costs.
- `docs/SKILL-PROMPT.md` — what an agent is told to do, and the never-do list.

## 3. Surfaces where a defect hides well

1. **Escalation** (`plan.build_plan`, the escalation block). The newest surface and the least
   exercised: attacker-controlled text now reaches a model. Check the escalation *set* (only
   judgements below `confidence_ask`), that a message is read at most once, that `""` and
   `None` mean different things, and that a body cannot be attributed to the wrong message
   (the `zip`/`by_position` class of bug is real here — it was once worth 2,180 decisions).
2. **The shell pass** (`scripts/damien-pass.sh`). It parses CLI output with `grep`/`awk`
   (`grep -oE '[0-9]+ message'`, `head -1 | awk '{print $2}'`). If the CLI's wording changes,
   this breaks **silently** and reports a wrong number. Check the failure modes, `set -euo
   pipefail` behaviour, and the dry-run/apply branches.
3. **The store's migrations** (`store.ADDED_COLUMNS`, the two dialects). Three columns have
   been added since the first release (`list_id`, `keep_in_inbox`, `body_excerpt`, and the plan
   cost columns). Check that a database created by an older version still works, in **both**
   SQLite and Postgres, and that `ON CONFLICT DO UPDATE` cannot clobber a column it does not
   mention (this bug already happened once — `body_excerpt` was silently dropped).
4. **Credentials.** Check the token file's mode, that no token is ever logged or put into an
   exception, the redaction path in `Settings.redacted()`, and the refresh failure paths.
5. **The rate limiter and batching** (`gmail.RateLimiter`, `op_batch`, `page_size`). A wrong
   limit is a quota ban or a silent truncation.
6. **Version and packaging.** `__version__` comes from installed metadata; the *source tree*
   declares its own. An editable install can disagree. Check the floor-check path an agent
   uses, and `pyproject.toml` metadata.

## 4. What to report

A triaged list, most serious first:

- **Critical** — a promise in §1 or §2 that the code does not keep; data loss; a write that
  cannot be undone; a leak of mail content or credentials.
- **Likely** — a defect that needs an unusual but realistic input (a hostile body, a 429, a
  half-finished run, an old database).
- **Speculative** — worth writing down, no repro.

For each: file:line, the scenario, the observable damage, and the smallest fix. If a claim in
§2 is *true* but the code only enforces it by accident or by a comment rather than a test, say
so — that is a finding too.
