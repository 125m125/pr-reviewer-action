import json
import threading
import time
from dataclasses import dataclass

import pytest

from pr_reviewer.conversation import Conversation
from pr_reviewer.specialist_runtime.budget import (
    BudgetExhausted,
    BudgetLedger,
    SessionLease,
)
from pr_reviewer.specialist_runtime.assignments import Assignment
from pr_reviewer.specialist_runtime.callbacks import CALLBACK_POOL
from pr_reviewer.specialist_runtime.coverage import CoverageLedger
from pr_reviewer.specialist_runtime.evidence import (
    EvidenceRecord,
    EvidenceStore,
    canonical_evidence_key,
)
from pr_reviewer.specialist_runtime.model_gateway import ModelTurnResult
from pr_reviewer.specialist_runtime.request_attempts import RequestAttemptJournal
from pr_reviewer.specialist_runtime.session import SpecialistSession
from pr_reviewer.specialist_runtime.types import (
    BudgetLimits,
    CoverageObligation,
    RunPhase,
    SpecialistAssignment,
)


def tool_call_response(name, arguments, call_id=None):
    call_id = call_id or f"call-{name}-{arguments.get('path', 'value')}"
    call = {"id": call_id, "name": name, "arguments": json.dumps(arguments)}
    return ModelTurnResult(
        response={}, tool_calls=(call,), text="", text_source="none",
        finish_reason="tool_calls", usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def checkpoint_response(*, inspected, unresolved):
    text = json.dumps({
        "inspected": inspected,
        "unresolved": unresolved,
        "hypotheses": [],
        "candidate_finding_ids": [],
        "invariants_evaluated": [],
        "unknowns": unresolved,
        "proposed_next_actions": [],
    })
    return ModelTurnResult(
        response={}, tool_calls=(), text=text, text_source="content",
        finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def candidate_checkpoint_response(candidate_ids):
    executor_result = {
        "tool": "read_file",
        "status": "ok",
        "result": {"content": "contents:a.py"},
    }
    evidence_id = canonical_evidence_key(
        "read_file", {"path": "a.py"}, executor_result,
    )
    text = json.dumps({
        "inspected": ["a.py"],
        "unresolved": ["OB-tests"],
        "candidate_finding_ids": list(candidate_ids),
        "candidate_findings": [{
            "candidate_id": candidate_id,
            "root_cause_fingerprint": f"root:{candidate_id}",
            "claim": f"The changed branch exposes issue {candidate_id}.",
            "affected_location": "a.py:4",
            "causal_chain": "The new state reaches an invalid branch.",
            "severity": "major",
            "category": "correctness",
            "supporting_evidence_ids": [evidence_id],
            "related_obligation_ids": ["OB-code"],
            "confidence_rationale": "Direct retained file evidence.",
            "user_visible_consequence": "The operation returns the wrong state.",
            "manual_validation": "Run the state transition test.",
        } for candidate_id in candidate_ids],
        "unknowns": ["OB-tests"],
    })
    return ModelTurnResult(
        response={}, tool_calls=(), text=text, text_source="content",
        finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


@dataclass(frozen=True)
class RecordedRequest:
    messages: str
    tools_enabled: bool
    deadline_at: float | None
    max_tokens: int
    ephemeral_user_note: str | None

    def messages_contain(self, value):
        return value in self.messages


class ScriptedGateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(RecordedRequest(
            messages=json.dumps(request.conversation._render_openai_messages()),
            tools_enabled=request.tools_enabled,
            deadline_at=request.deadline_at,
            max_tokens=request.max_tokens,
            ephemeral_user_note=request.ephemeral_user_note,
        ))
        assert self.responses, "model called more times than scripted"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def final_response(
    summary="reviewed", recommendation="approve", candidate_finding_ids=None,
):
    text = json.dumps({
        "summary": summary,
        "recommendation": recommendation,
        "candidate_finding_ids": candidate_finding_ids or [],
        "evidence_ids": [],
        "unknowns": [],
    })
    return ModelTurnResult(
        response={}, tool_calls=(), text=text, text_source="content",
        finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def invalid_response(text="not-json"):
    return ModelTurnResult(
        response={}, tool_calls=(), text=text, text_source="content",
        finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def reasoning_only_response(value):
    reasoning = json.dumps(value) if not isinstance(value, str) else value
    return ModelTurnResult(
        response={},
        tool_calls=(),
        text=reasoning,
        text_source="reasoning_fallback",
        finish_reason="stop",
        usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
        content="",
        reasoning=reasoning,
    )


def make_session(
    gateway, *, tool_calls=4, model_turns=8, recoveries=1,
    execute_tool=None, lease=None, assignment=None, obligations=None,
    budget_limits=None, max_tokens=1024,
    request_timeout_sec=30.0, max_context_tokens=24_000,
    recovery_max_tokens=None, clock=time.monotonic,
):
    obligations = obligations or (
        CoverageObligation(
            obligation_id="OB-code", origin="test", subject="a.py",
            required_evidence_categories=("implementation",), scope=("a.py",),
        ),
        CoverageObligation(
            obligation_id="OB-tests", origin="test", subject="tests/test_a.py",
            required_evidence_categories=("tests",), scope=("tests/test_a.py",),
        ),
    )
    assignment = assignment or SpecialistAssignment(
        assignment_id="assignment-1", objective="Review the assigned behavior",
        primary_obligation_ids=("OB-code", "OB-tests"), seed_paths=("a.py",),
    )
    conversation = Conversation(system="Gather evidence and checkpoint progress.")
    lease = lease or SessionLease(RunPhase.FOLLOWUP, time.monotonic() + 60.0)

    if execute_tool is None:
        def execute_tool(name, arguments):
            path = arguments.get("path", "")
            return {"tool": name, "status": "ok", "result": {"content": f"contents:{path}"}}

    return SpecialistSession(
        session_id="S1", assignment=assignment, conversation=conversation,
        gateway=gateway, execute_tool=execute_tool, evidence_store=EvidenceStore(),
        coverage=CoverageLedger(obligations),
        budget=BudgetLedger(budget_limits or BudgetLimits(
            model_turns=model_turns, tool_calls=tool_calls, recoveries=recoveries,
        )),
        lease=lease, request_timeout_sec=request_timeout_sec, max_tokens=max_tokens,
        max_context_tokens=max_context_tokens,
        recovery_max_tokens=recovery_max_tokens or max_tokens,
        clock=clock,
    )


def test_model_gateway_exception_still_charges_reserved_turn():
    gateway = ScriptedGateway([RuntimeError("provider unavailable")])
    session = make_session(gateway)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        session.explore()

    assert session.budget.snapshot().model_turns == 1
    assert len(gateway.requests) == 1
    assert tuple(item.status for item in session._request_events) == (
        "started", "failed",
    )
    assert session._request_events[0].request_id == session._request_events[1].request_id


def test_session_result_reports_each_actual_request_transition_once():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
        final_response(),
    ])
    session = make_session(gateway)

    checkpoint = session.explore()
    final = session.finalize()
    repeated = session.finalize()

    assert tuple(item.status for item in checkpoint.request_events) == (
        "started", "completed",
    )
    assert tuple(item.status for item in final.request_events) == (
        "started", "completed", "started", "completed",
    )
    assert repeated.request_events == final.request_events
    request_pairs = {}
    for event in final.request_events:
        request_pairs.setdefault(event.request_id, []).append(event.status)
    assert tuple(request_pairs.values()) == (
        ["started", "completed"], ["started", "completed"],
    )


def test_exploration_budget_note_is_ephemeral_and_updates_per_turn():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=[]),
    ])
    session = make_session(gateway, model_turns=4, tool_calls=4)

    session.explore()

    first, second = gateway.requests
    assert first.ephemeral_user_note == (
        "Exploration budget before this turn: 4 model turns and 4 tool calls remain. "
        "Prioritize unresolved correctness risks. Do not repeat completed checks."
    )
    assert second.ephemeral_user_note == (
        "Exploration budget before this turn: 3 model turns and 3 tool calls remain. "
        "Prioritize unresolved correctness risks. Do not repeat completed checks."
    )
    assert "Exploration budget before this turn" not in json.dumps(session.conversation.events)


