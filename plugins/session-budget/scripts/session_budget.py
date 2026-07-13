#!/usr/bin/env python3
"""
session_budget.py — Per-session usage budget guard for Claude Code.

Lets you cap how much of your plan's WEEKLY limit and 5-HOUR SESSION-WINDOW
limit a single Claude Code session (e.g. one agent working a user story) may
consume. When the budget is hit, the guard pauses the agent and asks YOU
whether to continue, or lets the agent write a handoff summary and stop.

Wire this single file to four hook events (SessionStart, UserPromptSubmit,
PreToolUse, PostToolUse) — it branches on hook_event_name internally.

Percentages are measured as DELTAS from the values recorded at session start,
so "weekly_pct_limit: 10" means "this session may consume 10 percentage
points of my weekly quota", regardless of where the quota already stood.

Usage sources (first that works wins):
  1. Env overrides  SESSION_BUDGET_FAKE_SESSION_PCT / SESSION_BUDGET_FAKE_WEEKLY_PCT
     (for testing / CI)
  2. Cache file     ~/.claude/session-budget/usage-cache.json  (if fresh)
  3. OAuth usage endpoint (the same data /usage shows). NOTE: this endpoint
     is not officially documented by Anthropic and may change; the guard
     degrades gracefully (see fail_policy) if it does.

Config resolution: env vars SESSION_BUDGET_* > ./.claude/session-budget.json
                   > ~/.claude/session-budget.json > built-in defaults.

Distributed as the "session-budget" Claude Code plugin. In-session controls
(slash commands provided by the plugin — budget-changing ones always route
through Claude Code's permission prompt so a human must approve):
  /session-budget:status      show budgets + live plan usage
  /session-budget:continue    extend the budget (requires your approval)
  /session-budget:handoff     write a handoff summary and stop

Terminal equivalents:
  python3 <plugin>/scripts/session_budget.py --status | --continue [pct] | --reset
"""

import json
import os
import sys
import time
import glob
import platform
import subprocess
import urllib.request
import urllib.error

# ----------------------------------------------------------------------------
# Constants & paths
# ----------------------------------------------------------------------------

HOME = os.path.expanduser("~")
GLOBAL_DIR = os.path.join(HOME, ".claude", "session-budget")
USAGE_CACHE = os.path.join(GLOBAL_DIR, "usage-cache.json")
PROJECT_STATE_DIR = os.path.join(".claude", "session-budget-state")
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

DEFAULTS = {
    "enabled": True,
    # Max share of the WEEKLY quota this session may consume (percentage points)
    "weekly_pct_limit": 10.0,
    # Max share of the rolling 5-HOUR window this session may consume.
    # (Claude Code has no daily limit; the 5-hour window is the short cycle.)
    "session_pct_limit": 20.0,
    # Warn when consumption crosses warn_ratio * limit
    "warn_ratio": 0.8,
    # "ask"  -> pause and ask the user (interactive sessions)
    # "deny" -> hard-stop with handoff instructions (unattended runs)
    "mode": "ask",
    # Each user approval ("continue") extends the budget by this many points
    "grace_pct": 5.0,
    # "open"   -> if usage can't be determined, allow and warn
    # "closed" -> if usage can't be determined, treat as tripped
    "fail_policy": "open",
    # Seconds to trust the usage cache before re-fetching
    "cache_ttl_seconds": 60,
    # Seconds between repeated user-facing warnings
    "warn_interval_seconds": 120,
    # Where the agent may still write its handoff summary after a stop
    "handoff_dir": os.path.join(".claude", "handoffs"),
}

# Files the agent must never modify directly (hard deny)
LOCKED_TOKENS = [
    "session-budget-state",
    "session_budget.py",
    "session-budget/usage-cache.json",
]
# Files/commands that change the budget: allowed only with explicit human
# approval via Claude Code's permission prompt (decision "ask")
ADMIN_TOKEN = "session-budget.json"

