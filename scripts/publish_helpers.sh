#!/usr/bin/env bash
set -euo pipefail

# Shared helpers for publish steps in action.yml.
# Source this script from each publish step, then call the functions.

# Sanitize model output: strip metadata markers and neutralize upstream references.
# Args: $1 = output file path
# Writes sanitized markdown to the output file using $REVIEW_MARKDOWN env var.
sanitize_review_markdown() {
  local output_file="$1"
  printf '%s\n' "$REVIEW_MARKDOWN" > "$output_file"
  python3 "${GITHUB_ACTION_PATH}/scripts/strip_metadata_markers.py" "$output_file"
  python3 "${GITHUB_ACTION_PATH}/scripts/sanitize_review_markdown.py" "$output_file"
}

# Resolve cleanup flag based on input value and publish mode.
# Args: $1 = CLEANUP_PREVIOUS_NATIVE_REVIEWS input, $2 = PUBLISH_MODE
# Outputs: "true" or "false" to stdout
resolve_cleanup_flag() {
  local cleanup_input="$1"
  local publish_mode="$2"
  local result=false

  case "$(printf '%s' "$cleanup_input" | tr '[:upper:]' '[:lower:]')" in
    true) result=true ;;
    false) result=false ;;
    auto|"")
      if [ "$publish_mode" = "review_comment" ] || [ "$publish_mode" = "review_verdict" ]; then
        result=true
      else
        result=false
      fi
      ;;
    *) echo "Invalid cleanup_previous_native_reviews value; expected auto, true, or false" >&2; return 1 ;;
  esac

  printf '%s' "$result"
}

