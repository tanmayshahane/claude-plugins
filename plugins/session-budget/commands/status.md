---
description: Show this session's usage budget, consumption so far, and live plan usage
allowed-tools: Bash
---

Run this command and report the output to the user in a short, readable form:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_budget.py" --status
```

Summarize: the configured budget (weekly % and 5-hour-window %), how much this
session has consumed against each, any grace extensions granted, and current
overall plan usage. Do not modify any session-budget files.