# ----------------------------------------------------------------------------
# Config / state helpers
# ----------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULTS)
    for path in (
        os.path.join(HOME, ".claude", "session-budget.json"),
        os.path.join(".claude", "session-budget.json"),
    ):
        data = _read_json(path)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    env_map = {
        "SESSION_BUDGET_ENABLED": ("enabled", lambda v: v.lower() not in ("0", "false", "no")),
        "SESSION_BUDGET_WEEKLY_PCT": ("weekly_pct_limit", float),
        "SESSION_BUDGET_SESSION_PCT": ("session_pct_limit", float),
        "SESSION_BUDGET_MODE": ("mode", str),
        "SESSION_BUDGET_GRACE_PCT": ("grace_pct", float),
        "SESSION_BUDGET_FAIL_POLICY": ("fail_policy", str),
        "SESSION_BUDGET_WARN_RATIO": ("warn_ratio", float),
    }
    for env, (key, cast) in env_map.items():
        if os.environ.get(env) is not None:
            try:
                cfg[key] = cast(os.environ[env])
            except ValueError:
                pass
    return cfg


def state_path(session_id):
    sid = (session_id or "default").replace("/", "_")
    return os.path.join(PROJECT_STATE_DIR, f"{sid}.json")


def load_state(session_id):
    return _read_json(state_path(session_id)) or {}


def save_state(session_id, state):
    _write_json(state_path(session_id), state)


# ----------------------------------------------------------------------------
# Usage retrieval
# ----------------------------------------------------------------------------

def _oauth_token():
    """Best-effort read of the Claude Code OAuth access token."""
    # macOS keychain
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s",
                 "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                data = json.loads(out.stdout.strip())
                return data.get("claudeAiOauth", {}).get("accessToken")
        except Exception:
            pass
    # Linux / Windows credentials file
    data = _read_json(os.path.join(HOME, ".claude", ".credentials.json"))
    if isinstance(data, dict):
        return data.get("claudeAiOauth", {}).get("accessToken")
    return None


def _extract_pcts(payload):
    """Defensively pull 5h / 7d utilization percentages from the payload."""
    session_pct = weekly_pct = None
    if not isinstance(payload, dict):
        return None
    for key, val in payload.items():
        if not isinstance(val, dict):
            continue
        util = val.get("utilization")
        if util is None:
            continue
        k = key.lower()
        if "five" in k or "5h" in k or "session" in k:
            session_pct = float(util)
        elif k in ("seven_day", "7d", "weekly") or ("seven" in k and "opus" not in k and "sonnet" not in k):
            weekly_pct = float(util)
    if session_pct is None and weekly_pct is None:
        return None
    return {"session_pct": session_pct or 0.0, "weekly_pct": weekly_pct or 0.0}


def get_usage(cfg):
    """Return {'session_pct','weekly_pct','source'} or None."""
    # 1) Test/CI env overrides
    fs = os.environ.get("SESSION_BUDGET_FAKE_SESSION_PCT")
    fw = os.environ.get("SESSION_BUDGET_FAKE_WEEKLY_PCT")
    if fs is not None or fw is not None:
        return {
            "session_pct": float(fs or 0),
            "weekly_pct": float(fw or 0),
            "source": "env-override",
        }

    # 2) Fresh cache
    cache = _read_json(USAGE_CACHE)
    now = time.time()
    if cache and (now - cache.get("fetched_at", 0)) < cfg["cache_ttl_seconds"]:
        return {
            "session_pct": cache["session_pct"],
            "weekly_pct": cache["weekly_pct"],
            "source": "cache",
        }

    # 3) OAuth usage endpoint (unofficial; parsed defensively)
    token = _oauth_token()
    if token:
        try:
            req = urllib.request.Request(
                OAUTH_USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "session-budget-hook",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode())
            pcts = _extract_pcts(payload)
            if pcts:
                _write_json(USAGE_CACHE, {**pcts, "fetched_at": now})
                return {**pcts, "source": "api"}
        except Exception:
            pass

    # 4) Stale cache is better than nothing
    if cache:
        return {
            "session_pct": cache["session_pct"],
            "weekly_pct": cache["weekly_pct"],
            "source": "stale-cache",
        }
    return None


# ----------------------------------------------------------------------------
# Budget math
# ----------------------------------------------------------------------------

def compute_consumption(state, usage):
    """Delta since session start; re-baseline if a quota window reset."""
    base = state.setdefault("baseline", {})
    changed = False
    for key in ("session_pct", "weekly_pct"):
        cur = usage[key]
        if key not in base:
            base[key] = cur
            changed = True
        elif cur < base[key] - 0.5:  # window reset (usage went down)
            base[key] = cur
            changed = True
    consumed = {
        "session": max(0.0, usage["session_pct"] - base["session_pct"]),
        "weekly": max(0.0, usage["weekly_pct"] - base["weekly_pct"]),
    }
    return consumed, changed