def test_invalid_exploration_stop_forces_checkpoint_before_later_finalization():
    gateway = ScriptedGateway([
        invalid_response("plain-text conclusion"),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        final_response(),
    ])
    session = make_session(gateway)

    checkpoint = session.explore()
    final = session.finalize()

    assert checkpoint.state.value == "checkpoint"
    assert final.state.value == "complete"
    assert [request.tools_enabled for request in gateway.requests] == [True, False, False]
    assert gateway.requests[1].messages_contain("Checkpoint requested (not a final report)")
    assert gateway.requests[2].messages_contain("Finalize this specialist assessment once")


def test_checkpoint_requests_explain_candidate_and_evidence_retention_contract():
    gateway = ScriptedGateway([
        invalid_response("plain-text material issue"),
        invalid_response('{"evidence_ids":["a.py"]}'),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    session = make_session(gateway)

    session.explore()

    checkpoint_request = gateway.requests[1].messages
    repair_request = gateway.requests[2].messages
    for prompt in (checkpoint_request, repair_request):
        assert "candidate_findings" in prompt
        assert "candidate_finding_ids" in prompt
        assert "exact retained evidence IDs" in prompt
        assert "repository paths are not evidence IDs" in prompt


def test_exploration_reserves_checkpoint_and_repair_turns():
    """Exploration cannot consume the turns reserved for structured retention."""
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}, call_id="first"),
        tool_call_response("read_file", {"path": "tests/test_a.py"}, call_id="second"),
        invalid_response("malformed-checkpoint"),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, model_turns=4)

    result = session.explore()

    assert result.state.value == "checkpoint"
    assert result.degraded is False
    assert [request.tools_enabled for request in gateway.requests] == [
        True, True, False, False,
    ]
    assert result.budget.model_turns == 4


