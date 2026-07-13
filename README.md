# session-budget — per-session usage budgets for Claude Code

[![tests](https://github.com/tanmayshahane/claude-plugins/actions/workflows/tests.yml/badge.svg)](https://github.com/tanmayshahane/claude-plugins/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Assign a usage budget to each Claude Code session — e.g. *"the agent working
this user story may consume at most 10% of my weekly quota and 20% of the
current 5-hour window."* When the budget is hit, Claude Code **pauses and asks
you** whether to continue. Decline, and the agent writes a handoff summary of
its context to `.claude/handoffs/` and stops cleanly, so you decide how to
proceed on your own terms.

Enforcement is a deterministic `PreToolUse` hook, not an instruction the model
may or may not follow: it holds even in bypass-permissions mode, and **the
agent can never raise its own budget** — every budget change routes through
Claude Code's permission prompt for explicit human approval.

> Claude Code meters usage in a rolling **5-hour window** and a **weekly**
> cap; there is no separate daily limit. Your "daily" budget here applies to
> the 5-hour window.

## Install

```
/plugin marketplace add tanmayshahane/claude-plugins
/plugin install session-budget@tanmayshahane-plugins
```

Install at **user scope** (the default) to apply it globally across all your
projects, or project scope to enforce it for all collaborators on one repo.
Requires `python3` on PATH (standard library only, no dependencies). On
Windows, ensure `python3` resolves.

Start a new session and you'll see:
`[session-budget] Budget active for this session: ...`

## How it works

Budgets are measured as **deltas from session start**, so every session begins
with its full allowance regardless of where your plan already stands.

1. **80% of budget** → a warning with the numbers.
2. **100%** → the next tool call opens Claude Code's permission prompt showing
   consumed vs. limit for both windows.
3. **Approve** → the budget extends by a grace increment (default +5 points)
   and it asks again at the new limit.
4. **Decline** → the agent writes a handoff (task state, key decisions, next
   steps) to `.claude/handoffs/` — always writable, even when the budget is
   exhausted — and stops. Resume anytime with `claude --resume` and
   `/session-budget:continue`.

## Commands

| command | what it does |
|---|---|
| `/session-budget:status` | Budgets, consumption so far, live plan usage |
| `/session-budget:set weekly=5 session=15` | Set the budget for **this session** (e.g. this user story) — human-confirmed |
| `/session-budget:set project weekly=10` | Set the **project default** for all future sessions — human-confirmed |
| `/session-budget:continue [pct]` | Extend the budget — always confirmed by you via a permission prompt |
| `/session-budget:handoff` | Save a handoff summary and stop cleanly |

## Configuration

Budgets layer, most specific wins:
**per-session > env vars at launch > project config > user config > defaults.**

Working three user stories in parallel with different allowances? Give each
its own session and budget — either at launch:

```bash
SESSION_BUDGET_WEEKLY_PCT=5  claude   # story A: small fix
SESSION_BUDGET_WEEKLY_PCT=10 claude   # story B: medium feature
SESSION_BUDGET_WEEKLY_PCT=25 claude   # story C: big refactor
```

or anytime mid-session with `/session-budget:set weekly=25`, which applies
only to that session. Each session tracks its own consumption delta; siblings
are unaffected.

Per-project (checked into the repo) or per-user: `.claude/session-budget.json`
in the project or `~/.claude/session-budget.json`:

```json
{
  "weekly_pct_limit": 10,
  "session_pct_limit": 20,
  "warn_ratio": 0.8,
  "mode": "ask",
  "grace_pct": 5,
  "fail_policy": "open"
}
```

Project default (checked into the repo): `.claude/session-budget.json`; per-user default: `~/.claude/session-budget.json`. Set
`mode: "deny"` for unattended agents — it hard-stops with handoff
instructions instead of prompting. `fail_policy` controls behavior when usage
data can't be read: `open` (allow + warn, default) or `closed` (require
approval).

Add to your project `.gitignore`:

```
.claude/session-budget-state/
.claude/handoffs/
```

## Security & privacy disclosure

Read this before installing — a budget tool that touches credentials should
tell you exactly what it does:

- To read your live usage percentages (the same numbers `/usage` shows), the
  hook reads the Claude Code OAuth token from your local credentials store
  (`~/.claude/.credentials.json`, or the macOS Keychain) and makes **one
  read-only HTTPS request to `api.anthropic.com`**, cached for 60 seconds. The
  token never goes anywhere else. No telemetry, no third-party calls, no data
  collection.
- That usage endpoint is **not officially documented** by Anthropic and may
  change. The guard parses it defensively and degrades per `fail_policy`
  (default: allow and warn). It is designed to never brick a session.
- The agent is blocked from modifying the plugin's script, state, and cache
  (hard deny), and from changing budget config or invoking budget-raising CLI
  flags without a human-approved permission prompt.
- Treat budgets as guardrails, not accounting-grade metering: usage numbers
  can lag slightly, quotas are account-wide (parallel sessions each track
  their own delta), and headless runs (`claude -p`, Agent SDK, CI) may draw
  from the separate Agent SDK credit pool introduced in June 2026 — for
  those, also consider `--max-budget-usd` / `--max-turns`.

## How it differs from similar tools

- **Usage monitors / statuslines / HUDs** (ccusage, claude-usage-monitor,
  claude-hud, IDE widgets): display-only; nothing pauses the agent.
- **claude-quotas**: gives Claude a quota self-introspection tool and a policy
  skill — the *model* decides when to wrap up. session-budget is the
  complementary hard layer: deterministic per-session budgets enforced by
  hooks, with human-only overrides.
- **route-guard / context optimizers**: govern context-window tokens and
  subagent spawning, not your subscription quota.

## Verify / demo

```bash
# 26 scenario tests with fake usage data (also run in CI)
bash plugins/session-budget/scripts/test_session_budget.sh

# validate structure with the Claude Code CLI
claude plugin validate ./plugins/session-budget

# 30-second live demo without spending quota — first tool call trips the budget
SESSION_BUDGET_FAKE_SESSION_PCT=50 SESSION_BUDGET_FAKE_WEEKLY_PCT=50 claude
```

## License

MIT © Tanmay Shahane
