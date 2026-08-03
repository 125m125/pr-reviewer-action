# Task 2 report: compact one-action negotiation

Implemented the model-facing negotiation simplification.

## Changes

- Added `compact_negotiation_context`, which projects unresolved work into short
  `U1`, `U2`, ... target handles and human-readable subjects. Controller-owned
  obligation IDs, session IDs, evidence categories, turn budgets, and leases are
  not required in the model response.
- Added `validate_compact_negotiation`. It accepts exactly one `{kind,target,reason}`
  object, maps the target to authoritative state, derives evidence/session/turn
  values, and then runs the existing semantic and feasibility validation.
- Gateway-backed negotiators now receive the compact target projection and cannot
  execute multi-action responses. Legacy in-process callbacks retain their typed
  state compatibility, but multi-action responses are rejected before execution.
- Follow-up negotiation now runs as a bounded re-evaluation loop: one action,
  one wave, reconciliation, then a fresh decision. It stops on no progress,
  exhausted capacity, or deadline cutoff.
- Added narrow `record-unknown` and `new-session` aliases with an event diagnostic;
  all existing ownership, coverage-gain, deadline, lease, and capacity checks stay
  authoritative.
- Updated the negotiator role prompt to describe the compact protocol.

## Verification

```text
\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_negotiation.py -k compact_negotiation
3 passed, 42 deselected in 0.07s

\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_negotiation.py
45 passed in 0.09s

\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_controller.py
153 passed in 2.58s

\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_cli.py
241 passed in 3.94s

Round-two regression coverage confirms that a `record_unknown` transition
(`PENDING` to `UNRESOLVED`) does not count as coverage progress and therefore
does not trigger another negotiator call.

The controller regression test verifies that newly collected evidence counts as
progress even while the same obligation remains unresolved, allowing a second
negotiation decision for the missing evidence category.
```

## Concerns

The old `validate_negotiation` full-shape API remains for existing in-process
callers and tests. Gateway model calls use the compact validator explicitly;
future callers should migrate to `validate_compact_negotiation`.
