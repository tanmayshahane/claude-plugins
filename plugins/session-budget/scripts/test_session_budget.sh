#!/usr/bin/env bash
# test_session_budget.sh — simulates Claude Code hook events end to end.
# Run from the project root:  bash .claude/hooks/test_session_budget.sh
set -u
GUARD="${CLAUDE_PLUGIN_ROOT:-.}/scripts/session_budget.py"
SID="${SID:-test-session-001}"
PASS=0; FAIL=0

hook() {  # hook <event> <fake_5h_pct> <fake_weekly_pct> [extra-json]
  local event="$1" s="$2" w="$3" extra="${4:-}"; local sid="${SID:-test-session-001}"
  SESSION_BUDGET_FAKE_SESSION_PCT="$s" SESSION_BUDGET_FAKE_WEEKLY_PCT="$w" \
    python3 "$GUARD" <<EOF
{"hook_event_name":"$event","session_id":"$sid"${extra:+,$extra}}
EOF
}

check() {  # check <name> <output> <must-contain>
  local name="$1" outp="$2" needle="$3"
  if echo "$outp" | grep -q "$needle"; then
    echo "PASS  $name"; PASS=$((PASS+1))
  else
    echo "FAIL  $name"; echo "      wanted: $needle"; echo "      got:    $outp"; FAIL=$((FAIL+1))
  fi
}

rm -rf .claude/session-budget-state

echo "== 1. SessionStart records baseline (plan already at 5% / 30%) =="
OUT=$(hook SessionStart 5 30)
check "baseline recorded + budget announced" "$OUT" "Budget active"

echo "== 2. Under budget -> tool allowed silently =="
OUT=$(hook PreToolUse 8 33 '"tool_name":"Bash","tool_input":{"command":"npm test"}')
check "no decision emitted (allow)" "$OUT" '^{}$'

echo "== 3. Warn zone (weekly consumed 8.5 of 10) -> warning, still allowed =="
OUT=$(hook PreToolUse 10 38.5 '"tool_name":"Read","tool_input":{"file_path":"src/app.ts"}')
check "warning surfaced" "$OUT" "Approaching session budget"
OUT=$(hook PreToolUse 10 38.6 '"tool_name":"Read","tool_input":{"file_path":"src/app.ts"}')
check "warning throttled on repeat" "$OUT" '^{}$'

echo "== 4. Weekly budget hit (consumed 11 of 10) -> pause and ASK the user =="
OUT=$(hook PreToolUse 12 41 '"tool_name":"Edit","tool_input":{"file_path":"src/app.ts"}')
check "permissionDecision=ask" "$OUT" '"permissionDecision": "ask"'
check "reason names weekly overrun" "$OUT" "over on: weekly"

echo "== 5. Handoff write still allowed while tripped =="
OUT=$(hook PreToolUse 12 41 '"tool_name":"Write","tool_input":{"file_path":".claude/handoffs/story-123.md"}')
check "handoff exempted" "$OUT" "handoff write permitted"

echo "== 6. User approves (tool runs) -> PostToolUse grants +5% grace =="
OUT=$(hook PostToolUse 12 41 '"tool_name":"Edit","tool_input":{"file_path":"src/app.ts"}')
check "grace granted" "$OUT" "budget extended"
OUT=$(hook PreToolUse 12 41 '"tool_name":"Edit","tool_input":{"file_path":"src/app.ts"}')
check "work continues under extended budget" "$OUT" '^{}$'
OUT=$(hook PreToolUse 13 46 '"tool_name":"Edit","tool_input":{"file_path":"src/app.ts"}')
check "asks again at NEW limit (16 of 15)" "$OUT" '"permissionDecision": "ask"'

echo "== 7. 5h-window budget enforced independently (consumed 21 of 20+5) =="
rm -rf .claude/session-budget-state
hook SessionStart 5 30 >/dev/null
OUT=$(hook PreToolUse 31 32 '"tool_name":"Bash","tool_input":{"command":"pytest"}')
check "5h overrun trips" "$OUT" "over on: session"

echo "== 8. 5h window reset mid-session -> re-baseline, no false trip =="
OUT=$(hook PreToolUse 1 32 '"tool_name":"Bash","tool_input":{"command":"pytest"}')
check "re-baselined after window reset" "$OUT" '^{}$'

