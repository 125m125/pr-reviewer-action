#!/usr/bin/env bash
set -euo pipefail

# ── wait_for_ci.sh ──────────────────────────────────────────────────────
# Polls GitHub's commit-status API until all checks are final (success/failure),
# a skip signal is detected, or the timeout expires.
#
# Exit codes:
#   0 – All checks reached a terminal state (success or failure).
#   1 – Timeout reached and ci_skip_on_timeout=true (review may proceed anyway).
#   2 – Fatal error (no token, repo/PR unresolvable, API unrecoverable).

GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
REPO="${REPO:-${GITHUB_REPOSITORY:-}}"
PR_NUMBER="${PR_NUMBER:-}"
CI_STATUS_CHECK="${CI_STATUS_CHECK:-false}"
CI_TIMEOUT_SEC="${CI_TIMEOUT_SEC:-300}"
CI_INTERVAL_SEC="${CI_INTERVAL_SEC:-15}"
CI_SKIP_ON_TIMEOUT="${CI_SKIP_ON_TIMEOUT:-true}"
OUTPUT_FILE="${GITHUB_OUTPUT:-/dev/null}"

if [[ "$CI_STATUS_CHECK" != "true" ]]; then
  echo "ci_status_skipped=true" >> "$OUTPUT_FILE"
  exit 0
fi

if [[ -z "$GH_TOKEN" || -z "$REPO" || -z "$PR_NUMBER" ]]; then
  echo "Missing GH_TOKEN, REPO, or PR_NUMBER for CI status check" >&2
  echo "ci_status_skipped=true" >> "$OUTPUT_FILE"
  exit 0
fi

# ── Helpers ─────────────────────────────────────────────────────────────

log() {
  echo "[CI-status] $(date +'%H:%M:%S') $1"
}

error() {
  log "ERROR: $1" >&2
}

# Get the head SHA of the PR (may differ from event.pull_request.head.sha if
# the action is invoked manually with a stale pr_number).
get_head_sha() {
  gh api "repos/$REPO/pulls/$PR_NUMBER" --jq '.head.sha' 2>/dev/null
}

# ── Main loop ───────────────────────────────────────────────────────────

sha="$(get_head_sha)"
if [[ -z "$sha" ]]; then
  error "Could not resolve head SHA for #$PR_NUMBER"
  exit 2
fi

log "Polling commit-status for $sha (timeout=${CI_TIMEOUT_SEC}s, interval=${CI_INTERVAL_SEC}s)..."

elapsed=0
while true; do
  if [[ "$elapsed" -ge "$CI_TIMEOUT_SEC" ]]; then
    log "Timeout reached after ${elapsed}s"
    if [[ "$(printf '%s' "$CI_SKIP_ON_TIMEOUT" | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
      log "ci_skip_on_timeout=true — proceeding without CI context"
      echo "ci_status_skipped=true" >> "$OUTPUT_FILE"
      exit 1   # non-zero so the caller can decide whether to skip or continue
    else
      error "Timeout reached and ci_skip_on_timeout=false — aborting review"
      echo "ci_status_skipped=true" >> "$OUTPUT_FILE"
      exit 2
    fi
  fi

  response="$(gh api "repos/$REPO/commits/$sha/status" 2>/dev/null || true)"

  if [[ -z "$response" ]]; then
    log "API returned empty; retrying in ${CI_INTERVAL_SEC}s..."
    sleep "$CI_INTERVAL_SEC"
    elapsed=$((elapsed + CI_INTERVAL_SEC))
    continue
  fi

  # Determine overall state: pending | success | failure | error | neutral
  overall="$(printf '%s' "$response" | jq -r '.state // "unknown"' 2>/dev/null || echo "unknown")"

  # Check for any non-completed checks in the check-runs API as well (the combined
  # status API sometimes lags behind). GitHub Checks API statuses are:
  # queued, in_progress, completed, requested, waiting, stale.
  # We count all runs where status != "completed" (i.e., still running or queued).
  pending_checks=0
  check_runs_response=""
  if [[ "$overall" != "failure" && "$overall" != "success" ]]; then
    # Fetch check-runs once and reuse for both pending and failed counts
    check_runs_response="$(gh api "repos/$REPO/commits/$sha/check-runs?per_page=100" 2>/dev/null || echo "{}")"
    pending_checks="$(printf '%s' "$check_runs_response" | jq '[.check_runs[] | select(.status != "completed")] | length' 2>/dev/null || echo 0)"
  fi

  if [[ "$overall" == "success" || "$overall" == "failure" ]]; then
    # Terminal state reached (even a single failure counts as terminal).
    log "CI checks finalized: $overall"
    echo "ci_status_final=$overall" >> "$OUTPUT_FILE"
    echo "ci_status_skipped=false" >> "$OUTPUT_FILE"
    exit 0
  fi

  # Detect failed check runs even when the combined status API hasn't caught up yet.
  # This handles repos that rely primarily on GitHub Checks (not legacy statuses).
  if [[ -n "$check_runs_response" ]]; then
    failed_checks="$(printf '%s' "$check_runs_response" | jq '[.check_runs[] | select(.status == "completed" and .conclusion == "failure")] | length' 2>/dev/null || echo 0)"
    if [[ "$failed_checks" -gt 0 ]]; then
      log "Detected $failed_checks failed check run(s) before combined status updated — treating as failure"
      echo "ci_status_final=failure" >> "$OUTPUT_FILE"
      echo "ci_status_skipped=false" >> "$OUTPUT_FILE"
      exit 0
    fi
  fi

  if [[ "$pending_checks" -gt 0 ]]; then
    log "Overall=$overall, $pending_checks check(s) not yet completed — waiting ${CI_INTERVAL_SEC}s..."
  else
    log "Overall=$overall — waiting ${CI_INTERVAL_SEC}s..."
  fi

  sleep "$CI_INTERVAL_SEC"
  elapsed=$((elapsed + CI_INTERVAL_SEC))
done
