# Task 2 report — Reasoning-preserving structured-role continuation

## RED

Command:

```powershell
$env:PYTHONPATH=(Get-Location).Path
.\.venv\Scripts\pytest.exe -q tests/test_conversation.py tests/test_native_tool_loop.py tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_controller.py
```

Result before production changes: **6 failed, 285 passed**.

The failures reproduced:

- missing neutral `assistant_reasoning` state and compaction;
- loss of `reasoning_content` when ordinary content and tool calls were also present;
- run 30543173785's critic-shaped reasoning-only `finish_reason=length` response receiving no continuation;
- change-summarizer repair discarding reasoning and partial content;
- the base/future handoff-summarizer role adapter rebuilding instead of continuing retained history.

Independent review then found that history compaction selected reasoning and
tool-call events independently. The added mixed-shape regression failed
**1 failed, 71 deselected**, reproducing stale reasoning from an old turn being
attached to a newer tool call.

## GREEN

Focused Task 2 command: **292 passed**.

Broader specialist/runtime command:

```powershell
$testFiles = @(Get-ChildItem tests -Filter 'test_specialist_runtime_*.py') +
  @('tests/test_response_parser.py',
    'tests/test_run_native_loop_wiring.py',
    'tests/test_conversation.py',
    'tests/test_native_tool_loop.py')
.\.venv\Scripts\pytest.exe -q @testFiles
```

Result: **847 passed**.

Full `pytest -q tests` result: **1837 passed, 21 failed**. All 21 failures are
Windows/POSIX test-environment incompatibilities outside the changed surface:
POSIX `0600` mode assertions, Bash commands containing unquoted Windows Python
paths, unavailable Windows `python3`, and Bash fake `gh` scripts.

Additional checks:

- `git diff --check`: clean.
- `.\.venv\Scripts\python.exe -m compileall -q pr_reviewer`: clean.

## Implementation

- Added bounded, compactable neutral `assistant_reasoning` events. Compaction
  retains or removes reasoning/content/calls from a completed assistant turn
  atomically, preventing cross-turn reasoning grafts.
- OpenAI replay retains `reasoning_content`, ordinary `content`, and native
  `tool_calls` in the same assistant message; Anthropic replay uses supported
  text blocks.
- Gateway results retain ordinary content and private reasoning separately.
  Structured-role JSON parsing reads ordinary content only.
- Planner, negotiator, critic, finalizer, change summarizer, and any later
  bounded role such as handoff summarizer use the same continuation/forced-JSON
  implementation. Planner retains its three-request budget; other roles use
  one bounded repair request.
- The forced request carries retained history, disables reasoning with
  `reasoning_effort=none`, and requests only the required JSON object.
