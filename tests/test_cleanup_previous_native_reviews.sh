#!/usr/bin/env bash
set -euo pipefail

# Tests for issue #106: cleanup of superseded native PR reviews
# These tests validate the shell logic used by the native review publish steps.

PASS=0
FAIL=0

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

check_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (got '$haystack', should not contain '$needle')"
    FAIL=$((FAIL + 1))
  fi
}

check_exists() {
  local desc="$1" result="$2"
  if [[ "$result" -gt 0 ]]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc (expected to exist)"
    FAIL=$((FAIL + 1))
  fi
}

# The cleanup resolution logic extracted from action.yml.
# It resolves CLEANUP_PREVIOUS_NATIVE_REVIEWS to CLEANUP_NATIVE_REVIEWS based on PUBLISH_MODE.
resolve_cleanup() {
  local cleanup_input="$1" publish_mode="$2"
  local cleanup_native_reviews=false

  case "$(printf '%s' "$cleanup_input" | tr '[:upper:]' '[:lower:]')" in
    true) cleanup_native_reviews=true ;;
    false) cleanup_native_reviews=false ;;
    auto|"")
      if [ "$publish_mode" = "review_comment" ] || [ "$publish_mode" = "review_verdict" ]; then
        cleanup_native_reviews=true
      else
        cleanup_native_reviews=false
      fi
      ;;
    *) echo "Invalid value" >&2; return 1 ;;
  esac

  printf '%s' "$cleanup_native_reviews"
}

echo "=== Cleanup resolution: auto + review_comment ==="
result="$(resolve_cleanup "auto" "review_comment")"
check "auto resolves true for review_comment" "$result" "true"

echo ""
echo "=== Cleanup resolution: auto + review_verdict ==="
result="$(resolve_cleanup "auto" "review_verdict")"
check "auto resolves true for review_verdict" "$result" "true"

echo ""
echo "=== Cleanup resolution: auto + comment ==="
result="$(resolve_cleanup "auto" "comment")"
check "auto resolves false for comment" "$result" "false"

echo ""
echo "=== Cleanup resolution: explicit true ==="
result="$(resolve_cleanup "true" "comment")"
check "true forces cleanup even for comment mode" "$result" "true"

result="$(resolve_cleanup "TRUE" "comment")"
check "TRUE (uppercase) forces cleanup" "$result" "true"

echo ""
echo "=== Cleanup resolution: explicit false ==="
result="$(resolve_cleanup "false" "review_comment")"
check "false disables cleanup for review_comment" "$result" "false"

result="$(resolve_cleanup "False" "review_verdict")"
check "False (mixed case) disables cleanup" "$result" "false"

echo ""
echo "=== Cleanup resolution: empty defaults to auto ==="
result="$(resolve_cleanup "" "review_comment")"
check "empty string resolves true for review_comment" "$result" "true"

result="$(resolve_cleanup "" "comment")"
check "empty string resolves false for comment" "$result" "false"

echo ""
echo "=== Review body marker validation ==="

ACTION_YML="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/action.yml"

# Check that native review bodies include the ai-pr-reviewer marker
BODY_CONTENT_REVIEW_COMMENT=$(awk '/Publish review comment \(non-blocking\)/,/^    - name: Publish review verdict/' "$ACTION_YML")
BODY_CONTENT_REVIEW_VERDICT=$(awk '/Publish review verdict/,0' "$ACTION_YML")

check_contains "review_comment step uses METADATA_MARKER in body" \
  "$BODY_CONTENT_REVIEW_COMMENT" "METADATA_MARKER"

check_contains "review_verdict step uses METADATA_MARKER in body" \
  "$BODY_CONTENT_REVIEW_VERDICT" "METADATA_MARKER"

# Check that review_comment body does NOT have the marker in the sticky comment mode (comment mode)
STICKY_COMMENT_BODY=$(awk '/Publish review comment$/,/^    - name: Publish review comment \(non-blocking\)/' "$ACTION_YML")
check_contains "sticky comment mode uses COMMENT_MARKER variable" \
  "$STICKY_COMMENT_BODY" "COMMENT_MARKER"

echo ""
echo "=== Helper script validation ==="

