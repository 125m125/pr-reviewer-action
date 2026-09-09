# Task 1: Planner continuation adapter report

## Scope delivered

- Added the planner-only, bounded three-request continuation adapter.
- Retains truncated intermediate reasoning in the temporary planner conversation.
- Forces the third request to use `reasoning_effort: none` and a JSON-only note.
- Uses `SPECIALIST_PLANNER_MAX_TOKENS` as the planner request allowance without
  reducing it to the generic session output limit.

## TDD evidence

1. Added provider-transport tests for two reasoning-only `length` responses,
   the final JSON request, retained first/second reasoning, forced final
   reasoning disablement, preserved JSON response formatting, and the 8192
   planner allowance over a 4096 caller limit.
2. RED command:

   ```powershell
   $env:PYTHONPATH = (Get-Location).Path
   .\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_controller.py
   ```

   Result: 2 failures / 98 passes. The first failed while attempting to parse
   the first `reasoning_content` response as JSON; the second observed the
   generic 512-token request value instead of 8192.
3. After the minimal implementation, the same focused command passed: 100
   tests.

## Final verification

- Focused CLI and controller tests: 100 passed.
- Specialist-runtime Python suite: 512 passed.
- `git diff --check`: clean.

PowerShell does not expand `tests/test_specialist_runtime_*.py` for pytest;
the requested suite was therefore run with `Get-ChildItem` expansion and the
same pytest invocation.

## Round 1 fix: invalid forced-final planner response

The controller previously entered `planner:repair:1` after an assignment-invalid
JSON response from the forced third planner request. That bypassed the physical
three-request cap. Parsed planner mappings now carry whether the forced-final
request was used; when that final mapping fails deterministic assignment
validation, the controller records the validation degradation and takes the
existing deterministic assignment fallback without a repair request.

### Regression TDD evidence

1. Added a controller-level provider-transport regression with two truncated
   reasoning responses followed by syntactically valid but assignment-invalid
   JSON (`{"assignments":[]}`).
2. RED command:

   ```powershell
   $env:PYTHONPATH = (Get-Location).Path
   .\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_controller.py -k truncated_planner_final_json
   ```

   Result: failed as expected: the transport recorded 4 requests instead of 3.
3. GREEN command:

   ```powershell
   $env:PYTHONPATH = (Get-Location).Path
   .\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_controller.py
   ```

   Result: 101 passed in 4.29s.

## Round 2 fix: controller-wide logical planner budget

The forced-final marker did not cover an assignment-invalid JSON object from
the second continuation. The controller now creates one mutable
`PlannerRequestBudget` with three remaining physical requests for each `_plan`
invocation and passes it through every planner `RoleRequest`, including
semantic repair. The planner adapter consumes one budget unit before each
provider request. A repair remains available when capacity is left; otherwise
the existing deterministic fallback is used.

### Regression TDD evidence

1. Added a provider-transport regression for a truncated first response,
   assignment-invalid second response, and truncated repair response. It
   asserts that no fourth provider request is made. Added coverage that a
   valid semantic repair still succeeds with remaining budget.
2. RED command:

   ```powershell
   $env:PYTHONPATH = (Get-Location).Path
   .\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_controller.py -k "invalid_second_planner_continuation or semantic_repair_uses_remaining"
   ```

   Result: 1 failed, 1 passed; the invalid-second-continuation case recorded
   4 requests rather than 3.
3. GREEN command:

   ```powershell
   $env:PYTHONPATH = (Get-Location).Path
   .\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_controller.py
   ```

   Result: 103 passed in 3.36s.

## Final-review fix: global finalization mode

The adapter previously used its local continuation index to determine forced
finalization. A semantic repair starts a fresh adapter loop, so its globally
third request could still retain configured reasoning. Finalization now uses
the shared planner budget's last remaining unit when that budget is present;
the adapter-local third-turn rule remains only for unbudgeted direct calls.

### Regression TDD evidence

1. Extended the second-continuation semantic-repair transport regression to
   configure `AI_REASONING_EFFORT=high` and assert that its globally third
   payload sends `reasoning_effort: none` plus the JSON-only user instruction.
2. RED command:

   ```powershell
   $env:PYTHONPATH = (Get-Location).Path
   .\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_controller.py -k invalid_second_planner_continuation
   ```

   Result: failed as expected: the third payload carried `high` rather than
   `none` reasoning effort.
3. GREEN verification:

   ```powershell
   $env:PYTHONPATH = (Get-Location).Path
   .\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_controller.py
   ```

   Result: 103 passed in 3.37s.

   The complete specialist-runtime suite, expanded with `Get-ChildItem` for
   PowerShell, also passed: 515 passed in 8.48s.