def effective_limits(cfg, state):
    """Per-session override (if set) beats config; grace extends either."""
    override = state.get("limits_override", {})
    grace = state.get("grace", {"session": 0.0, "weekly": 0.0})
    return {
        "session": override.get("session", cfg["session_pct_limit"]) + grace.get("session", 0.0),
        "weekly": override.get("weekly", cfg["weekly_pct_limit"]) + grace.get("weekly", 0.0),
    }


def budget_status(cfg, state, usage):
    """Returns (level, detail) where level in {'ok','warn','tripped','unknown'}."""
    if usage is None:
        return "unknown", None
    consumed, _ = compute_consumption(state, usage)
    limits = effective_limits(cfg, state)
    detail = {"consumed": consumed, "limits": limits, "raw": usage}
    tripped = [k for k in ("weekly", "session") if consumed[k] >= limits[k]]
    if tripped:
        detail["tripped_on"] = tripped
        return "tripped", detail
    warned = [
        k for k in ("weekly", "session")
        if limits[k] > 0 and consumed[k] >= cfg["warn_ratio"] * limits[k]
    ]
    if warned:
        detail["warned_on"] = warned
        return "warn", detail
    return "ok", detail


def fmt(detail):
    c, l = detail["consumed"], detail["limits"]
    return (
        f"weekly {c['weekly']:.1f}%/{l['weekly']:.1f}% · "
        f"5h-window {c['session']:.1f}%/{l['session']:.1f}% "
        f"(source: {detail['raw']['source']})"
    )


# ----------------------------------------------------------------------------
# Tamper & handoff path checks
# ----------------------------------------------------------------------------

def _tool_paths_text(tool_name, tool_input):
    parts = []
    if isinstance(tool_input, dict):
        for key in ("file_path", "path", "notebook_path", "command"):
            v = tool_input.get(key)
            if isinstance(v, str):
                parts.append(v)
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for e in edits:
                if isinstance(e, dict) and isinstance(e.get("file_path"), str):
                    parts.append(e["file_path"])
    return " ".join(parts)


def classify_guard_interaction(tool_name, tool_input):
    """How a tool call touches the guard itself.

    Returns one of:
      None      -> unrelated to the guard
      'allow'   -> read-only guard CLI (--status); always permitted
      'ask'     -> budget-changing action; needs explicit human approval
      'deny'    -> tampering with guard script/state; never permitted
    """
    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
        return None
    text = _tool_paths_text(tool_name, tool_input)

    if "session_budget.py" in text:
        if tool_name == "Bash":
            if ("--continue" in text) or ("--reset" in text) or ("--set" in text):
                return "ask"      # legit CLI, but only a human may confirm
            if "--status" in text:
                return "allow"    # read-only, fine even when tripped
            return "deny"         # anything else touching the script
        return "deny"             # editing the script file

    if any(tok in text for tok in LOCKED_TOKENS):
        return "deny"

    if ADMIN_TOKEN in text:
        return "ask"              # editing budget config = changing budget

    return None


def is_handoff_write(cfg, tool_name, tool_input):
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return False
    text = _tool_paths_text(tool_name, tool_input)
    return cfg["handoff_dir"].replace("\\", "/") in text.replace("\\", "/")


# ----------------------------------------------------------------------------
# Hook event handlers
# ----------------------------------------------------------------------------

def out(obj):
    print(json.dumps(obj))
    sys.exit(0)


def handle_session_start(cfg, session_id):
    usage = get_usage(cfg)
    state = {"started_at": time.time(), "grace": {"session": 0.0, "weekly": 0.0}}
    if usage:
        state["baseline"] = {
            "session_pct": usage["session_pct"],
            "weekly_pct": usage["weekly_pct"],
        }
        save_state(session_id, state)
        msg = (
            f"[session-budget] Budget active for this session: "
            f"≤{cfg['weekly_pct_limit']:.0f}% of weekly quota, "
            f"≤{cfg['session_pct_limit']:.0f}% of the 5h window. "
            f"Plan usage now: weekly {usage['weekly_pct']:.1f}%, "
            f"5h {usage['session_pct']:.1f}%."
        )
        out({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": msg,
            },
            "systemMessage": msg,
        })
    else:
        save_state(session_id, state)
        out({"systemMessage": (
            "[session-budget] Enabled, but usage data is unavailable right now "
            f"(fail_policy={cfg['fail_policy']}). Baseline will be set on first "
            "successful read."
        )})


