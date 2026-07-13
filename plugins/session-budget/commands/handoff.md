---
description: Save a handoff summary of this session's work and stop cleanly
argument-hint: "[optional short task label]"
---

The user wants to stop this session (typically because its usage budget was
reached) and preserve context for later. Do the following, then end your turn:

1. Write a handoff file to `.claude/handoffs/` named
   `<YYYY-MM-DD>-<task-label-or-branch>.md` containing:
   - **Objective**: the task/user story this session was working on
   - **State**: what is done, what is in progress, what is untouched
   - **Key decisions**: choices made and why (include file paths)
   - **Next steps**: ordered, concrete actions for whoever resumes
   - **Gotchas**: anything surprising discovered (failing tests, quirks)
2. Do NOT start any new work after writing the file.
3. Tell the user: the handoff path, and that they can resume later with
   `claude --resume` or a new session, extending the budget via
   `/session-budget:continue` if needed.

Writes to `.claude/handoffs/` are always permitted by session-budget, even when
the budget is exhausted.
