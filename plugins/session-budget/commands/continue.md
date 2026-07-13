---
description: Extend this session's usage budget (you will be asked to approve)
argument-hint: "[extra percentage points, default 5]"
allowed-tools: Bash
---

The user wants to extend the session's usage budget by $ARGUMENTS percentage
points (if no number was given, omit the argument to use the default grace).

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_budget.py" --continue $ARGUMENTS
```

Note: the session-budget hook will intercept this call and show a permission
prompt — that is expected and by design, so the human confirms every budget
change. After it runs, report the new effective budget. Never run this command
on your own initiative; only when the user explicitly asks.