def test_checkpoint_request_includes_compact_schema_contract():
    """The checkpoint user message describes required keys and candidate retention."""
    gateway = ScriptedGateway([
        invalid_response("plain-text conclusion"),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    session = make_session(gateway)

    session.explore()

    checkpoint_request = json.loads(gateway.requests[1].messages)[-1]["content"]
    assert "Required keys:" in checkpoint_request
    assert '"unresolved"' in checkpoint_request
    assert '"candidate_finding_ids"' in checkpoint_request
    assert '"candidate_findings"' in checkpoint_request
    assert "include both its object and ID" in checkpoint_request


def test_malformed_checkpoint_is_repaired_before_projection():
    """One malformed structured checkpoint receives one bounded repair request."""
    gateway = ScriptedGateway([
        invalid_response("plain-text conclusion"),
        invalid_response("still-malformed"),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    session = make_session(gateway)

    result = session.explore()

    assert result.degraded is False
    assert len(gateway.requests) == 3
    assert gateway.requests[1].tools_enabled is False
    assert gateway.requests[2].tools_enabled is False
    assert gateway.requests[2].messages_contain("Repair the previous checkpoint")


def test_exploration_reasoning_json_is_not_admitted_or_candidate_signaled():
    private_checkpoint = {
        "inspected": ["a.py"],
        "unresolved": [],
        "candidate_finding_ids": ["private-candidate"],
        "candidate_findings": [{
            "candidate_id": "private-candidate",
            "claim": "private draft only",
        }],
        "unknowns": [],
    }
    gateway = ScriptedGateway([
        reasoning_only_response(private_checkpoint),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])

    result = make_session(gateway).explore()

    assert len(gateway.requests) == 2
    assert result.degraded is False
    assert result.checkpoint.candidate_finding_ids == ()
    assert "candidate-retention-unknown" not in result.checkpoint.unknowns
    assert gateway.requests[1].messages_contain("reasoning_content")
    assert gateway.requests[1].messages_contain("private-candidate")


def test_checkpoint_request_repairs_reasoning_only_json_from_retained_history():
    gateway = ScriptedGateway([
        reasoning_only_response({
            "inspected": ["a.py"],
            "unresolved": [],
            "candidate_finding_ids": [],
            "unknowns": [],
        }),
        checkpoint_response(inspected=[], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway)

    result = session.request_checkpoint("controller-request")

    assert len(gateway.requests) == 2
    assert result.degraded is False
    assert "OB-tests" in result.checkpoint.unknowns
    assert gateway.requests[1].messages_contain("reasoning_content")
    assert gateway.requests[1].messages_contain("Repair the previous checkpoint")


def test_checkpoint_reasoning_only_repair_degrades_to_projection():
    gateway = ScriptedGateway([
        invalid_response("not a checkpoint"),
        reasoning_only_response({
            "inspected": ["a.py"],
            "unresolved": [],
            "candidate_finding_ids": [],
            "unknowns": [],
        }),
    ])
    session = make_session(gateway)

    result = session.request_checkpoint("controller-request")

    assert result.degraded is True
    assert set(result.checkpoint.unknowns) == {"OB-code", "OB-tests"}


def test_unrecoverable_candidate_text_is_reported_as_retention_unknown():
    """Fallback state cannot look like a trustworthy zero-findings checkpoint."""
    gateway = ScriptedGateway([
        invalid_response(
            '{"unresolved": [], "candidate_findings": '
            '[{"candidate_id": "candidate-lost", "claim": "material issue"}]'
        ),
        invalid_response("still-not-json"),
    ])
    session = make_session(gateway, model_turns=2)

    result = session.explore()

    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns
    assert result.checkpoint.candidate_finding_ids == ()


def test_exploration_candidate_text_survives_checkpoint_handoff_as_unknown():
    """A later clean checkpoint cannot erase an earlier malformed candidate."""
    gateway = ScriptedGateway([
        invalid_response(
            '{"unresolved": [], "candidate_findings": '
            '[{"candidate_id": "candidate-lost", "claim": "material issue"}]'
        ),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    session = make_session(gateway, model_turns=4)

    result = session.explore()

    assert [request.tools_enabled for request in gateway.requests] == [
        True, False, False,
    ]
    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns


def test_tool_turn_candidate_signal_survives_resume_and_clean_checkpoint():
    """Text beside a tool call remains a lifetime retention obligation."""
    candidate_turn = tool_call_response(
        "read_file", {"path": "a.py"}, call_id="candidate-tool",
    )
    candidate_turn = ModelTurnResult(**{
        **candidate_turn.__dict__,
        "text": (
            '{"candidate_finding_ids":["candidate-tool-loss"],'
            '"candidate_findings":[{"candidate_id":"candidate-tool-loss",'
            '"claim":"material issue"}]}'
        ),
        "text_source": "content",
    })
    clean = checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"])
    gateway = ScriptedGateway([
        candidate_turn,
        clean,
        clean,
        clean,
        clean,
        clean,
        clean,
    ])
    session = make_session(gateway, model_turns=7)

    first = session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    resumed = session.explore()

    assert first.degraded is True
    assert resumed.degraded is True
    assert "candidate-retention-unknown" in first.checkpoint.unknowns
    assert "candidate-retention-unknown" in resumed.checkpoint.unknowns
    assert resumed.checkpoint.candidate_finding_ids == ()
    assert len(gateway.requests) == 7


def test_anonymous_candidate_signal_cannot_be_cleared_by_unrelated_admission():
    """An unidentified material shape remains unknown after a named admission."""
    anonymous_turn = tool_call_response(
        "read_file", {"path": "a.py"}, call_id="anonymous-tool",
    )
    anonymous_turn = ModelTurnResult(**{
        **anonymous_turn.__dict__,
        "text": '{"candidate_findings":[{"claim":"unidentified issue"}]}',
        "text_source": "content",
    })
    named = candidate_checkpoint_response(("candidate-named",))
    gateway = ScriptedGateway([anonymous_turn, named, named, named])
    session = make_session(gateway, model_turns=4)

    result = session.explore()

    assert result.checkpoint.candidate_finding_ids == ("candidate-named",)
    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns


def test_candidate_id_overflow_cannot_be_cleared_by_bounded_admissions():
    """IDs beyond the retention bound keep the conservative loss warning."""
    declared_ids = tuple(f"candidate-{index:02d}" for index in range(21))
    overflow_turn = tool_call_response(
        "read_file", {"path": "a.py"}, call_id="overflow-tool",
    )
    overflow_turn = ModelTurnResult(**{
        **overflow_turn.__dict__,
        "text": json.dumps({
            "candidate_finding_ids": list(declared_ids),
        }),
        "text_source": "content",
    })
    bounded = candidate_checkpoint_response(declared_ids[:20])
    clean = checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"])
    gateway = ScriptedGateway([
        overflow_turn,
        bounded,
        bounded,
        bounded,
        clean,
        clean,
        clean,
    ])
    session = make_session(gateway, model_turns=7)

    first = session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    resumed = session.explore()

    assert len(first.checkpoint.candidate_finding_ids) == 20
    assert first.degraded is True
    assert resumed.degraded is True
    assert "candidate-retention-unknown" in resumed.checkpoint.unknowns


def test_checkpoint_admission_failure_preserves_exploration_candidate_unknown():
    """A failed checkpoint request cannot erase prior malformed candidate material."""
    gateway = ScriptedGateway([
        invalid_response(
            '{"unresolved": [], "candidate_findings": '
            '[{"candidate_id": "candidate-lost", "claim": "material issue"}]'
        ),
        TimeoutError("checkpoint provider timed out"),
    ])
    session = make_session(gateway, model_turns=4)

    result = session.explore()

    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns
    assert len(gateway.requests) == 2


def test_partial_candidate_retention_is_reported_when_one_candidate_survives():
    """One admitted candidate cannot mask a separately dropped declaration."""
    executor_result = {
        "tool": "read_file", "status": "ok",
        "result": {"content": "contents:a.py"},
    }
    evidence_id = canonical_evidence_key(
        "read_file", {"path": "a.py"}, executor_result,
    )
    checkpoint = checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"])
    raw = json.loads(checkpoint.text)
    raw["candidate_finding_ids"] = ["candidate-code", "candidate-lost"]
    raw["candidate_findings"] = [
        {
            "candidate_id": "candidate-code",
            "root_cause_fingerprint": "root:candidate-code",
            "claim": "The changed branch skips the cancellation state.",
            "affected_location": "a.py:4",
            "causal_chain": "The new state reaches a switch without a matching arm.",
            "severity": "major",
            "category": "correctness",
            "supporting_evidence_ids": [evidence_id],
            "related_obligation_ids": ["OB-code"],
            "confidence_rationale": "Direct retained file evidence.",
            "user_visible_consequence": "Cancelled work is shown as active.",
            "manual_validation": "Run the cancellation-state test.",
        },
        {"candidate_id": "candidate-lost", "claim": "incomplete candidate"},
    ]
    mixed_checkpoint = ModelTurnResult(**{
        **checkpoint.__dict__, "text": json.dumps(raw),
    })
    repaired = ModelTurnResult(**{
        **checkpoint.__dict__,
        "text": json.dumps({
            **raw,
            "candidate_finding_ids": ["candidate-code"],
            "candidate_findings": [raw["candidate_findings"][0]],
        }),
    })
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        invalid_response("plain-text conclusion"),
        mixed_checkpoint,
        repaired,
    ])
    session = make_session(gateway, model_turns=5)

    result = session.explore()

    assert result.checkpoint.candidate_finding_ids == ("candidate-code",)
    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns
    assert len(gateway.requests) == 4


def test_hanging_specialist_gateways_share_global_orphan_cap():
    release = threading.Event()
    entered = []

    class HangingGateway:
        def complete(self, request):
            del request
            entered.append(len(entered))
            release.wait(2)
            return checkpoint_response(inspected=[], unresolved=[])

    sessions = [
        make_session(HangingGateway(), request_timeout_sec=0.005)
        for _ in range(CALLBACK_POOL.capacity + 1)
    ]
    try:
        for session in sessions:
            with pytest.raises(Exception):
                session.explore()
        assert len(entered) == CALLBACK_POOL.capacity
        assert all(session.budget.snapshot().model_turns == 1 for session in sessions)
        assert tuple(item.status for item in sessions[0].request_events) == (
            "started", "timed_out",
        )
        assert tuple(item.status for item in sessions[-1].request_events) == (
            "started", "failed",
        )
    finally:
        release.set()
        deadline = time.monotonic() + 1
        while CALLBACK_POOL.in_flight and time.monotonic() < deadline:
            time.sleep(0.005)


def test_hostile_gateway_baseexception_still_gets_one_terminal_event():
    class HostileError(BaseException):
        def __str__(self):
            raise KeyboardInterrupt("hostile error formatter")

    class HostileGateway:
        def complete(self, request):
            del request
            raise HostileError()

    session = make_session(HostileGateway())

    with pytest.raises(HostileError):
        session.explore()

    assert tuple(item.status for item in session.request_events) == (
        "started", "failed",
    )
    assert session.request_events[-1].error.endswith("[unserializable]")


def test_session_bounds_output_and_rejects_input_before_transport():
    bounded_gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
    ])
    bounded = make_session(
        bounded_gateway,
        budget_limits=BudgetLimits(
            model_turns=2, tool_calls=1, recoveries=1,
            output_tokens=4,
        ),
    )

    bounded.explore()

    assert bounded_gateway.requests[0].max_tokens == 4

    refused_gateway = ScriptedGateway([])
    refused = make_session(
        refused_gateway,
        budget_limits=BudgetLimits(
            model_turns=2, tool_calls=1, recoveries=1,
            input_tokens=1,
        ),
    )
    with pytest.raises(BudgetExhausted, match="input token limit exhausted"):
        refused.explore()
    assert refused_gateway.requests == []
    assert refused.budget.snapshot().model_turns == 0


def test_actual_token_overflow_remains_charged_without_retry():
    response = checkpoint_response(inspected=[], unresolved=[])
    response = ModelTurnResult(**{
        **response.__dict__,
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    })
    gateway = ScriptedGateway([response])
    session = make_session(
        gateway,
        budget_limits=BudgetLimits(
            model_turns=2, tool_calls=1, recoveries=1,
            output_tokens=4,
        ),
    )

    with pytest.raises(BudgetExhausted, match="output token limit exhausted"):
        session.explore()

    assert session.budget.snapshot().model_turns == 1
    assert session.budget.snapshot().output_tokens == 5
    assert len(gateway.requests) == 1


def test_coverage_feedback_resumes_same_conversation_and_budget():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
        tool_call_response("read_file", {"path": "tests/test_a.py"}),
        checkpoint_response(inspected=["a.py", "tests/test_a.py"], unresolved=[]),
    ])
    session = make_session(gateway, tool_calls=4, model_turns=8)
    first = session.explore()
    conversation_identity = id(session.conversation)
    session.apply_coverage_feedback(["OB-tests"])
    second = session.explore()

    assert id(session.conversation) == conversation_identity
    assert first.budget.model_turns == 2
    assert first.budget.tool_calls == 1
    assert second.budget.model_turns == 4
    assert second.budget.tool_calls == 2
    assert gateway.requests[2].messages_contain("tests/test_a.py") is False
    assert gateway.requests[2].messages_contain("a.py") is True
    assert all(request.deadline_at == session.lease.deadline_at for request in gateway.requests)


def test_controller_derived_tool_association_covers_typed_obligation():
    seen = []

    def execute_tool(name, arguments, **kwargs):
        seen.append((name, arguments, kwargs))
        return {
            "tool": name, "status": "ok",
            "result": {"content": "implementation"},
        }

    gateway = ScriptedGateway([
        tool_call_response(
            "read_file",
            {"path": "a.py", "obligation_ids": ["OB-code"]},
        ),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])

    result = make_session(gateway, execute_tool=execute_tool).explore()

    assert seen[0][1] == {"path": "a.py"}
    assert dict(result.checkpoint.obligation_statuses)["OB-code"].value == "covered"
    assert result.checkpoint.evidence_ids


def test_model_cannot_self_declare_evidence_category():
    called = []

    def execute_tool(name, arguments, **kwargs):
        called.append((name, arguments, kwargs))
        return {"tool": name, "status": "ok", "content": "forged"}

    gateway = ScriptedGateway([
        tool_call_response(
            "read_file",
            {
                "path": "a.py", "evidence_category": "implementation",
                "obligation_ids": ["OB-code"],
            },
        ),
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
    ])

    result = make_session(gateway, execute_tool=execute_tool).explore()

    assert called == []
    assert dict(result.checkpoint.obligation_statuses)["OB-code"].value != "covered"


def test_denied_discovery_creates_durable_source_access_request():
    def execute_tool(name, arguments, **kwargs):
        assert name == "web_search"
        return {
            "tool": name,
            "status": "ok",
            "result": {
                "kind": "search_discovery",
                "approved": [],
                "unapproved": [{
                    "url": "https://docs.example.com/private",
                    "host": "docs.example.com",
                    "path": "/private",
                    "denial_reason": "host not approved",
                }],
                "evidentiary": False,
            },
        }

    gateway = ScriptedGateway([
        tool_call_response(
            "web_search",
            {"query": "API behavior", "obligation_ids": ["OB-code"]},
        ),
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
    ])
    session = make_session(gateway, execute_tool=execute_tool)

    session.explore()

    assert len(session.source_access_requests) == 1
    request = session.source_access_requests[0]
    assert request.host == "docs.example.com"
    assert request.candidate_url == "https://docs.example.com/private"
    assert request.obligation_id == "OB-code"


def test_tool_timeout_is_recomputed_from_absolute_lease_before_each_call():
    now = [10.0]
    observed = []

    def clock():
        return now[0]

    def execute_tool(name, arguments, *, timeout_sec, deadline_at):
        observed.append((timeout_sec, deadline_at))
        now[0] += 2.0
        return {"tool": name, "status": "ok", "content": arguments["path"]}

    gateway = ScriptedGateway([
        ModelTurnResult(
            response={},
            tool_calls=(
                {
                    "id": "first", "name": "read_file",
                    "arguments": json.dumps({"path": "a.py"}),
                },
                {
                    "id": "second", "name": "read_file",
                    "arguments": json.dumps({"path": "tests/test_a.py"}),
                },
            ),
            text="", text_source="none", finish_reason="tool_calls",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            request_diagnostics={},
        ),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    lease = SessionLease(RunPhase.FOLLOWUP, deadline_at=13.0)
    session = make_session(
        gateway, execute_tool=execute_tool, lease=lease, clock=clock,
        request_timeout_sec=30.0,
    )

    session.explore()

    assert observed == [(3.0, 13.0), (1.0, 13.0)]


def test_context_admission_reserves_requested_output_before_transport():
    gateway = ScriptedGateway([])
    session = make_session(
        gateway, max_tokens=512, max_context_tokens=520,
    )

    result = session.explore()

    assert gateway.requests == []
    assert result.degraded is True
    assert result.state.value == "checkpoint"


def test_recovery_reconstructs_context_without_resetting_lifetime_state():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, tool_calls=5, model_turns=10, recoveries=1)
    session.explore()
    old_conversation = session.conversation
    checkpoint = session.latest_checkpoint

    recovered = session.recover("repetitive-transcript")

    assert session.conversation is not old_conversation
    assert session.latest_checkpoint == checkpoint
    assert recovered.state.value == "exploring"
    assert session.budget.snapshot().recoveries == 1
    assert session.budget.snapshot().tool_calls > 0
    assert session.conversation_contains_evidence_ids(checkpoint.evidence_ids)
    assert "contents:a.py" in json.dumps(session.conversation.events)
    assert "OB-tests" in json.dumps(session.conversation.events)


def test_recovery_next_turn_uses_recovery_output_ceiling():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
    ])
    session = make_session(
        gateway, max_tokens=1024, recovery_max_tokens=123,
    )

    session.explore()
    session.recover("repetitive-transcript")
    session.explore()

    assert [request.max_tokens for request in gateway.requests] == [1024, 123]


def test_recovery_rejects_unrecorded_reason_without_mutating_state():
    gateway = ScriptedGateway([])
    session = make_session(gateway)
    conversation = session.conversation

    try:
        session.recover("retry-for-more-budget")
    except ValueError as exc:
        assert "recorded recovery reason" in str(exc)
    else:
        raise AssertionError("unrecorded recovery reason was accepted")

    assert session.conversation is conversation
    assert session.budget.snapshot().recoveries == 0


def test_recovery_preserves_tool_deduplication_identity():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}, call_id="original"),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
        tool_call_response("read_file", {"path": "a.py"}, call_id="after-recovery"),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, model_turns=8)
    session.explore()
    session.recover("polluted-transcript")

    result = session.explore()

    assert result.budget.tool_calls == 1
    assert result.budget.tool_rejections == 1
    assert "replayed_duplicate" in json.dumps(session.conversation.events)


