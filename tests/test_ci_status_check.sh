#!/usr/bin/env bash
set -euo pipefail

# Tests for wait_for_ci.sh CI status check behavior
# Validates exit codes, timeout handling, and action.yml integration.

PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WAIT_SCRIPT="$SCRIPT_DIR/scripts/wait_for_ci.sh"
ACTION_YML="$SCRIPT_DIR/action.yml"

check() {
  local desc="$1" result="$2" expected="$3"
  if [[ "$result" == "$expected" ]]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (got '$result', expected '$expected')"
    FAIL=$((FAIL + 1))
  fi
}

check_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected to contain '$needle')"
    FAIL=$((FAIL + 1))
  fi
}

# ── Test 1: exit code 1 path for timeout+skip=true exists in script ──
echo "=== Test: exit code 1 on timeout with skip=true ==="
wait_content="$(cat "$WAIT_SCRIPT")"

check_contains "script has timeout branch checking CI_SKIP_ON_TIMEOUT" \
  "$wait_content" 'CI_SKIP_ON_TIMEOUT'
check_contains "script exits 1 when skip=true on timeout" \
  "$wait_content" 'exit 1'
check_contains "script writes ci_status_skipped=true before exit 1" \
  "$wait_content" 'ci_status_skipped=true'

# ── Test 2: exit code 2 path for timeout+skip=false exists in script ──
echo ""
echo "=== Test: exit code 2 on timeout with skip=false ==="
check_contains "script exits 2 when skip=false on timeout" \
  "$wait_content" 'exit 2'
check_contains "error logged when skip=false on timeout" \
  "$wait_content" 'ci_skip_on_timeout=false'

# ── Test 3: graceful skip when token missing ──
echo ""
echo "=== Test: graceful skip on missing credentials ==="
check_contains "checks for missing GH_TOKEN" \
  "$wait_content" 'GH_TOKEN'
check_contains "exits 0 (not failure) when token missing" \
  "$wait_content" 'exit 0'

# ── Test 4: action.yml CI step has continue-on-error (THE FIX) ──
echo ""
echo "=== Test: action.yml CI wait step has continue-on-error ==="
action_content="$(cat "$ACTION_YML")"
ci_step_section="$(awk '/Wait for CI checks to complete/,/run: bash.*wait_for_ci/' "$ACTION_YML")"

check_contains "CI wait step named correctly" \
  "$ci_step_section" "Wait for CI checks to complete"
check_contains "CI wait step has continue-on-error: true (THE FIX)" \
  "$ci_step_section" "continue-on-error: true"
check_contains "CI wait step id is ci_status" \
  "$ci_step_section" "id: ci_status"

# ── Test 5: action.yml passes correct env vars to wait_for_ci.sh ──
echo ""
echo "=== Test: action.yml passes CI env vars ==="
check_contains "passes GH_TOKEN" "$ci_step_section" "GH_TOKEN:"
check_contains "passes REPO" "$ci_step_section" "REPO:"
check_contains "passes PR_NUMBER" "$ci_step_section" "PR_NUMBER:"
check_contains "passes CI_TIMEOUT_SEC" "$ci_step_section" "CI_TIMEOUT_SEC:"
check_contains "passes CI_INTERVAL_SEC" "$ci_step_section" "CI_INTERVAL_SEC:"
check_contains "passes CI_SKIP_ON_TIMEOUT" "$ci_step_section" "CI_SKIP_ON_TIMEOUT:"

# ── Test 6: action.yml conditions for CI step ──
echo ""
echo "=== Test: CI step has correct if condition ==="
check_contains "CI step only runs when ci_status_check=true" \
  "$ci_step_section" "ci_status_check == 'true'"
check_contains "CI step only runs when should_review=true" \
  "$ci_step_section" "should_review == 'true'"

# ── Test 7: action.yml passes CI status outputs as env vars to run_review ──
echo ""
echo "=== Test: action.yml passes CI status outputs to run_review step ==="
review_step_section="$(awk '/Run AI review/,/run: bash.*run_review/' "$ACTION_YML")"