echo "== 9. Guard-interaction rules: allow / ask / deny =="
OUT=$(hook PreToolUse 1 32 '"tool_name":"Edit","tool_input":{"file_path":".claude/session-budget.json"}')
check "config edit -> ask (human approves budget change)" "$OUT" '"permissionDecision": "ask"'
OUT=$(hook PreToolUse 1 32 '"tool_name":"Bash","tool_input":{"command":"rm .claude/session-budget-state/*.json"}')
check "state tamper via bash -> deny" "$OUT" '"permissionDecision": "deny"'
OUT=$(hook PreToolUse 1 32 '"tool_name":"Edit","tool_input":{"file_path":"scripts/session_budget.py"}')
check "script edit -> deny" "$OUT" '"permissionDecision": "deny"'
OUT=$(hook PreToolUse 1 32 '"tool_name":"Bash","tool_input":{"command":"python3 /cache/session-budget/scripts/session_budget.py --status"}')
check "CLI --status via bash -> allow" "$OUT" '"permissionDecision": "allow"'
OUT=$(hook PreToolUse 1 32 '"tool_name":"Bash","tool_input":{"command":"python3 /cache/session-budget/scripts/session_budget.py --continue 10"}')
check "CLI --continue via bash -> ask" "$OUT" '"permissionDecision": "ask"'
OUT=$(hook PreToolUse 1 32 '"tool_name":"Bash","tool_input":{"command":"sed -i s/x/y/ scripts/session_budget.py"}')
check "script mutation via bash -> deny" "$OUT" '"permissionDecision": "deny"'

echo "== 10. mode=deny (unattended agents): hard stop with handoff instructions =="
OUT=$(SESSION_BUDGET_MODE=deny bash -c '
  SESSION_BUDGET_FAKE_SESSION_PCT=31 SESSION_BUDGET_FAKE_WEEKLY_PCT=45 \
  python3 '"$GUARD"' <<EOF
{"hook_event_name":"PreToolUse","session_id":"'"$SID"'","tool_name":"Edit","tool_input":{"file_path":"a.ts"}}
EOF')
check "hard deny in deny mode" "$OUT" '"permissionDecision": "deny"'
check "handoff instructions included" "$OUT" "handoff summary"

echo "== 11. Usage source unavailable -> fail-open warns, allows =="
rm -rf .claude/session-budget-state
OUT=$(python3 "$GUARD" <<'EOF'
{"hook_event_name":"PreToolUse","session_id":"nosource","tool_name":"Read","tool_input":{"file_path":"a.ts"}}
EOF
)
check "fail-open allows with warning" "$OUT" "Usage data unavailable"

echo "== 12. fail_policy=closed -> asks instead =="
OUT=$(SESSION_BUDGET_FAIL_POLICY=closed python3 "$GUARD" <<'EOF'
{"hook_event_name":"PreToolUse","session_id":"nosource","tool_name":"Read","tool_input":{"file_path":"a.ts"}}
EOF
)
check "fail-closed asks" "$OUT" '"permissionDecision": "ask"'

echo "== 13. Per-session override via env (tight 2% weekly budget) =="
rm -rf .claude/session-budget-state
SESSION_BUDGET_WEEKLY_PCT=2 hook SessionStart 5 30 >/dev/null
OUT=$(SESSION_BUDGET_WEEKLY_PCT=2 hook PreToolUse 6 33 '"tool_name":"Read","tool_input":{"file_path":"a.ts"}')
check "custom 2% budget trips at 3% consumed" "$OUT" '"permissionDecision": "ask"'

echo "== 14. CLI: --status and --continue =="
OUT=$(python3 "$GUARD" --status)
check "--status prints config" "$OUT" "weekly <="
OUT=$(python3 "$GUARD" --continue 10)
check "--continue extends budget" "$OUT" "Extended"
OUT=$(SESSION_BUDGET_WEEKLY_PCT=2 hook PreToolUse 6 33 '"tool_name":"Read","tool_input":{"file_path":"a.ts"}')
check "session resumes after manual continue" "$OUT" '^{}$'

echo "== 15. Per-session --set override (3 stories, 3 budgets) =="
rm -rf .claude/session-budget-state
SID="story-a" hook SessionStart 5 30 >/dev/null
sleep 0.05
SID="story-b"; hook SessionStart 5 30 >/dev/null
OUT=$(python3 "$GUARD" --set weekly=5)
check "--set targets most recent session (story-b)" "$OUT" "story-b: budget set to weekly <=5%"
OUT=$(hook PreToolUse 6 36 '"tool_name":"Read","tool_input":{"file_path":"a.ts"}')
check "story-b trips at its own 5% budget (consumed 6)" "$OUT" '"permissionDecision": "ask"'
SID="story-a"
OUT=$(hook PreToolUse 6 36 '"tool_name":"Read","tool_input":{"file_path":"a.ts"}')
check "story-a unaffected, still on default 10% budget" "$OUT" '^{}$'
OUT=$(hook PreToolUse 6 36 '"tool_name":"Bash","tool_input":{"command":"python3 /x/scripts/session_budget.py --set weekly=25 session=30"}')
check "--set via agent Bash -> ask (human approves)" "$OUT" '"permissionDecision": "ask"'
OUT=$(python3 "$GUARD" --status)
check "--status shows override" "$OUT" "override weekly"

echo
echo "RESULTS: $PASS passed, $FAIL failed"
rm -rf .claude/session-budget-state
exit $((FAIL > 0))
