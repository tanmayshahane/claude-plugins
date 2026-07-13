---
description: Set the usage budget for THIS session (per user story) — you will be asked to approve
argument-hint: "weekly=5 session=15   (or 'project weekly=10 ...' for the repo default)"
allowed-tools: Bash, Read, Write, Edit
---

The user wants to set a usage budget: $ARGUMENTS

**Default — per-SESSION budget** (applies only to the current session, e.g.
the agent working one user story). Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_budget.py" --set $ARGUMENTS
```

Accepted keys: `weekly=<pct>` and `session=<pct>` (aliases: `daily`, `5h`).
The command targets the currently active session automatically.

**Only if the user explicitly said "project"**: instead edit
`.claude/session-budget.json` in the repo (keys `weekly_pct_limit`,
`session_pct_limit`, `mode`, `grace_pct`), which becomes the default for all
future sessions in this project. Per-session overrides always beat it.

Note: either path will trigger a permission prompt from the session-budget
hook — that is by design, so the human confirms every budget change. Never
invoke this on your own initiative. Afterwards, confirm the new effective
budget to the user and remind them it's measured as a delta from session
start.
