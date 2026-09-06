# Task 3 report — Conservative candidate consolidation and critic fallback

## RED

Focused command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_specialist_runtime_adjudication.py `
  tests/test_specialist_runtime_adjudication_adversarial.py `
  tests/test_specialist_runtime_controller.py `
  -k "controller_root_identity or controller_root_merge or critic_degradation_emits_one_verification"
```

Before production changes, collection failed because
`consolidate_candidates` did not exist. The new tests covered:

- repeated budget-validation, workflow-trigger, rationale-format, and
  location-normalization roots from separate specialists;
- precise changed-location, evidence, obligation, severity, and contributor
  retention;
- same-file/different-symbol and same-symbol/different-category non-merges;
- model-provided fingerprint non-authority; and
- critic degradation producing one verification request for a consolidated
  root.

Self-review added two further conservative identity regressions:

- anchorless same-file/same-category concerns with one known changed symbol:
  **1 failed** because the symbol was inferred without an explicit match;
- partial symbol names such as `validate_budget_window`:
  **1 failed** because substring matching admitted them as `validate_budget`.

Independent review found two contributor-laundering paths. The production-shaped
regressions failed before the fix:

- an unsupported duplicate supplied a more precise changed line; and
- a duplicate citing retained but obligation-irrelevant evidence supplied a
  stronger severity.

Combined review-fix RED: **3 failed**.

Re-review then added the reused-valid-evidence case: **1 failed** because a
duplicate could cite the representative's valid evidence while supplying a
fabricated line and blocker severity.

The final mutation combined shared satisfying evidence with a unique unrelated
record: **1 failed** until donor uniqueness was restricted to the exact
obligation-satisfying evidence IDs.

## GREEN

Focused Task 3 suites:

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_specialist_runtime_adjudication.py `
  tests/test_specialist_runtime_adjudication_adversarial.py `
  tests/test_specialist_runtime_controller.py
```

Result: **255 passed**.

Full `pytest tests/ -q` result: **1853 passed, 21 failed**. All 21 failures are
Windows/POSIX test-environment incompatibilities outside the changed surface:
the POSIX `0600` assertion, Bash commands containing Windows Python paths,
unavailable Windows `python3`, and Bash fake-`gh` scripts.

Additional checks:

- `git diff --check`: clean.
- Targeted `python -m compileall`: clean.

## Implementation

- Added deterministic pre-critic consolidation keyed only by a normalized
  controller-known changed path, an explicitly matched changed symbol/contract,
  and a normalized root-cause category. Candidate IDs and model fingerprints
  do not authorize identity.
- Changed anchors come from immutable change facts (`symbols`,
  `action_inputs`, `workflow_steps`, `workflow_keys`, and `headings`) or
  controller obligations. Ambiguous, partial, or absent anchor matches remain
  separate.
- Consolidated candidates retain stable contributor IDs, valid retained
  evidence, known related obligations, and a canonical controller root
  fingerprint.
- Location precision and strongest severity can be donated only by a
  contributor whose retained evidence satisfies its related controller-owned
  obligation, using the existing `evidence_satisfies_obligation` predicate,
  and whose exact satisfying evidence ID is unique within the consolidated
  group. A unique unrelated record cannot unlock donation. When support is
  shared and therefore cannot distinguish contributors, conflicting lines
  fall back to file-level and the lower severity wins. Unsupported duplicates
  cannot launder a fabricated line or severity.
- The critic receives one candidate per controller root and retains its existing
  authority to keep, reject, merge, request verification, or downgrade.
- If the critic is missing or degrades, the conservative fallback now emits at
  most one verification request per consolidated root. Pre-critic contributor
  merge dispositions remain visible in the event journal and terminal artifact.

## Self-review

- Confirmed exact duplicate candidate-ID collision behavior remains fail-closed
  before semantic consolidation.
- Confirmed singletons with known roots receive controller fingerprints, while
  model-provided fingerprints cannot merge distinct roots.
- Confirmed malformed/anchorless candidates and unrelated same-file concerns
  remain separate.
- Confirmed invalid evidence and unknown obligations are not unioned into a
  consolidated candidate when controller authority can filter them.
- Confirmed no unrelated files or existing untracked diagnostic artifacts were
  modified.