def test_failed_recovery_admission_preserves_prior_observable_state():
    gateway = ScriptedGateway([])
    session = make_session(gateway, recoveries=1)
    session.recover("context-pressure")
    prior_state = session.state
    prior_conversation = session.conversation
    prior_checkpoint = session.latest_checkpoint

    with pytest.raises(BudgetExhausted, match="recovery limit exhausted"):
        session.recover("context-pressure")

    assert session.state is prior_state
    assert session.conversation is prior_conversation
    assert session.latest_checkpoint is prior_checkpoint
    assert session.budget.snapshot().recoveries == 1


def test_no_progress_guard_requests_checkpoint_instead_of_final_report():
    repeated = tool_call_response("read_file", {"path": "a.py"}, call_id="repeat")
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}, call_id="first"),
        repeated,
        repeated,
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, tool_calls=4, model_turns=8)

    result = session.explore()

    assert result.state.value == "checkpoint"
    assert result.budget.tool_calls == 1
    assert result.budget.model_turns == 4
    assert gateway.requests[-1].tools_enabled is False
    assert "not a final report" in gateway.requests[-1].messages.lower()


def test_no_progress_guard_projects_checkpoint_when_no_model_turn_remains():
    repeated = tool_call_response("read_file", {"path": "a.py"}, call_id="repeat")
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}, call_id="first"),
        repeated,
        repeated,
    ])
    session = make_session(gateway, tool_calls=4, model_turns=3)

    result = session.explore()

    assert result.state.value == "checkpoint"
    assert result.degraded is True
    assert result.budget.model_turns == 3
    assert result.budget.tool_calls == 1
    assert len(gateway.requests) == 3