# Cleanup previous managed native reviews.
# Requires env: GH_TOKEN, REPO, PR_NUMBER; optional COMMENT_MARKER.
# Args: $1 = resolved cleanup flag ("true"/"false")
#
# Managed reviews are matched by the marker their bodies START with, never by
# author. Author matching via `gh api user` was structurally broken: /user
# returns 403 for installation tokens (both the default GITHUB_TOKEN and
# GitHub App tokens), and on HTTP errors gh prints the JSON error body to
# stdout — so the "actor" became a JSON blob that matched no review and
# cleanup silently did nothing on every run (#190). Marker matching is safe
# because cleanup runs before the new review is posted, so it can never touch
# the review the current run is about to create, and it keeps working when
# the workflow's token identity changes (e.g. default token → app token).
cleanup_native_reviews() {
  local should_cleanup="$1"
  if [ "$should_cleanup" != "true" ]; then
    return 0
  fi

  echo "Cleaning up previous managed native reviews for #$PR_NUMBER"
  local reviews_json
  if ! reviews_json="$(gh api "repos/$REPO/pulls/$PR_NUMBER/reviews" --paginate 2>/dev/null)"; then
    echo "  WARN: Could not list reviews for #$PR_NUMBER; skipping cleanup" >&2
    return 0
  fi

  # Managed bodies start with the configured marker (or, for reviews created
  # by older action versions, the bare/JSON "<!-- ai-pr-reviewer" prefix).
  # Reviews already stubbed as outdated are skipped unless they still carry a
  # live verdict (a previous run may have patched the body but failed the
  # dismissal). The list query carries id and state together so no per-review
  # GET is needed afterwards.
  PREV_REVIEWS="$(printf '%s' "$reviews_json" \
    | jq -r --arg marker "${COMMENT_MARKER:-<!-- ai-pr-reviewer -->}" \
        '.[] | select(((.body // "") | startswith($marker))
                   or ((.body // "") | startswith("<!-- ai-pr-reviewer")))
             | select(((.state // "") == "APPROVED" or (.state // "") == "CHANGES_REQUESTED")
                   or (((.body // "") | contains("_Outdated: superseded")) | not))
             | "\(.id)\t\(.node_id // "")\t\(.state // "")"' 2>/dev/null || echo "")"
  if [ -n "$PREV_REVIEWS" ]; then
    while IFS=$'\t' read -r REVIEW_ID REVIEW_NODE_ID REVIEW_STATE; do
      if [ -z "$REVIEW_ID" ]; then continue; fi
      # Dismiss approval/request-changes reviews to stop stale verdicts from counting
      if [ "$REVIEW_STATE" = "APPROVED" ] || [ "$REVIEW_STATE" = "CHANGES_REQUESTED" ]; then
        if gh api "repos/$REPO/pulls/$PR_NUMBER/reviews/$REVIEW_ID/dismissals" --method PUT -f message="Superseded by a newer automated review for this pull request." --jq '.id' >/dev/null 2>&1; then
          echo "  Dismissed outdated managed review #$REVIEW_ID ($REVIEW_STATE)"
        else
          echo "  WARN: Could not dismiss review #$REVIEW_ID (may require additional permissions)" >&2
        fi
      fi
      # Hide the review in the PR timeline. Dismissal only strikes the verdict;
      # the full review text stays expanded without this. PullRequestReview
      # implements GraphQL's Minimizable, the same mechanism as the UI's
      # "Hide" menu.
      if [ -n "$REVIEW_NODE_ID" ]; then
        if gh api graphql \
            -f query='mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { minimizedComment { isMinimized } } }' \
            -f id="$REVIEW_NODE_ID" >/dev/null 2>&1; then
          echo "  Minimized (hidden as outdated) review #$REVIEW_ID"
        else
          echo "  WARN: Could not minimize review #$REVIEW_ID (may require additional permissions)" >&2
        fi
      fi
      # Collapse the body to a one-line stub so even an expanded review stays
      # compact (and so already-processed reviews are skipped next run). The
      # endpoint is PUT — the previous PATCH was a 404 that the old code
      # misreported as "reviews may be read-only".
      OUTDATED_BODY="$(printf '<!-- ai-pr-reviewer -->\n_Outdated: superseded by a newer automated review._')"
      if ! gh api "repos/$REPO/pulls/$PR_NUMBER/reviews/$REVIEW_ID" --method PUT -f body="$OUTDATED_BODY" >/dev/null 2>&1; then
        echo "  WARN: Could not update review #$REVIEW_ID body" >&2
      else
        echo "  Marked review #$REVIEW_ID as outdated/superseded"
      fi
    done <<< "$PREV_REVIEWS"
  else
    echo "  No previous managed native reviews found for #$PR_NUMBER"
  fi
}

# Build metadata marker JSON string.
# Requires env: HEAD_SHA, EFFECTIVE_SCOPE, REVIEW_RESULT
# Args: $1 = base_sha, $2 = previous_head_sha (optional, empty if not incremental)
# Outputs: metadata marker string to stdout
build_metadata_marker() {
  local base_sha="$1"
  local previous_head_sha="${2:-}"

  # Built with jq instead of string surgery: the old "${marker%,*}" trick for
  # appending previous_head_sha cut at the LAST comma, silently dropping
  # review_result and the closing " -->" — which made incremental markers
  # unparseable and degraded the next run back to a full review.
  local marker_json
  marker_json="$(jq -nc \
    --arg head "${HEAD_SHA:-unknown}" \
    --arg base "$base_sha" \
    --arg scope "${EFFECTIVE_SCOPE}" \
    --arg result "${REVIEW_RESULT}" \
    --arg checks "${REQUIRED_CHECKS:-}" \
    --arg route "${REVIEW_ROUTE:-}" \
    --arg esc "${ESCALATION_REASON:-}" \
    --arg prev "$previous_head_sha" \
    '{version: 1, head_sha: $head, base_sha: $base, review_scope: $scope, review_result: $result}
     + (if $checks == "" or $checks == "none" then {} else {required_checks: $checks} end)
     + (if $route == "" or $route == "legacy" then {} else {review_route: $route} end)
     + (if $esc == "" then {} else {escalation_reason: ($esc | split(","))} end)
     + (if $scope == "incremental" and $prev != "" then {previous_head_sha: $prev} else {} end)')"
  printf '<!-- ai-pr-reviewer:%s -->' "$marker_json"
}

# Validate that PR_NUMBER is set.
# Requires env: PR_NUMBER
# Args: $1 = mode description for error message
validate_pr_number() {
  local mode_desc="$1"
  if [ -z "${PR_NUMBER:-}" ]; then
    echo "publish_${mode_desc} requires a pull_request event or explicit pr_number" >&2
    exit 1
  fi
}
