# Task 1 report: candidate update checkpoints

Implemented the candidate lifecycle checkpoint protocol in
`pr_reviewer/specialist_runtime/session.py`.

## Changes

- Checkpoints accept additive `candidate_updates` and `new_candidates` arrays.
- Existing active candidates are carried forward when both arrays are empty.
- `withdrawn` and `superseded` updates remove a known candidate from the active
  findings; omission never withdraws a candidate.
- Checkpoint requests include a compact active-candidate register with stable
  candidate IDs, claims, and locations.
- Retention accounting uses structured candidate fields, while malformed
  candidate-shaped JSON remains conservatively degraded and ordinary prose is
  not treated as candidate state.
- Legacy `candidate_findings` checkpoints remain accepted for compatibility.

## Verification

Command:

```text
$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_session.py -q
```

Output:

```text
.......................................................                  [100%]
55 passed in 0.27s
```

Additional runtime regression suite:

```text
$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_replay.py tests/test_specialist_runtime_scheduler.py -q
207 passed in 6.51s
```

## Concerns

The legacy candidate object shape is intentionally retained for older model
providers. New prompts prefer short candidate IDs and structured updates, but
the controller still conservatively requests repair when malformed candidate
objects cannot be accounted for.

## Fix round 1

- Candidate registers now explicitly advertise and preserve exact controller
  IDs; no unmapped short aliases are promised.
- `superseded` updates require `superseded_by` and the replacement must be an
  active candidate in the same checkpoint.
- Candidate retention heuristics only treat malformed JSON/fenced candidate
  payloads as material; ordinary prose containing candidate vocabulary is safe.

Verification:

```text
$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_session.py -q
58 passed in 0.28s

$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_replay.py tests/test_specialist_runtime_scheduler.py -q
207 passed in 6.72s
```

## Fix round 2

- Candidate updates are applied atomically. Self-supersedes are rejected, and
  every superseded replacement must remain active after all updates in the
  checkpoint; invalid payloads leave the prior candidate registry untouched.
- Retention detection also catches bounded malformed candidate JSON embedded
  after prose, while ordinary prose containing candidate vocabulary remains
  non-material.

Verification:

```text
$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_session.py -q
61 passed in 0.29s

$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_replay.py tests/test_specialist_runtime_scheduler.py -q
207 passed in 6.54s
```

## Final fix round

Embedded malformed-candidate detection now requires a candidate field followed
by JSON-like syntax (`:` and `[`/`{`). This catches truncated payloads embedded
after prose without treating unrelated braces in ordinary prose as candidate
state.

Verification:

```text
$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_session.py -q
62 passed in 0.27s

$env:PYTHONPATH='.'; .\\.venv\\Scripts\\pytest.exe tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_replay.py tests/test_specialist_runtime_scheduler.py -q
207 passed in 6.32s
```
