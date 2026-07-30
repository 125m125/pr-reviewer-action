# Task 4 report — Tool-aware follow-up budgeting

## Outcome

- Durable-session negotiation now projects both remaining model turns and
  remaining tool calls from the live lifetime ledger (or the detached result
  usage for test/custom sessions).
- `resume` and `consult` are infeasible when their durable session has no tool
  calls left, even if model turns remain. The deterministic fallback can still
  choose a bounded fresh session or `record_unknown`.
- Fresh sessions retain the existing controller-owned per-session budget. No
  durable ledger is replaced or recharged, and existing artifact hard-total
  accounting is unchanged.
- Runtime, action input, deprecated alias, and dogfood workflow defaults now use
  64 model turns and 128 tool calls.
- Specialist transient status already reported both remaining turns and tools;
  no duplicate status mechanism was added.

## RED

Focused tests were added before production changes. With
`.venv\Scripts\python.exe -m pytest`, they failed for the intended reasons:

- `SessionResources` rejected `remaining_tool_calls` as an unknown field.
- a 64-turn/two-tools-per-turn ledger exhausted the old 20-tool default.
- `action.yml` and `.github/workflows/ai-pr-review.yaml` still exposed 20 tools.

## GREEN

Final scoped verification:

```text
681 passed in 10.07s
```

This covered every `tests/test_specialist_runtime_*.py` module plus
`tests/test_action_inputs.py` and `tests/test_ai_pr_review_workflow.py`.
`git diff --check` also passed.

## Self-review

The feasibility rule intentionally requires only positive tool capacity rather
than accepting model-estimated tool counts. The model does not own reliable
tool-cost estimation; the session's hard lifetime ledger remains the authority
for every actual call. This prevents silent recharge without introducing a
second speculative budget.
