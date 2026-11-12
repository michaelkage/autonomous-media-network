#!/usr/bin/env bash
set -euo pipefail

# Apply the repository's baseline main-branch protection.
# Requires GitHub CLI authentication with repository administration permission.

if ! command -v gh >/dev/null 2>&1; then
  echo "error: GitHub CLI (gh) is required" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required" >&2
  exit 1
fi

repo="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
branch="main"

gh auth status >/dev/null
default_branch="$(gh api "repos/${repo}" --jq .default_branch)"
if [[ "${default_branch}" != "${branch}" ]]; then
  echo "error: expected default branch 'main', found '${default_branch}'" >&2
  exit 1
fi

required_contexts='[
  "test (Python 3.11)",
  "test (Python 3.12)",
  "test (Python 3.13)",
  "Reddit Devvit bridge"
]'

payload="$(jq -n --argjson contexts "${required_contexts}" '{
  required_status_checks: {
    strict: true,
    contexts: $contexts
  },
  enforce_admins: true,
  required_pull_request_reviews: {
    dismiss_stale_reviews: true,
    required_approving_review_count: 0,
    require_code_owner_reviews: false,
    require_last_push_approval: false
  },
  restrictions: null,
  allow_force_pushes: false,
  allow_deletions: false,
  required_linear_history: true
}')"

printf '%s' "${payload}" |
  gh api --method PUT "repos/${repo}/branches/${branch}/protection" --input - >/dev/null

protection="$(gh api "repos/${repo}/branches/${branch}/protection")"
if ! jq -e --argjson contexts "${required_contexts}" '
  .required_status_checks.strict == true and
  (.required_status_checks.contexts | sort) == ($contexts | sort) and
  .enforce_admins.enabled == true and
  .required_pull_request_reviews != null and
  .allow_force_pushes.enabled == false and
  .allow_deletions.enabled == false and
  .required_linear_history.enabled == true
' <<<"${protection}" >/dev/null; then
  echo "error: GitHub did not return the expected branch-protection policy" >&2
  exit 1
fi

echo "Branch protection verified for ${repo}:${branch}"
echo "Required CI checks: Python 3.11, 3.12, 3.13, Reddit Devvit bridge"
echo "Pull requests required; force-pushes and branch deletion disabled."