HELPER_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/publish_helpers.sh"

check_exists "publish_helpers.sh exists" \
  "$(test -f "$HELPER_SCRIPT" && echo 1 || echo 0)"

check_exists "publish_helpers.sh is executable" \
  "$(test -x "$HELPER_SCRIPT" && echo 1 || echo 0)"

check_contains "helper contains sanitize_review_markdown function" \
  "$(cat "$HELPER_SCRIPT")" "sanitize_review_markdown"

check_contains "helper contains resolve_cleanup_flag function" \
  "$(cat "$HELPER_SCRIPT")" "resolve_cleanup_flag"

check_contains "helper contains cleanup_native_reviews function" \
  "$(cat "$HELPER_SCRIPT")" "cleanup_native_reviews"

check_contains "helper contains build_metadata_marker function" \
  "$(cat "$HELPER_SCRIPT")" "build_metadata_marker"

echo ""
echo "=== Cleanup logic presence validation ==="

# Verify native review steps source the helper script and call functions
BODY_CONTENT_REVIEW_COMMENT=$(awk '/Publish review comment \(non-blocking\)/,/^    - name: Publish review verdict/' "$ACTION_YML")
BODY_CONTENT_REVIEW_VERDICT=$(awk '/Publish review verdict/,0' "$ACTION_YML")

check_contains "review_comment step sources publish_helpers.sh" \
  "$BODY_CONTENT_REVIEW_COMMENT" "publish_helpers.sh"

check_contains "review_verdict step sources publish_helpers.sh" \
  "$BODY_CONTENT_REVIEW_VERDICT" "publish_helpers.sh"

check_contains "review_comment step calls cleanup_native_reviews" \
  "$BODY_CONTENT_REVIEW_COMMENT" "cleanup_native_reviews"

check_contains "review_verdict step calls cleanup_native_reviews" \
  "$BODY_CONTENT_REVIEW_VERDICT" "cleanup_native_reviews"

check_contains "review_comment step calls resolve_cleanup_flag" \
  "$BODY_CONTENT_REVIEW_COMMENT" "resolve_cleanup_flag"

check_contains "review_verdict step calls resolve_cleanup_flag" \
  "$BODY_CONTENT_REVIEW_VERDICT" "resolve_cleanup_flag"

echo ""
echo "=== Input definition validation ==="

check_exists "action.yml has cleanup_previous_native_reviews input" \
  "$(grep -c 'cleanup_previous_native_reviews:' "$ACTION_YML" || echo 0)"

check_contains "cleanup_previous_native_reviews default is auto" \
  "$(cat "$ACTION_YML")" 'default: "auto"'

check_exists "action.yml has CLEANUP_PREVIOUS_NATIVE_REVIEWS env in review_comment step" \
  "$(grep -c 'CLEANUP_PREVIOUS_NATIVE_REVIEWS' <<< "$BODY_CONTENT_REVIEW_COMMENT" || echo 0)"

check_exists "action.yml has CLEANUP_PREVIOUS_NATIVE_REVIEWS env in review_verdict step" \
  "$(grep -c 'CLEANUP_PREVIOUS_NATIVE_REVIEWS' <<< "$BODY_CONTENT_REVIEW_VERDICT" || echo 0)"

echo ""
echo "=== README.md documentation validation ==="

README_MD="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/README.md"

check_exists "README documents cleanup_previous_native_reviews input" \
  "$(grep -c 'cleanup_previous_native_reviews' "$README_MD" || echo 0)"

check_exists "README has native review cleanup section" \
  "$(grep -c 'Native review cleanup' "$README_MD" || echo 0)"

check_contains "README documents auto/default behavior" \
  "$(cat "$README_MD")" "auto"

check_contains "README documents cleanup for review_comment" \
  "$(cat "$README_MD")" "review_comment"

check_exists "README mentions ai-pr-reviewer marker" \
  "$(grep -c 'ai-pr-reviewer' "$README_MD" || echo 0)"

check_exists "README documents dismissal behavior" \
  "$(grep -ci 'dismiss' "$README_MD" || echo 0)"

check_exists "README warns about permission requirements" \
  "$(grep -ci 'permission\|warn' "$README_MD" || echo 0)"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
