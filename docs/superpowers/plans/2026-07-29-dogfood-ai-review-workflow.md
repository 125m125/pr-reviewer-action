# Dogfood AI Review Workflow Implementation Plan

**Goal:** Migrate the imported AI review workflow to this repository's current
version-2 specialist runtime and add tailored policy/prompt configuration.

**Architecture:** A label-only workflow runs the pinned action in the existing
minimal container on the LM Studio runner. Qwen 3.6 performs sequential,
bounded specialist sessions selected by a current-branch version-2 policy.

**Tech Stack:** GitHub Actions YAML, JSON policy, Markdown prompt/rules, pytest.

---

### Task 1: Tailor the workflow

**Files:**
- Modify: `.github/workflows/ai-pr-review.yaml`

1. Reduce the trigger and job guard to the manual `ai-review` label flow.
2. Preserve the container preparation and hardening, and pin the reviewed image
   by immutable OCI index digest.
3. Select Qwen 3.6 and retain LM Studio structured-output settings.
4. Replace deprecated pass/packet configuration with version-2 policy and
   lifetime session inputs.
5. Configure the repository prompt as an appended addendum.
6. Wire an optional trusted search endpoint while retaining current-policy
   source enforcement.
7. Keep safe publishing and read-only tool constraints.

### Task 2: Add repository policy and prompt

**Files:**
- Create: `.github/ai-review-policy.json`
- Create: `.github/ai-review-prompt.md`

1. Define components and related contracts for orchestration, runtime,
   transport, publishing, and verification/documentation.
2. Define bounded coverage/dedicated recipes for realistic risks in this repo.
3. Allow only narrowly selected official documentation hosts.
4. Restrict publishing to non-approving `review_comment`.
5. Add concise review priorities that complement, rather than replace, the
   bundled specialist protocol and existing rules.

### Task 3: Validate and review

**Files:**
- Test: `.github/workflows/ai-pr-review.yaml`
- Test: `.github/ai-review-policy.json`
- Test: `.github/ai-review-prompt.md`

1. Parse YAML/JSON and run the repository's policy loader.
2. Run focused policy, action-input, migration, and workflow tests.
3. Request a code review focused on realistic security, compatibility, and
   runtime failures; reject purely speculative or stylistic churn.
4. Apply justified fixes and re-run focused verification.
5. Commit the configuration as a cohesive change.