def test_checkpoint_attaches_only_successful_retained_evidence_ids():
    claimed = "evidence:not-retained"
    checkpoint = checkpoint_response(inspected=[], unresolved=["OB-tests"])
    raw = json.loads(checkpoint.text)
    raw["evidence_ids"] = [claimed]
    raw["evidence_by_obligation"] = {"OB-code": [claimed]}
    checkpoint = ModelTurnResult(**{**checkpoint.__dict__, "text": json.dumps(raw)})
    gateway = ScriptedGateway([checkpoint])
    session = make_session(gateway)

    result = session.explore()

    assert claimed not in result.checkpoint.evidence_ids
    assert dict(result.checkpoint.obligation_statuses)["OB-code"].value == "pending"


def test_checkpoint_collects_only_evidence_backed_candidate_objects():
    executor_result = {
        "tool": "read_file",
        "status": "ok",
        "result": {"content": "contents:a.py"},
    }
    evidence_id = canonical_evidence_key(
        "read_file", {"path": "a.py"}, executor_result,
    )
    checkpoint = checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"])
    raw = json.loads(checkpoint.text)
    raw["candidate_finding_ids"] = ["candidate-code", "candidate-forged"]
    raw["candidate_findings"] = [
        {
            "candidate_id": "candidate-code",
            "root_cause_fingerprint": "root:candidate-code",
            "claim": "The changed branch skips the cancellation state.",
            "affected_location": "a.py:4",
            "causal_chain": "The new state reaches a switch without a matching arm.",
            "severity": "major",
            "category": "correctness",
            "supporting_evidence_ids": [evidence_id],
            "related_obligation_ids": ["OB-code"],
            "confidence_rationale": "Direct retained file evidence.",
            "user_visible_consequence": "Cancelled work is shown as active.",
            "manual_validation": "Run the cancellation-state test.",
        },
        {
            "candidate_id": "candidate-forged",
            "root_cause_fingerprint": "root:candidate-forged",
            "claim": "A claim without retained evidence.",
            "affected_location": "a.py:5",
            "causal_chain": "Unsupported.",
            "supporting_evidence_ids": ["evidence:not-retained"],
            "related_obligation_ids": ["OB-code"],
        },
    ]
    checkpoint = ModelTurnResult(**{
        **checkpoint.__dict__,
        "text": json.dumps(raw),
    })
    repaired = ModelTurnResult(**{
        **checkpoint.__dict__,
        "text": json.dumps({
            **raw,
            "candidate_finding_ids": ["candidate-code"],
            "candidate_findings": [raw["candidate_findings"][0]],
        }),
    })
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint,
        repaired,
        repaired,
    ])
    session = make_session(gateway)

    result = session.explore()

    assert result.checkpoint.candidate_finding_ids == ("candidate-code",)
    assert tuple(item.candidate_id for item in session.candidate_findings) == (
        "candidate-code",
    )
    candidate = session.candidate_findings[0]
    assert candidate.supporting_evidence_ids == (evidence_id,)
    assert candidate.related_obligation_ids == ("OB-code",)
    assert candidate.collector_session_id == "S1"
    assert "candidate-retention-unknown" in result.checkpoint.unknowns