def handle_user_prompt(cfg, session_id):
    usage = get_usage(cfg)
    state = load_state(session_id)
    level, detail = budget_status(cfg, state, usage)
    save_state(session_id, state)
    if level in ("warn", "tripped"):
        msg = f"[session-budget] {'BUDGET REACHED' if level == 'tripped' else 'Approaching budget'} — {fmt(detail)}"
        out({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": msg,
            },
            "systemMessage": msg,
        })
    out({})


def handle_pre_tool(cfg, session_id, data):
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Guard-related tool calls are classified before any budget math
    verdict = classify_guard_interaction(tool_name, tool_input)
    if verdict == "allow":
        out({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "session-budget: read-only status check.",
            }
        })
    if verdict == "ask":
        out({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    "session-budget: this action CHANGES the session usage budget. "
                    "Approve only if you (the human) requested it."
                ),
            }
        })
    if verdict == "deny":
        out({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "session-budget: modifying the guard's script, state, or cache "
                    "is not allowed from within the session. Use "
                    "/session-budget:continue or ask the user to run the CLI."
                ),
            }
        })

    usage = get_usage(cfg)
    state = load_state(session_id)
    level, detail = budget_status(cfg, state, usage)

    if level == "unknown":
        if cfg["fail_policy"] == "closed":
            out({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": (
                        "session-budget: cannot determine current usage and "
                        "fail_policy is 'closed'. Approve to proceed anyway."
                    ),
                },
            })
        # fail-open: warn (throttled) and allow
        now = time.time()
        if now - state.get("last_unknown_warn", 0) > cfg["warn_interval_seconds"]:
            state["last_unknown_warn"] = now
            save_state(session_id, state)
            out({"systemMessage": "[session-budget] Usage data unavailable; allowing (fail_policy=open)."})
        out({})

    if level == "tripped":
        # Always let the agent save a handoff summary
        if is_handoff_write(cfg, tool_name, tool_input):
            save_state(session_id, state)
            out({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "session-budget: handoff write permitted.",
                }
            })
        state["tripped_at"] = state.get("tripped_at") or time.time()
        state["pending_ask"] = True
        save_state(session_id, state)
        reason = (
            f"session-budget: session budget reached — {fmt(detail)} "
            f"(over on: {', '.join(detail['tripped_on'])}). "
            f"APPROVE this tool call to continue with +{cfg['grace_pct']:.0f}% grace. "
            f"DECLINE to stop: Claude should then write a handoff summary "
            f"(task state, decisions, next steps) to {cfg['handoff_dir']}/ and end the turn."
        )
        if cfg["mode"] == "deny":
            out({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason + (
                        " (mode=deny: write the handoff summary now, then stop "
                        "and tell the user how to resume — they can run "
                        "/session-budget:continue to extend the budget.)"
                    ),
                },
                "systemMessage": f"[session-budget] BUDGET REACHED — agent stopped. {fmt(detail)}",
            })
        out({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            },
            "systemMessage": f"[session-budget] BUDGET REACHED — approval required. {fmt(detail)}",
        })

    if level == "warn":
        now = time.time()
        if now - state.get("last_warn", 0) > cfg["warn_interval_seconds"]:
            state["last_warn"] = now
            save_state(session_id, state)
            out({"systemMessage": f"[session-budget] Approaching session budget — {fmt(detail)}"})

    save_state(session_id, state)
    out({})


def handle_post_tool(cfg, session_id, data):
    """If a tool ran while we were tripped+asking, the user approved -> grace."""
    state = load_state(session_id)
    if state.get("pending_ask"):
        grace = state.setdefault("grace", {"session": 0.0, "weekly": 0.0})
        grace["session"] += cfg["grace_pct"]
        grace["weekly"] += cfg["grace_pct"]
        state["pending_ask"] = False
        save_state(session_id, state)
        out({"systemMessage": (
            f"[session-budget] Continue approved — budget extended by "
            f"+{cfg['grace_pct']:.0f}% (weekly and 5h). It will ask again at the new limit."
        )})
    out({})


# ----------------------------------------------------------------------------
# CLI (human controls, run from a separate terminal)
# ----------------------------------------------------------------------------

