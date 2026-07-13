---
description: Show this session's usage budget, consumption so far, and live plan usage
allowed-tools: Bash(python3 *session_budget.py --status*)
model: haiku
---

Run exactly this command:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_budget.py" --status
```

Then relay its output to the user verbatim in a code block, with at most one
short sentence of framing. Do not analyze, interpret, or expand on it. Do not
run any other commands or modify any files.

(Tip for the user: running the same command in a regular terminal shows this
with zero token usage.)