def test_tool_result_enters_conversation_only_after_evidence_redaction():
    secret = "supersecretvalue"

    def execute_tool(name, arguments):
        return {
            "tool": name,
            "status": "ok",
            "result": {"content": f"token={secret}\npublic"},
        }

    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, execute_tool=execute_tool)

    session.explore()

    retained = json.dumps(session.conversation.events) + repr(session.evidence_store.snapshot())
    assert secret not in retained
    assert "[REDACTED]" in retained


def test_tool_deduplication_retains_only_sanitized_evidence_records():
    secret = "raw-dedup-secret"

    def execute_tool(name, arguments):
        return {
            "tool": name,
            "status": "ok",
            "result": {"content": f"token={secret}"},
        }

    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, execute_tool=execute_tool)

    session.explore()

    retained = tuple(session._successful_requests.values())
    assert retained
    assert all(isinstance(record, EvidenceRecord) for record in retained)
    assert secret not in repr(retained)


def test_inspected_path_cannot_cover_an_unrelated_obligation_scope():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=[]),
    ])
    session = make_session(gateway)

    result = session.explore()
    statuses = dict(result.checkpoint.obligation_statuses)

    assert statuses["OB-code"].value == "covered"
    assert statuses["OB-tests"].value == "pending"


def test_declared_evidence_cannot_cover_an_unrelated_obligation_scope():
    executor_result = {
        "tool": "read_file", "status": "ok",
        "result": {"content": "contents:a.py"},
    }
    evidence_id = canonical_evidence_key(
        "read_file", {"path": "a.py"}, executor_result,
    )
    checkpoint = checkpoint_response(inspected=[], unresolved=[])
    raw = json.loads(checkpoint.text)
    raw["evidence_by_obligation"] = {"OB-tests": [evidence_id]}
    checkpoint = ModelTurnResult(**{**checkpoint.__dict__, "text": json.dumps(raw)})
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint,
    ])

    result = make_session(gateway).explore()

    assert dict(result.checkpoint.obligation_statuses)["OB-tests"].value == "pending"
    assert "OB-tests" in result.checkpoint.unknowns