def _all_states():
    return sorted(glob.glob(os.path.join(PROJECT_STATE_DIR, "*.json")))


def cli(argv):
    cfg = load_config()
    if "--status" in argv:
        usage = get_usage(cfg)
        print("session-budget status")
        print(f"  config: weekly ≤{cfg['weekly_pct_limit']}%, 5h ≤{cfg['session_pct_limit']}%, "
              f"mode={cfg['mode']}, fail_policy={cfg['fail_policy']}")
        if usage:
            print(f"  plan usage now: weekly {usage['weekly_pct']:.1f}%, "
                  f"5h {usage['session_pct']:.1f}% (source: {usage['source']})")
        else:
            print("  plan usage now: unavailable")
        for p in _all_states():
            st = _read_json(p) or {}
            sid = os.path.basename(p)[:-5]
            base = st.get("baseline", {})
            grace = st.get("grace", {})
            override = st.get("limits_override", {})
            ov = (f", override weekly ≤{override['weekly']:g}%" if "weekly" in override else "") + \
                 (f", 5h ≤{override['session']:g}%" if "session" in override else "")
            print(f"  session {sid}: baseline weekly {base.get('weekly_pct', '?')}%, "
                  f"5h {base.get('session_pct', '?')}%, "
                  f"grace +{grace.get('weekly', 0)}%/+{grace.get('session', 0)}%{ov}")
        return 0
    if "--continue" in argv:
        idx = argv.index("--continue")
        extra = float(argv[idx + 1]) if idx + 1 < len(argv) and argv[idx + 1].replace('.', '', 1).isdigit() else cfg["grace_pct"]
        states = _all_states()
        if not states:
            print("No active session state found.")
            return 1
        for p in states:
            st = _read_json(p) or {}
            grace = st.setdefault("grace", {"session": 0.0, "weekly": 0.0})
            grace["session"] += extra
            grace["weekly"] += extra
            st["pending_ask"] = False
            _write_json(p, st)
            print(f"Extended {os.path.basename(p)[:-5]} by +{extra}% (weekly and 5h).")
        return 0
    if "--set" in argv:
        # e.g. --set weekly=5 session=15   (applies to the CURRENT session:
        # the most recently active state file; use --all for every session)
        pairs = {}
        for a in argv:
            if "=" in a:
                k, _, v = a.partition("=")
                k = k.lower().strip("-")
                if k in ("weekly", "week", "w"):
                    pairs["weekly"] = float(v)
                elif k in ("session", "daily", "5h", "s"):
                    pairs["session"] = float(v)
        if not pairs:
            print("Usage: --set weekly=<pct> session=<pct> [--all]")
            return 1
        states = _all_states()
        if not states:
            print("No active session state found — start a Claude Code session first.")
            return 1
        targets = states if "--all" in argv else [max(states, key=os.path.getmtime)]
        for p in targets:
            st = _read_json(p) or {}
            override = st.setdefault("limits_override", {})
            override.update(pairs)
            st["grace"] = {"session": 0.0, "weekly": 0.0}
            st["pending_ask"] = False
            _write_json(p, st)
            shown = ", ".join(f"{k} ≤{v:g}%" for k, v in override.items())
            print(f"Session {os.path.basename(p)[:-5]}: budget set to {shown} "
                  f"(unset values use config defaults; grace reset).")
        return 0
    if "--reset" in argv:
        for p in _all_states():
            os.remove(p)
            print(f"Removed {p}")
        return 0
    print(__doc__)
    return 1


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # never break the session on malformed input

    cfg = load_config()
    if not cfg["enabled"]:
        out({})

    event = data.get("hook_event_name", "")
    session_id = data.get("session_id", "default")

    try:
        if event == "SessionStart":
            handle_session_start(cfg, session_id)
        elif event == "UserPromptSubmit":
            handle_user_prompt(cfg, session_id)
        elif event == "PreToolUse":
            handle_pre_tool(cfg, session_id, data)
        elif event == "PostToolUse":
            handle_post_tool(cfg, session_id, data)
    except SystemExit:
        raise
    except Exception as e:
        # Fail-open on our own bugs: warn the user, never block work.
        print(json.dumps({"systemMessage": f"[session-budget] hook error ({e}); allowing."}))
        sys.exit(0)
    out({})


if __name__ == "__main__":
    main()