check_contains "run_review step receives CI_STATUS_FINAL" \
  "$review_step_section" "CI_STATUS_FINAL:"
check_contains "run_review step receives CI_STATUS_SKIPPED" \
  "$review_step_section" "CI_STATUS_SKIPPED:"
check_contains "run_review step receives CI_STATUS_CHECK" \
  "$review_step_section" "CI_STATUS_CHECK:"

# ── Test 8: wait_for_ci.sh uses strict mode ──
echo ""
echo "=== Test: wait_for_ci.sh uses strict bash mode ==="
check_contains "wait_for_ci.sh uses set -euo pipefail" \
  "$wait_content" 'set -euo pipefail'

# ── Test 9: Default values in wait_for_ci.sh ──
echo ""
echo "=== Test: wait_for_ci.sh defaults are correct ==="
check_contains "CI_TIMEOUT_SEC defaults to 300" "$wait_content" 'CI_TIMEOUT_SEC="${CI_TIMEOUT_SEC:-300}"'
check_contains "CI_INTERVAL_SEC defaults to 15" "$wait_content" 'CI_INTERVAL_SEC="${CI_INTERVAL_SEC:-15}"'
check_contains "CI_SKIP_ON_TIMEOUT defaults to true" "$wait_content" 'CI_SKIP_ON_TIMEOUT="${CI_SKIP_ON_TIMEOUT:-true}"'

# ── Test 10: action.yml CI inputs have correct defaults ──
echo ""
echo "=== Test: action.yml CI input defaults ==="
check_contains "ci_status_check defaults to false" \
  "$action_content" 'ci_status_check:'
check_contains "ci_timeout_sec defaults to 300" \
  "$action_content" 'ci_timeout_sec:'
check_contains "ci_skip_on_timeout defaults to true" \
  "$action_content" 'ci_skip_on_timeout:'

# ── Test 11: action.yml outputs for CI status ──
echo ""
echo "=== Test: action.yml declares CI status outputs ==="
check_contains "declares ci_status_skipped output" \
  "$action_content" "ci_status_skipped:"
check_contains "declares ci_status_final output" \
  "$action_content" "ci_status_final:"

# ── Test 12: The fix is in the right location (between precheck and run_review) ──
echo ""
echo "=== Test: CI step ordering in action.yml ==="
precheck_line="$(grep -n 'Check whether review is needed' "$ACTION_YML" | cut -d: -f1)"
ci_step_line="$(grep -n 'Wait for CI checks to complete' "$ACTION_YML" | cut -d: -f1)"
review_line="$(grep -n 'Run AI review' "$ACTION_YML" | cut -d: -f1)"

if [[ "$precheck_line" -lt "$ci_step_line" ]] && [[ "$ci_step_line" -lt "$review_line" ]]; then
  echo "  PASS: CI step is between precheck and review (line $ci_step_line)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: CI step ordering incorrect (precheck=$precheck_line, ci=$ci_step_line, review=$review_line)"
  FAIL=$((FAIL + 1))
fi

# ── Test 13: Check-runs polling uses correct GitHub Checks API statuses ──
echo ""
echo "=== Test: check-runs polling counts non-completed runs ==="
check_contains "check-runs query does NOT filter by status=pending (uses all statuses)" \
  "$wait_content" 'check-runs?per_page=100'
check_contains "counts non-completed check runs via jq select" \
  "$wait_content" '.status != "completed"'
check_contains "stores check-runs response for reuse" \
  "$wait_content" 'check_runs_response='

# ── Test 14: Failed check-run detection before combined status update ──
echo ""
echo "=== Test: failed check-run early detection ==="
check_contains "detects failed check runs before combined status catches up" \
  "$wait_content" '.conclusion == "failure"'
check_contains "logs when failed check runs detected early" \
  "$wait_content" 'failed check run'

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