def test_model_cannot_remove_a_deterministic_mandatory_gap():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=[]),
    ])

    result = make_session(gateway).explore()

    assert set(result.checkpoint.unknowns) == {"OB-code", "OB-tests"}
    assert result.checkpoint.proposed_next_actions == result.checkpoint.unknowns


def test_unscoped_obligation_requires_matching_evidence_category():
    executor_result = {
        "tool": "read_file", "status": "ok",
        "result": {"content": "contents:a.py"},
    }
    evidence_id = canonical_evidence_key(
        "read_file", {"path": "a.py"}, executor_result,
    )
    obligation = CoverageObligation(
        obligation_id="OB-unscoped", origin="test", subject="repository",
        required_evidence_categories=("tool-result",),
    )
    assignment = SpecialistAssignment(
        assignment_id="assignment-unscoped", objective="Review repository evidence",
        primary_obligation_ids=("OB-unscoped",),
    )
    checkpoint = checkpoint_response(inspected=[], unresolved=[])
    raw = json.loads(checkpoint.text)
    raw["evidence_by_obligation"] = {"OB-unscoped": [evidence_id]}
    checkpoint = ModelTurnResult(**{**checkpoint.__dict__, "text": json.dumps(raw)})
    gateway = ScriptedGateway([
        tool_call_response(
            "read_file", {"path": "a.py", "obligation_ids": ["OB-unscoped"]},
        ),
        checkpoint,
    ])

    result = make_session(
        gateway, obligations=(obligation,), assignment=assignment,
    ).explore()

    assert dict(result.checkpoint.obligation_statuses)["OB-unscoped"].value == "covered"
    assert result.checkpoint.unknowns == ()


def test_session_consumes_task_three_assignment_contract():
    assignment = Assignment(
        id="task-three", title="Implementation review", objective="Review behavior",
        obligation_ids=("OB-code", "OB-tests"), recipe_ids=(), lenses=("correctness",),
        seed_paths=("a.py",), boundary_paths=("a.py",),
        expected_evidence=("implementation", "tests"), estimated_turns=2,
        priority="normal", primary_obligation_ids=("OB-code", "OB-tests"),
    )
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
    ])

    result = make_session(gateway, assignment=assignment).explore()

    assert result.state.value == "checkpoint"
    assert "task-three" in gateway.requests[0].messages
    assert "correctness" in gateway.requests[0].messages


def test_finalize_disables_tools_repairs_schema_once_and_is_once_only():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=[]),
        invalid_response(),
        final_response(summary="repaired"),
    ])
    session = make_session(gateway, model_turns=8)
    session.explore()

    first = session.finalize()
    second = session.finalize()

    assert first is second
    assert first.state.value == "complete"
    assert first.report["summary"] == "repaired"
    assert first.budget.model_turns == 4
    assert len(gateway.requests) == 4
    assert gateway.requests[2].tools_enabled is False
    assert gateway.requests[3].tools_enabled is False


def test_finalize_repairs_reasoning_only_json_without_admitting_private_report():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        reasoning_only_response({
            "summary": "private draft",
            "recommendation": "approve",
            "candidate_finding_ids": [],
            "evidence_ids": [],
            "unknowns": [],
        }),
        final_response(summary="declared repaired report"),
    ])
    session = make_session(gateway, model_turns=6)
    session.explore()

    result = session.finalize()

    assert result.degraded is False
    assert result.report["summary"] == "declared repaired report"
    assert len(gateway.requests) == 3
    assert gateway.requests[2].messages_contain("reasoning_content")
    assert gateway.requests[2].messages_contain("private draft")


def test_finalize_reasoning_only_repair_degrades_to_checkpoint_fallback():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        invalid_response("not a report"),
        reasoning_only_response({
            "summary": "private repaired draft",
            "recommendation": "approve",
            "candidate_finding_ids": [],
            "evidence_ids": [],
            "unknowns": [],
        }),
    ])
    session = make_session(gateway, model_turns=6)
    session.explore()

    result = session.finalize()

    assert result.degraded is True
    assert result.report["source"] == "checkpoint-fallback"


def test_finalize_falls_back_to_structured_checkpoint_after_one_repair():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        invalid_response("bad-one"),
        invalid_response("bad-two"),
    ])
    session = make_session(gateway, model_turns=6)
    session.explore()

    result = session.finalize()

    assert result.state.value == "complete"
    assert result.degraded is True
    assert result.report["source"] == "checkpoint-fallback"
    assert result.report["unknowns"] == ["OB-code", "OB-tests"]


