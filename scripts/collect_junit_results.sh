#!/usr/bin/env bash
set -euo pipefail

# Discover bounded, same-head JUnit artifacts and normalize them for the
# specialist runtime. Test-reporting actions may render checks, but the XML
# artifact remains the complete source for passed and skipped cases.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/platform_api.sh
source "$SCRIPT_DIR/platform_api.sh"

output="${1:?output manifest required}"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

reason="no same-head JUnit artifact matched junit/test-results"
archives=()
if platform_actions_artifacts_for_head "$REPO" "$PR_HEAD_SHA" > "$tmp/artifacts.json"; then
  while IFS=$'\t' read -r id name size; do
    [[ "$id" =~ ^[0-9]+$ ]] || continue
    [[ "$size" =~ ^[0-9]+$ ]] || size=0
    (( size <= 52428800 )) || continue
    safe_name="$(printf '%s' "$name" | tr -cs 'A-Za-z0-9._-' '_' | cut -c1-120)"
    destination="$tmp/${id}-${safe_name:-junit}.zip"
    if platform_artifact_download "$REPO" "$id" "$destination"; then
      archives+=("$destination")
    fi
  done < <(
    jq -r '
      [.[]
       | select(.name | test("junit|test[-_ .]?results?"; "i"))]
      | sort_by(.created_at) | reverse | .[:8][]
      | [.id, .name, .size_in_bytes] | @tsv
    ' "$tmp/artifacts.json"
  )
  [[ ${#archives[@]} -gt 0 ]] || reason="no same-head JUnit artifact matched junit/test-results"
else
  reason="GitHub Actions artifacts could not be listed"
fi

PYTHONPATH="$SCRIPT_DIR/..${PYTHONPATH:+:$PYTHONPATH}" python3 -m \
  pr_reviewer.specialist_runtime.test_results \
  --repository "$REPO" --head-sha "$PR_HEAD_SHA" --output "$output" \
  --unavailable-reason "$reason" "${archives[@]}"
