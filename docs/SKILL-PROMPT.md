# The agent skill

Damien ships an agent skill: a single `SKILL.md` in the AgentSkills format, packaged with the
code at `src/damien/skill/SKILL.md`. It tells an agent how to triage mail with Damien — which
commands to run, what needs a person's approval, and what it must never do.

Install it into an agent host:

```bash
damien skill install --target openclaw   # ~/.openclaw/skills/damien-mail-triage/SKILL.md
damien skill install --target hermes     # ~/.hermes/skills/damien-mail-triage/SKILL.md
```

`--dir PATH` installs into another skills folder (for example a workspace `skills/`) as
`PATH/damien-mail-triage/SKILL.md`; `--force` overwrites an existing copy.

Have the host's own scheduler run `damien pass weekly` every Monday and `damien pass monthly` on the 1st.

The skill tells the agent to run `damien doctor` first (it changes nothing) and to prefer
`--json` output wherever a command offers it. Read the packaged file for the full instructions;
it is the source of truth, not this page.