def _session_with_retained_candidate(final_responses):
    executor_result = {
        "tool": "read_file",
        "status": "ok",
        "result": {"content": "contents:a.py"},
    }
    evidence_id = canonical_evidence_key(
        "read_file", {"path": "a.py"}, executor_result,
    )
    checkpoint = checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"])
    raw = json.loads(checkpoint.text)
    raw["candidate_finding_ids"] = ["candidate-code"]
    raw["candidate_findings"] = [{
        "candidate_id": "candidate-code",
        "root_cause_fingerprint": "root:candidate-code",
        "claim": "The changed branch skips the cancellation state.",
        "affected_location": "a.py:4",
        "causal_chain": "The new state reaches a switch without a matching arm.",
        "severity": "major",
        "category": "correctness",
        "supporting_evidence_ids": [evidence_id],
        "related_obligation_ids": ["OB-code"],
        "confidence_rationale": "Direct retained file evidence.",
        "user_visible_consequence": "Cancelled work is shown as active.",
        "manual_validation": "Run the cancellation-state test.",
    }]
    checkpoint = ModelTurnResult(**{
        **checkpoint.__dict__,
        "text": json.dumps(raw),
    })
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint,
        *final_responses,
    ])
    session = make_session(gateway)
    session.explore()
    return session, gateway


def test_checkpoint_admission_failure_keeps_already_admitted_candidate():
    """A transport fallback does not turn a retained candidate into loss."""
    session, _gateway = _session_with_retained_candidate([
        TimeoutError("checkpoint provider timed out"),
    ])

    result = session.request_checkpoint("controller-request")

    assert result.checkpoint.candidate_finding_ids == ("candidate-code",)
    assert "candidate-retention-unknown" not in result.checkpoint.unknowns


def test_finalization_repairs_dangling_candidate_references_without_dropping_valid_ids():
    session, gateway = _session_with_retained_candidate([
        final_response(candidate_finding_ids=["candidate-code", "candidate-forged"]),
        final_response(summary="repaired", candidate_finding_ids=["candidate-code"]),
    ])

    result = session.finalize()

    assert result.degraded is False
    assert result.report["summary"] == "repaired"
    assert result.report["candidate_finding_ids"] == ["candidate-code"]
    assert result.finalization_diagnostics == ({
        "code": "invalid_candidate_finding_references",
        "attempt": "initial",
        "candidate_finding_ids": ("candidate-forged",),
        "omitted_count": 0,
    },)
    assert gateway.requests[3].messages_contain("candidate-forged")
    assert gateway.requests[3].messages_contain("latest checkpoint")


def test_repeated_dangling_candidate_references_degrade_with_bounded_diagnostics():
    forged_ids = [f"candidate-forged-{index}" for index in range(25)]
    session, _gateway = _session_with_retained_candidate([
        final_response(candidate_finding_ids=["candidate-code", *forged_ids]),
        final_response(candidate_finding_ids=forged_ids),
    ])

    result = session.finalize()

    assert result.degraded is True
    assert result.report["source"] == "checkpoint-fallback"
    assert result.report["candidate_finding_ids"] == ["candidate-code"]
    assert tuple(
        item["attempt"] for item in result.finalization_diagnostics
    ) == ("initial", "repair")
    assert all(
        len(item["candidate_finding_ids"]) == 20
        and item["omitted_count"] == 5
        for item in result.finalization_diagnostics
    )


def test_finalize_falls_back_when_schema_repair_has_no_lifetime_turn_left():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        invalid_response("last-turn-was-invalid"),
    ])
    session = make_session(gateway, model_turns=2)
    session.explore()

    result = session.finalize()

    assert result.state.value == "complete"
    assert result.degraded is True
    assert result.report["source"] == "checkpoint-fallback"
    assert result.budget.model_turns == 2
    assert len(gateway.requests) == 2


def test_exhausted_finalization_repair_records_no_unadmitted_request_attempt():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        invalid_response("last-admitted-turn-was-invalid"),
    ])
    session = make_session(gateway, model_turns=2)
    attempts = RequestAttemptJournal()
    session.bind_request_attempt_journal(attempts, "assignment-1")
    session.explore()

    result = session.finalize()
    recorded = attempts.close_since(0)

    assert result.report["source"] == "checkpoint-fallback"
    assert result.budget.model_turns == 2
    assert len(gateway.requests) == 2
    assert tuple(item.status for item in recorded) == ("completed", "completed")
    assert tuple(event.status for event in result.request_events) == (
        "started", "completed", "started", "completed",
    )


def test_initial_finalization_budget_exhaustion_caches_one_fallback():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
    ])
    session = make_session(gateway, model_turns=1)
    session.explore()

    first = session.finalize()
    second = session.finalize()

    assert first is second
    assert first.state.value == "complete"
    assert first.degraded is True
    assert first.report["source"] == "checkpoint-fallback"
    assert len(gateway.requests) == 1


def test_provider_exception_during_initial_finalization_caches_fallback():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
        RuntimeError("provider unavailable"),
    ])
    session = make_session(gateway)
    session.explore()

    first = session.finalize()
    second = session.finalize()

    assert first is second
    assert first.state.value == "complete"
    assert first.degraded is True
    assert first.report["source"] == "checkpoint-fallback"
    assert first.budget.model_turns == 2
    assert len(gateway.requests) == 2


def test_provider_exception_during_finalization_repair_caches_fallback():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
        invalid_response(),
        RuntimeError("repair provider unavailable"),
    ])
    session = make_session(gateway)
    session.explore()

    first = session.finalize()
    second = session.finalize()

    assert first is second
    assert first.state.value == "complete"
    assert first.degraded is True
    assert first.report["source"] == "checkpoint-fallback"
    assert first.budget.model_turns == 3
    assert len(gateway.requests) == 3


def test_expired_lease_refuses_exploration_and_finalization_requests():
    lease = SessionLease(RunPhase.FOLLOWUP, deadline_at=0.0)
    explore_gateway = ScriptedGateway([])
    explore_session = make_session(explore_gateway, lease=lease)

    with pytest.raises(TimeoutError, match="session lease expired"):
        explore_session.explore()

    finalize_gateway = ScriptedGateway([])
    finalize_session = make_session(finalize_gateway, lease=lease)
    first = finalize_session.finalize()
    second = finalize_session.finalize()

    assert explore_gateway.requests == []
    assert finalize_gateway.requests == []
    assert first is second
    assert first.state.value == "complete"
    assert first.degraded is True
    assert first.report["source"] == "checkpoint-fallback"
