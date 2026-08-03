# Task 3 report: bounded context references

## Implemented

- Summary validation still requires every direct change row to name an exact changed path.
- Summary prose may mention an unchanged path only when the controller supplied it as deterministic context (`context_paths`, affected consumer/producer/callee fields, path-bearing topology relationships, generated-artifact context, or retained evidence supplied at handoff time).
- Direct change verbs applied to an unchanged context path are still rejected; contextual wording such as “affects” or “traced into” is accepted.
- Handoff reference arrays remain bounded internal provenance metadata. They are never rendered as file inventories in the human-facing comment.
- Handoff prose may use controller-authorized context paths for “What the AI reviewed”, while findings and direct change claims continue to use changed paths.

## Verification

Commands and results:

```text
$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_controller.py -q
157 passed in 2.66s

$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_adjudication.py tests/test_specialist_runtime_adjudication_adversarial.py -q
124 passed in 0.18s
```

The focused tests cover accepted controller-supplied context, rejection of direct unchanged-path claims, rejection of arbitrary tracked paths, and natural-language handoff rendering without provenance arrays.

The direct-claim regression coverage also includes “changes in”, “the change to”,
“adds behavior to”, “refactoring of”, and passive “was modified” wording for
unchanged context paths; all are rejected while causal “affects” wording remains
accepted.

Follow-up verification after broadening the direct-claim guard:

```text
$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_adjudication.py tests/test_specialist_runtime_adjudication_adversarial.py -q
287 passed in 2.93s
```

## Notes

Context paths are intersected with controller-known repository paths. This intentionally keeps arbitrary model-invented paths rejected. The direct-change detector is deliberately narrow and only treats a nearby change verb as a direct claim; ordinary causal wording remains available to the handoff.
