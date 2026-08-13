import json
import threading
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass, replace

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
from pr_reviewer.specialist_runtime.model_gateway import (
    ModelTurnResult,
    OpenAIModelGateway,
)
from pr_reviewer.specialist_runtime.request_attempts import RequestAttemptJournal
from pr_reviewer.specialist_runtime.session import (
    COMPACTED_EVIDENCE_TOOL_NAME,
    SpecialistSession,
    _candidate_retention_signal,
    _is_context_limit_error,
    _resolve_retained_evidence_id,
    _rewrite_rationale_evidence_ids,
    specialist_assignment_prompt,
)
from pr_reviewer.specialist_runtime.types import (
    BudgetLimits,
    CoverageObligation,
    RunPhase,
    SessionCheckpoint,
    SessionState,
    SpecialistAssignment,
)
from pr_reviewer.transport import ModelRequestError


@pytest.mark.parametrize(
    "message, body",
    (
        ("provider rejected context_length_exceeded", ""),
        ("provider rejected request", '{"error":"Context Size is too large"}'),
        ("Maximum Context reached", ""),
        ("provider rejected request", '{"message":"PROMPT TOO LONG"}'),
        ("Too Many Tokens for this model", ""),
    ),
)
def test_context_limit_error_classifies_only_approved_provider_signals(
    message, body,
):
    error = ModelRequestError(message, status=400, body=body)

    assert _is_context_limit_error(error) is True


def test_context_limit_error_rejects_unrelated_http_500():
    error = ModelRequestError(
        "model provider rejected request with HTTP 500",
        status=500,
        body='{"error":"internal server error"}',
    )

    assert _is_context_limit_error(error) is False


@pytest.mark.parametrize(
    "error",
    (
        ModelRequestError("prompt too long", timeout=True),
        ModelRequestError("prompt too long", status=401, body="too many tokens"),
        TimeoutError("prompt too long"),
        CancelledError("maximum context"),
    ),
)
def test_context_limit_error_does_not_reclassify_timeout_auth_or_cancellation(error):
    assert _is_context_limit_error(error) is False


def tool_call_response(name, arguments, call_id=None):
    call_id = call_id or f"call-{name}-{arguments.get('path', 'value')}"
    call = {"id": call_id, "name": name, "arguments": json.dumps(arguments)}
    return ModelTurnResult(
        response={}, tool_calls=(call,), text="", text_source="none",
        finish_reason="tool_calls", usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def checkpoint_response(*, inspected, unresolved, **overrides):
    payload = {
        "inspected": inspected,
        "unresolved": unresolved,
        "hypotheses": [],
        "candidate_finding_ids": [],
        "invariants_evaluated": [],
        "unknowns": unresolved,
        "proposed_next_actions": [],
        "working_summary": "The checkpoint retains the current working state.",
        "completed_steps": ["Reviewed the assigned checkpoint scope."],
    }
    payload.update(overrides)
    text = json.dumps(payload)
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
        "working_summary": "The candidate checkpoint retains the working state.",
        "completed_steps": ["Collected and assessed the candidate evidence."],
    })
    return ModelTurnResult(
        response={}, tool_calls=(), text=text, text_source="content",
        finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def candidate_update_checkpoint_response(*, updates=(), new_candidates=(), unresolved=("OB-tests",)):
    """Build the compact lifecycle checkpoint shape used by new protocol tests."""
    text = json.dumps({
        "inspected": [],
        "unresolved": list(unresolved),
        "candidate_updates": list(updates),
        "new_candidates": list(new_candidates),
        "unknowns": list(unresolved),
        "working_summary": "The candidate lifecycle state remains cumulative.",
        "completed_steps": ["Reviewed the active candidate lifecycle updates."],
    })
    return ModelTurnResult(
        response={}, tool_calls=(), text=text, text_source="content",
        finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def test_shortened_evidence_ids_are_expanded_only_when_unambiguous():
    retained = {
        "evidence:abcdef0123456789": object(),
        "evidence:fedcba9876543210": object(),
    }

    assert (
        _resolve_retained_evidence_id("evidence:abcdef01...", retained)
        == "evidence:abcdef0123456789"
    )
    assert _resolve_retained_evidence_id("evidence:abc...", retained) is None
    assert _rewrite_rationale_evidence_ids(
        "consequence_support:reachable_input_path; evidence_ids=evidence:abcdef01...",
        retained,
    ).endswith("evidence_ids=evidence:abcdef0123456789")


def test_validated_compaction_registers_replaced_results_for_bounded_retrieval():
    session = make_session(
        ScriptedGateway([
            checkpoint_response(inspected=["0.py"], unresolved=["OB-tests"]),
        ]),
        max_context_tokens=100_000,
    )
    records = [
        seed_successful_tool_exchange(
            session,
            call_id=f"call-{index}",
            path=f"{index}.py",
            content="important-tail\n" + (str(index) * 5_000),
        )
        for index in range(3)
    ]

    session.request_checkpoint("context-pressure", disposition="compact_resume")

    record = records[0]
    assert record.id in session._compacted_evidence
    assert any(
        event.get("epoch_continuation") and record.id in event["content"]
        for event in session.conversation.events
    )


def test_compacted_evidence_reader_is_strict_and_deduplicated():
    session = make_session(ScriptedGateway([]))
    record, _collection = session.evidence_store.add_tool_result_with_collection(
        session_id=session.session_id,
        tool="read_file",
        arguments={"path": "a.py"},
        result={"status": "ok", "content": "retained source"},
    )
    session._compacted_evidence[record.id] = record

    first = {
        "id": "read-1",
        "name": COMPACTED_EVIDENCE_TOOL_NAME,
        "arguments": json.dumps({
            "evidence_id": record.id, "target": "OB-code",
            "purpose": "obligation_resolution", "offset": 0, "limit": 100,
        }),
    }
    assert session._execute_calls((first,)) is False
    assert "retained source" in session.conversation.events[-1]["content"]
    assert session.budget.snapshot().tool_calls == 0

    repeated = {**first, "id": "read-2"}
    session._execute_calls((repeated,))
    assert "replayed_compacted" in session.conversation.events[-1]["content"]

    rejected = {
        "id": "read-3",
        "name": COMPACTED_EVIDENCE_TOOL_NAME,
        "arguments": json.dumps({
            "evidence_id": "evidence:not-compacted", "target": "OB-code",
            "purpose": "obligation_resolution",
        }),
    }
    session._execute_calls((rejected,))
    assert "not marked as compacted" in session.conversation.events[-1]["content"]


def test_compacted_evidence_requires_authorized_target_and_purpose():
    session = make_session(ScriptedGateway([]))
    record = session.evidence_store.add_tool_result(
        session_id=session.session_id, tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "content": "retained"},
    )
    session._compacted_evidence[record.id] = record

    missing = session._read_compacted_evidence({"evidence_id": record.id})
    unknown = session._read_compacted_evidence({
        "evidence_id": record.id, "target": "invented",
        "purpose": "candidate_support",
    })

    assert missing["status"] == "error"
    assert unknown["status"] == "error"


def test_reworded_checkpoint_does_not_count_as_semantic_progress():
    session = make_session(ScriptedGateway([]))
    first = SessionCheckpoint(
        session_id=session.session_id, state=SessionState.CHECKPOINT,
        working_summary="Checked delivery behavior.", completed_steps=("Read a.py",),
        proposed_next_actions=("Inspect the consumer",),
    )
    second = replace(
        first, working_summary="Delivery behavior was checked in detail.",
        completed_steps=("Inspected a.py",),
    )

    assert session._checkpoint_progress_fingerprint(first) == (
        session._checkpoint_progress_fingerprint(second)
    )


def test_compaction_without_valid_checkpoint_keeps_assistant_analysis():
    session = make_session(ScriptedGateway([]), max_context_tokens=1_000)
    session.conversation.add_assistant_text("old conclusion: " + ("x" * 5_000))
    session.conversation.add_assistant_text("new conclusion: " + ("y" * 5_000))
    before = json.loads(json.dumps(session.conversation.events))

    session._compact_conversation()

    assert session.conversation.events == before


@dataclass(frozen=True)
class RecordedRequest:
    messages: str
    tools_enabled: bool
    deadline_at: float | None
    max_tokens: int
    ephemeral_user_note: str | None
    reasoning_effort: str | None
    response_schema: dict | None

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
            reasoning_effort=request.reasoning_effort,
            response_schema=request.response_schema,
        ))
        assert self.responses, "model called more times than scripted"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class EstimatingGateway(ScriptedGateway):
    def __init__(self, responses, *, rendered_bytes, usages=()):
        super().__init__(responses)
        self.rendered_bytes = rendered_bytes
        self.usages = list(usages)
        self.rendered_requests = []

    def rendered_request_bytes(self, request):
        self.rendered_requests.append(request)
        return self.rendered_bytes

    def complete(self, request):
        response = super().complete(request)
        if self.usages:
            response = replace(response, usage=self.usages.pop(0))
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


def seed_successful_tool_exchange(
    session, *, call_id, path, content, reasoning="private analysis",
):
    result = {"status": "ok", "content": content}
    record, _collection = session.evidence_store.add_tool_result_with_collection(
        session_id=session.session_id,
        tool="read_file",
        arguments={"path": path},
        result=result,
    )
    session._tool_call_evidence_ids[call_id] = record.id
    session.conversation.add_assistant_turn(
        reasoning=reasoning,
        calls=[{
            "id": call_id,
            "name": "read_file",
            "arguments": json.dumps({"path": path}),
        }],
    )
    session.conversation.add_tool_result(
        call_id,
        {
            "evidence_id": record.id,
            "status": record.status,
            "content": record.content,
        },
    )
    return record


def test_obligation_state_tools_use_short_handles_without_repository_budget():
    session = make_session(ScriptedGateway([]))
    tool_names = {item["name"] for item in session.conversation.tool_schemas}
    before = session.budget.snapshot().tool_calls

    progressed = session._execute_calls(({
        "id": "explain-1", "name": "explain_obligation",
        "arguments": json.dumps({"target": "O1"}),
    },))

    assert {
        "explain_obligation", "get_obligation_status",
        "propose_obligation_resolution",
    }.issubset(tool_names)
    assert progressed is True
    assert session.budget.snapshot().tool_calls == before
    assert '"target": "O1"' in session.conversation.events[-1]["content"]
    assert "OB-code" not in session.conversation.events[-1]["content"]


def test_specialist_assignment_exposes_controller_obligation_handles():
    assignment = SpecialistAssignment(
        assignment_id="assignment-1",
        objective="Review the assigned behavior",
        primary_obligation_ids=("OB-code", "OB-tests"),
        seed_paths=("a.py",),
    )

    prompt = specialist_assignment_prompt(assignment)
    payload = json.loads(prompt.split("\n", 1)[1])

    assert payload["obligation_targets"] == [
        {"target": "O1", "obligation_id": "OB-code"},
        {"target": "O2", "obligation_id": "OB-tests"},
    ]
    assert "Use the short target handles" in payload["obligation_protocol"]


def test_resolution_accepts_owned_full_id_and_stringified_array_arguments():
    session = make_session(ScriptedGateway([]))
    session._execute_calls(({
        "id": "read-1", "name": "read_file",
        "arguments": json.dumps({"path": "a.py", "targets": ["O1"]}),
    },))
    read_result = json.loads(session.conversation.events[-1]["content"])

    session._execute_calls(({
        "id": "resolve-1", "name": "propose_obligation_resolution",
        "arguments": json.dumps({
            "target": "OB-code",
            "disposition": "covered",
            "reason": "The changed implementation preserves the contract.",
            "evidence_ids": json.dumps([read_result["evidence_id"]]),
            "next_actions": "[]",
        }),
    },))

    proposal = json.loads(session.conversation.events[-1]["content"])
    assert proposal == {
        "accepted": True,
        "target": "O1",
        "disposition": "covered",
        "reason": "accepted",
    }


def test_repeated_identical_obligation_rejection_pauses_instead_of_compacting():
    rejected = tool_call_response(
        "propose_obligation_resolution",
        {
            "target": "not-assigned",
            "disposition": "covered",
            "reason": "The implementation was inspected.",
            "evidence_ids": [],
            "next_actions": [],
        },
        call_id="reject-1",
    )
    rejected_again = replace(
        rejected,
        tool_calls=({**rejected.tool_calls[0], "id": "reject-2"},),
    )
    gateway = ScriptedGateway([
        rejected,
        rejected_again,
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
    ])
    session = make_session(gateway, model_turns=6)

    result = session.explore()

    assert len(gateway.requests) == 3
    assert result.state.value == "checkpoint"
    assert session._finalization_diagnostics[-1]["reason"] == (
        "repeated-obligation-rejection"
    )
    assert session._finalization_diagnostics[-1]["disposition"] == "pause"


def test_targeted_read_retains_neutral_evidence_until_resolution_is_accepted():
    session = make_session(ScriptedGateway([]))
    read = {
        "id": "read-1", "name": "read_file",
        "arguments": json.dumps({"path": "a.py", "targets": ["O1"]}),
    }

    session._execute_calls((read,))

    result = json.loads(session.conversation.events[-1]["content"])
    assert result["coverage_effect"] == "neutral_evidence_retained"
    assert result["eligible_targets"] == ["O1"]
    assert session.coverage.obligation_statuses()["OB-code"].value == "pending"

    session._execute_calls(({
        "id": "resolve-1", "name": "propose_obligation_resolution",
        "arguments": json.dumps({
            "target": "O1", "disposition": "covered",
            "reason": "The changed implementation preserves the contract.",
            "evidence_ids": [result["evidence_id"]], "next_actions": [],
        }),
    },))

    proposal = json.loads(session.conversation.events[-1]["content"])
    assert proposal["accepted"] is True
    assert session.coverage.obligation_statuses()["OB-code"].value == "pending"
    assert session.obligation_assessments.assessment("O1").disposition.value == "covered"


def test_untargeted_read_is_not_associated_with_every_current_gap():
    session = make_session(ScriptedGateway([]))

    session._execute_calls(({
        "id": "read-1", "name": "read_file",
        "arguments": json.dumps({"path": "a.py"}),
    },))

    result = json.loads(session.conversation.events[-1]["content"])
    assert result["eligible_targets"] == []
    assert session.evidence_store.snapshot().associations == ()


def test_resolution_can_bind_untargeted_retained_evidence_when_scope_matches():
    session = make_session(ScriptedGateway([]))
    session._execute_calls(({
        "id": "read-1", "name": "read_file",
        "arguments": json.dumps({"path": "a.py"}),
    },))
    evidence_id = json.loads(session.conversation.events[-1]["content"])[
        "evidence_id"
    ]

    session._execute_calls(({
        "id": "resolve-1", "name": "propose_obligation_resolution",
        "arguments": json.dumps({
            "target": "O1", "disposition": "covered",
            "reason": "The retained implementation matches the assigned scope.",
            "evidence_ids": [evidence_id], "next_actions": [],
        }),
    },))

    proposal = json.loads(session.conversation.events[-1]["content"])
    assert proposal["accepted"] is True
    assert session.obligation_assessments.assessment("O1").disposition.value == (
        "covered"
    )
    assert session.evidence_store.snapshot().associations_for(
        evidence_id, "OB-code",
    )


def test_resolution_does_not_bind_untargeted_evidence_outside_scope():
    session = make_session(ScriptedGateway([]))
    session._execute_calls(({
        "id": "read-1", "name": "read_file",
        "arguments": json.dumps({"path": "tests/test_a.py"}),
    },))
    evidence_id = json.loads(session.conversation.events[-1]["content"])[
        "evidence_id"
    ]

    session._execute_calls(({
        "id": "resolve-1", "name": "propose_obligation_resolution",
        "arguments": json.dumps({
            "target": "O1", "disposition": "covered",
            "reason": "Unrelated retained evidence should not prove the implementation.",
            "evidence_ids": [evidence_id], "next_actions": [],
        }),
    },))

    proposal = json.loads(session.conversation.events[-1]["content"])
    assert proposal["accepted"] is False
    assert proposal["reason"] == "covered requires eligible retained evidence"
    assert not session.evidence_store.snapshot().associations_for(
        evidence_id, "OB-code",
    )


def test_duplicate_targeted_read_reports_target_metadata_without_new_budget():
    session = make_session(ScriptedGateway([]))
    before = session.budget.snapshot().tool_calls
    session._execute_calls(({
        "id": "read-1", "name": "read_file",
        "arguments": json.dumps({"path": "a.py"}),
    },))
    session._execute_calls(({
        "id": "read-2", "name": "read_file",
        "arguments": json.dumps({"path": "a.py", "targets": ["O1"]}),
    },))

    result = json.loads(session.conversation.events[-1]["content"])
    assert result["replayed_duplicate"] is True
    assert result["eligible_targets"] == ["O1"]
    assert result["coverage_effect"] == "neutral_evidence_retained"
    assert session.budget.snapshot().tool_calls == before + 1


def test_obligation_state_tools_have_a_separate_bounded_allowance():
    session = make_session(ScriptedGateway([]))

    for index in range(session.OBLIGATION_LOCAL_TOOL_CALL_LIMIT + 1):
        session._execute_calls(({
            "id": f"explain-{index}", "name": "explain_obligation",
            "arguments": json.dumps({"target": "O1"}),
        },))

    result = json.loads(session.conversation.events[-1]["content"])
    assert result["accepted"] is False
    assert "allowance exhausted" in result["reason"]


def test_checkpoint_obligation_update_uses_same_resolution_validator():
    executor_result = {
        "tool": "read_file", "status": "ok",
        "result": {"content": "contents:a.py"},
    }
    evidence_id = canonical_evidence_key(
        "read_file", {"path": "a.py"}, executor_result,
    )
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py", "targets": ["O1"]}),
        checkpoint_response(
            inspected=["a.py"], unresolved=["OB-tests"],
            obligation_updates=[{
                "target": "O1", "disposition": "covered",
                "reason": "The changed implementation preserves the contract.",
                "evidence_ids": [evidence_id], "next_actions": [],
            }],
        ),
    ])
    session = make_session(gateway)

    result = session.explore()

    assessment = result.checkpoint.obligation_assessments[0]
    assert assessment.disposition.value == "covered"
    assert "OB-code" not in result.checkpoint.unknowns


def test_invalid_checkpoint_obligation_batch_rolls_back_earlier_updates():
    session = make_session(ScriptedGateway([]))
    session._execute_calls(({
        "id": "read-1", "name": "read_file",
        "arguments": json.dumps({"path": "a.py", "targets": ["O1"]}),
    },))
    evidence_id = json.loads(session.conversation.events[-1]["content"])["evidence_id"]
    payload = {
        "unresolved": ["OB-tests"],
        "obligation_updates": [{
            "target": "O1", "disposition": "covered", "reason": "Covered.",
            "evidence_ids": [evidence_id], "next_actions": [],
        }, {
            "target": "O99", "disposition": "blocked", "reason": "Unknown.",
            "evidence_ids": [], "next_actions": [],
        }],
    }

    checkpoint = session._checkpoint_from_text(json.dumps(payload))

    assert checkpoint is None
    assert session.obligation_assessments.assessment("O1").disposition.value == "pending"


def test_provider_prompt_usage_calibrates_next_same_mode_admission():
    gateway = EstimatingGateway(
        [
            checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
            checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
        ],
        rendered_bytes=32_000,
        usages=(
            {"prompt_tokens": 12_000, "completion_tokens": 100},
            {"prompt_tokens": 8_000, "completion_tokens": 80},
        ),
    )
    session = make_session(gateway, max_context_tokens=20_000)
    attempts = RequestAttemptJournal()
    session.bind_request_attempt_journal(attempts, "assignment-1")

    session.request_checkpoint("controller-request")
    session.request_checkpoint("controller-request")
    estimate = session._estimate_admission(
        tools_enabled=False, max_tokens=2_048,
    )

    assert estimate.source == "provider-calibrated"
    assert estimate.input_tokens >= 12_000
    assert session._admission_calibration["structured"].last_completion_tokens == 80
    terminal = attempts.close_since(0)[-1]
    assert terminal.admission_source == "provider-calibrated"
    assert terminal.actual_prompt_tokens == 8_000
    assert terminal.actual_completion_tokens == 80


@pytest.mark.parametrize("usage", (
    {},
    {"prompt_tokens": 0, "completion_tokens": 0},
    {"prompt_tokens": -10, "completion_tokens": -2},
    {"prompt_tokens": "12000", "completion_tokens": "100"},
))
def test_rendered_admission_falls_back_without_valid_provider_usage(usage):
    gateway = EstimatingGateway(
        [checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"])],
        rendered_bytes=6_001,
        usages=(usage,),
    )
    session = make_session(gateway, max_context_tokens=20_000)

    session.request_checkpoint("controller-request")
    estimate = session._estimate_admission(
        tools_enabled=True, max_tokens=2_048,
    )

    assert estimate.source == "rendered-fallback"
    assert estimate.input_tokens == 2_001
    assert estimate.admission_tokens == 2_001 + 2_048 + 256


def test_provider_calibration_carries_from_structured_to_tools_mode():
    gateway = EstimatingGateway(
        [checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"])],
        rendered_bytes=32_000,
        usages=({"prompt_tokens": 12_000, "completion_tokens": 100},),
    )
    session = make_session(gateway, max_context_tokens=20_000)

    session.request_checkpoint("controller-request")
    structured = session._estimate_admission(
        tools_enabled=False, max_tokens=2_048,
    )
    tools = session._estimate_admission(
        tools_enabled=True, max_tokens=2_048,
    )

    assert structured.source == "provider-calibrated"
    assert tools.source == "provider-calibrated"
    assert tools.input_tokens >= 12_000


def test_actual_tool_prompt_usage_replaces_full_history_byte_fallback_for_checkpoint():
    gateway = EstimatingGateway(
        [checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"])],
        rendered_bytes=190_000,
        usages=({"prompt_tokens": 46_459, "completion_tokens": 58},),
    )
    session = make_session(gateway, max_context_tokens=80_056)

    session._request(
        tools_enabled=True, schema=None, purpose="exploration",
    )
    gateway.rendered_bytes = 194_161
    estimate = session._estimate_admission(
        tools_enabled=False, max_tokens=8_192,
    )

    assert estimate.source == "provider-calibrated"
    assert estimate.input_tokens < 52_000
    assert estimate.input_tokens < 64_721


def test_fractional_provider_usage_is_rounded_up_for_calibration():
    gateway = EstimatingGateway(
        [checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"])],
        rendered_bytes=32_000,
        usages=({"prompt_tokens": 12_000.5, "completion_tokens": 100.25},),
    )
    session = make_session(gateway, max_context_tokens=20_000)

    session.request_checkpoint("controller-request")

    calibration = session._admission_calibration["structured"]
    assert calibration.last_prompt_tokens == 12_001
    assert calibration.last_completion_tokens == 101


def test_model_gateway_exception_still_charges_reserved_turn():
    gateway = ScriptedGateway([RuntimeError("provider unavailable")])
    session = make_session(gateway)
    attempts = RequestAttemptJournal()
    session.bind_request_attempt_journal(attempts, "assignment-1")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        session.explore()

    assert session.budget.snapshot().model_turns == 1
    assert len(gateway.requests) == 1
    assert tuple(item.status for item in session._request_events) == (
        "started", "failed",
    )
    assert session._request_events[0].request_id == session._request_events[1].request_id
    terminal = attempts.close_since(0)[0]
    assert terminal.admission_tokens > terminal.input_tokens
    assert terminal.actual_prompt_tokens == 0
    assert terminal.actual_completion_tokens == 0


def test_emergency_checkpoint_recovers_one_exploration_context_error():
    provider_error = ModelRequestError(
        "provider rejected request with HTTP 400: "
        "api_key=super-secret context_length_exceeded",
        status=400,
        body='{"code":"context_length_exceeded"}',
    )
    gateway = ScriptedGateway([
        provider_error,
        checkpoint_response(
            inspected=["0.py", "1.py", "2.py"],
            unresolved=["OB-tests"],
            working_summary="Recovered the accepted exploration state.",
            proposed_next_actions=["Continue with the remaining test obligation."],
        ),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    records = [
        seed_successful_tool_exchange(
            session,
            call_id=f"accepted-{index}",
            path=f"{index}.py",
            content=f"accepted evidence {index}",
        )
        for index in range(3)
    ]
    attempts = RequestAttemptJournal()
    session.bind_request_attempt_journal(attempts, "assignment-1")

    result = session.explore()

    assert result.state.value == "checkpoint"
    assert result.degraded is False
    assert len(gateway.requests) == 2
    assert [request.tools_enabled for request in gateway.requests] == [True, False]
    assert gateway.requests[1].max_tokens == session.checkpoint_max_tokens
    assert gateway.requests[1].reasoning_effort == "none"
    first_messages = json.loads(gateway.requests[0].messages)
    emergency_messages = json.loads(gateway.requests[1].messages)
    assert emergency_messages[:-1] == first_messages
    assert "Checkpoint reason: provider-context-limit." in emergency_messages[-1]["content"]
    assert session._checkpoint_spans[-1].compacted is True
    assert records[0].id in session._compacted_evidence
    terminal_attempts = attempts.close_since(0)
    assert [attempt.status for attempt in terminal_attempts] == ["failed", "completed"]
    assert "context_length_exceeded" in terminal_attempts[0].error
    assert "super-secret" not in terminal_attempts[0].error
    assert result.budget.model_turns == 2
    diagnostic = result.finalization_diagnostics[-1]
    assert diagnostic["disposition"] == "compact_resume"
    assert diagnostic["compaction_level"] == "emergency"
    assert diagnostic["emergency_outcome"] == "checkpoint_succeeded"


def test_emergency_checkpoint_context_error_stops_without_a_third_request():
    gateway = ScriptedGateway([
        ModelRequestError("prompt too long", status=400),
        ModelRequestError("maximum context exceeded", status=400),
    ])
    session = make_session(gateway, max_context_tokens=100_000)

    result = session.explore()

    assert len(gateway.requests) == 2
    assert [request.tools_enabled for request in gateway.requests] == [True, False]
    assert result.state.value == "checkpoint"
    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns
    assert result.budget.model_turns == 2
    diagnostic = result.finalization_diagnostics[-1]
    assert diagnostic["compaction_level"] == "none"
    assert diagnostic["emergency_outcome"] == "failed_no_checkpoint"


def test_emergency_checkpoint_context_error_uses_two_physical_provider_calls():
    physical_payloads = []

    def counting_transport(base_url, api_format, payload, api_key, timeout_sec):
        physical_payloads.append(payload)
        raise ModelRequestError(
            "model provider rejected request: context_length_exceeded",
            status=400,
            body='{"code":"context_length_exceeded"}',
        )

    gateway = OpenAIModelGateway(
        base_url="http://provider.test/v1",
        api_key="test-key",
        default_model="test-model",
        transport=counting_transport,
    )
    session = make_session(gateway, max_context_tokens=100_000)
    attempts = RequestAttemptJournal()
    session.bind_request_attempt_journal(attempts, "assignment-1")

    result = session.explore()

    assert len(physical_payloads) == 2
    assert "tools" in physical_payloads[0]
    assert "tools" not in physical_payloads[1]
    assert "response_format" in physical_payloads[1]
    assert result.degraded is True
    assert result.budget.model_turns == 2
    assert [attempt.status for attempt in attempts.close_since(0)] == [
        "failed", "failed",
    ]


def test_invalid_emergency_checkpoint_does_not_request_repair():
    gateway = ScriptedGateway([
        ModelRequestError("prompt too long", status=400),
        invalid_response("not a valid checkpoint"),
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)

    result = session.explore()

    assert len(gateway.requests) == 2
    assert [request.tools_enabled for request in gateway.requests] == [True, False]
    assert result.state.value == "checkpoint"
    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns
    assert result.budget.model_turns == 2


def test_rejected_emergency_checkpoint_rolls_back_all_tentative_state():
    rejected_candidate = (
        '{"candidate_findings":[{"candidate_id":"phantom-candidate",'
        '"claim":"rejected emergency output"}]}'
    )
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=["a.py"],
            unresolved=["OB-tests"],
            working_summary="Prior validated state.",
        ),
        ModelRequestError("prompt too long", status=400),
        invalid_response(rejected_candidate),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    attempts = RequestAttemptJournal()
    session.bind_request_attempt_journal(attempts, "assignment-1")
    session.request_checkpoint("controller-request", disposition="pause")
    prior_signal = session._candidate_retention_signal

    emergency_result = session.explore()

    assert session._candidate_retention_signal == prior_signal
    transcript = json.dumps(session.conversation.events, sort_keys=True)
    assert "phantom-candidate" not in transcript
    assert "rejected emergency output" not in transcript
    assert "Checkpoint reason: provider-context-limit." not in transcript
    emergency_diagnostic = emergency_result.finalization_diagnostics[-1]
    assert emergency_diagnostic["initial_parse"] == "invalid"
    assert emergency_diagnostic["fallback_projection"] is False
    assert emergency_diagnostic["retention_unknown"] is False
    failed_exploration = attempts.close_since(0)[1]
    assert failed_exploration.purpose == "exploration"
    assert failed_exploration.status == "failed"
    assert "prompt too long" in failed_exploration.error

    later_result = session.request_checkpoint("controller-request")

    assert later_result.degraded is False
    assert len(gateway.requests) == 4
    assert [attempt.purpose for attempt in attempts.close_since(0)] == [
        "checkpoint", "exploration", "checkpoint", "checkpoint",
    ]


def test_retention_losing_emergency_without_prior_reports_projected_fallback():
    candidate_turn = tool_call_response(
        "read_file", {"path": "a.py"}, call_id="candidate-before-pressure",
    )
    candidate_turn = ModelTurnResult(**{
        **candidate_turn.__dict__,
        "text": (
            '{"candidate_finding_ids":["candidate-unretained"],'
            '"candidate_findings":[{"candidate_id":"candidate-unretained",'
            '"claim":"material candidate before context pressure"}]}'
        ),
        "text_source": "content",
    })
    gateway = ScriptedGateway([
        candidate_turn,
        ModelRequestError("maximum context exceeded", status=400),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)

    result = session.explore()

    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns
    assert len(gateway.requests) == 3
    diagnostic = result.finalization_diagnostics[-1]
    assert diagnostic["reason"] == "provider-context-limit"
    assert diagnostic["initial_parse"] == "valid"
    assert diagnostic["repair_attempted"] is False
    assert diagnostic["repair_parse"] == "not_attempted"
    assert diagnostic["fallback_projection"] is True
    assert diagnostic["retention_unknown"] is True


def test_failed_emergency_checkpoint_reconstructs_previous_valid_checkpoint():
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=["a.py"],
            unresolved=["OB-tests"],
            working_summary="Prior validated state.",
        ),
        ModelRequestError("too many tokens", status=400),
        ModelRequestError("context size exceeded", status=400),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    session.request_checkpoint("controller-request", disposition="pause")
    previous_checkpoint = session.latest_checkpoint

    result = session.explore()

    assert len(gateway.requests) == 3
    assert [request.tools_enabled for request in gateway.requests] == [
        False, True, False,
    ]
    assert result.state.value == "checkpoint"
    assert result.degraded is False
    assert result.checkpoint == previous_checkpoint
    assert any(
        event.get("emergency_reconstruction")
        for event in session.conversation.events
    )
    diagnostic = result.finalization_diagnostics[-1]
    assert diagnostic["compaction_level"] == "emergency_reconstruction"
    assert diagnostic["emergency_outcome"] == "fallback_reconstructed"
    assert diagnostic["compaction_input_tokens_before"] > 0
    assert diagnostic["compaction_input_tokens_after"] > 0
    assert session.finalize().degraded is False


def test_rejected_emergency_projection_does_not_poison_prior_checkpoint_finalization():
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=["a.py"], unresolved=["OB-tests"],
            working_summary="Prior validated state.",
        ),
        ModelRequestError("too many tokens", status=400),
        invalid_response("not a checkpoint"),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    session.request_checkpoint("controller-request", disposition="pause")

    recovered = session.explore()

    assert recovered.degraded is False
    assert session.finalize().degraded is False


def test_emergency_checkpoint_guard_is_session_lifetime():
    gateway = ScriptedGateway([
        ModelRequestError("prompt too long", status=400),
        checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
        ModelRequestError("too many tokens", status=400),
    ])
    session = make_session(gateway, max_context_tokens=100_000)

    first = session.explore()
    second = session.explore()

    assert first.degraded is False
    assert second.state.value == "checkpoint"
    assert len(gateway.requests) == 3
    assert [request.tools_enabled for request in gateway.requests] == [
        True, False, True,
    ]


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
        "started", "completed",
    )
    assert repeated.request_events == final.request_events
    request_pairs = {}
    for event in final.request_events:
        request_pairs.setdefault(event.request_id, []).append(event.status)
    assert tuple(request_pairs.values()) == (["started", "completed"],)


def test_exploration_budget_is_enforced_without_wire_budget_notes():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=[]),
    ])
    session = make_session(gateway, model_turns=4, tool_calls=4)

    session.explore()

    first, second = gateway.requests
    assert first.ephemeral_user_note is None
    assert second.ephemeral_user_note is None
    assert "Exploration budget before this turn" not in json.dumps(session.conversation.events)


def test_invalid_exploration_stop_forces_checkpoint_before_later_finalization():
    gateway = ScriptedGateway([
        invalid_response("plain-text conclusion"),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    session = make_session(gateway)

    checkpoint = session.explore()
    final = session.finalize()

    assert checkpoint.state.value == "checkpoint"
    assert final.state.value == "complete"
    assert [request.tools_enabled for request in gateway.requests] == [True, False]
    assert gateway.requests[1].messages_contain("Checkpoint requested (not a final report)")


def test_finalization_closes_from_valid_checkpoint_without_model_call():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    session = make_session(gateway)

    session.explore()
    result = session.finalize()

    assert result.degraded is False
    assert result.report["source"] == "checkpoint-finalization"
    assert len(gateway.requests) == 1


def test_checkpoint_derives_candidate_ids_from_candidate_objects():
    response = candidate_checkpoint_response(["candidate-code"])
    payload = json.loads(response.text)
    payload.pop("candidate_finding_ids", None)
    response = ModelTurnResult(
        response={}, tool_calls=(), text=json.dumps(payload),
        text_source="content", finish_reason="stop",
        usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )
    session = make_session(ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        response,
    ]))

    result = session.explore()

    assert result.checkpoint.candidate_finding_ids == ("candidate-code",)


def test_checkpoint_carries_forward_candidates_when_update_arrays_are_empty():
    """Unchanged candidates remain active without replaying their full objects."""
    initial = candidate_checkpoint_response(("candidate-code",))
    second = candidate_update_checkpoint_response(updates=(), new_candidates=())
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        initial,
        second,
    ])
    session = make_session(gateway, model_turns=4)

    first = session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    second_result = session.explore()

    assert first.checkpoint.candidate_finding_ids == ("candidate-code",)
    assert second_result.checkpoint.candidate_finding_ids == ("candidate-code",)
    assert second_result.degraded is False


def test_checkpoint_retains_bounded_cumulative_working_state():
    """Recovery working state survives checkpoint parsing within hard bounds."""
    gateway = ScriptedGateway([checkpoint_response(
        inspected=["a.py"],
        unresolved=["OB-tests"],
        working_summary=(
            "The input reaches the controller through config validation. "
            + "x" * 2_000
        ),
        completed_steps=[
            "Compared action input and config fallback; values agree.",
            *[f"step-{index}: " + "y" * 600 for index in range(12)],
        ],
        hypotheses=[
            "Recovery authorization still needs a boundary test.",
            *[f"hypothesis-{index}" for index in range(12)],
        ],
        invariants_evaluated=[
            "Lifetime budget is not reset by follow-up.",
        ],
        proposed_next_actions=[
            "Inspect the recovery authorization test.",
        ],
    )])

    result = make_session(gateway).request_checkpoint("controller-request")

    assert result.checkpoint.working_summary.startswith("The input reaches")
    assert len(result.checkpoint.working_summary) == 2_000
    assert result.checkpoint.completed_steps == (
        "Compared action input and config fallback; values agree.",
        ("step-0: " + "y" * 492),
        ("step-1: " + "y" * 492),
        ("step-2: " + "y" * 492),
        ("step-3: " + "y" * 492),
        ("step-4: " + "y" * 492),
        ("step-5: " + "y" * 492),
        ("step-6: " + "y" * 492),
        ("step-7: " + "y" * 492),
        ("step-8: " + "y" * 492),
        ("step-9: " + "y" * 492),
        ("step-10: " + "y" * 491),
    )
    assert result.checkpoint.hypotheses == (
        "Recovery authorization still needs a boundary test.",
        *[f"hypothesis-{index}" for index in range(11)],
    )
    assert result.checkpoint.invariants_evaluated == (
        "Lifetime budget is not reset by follow-up.",
    )
    assert result.checkpoint.proposed_next_actions == (
        "Inspect the recovery authorization test.",
    )


def test_cumulative_checkpoint_payload_materializes_omitted_candidates():
    """Reconstruction gets controller-owned state without candidate replay."""
    initial = candidate_checkpoint_response(("candidate-code",))
    updated = candidate_update_checkpoint_response(
        updates=(), new_candidates=(),
    )
    updated_payload = json.loads(updated.text)
    updated_payload.update({
        "working_summary": "The retained candidate still needs a boundary test.",
        "completed_steps": ["Collected implementation evidence for a.py."],
        "hypotheses": ["The candidate remains reachable."],
        "invariants_evaluated": ["Coverage evidence remains retained."],
        "proposed_next_actions": ["Inspect the boundary test."],
    })
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        initial,
        ModelTurnResult(**{**updated.__dict__, "text": json.dumps(updated_payload)}),
    ])
    session = make_session(gateway, model_turns=4)

    session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    session.request_checkpoint("controller-request")
    payload = session._cumulative_checkpoint_payload()

    assert payload["latest_checkpoint"]["working_summary"] == (
        "The retained candidate still needs a boundary test."
    )
    assert payload["latest_checkpoint"]["completed_steps"] == [
        "Collected implementation evidence for a.py.",
    ]
    assert payload["candidate_findings"][0]["candidate_id"] == "candidate-code"
    assert payload["candidate_statuses"] == {"candidate-code": "active"}
    assert payload["latest_checkpoint"]["evidence_ids"]
    assert payload["coverage"]["obligation_statuses"]["OB-code"] == "pending"
    assert payload["evidence_metadata"][0]["id"].startswith("evidence:")
    assert "content" not in payload["evidence_metadata"][0]


def test_sparse_checkpoint_preserves_prior_cumulative_working_state():
    """Omitting optional working fields cannot erase continuation memory."""
    initial = checkpoint_response(
        inspected=[],
        unresolved=["OB-tests"],
        working_summary="The controller input was validated.",
        completed_steps=["Compared action input and config fallback."],
        hypotheses=["The fallback remains reachable."],
        invariants_evaluated=["Lifetime budget is cumulative."],
        proposed_next_actions=["Inspect the authorization boundary."],
    )
    sparse_payload = json.loads(checkpoint_response(
        inspected=[], unresolved=["OB-tests"],
    ).text)
    for field in (
        "working_summary", "completed_steps", "hypotheses",
        "invariants_evaluated", "proposed_next_actions",
    ):
        sparse_payload.pop(field, None)
    sparse = ModelTurnResult(**{
        **initial.__dict__, "text": json.dumps(sparse_payload),
    })
    session = make_session(ScriptedGateway([initial, sparse]), model_turns=4)

    first = session.request_checkpoint("controller-request")
    second = session.request_checkpoint("controller-request")

    assert second.checkpoint.working_summary == first.checkpoint.working_summary
    assert second.checkpoint.completed_steps == first.checkpoint.completed_steps
    assert second.checkpoint.hypotheses == first.checkpoint.hypotheses
    assert second.checkpoint.invariants_evaluated == first.checkpoint.invariants_evaluated
    assert second.checkpoint.proposed_next_actions == first.checkpoint.proposed_next_actions


def test_invalid_checkpoint_fallback_and_recovery_preserve_prior_working_state():
    """An invalid checkpoint keeps the prior recovery continuation state."""
    initial = checkpoint_response(
        inspected=[],
        unresolved=["OB-tests"],
        working_summary="The controller input was validated.",
        completed_steps=["Compared action input and config fallback."],
        hypotheses=["The fallback remains reachable."],
        invariants_evaluated=["Lifetime budget is cumulative."],
        proposed_next_actions=["Inspect the authorization boundary."],
    )
    session = make_session(ScriptedGateway([
        initial,
        invalid_response(),
        invalid_response(),
    ]), model_turns=4)

    first = session.request_checkpoint("controller-request")
    fallback = session.request_checkpoint("controller-request")
    session.recover("repetitive-transcript")

    assert fallback.degraded is True
    assert fallback.checkpoint.working_summary == first.checkpoint.working_summary
    assert fallback.checkpoint.completed_steps == first.checkpoint.completed_steps
    assert fallback.checkpoint.hypotheses == first.checkpoint.hypotheses
    assert fallback.checkpoint.invariants_evaluated == first.checkpoint.invariants_evaluated
    assert fallback.checkpoint.proposed_next_actions == first.checkpoint.proposed_next_actions
    assert "The controller input was validated." in json.dumps(session.conversation.events)


def test_checkpoint_withdraws_known_candidate_only_with_explicit_update():
    """Omission does not withdraw a candidate; an explicit update does."""
    initial = candidate_checkpoint_response(("candidate-code",))
    withdrawn = candidate_update_checkpoint_response(updates=({
        "candidate_id": "candidate-code",
        "status": "withdrawn",
        "reason": "The suspected path is unreachable.",
        "evidence_ids": [],
    },))
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        initial,
        withdrawn,
    ])
    session = make_session(gateway, model_turns=4)

    session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    result = session.explore()

    assert result.checkpoint.candidate_finding_ids == ()
    assert session.candidate_findings == ()
    assert result.degraded is False


def test_checkpoint_accepts_new_candidates_separately_from_updates():
    """New candidates use the dedicated array and become active state."""
    initial = candidate_update_checkpoint_response()
    candidate_payload = json.loads(candidate_checkpoint_response(("candidate-new",)).text)
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        initial,
        ModelTurnResult(**{
            **initial.__dict__,
            "text": json.dumps({
                "inspected": ["a.py"],
                "unresolved": ["OB-tests"],
                "candidate_updates": [],
                "new_candidates": candidate_payload["candidate_findings"],
                "unknowns": ["OB-tests"],
            }),
        }),
    ])
    session = make_session(gateway, model_turns=5)

    session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    result = session.explore()

    assert result.checkpoint.candidate_finding_ids == ("candidate-new",)
    assert result.degraded is False


def test_checkpoint_prompt_contains_compact_active_candidate_register():
    """Checkpoint requests expose short handles instead of requiring full replay."""
    gateway = ScriptedGateway([
        candidate_update_checkpoint_response(),
    ])
    session = make_session(gateway)

    session.request_checkpoint("controller-request")

    prompt = gateway.requests[0].messages
    assert "candidate_updates" in prompt
    assert "new_candidates" in prompt
    assert "Active candidates" in prompt


def test_checkpoint_turn_disables_reasoning_to_reserve_output_for_json():
    gateway = ScriptedGateway([candidate_update_checkpoint_response()])
    session = make_session(gateway)

    session.request_checkpoint("controller-request")

    assert gateway.requests[0].reasoning_effort == "none"


def test_textual_tool_markup_gets_repaired_before_checkpointing():
    malformed = ModelTurnResult(
        response={}, tool_calls=(),
        text=(
            "[read_pr_diff]\n<parameter=path>\na.py\n"
            "</function>\n</tool_call>"
        ),
        text_source="content", finish_reason="stop", usage={"prompt_tokens": 3},
        request_diagnostics={}, content=(
            "[read_pr_diff]\n<parameter=path>\na.py\n"
            "</function>\n</tool_call>"
        ),
    )
    gateway = ScriptedGateway([
        malformed,
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=[]),
    ])
    session = make_session(gateway, model_turns=5)

    result = session.explore()

    assert result.degraded is False
    assert any(
        "textual tool-call markup" in request.messages
        for request in gateway.requests
    )


def test_repeated_textual_tool_markup_checkpoints_with_compact_resume():
    malformed = ModelTurnResult(
        response={}, tool_calls=(),
        text="[tool]<parameter=path>a.py</parameter>[/tool]",
        text_source="content", finish_reason="stop",
        usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )
    gateway = ScriptedGateway([
        malformed,
        malformed,
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    session = make_session(gateway, model_turns=4)

    result = session.explore()

    assert result.state.value == "checkpoint"
    assert len(gateway.requests) == 4
    assert gateway.requests[2].tools_enabled is False
    assert gateway.requests[2].messages_contain(
        "Checkpoint reason: malformed-textual-tool-call."
    )
    assert gateway.requests[2].messages_contain(
        "Immediate compaction after validation: yes."
    )
    assert gateway.requests[2].messages_contain(
        "After validation, resume the specialist session."
    )


def test_checkpoint_register_uses_exact_candidate_ids_without_unmapped_aliases():
    """The register must not advertise aliases the controller cannot resolve."""
    initial = candidate_checkpoint_response(("finding:long-stable-id",))
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        initial,
        candidate_update_checkpoint_response(),
    ])
    session = make_session(gateway, model_turns=4)

    session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    session.explore()

    prompt = gateway.requests[-1].messages
    assert "finding:long-stable-id" in prompt
    assert "short IDs" not in prompt


def test_superseded_update_requires_known_replacement_candidate():
    """Superseding a candidate must identify an active replacement."""
    initial = candidate_checkpoint_response(("candidate-code",))
    invalid = candidate_update_checkpoint_response(updates=({
        "candidate_id": "candidate-code",
        "status": "superseded",
        "reason": "Replaced by a narrower issue.",
    },))
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        initial,
        invalid,
        candidate_update_checkpoint_response(),
        candidate_update_checkpoint_response(),
    ])
    session = make_session(gateway, model_turns=6)

    session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    result = session.explore()

    assert result.degraded is True
    assert result.checkpoint.candidate_finding_ids == ("candidate-code",)


def test_plain_prose_candidate_words_do_not_trigger_retention_unknown():
    """Candidate vocabulary in ordinary prose is not structured candidate state."""
    gateway = ScriptedGateway([
        invalid_response(
            "I reviewed candidate_findings and the candidate_id/claim wording "
            "is not applicable to this specialist."
        ),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    result = make_session(gateway).explore()

    assert result.degraded is False
    assert "candidate-retention-unknown" not in result.checkpoint.unknowns


def test_superseded_update_rejects_self_reference_and_preserves_active_state():
    """Invalid self-supersede cannot mutate the prior candidate registry."""
    initial = candidate_checkpoint_response(("candidate-code",))
    invalid = candidate_update_checkpoint_response(updates=({
        "candidate_id": "candidate-code",
        "status": "superseded",
        "superseded_by": "candidate-code",
    },))
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        initial,
        invalid,
        candidate_update_checkpoint_response(),
        candidate_update_checkpoint_response(),
    ])
    session = make_session(gateway, model_turns=7)

    session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    result = session.explore()

    assert result.degraded is False
    assert result.checkpoint.candidate_finding_ids == ("candidate-code",)
    assert tuple(item.candidate_id for item in session.candidate_findings) == (
        "candidate-code",
    )


def test_superseded_replacement_must_remain_active_atomically():
    """A replacement withdrawn in the same checkpoint rejects the whole update."""
    initial = candidate_checkpoint_response(("candidate-code",))
    payload = json.loads(candidate_checkpoint_response(("candidate-new",)).text)
    invalid = candidate_update_checkpoint_response(
        updates=(
            {
                "candidate_id": "candidate-code",
                "status": "superseded",
                "superseded_by": "candidate-new",
            },
            {
                "candidate_id": "candidate-new",
                "status": "withdrawn",
            },
        ),
        new_candidates=payload["candidate_findings"],
    )
    valid_new = candidate_update_checkpoint_response(
        new_candidates=payload["candidate_findings"],
    )
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        initial,
        valid_new,
    ])
    session = make_session(gateway, model_turns=5)

    session.explore()
    session.apply_coverage_feedback(["OB-tests"])
    session.explore()
    checkpoint = session._checkpoint_from_text(invalid.text)

    assert checkpoint is None
    assert tuple(item.candidate_id for item in session.candidate_findings) == (
        "candidate-code", "candidate-new",
    )


def test_embedded_malformed_candidate_json_is_retention_material():
    """Truncated candidate JSON after prose remains conservatively degraded."""
    gateway = ScriptedGateway([
        invalid_response(
            'Here is the checkpoint draft: {"candidate_findings": '
            '[{"candidate_id":"candidate-lost","claim":"issue"}'
        ),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    result = make_session(gateway).explore()

    assert result.degraded is True
    assert "candidate-retention-unknown" in result.checkpoint.unknowns


def test_candidate_words_with_unrelated_braces_are_not_retention_material():
    """Candidate vocabulary plus non-JSON braces remains ordinary prose."""
    gateway = ScriptedGateway([
        invalid_response(
            "I reviewed candidate_findings and candidate_id/claim wording in {docs}."
        ),
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    result = make_session(gateway).explore()

    assert result.degraded is False
    assert "candidate-retention-unknown" not in result.checkpoint.unknowns


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
        assert "candidate_finding_ids" not in prompt
        assert "exact retained evidence IDs" in prompt
        assert "repository paths are not evidence IDs" in prompt


@pytest.mark.parametrize(("disposition", "compaction", "lifecycle"), (
    (
        "compact_resume",
        "Immediate compaction after validation: yes.",
        "After validation, resume the specialist session.",
    ),
    (
        "pause",
        "Immediate compaction after validation: no.",
        "After validation, pause for controller evaluation.",
    ),
    (
        "finalize",
        "Immediate compaction after validation: no.",
        "After validation, finalize without resuming the specialist session.",
    ),
))
def test_checkpoint_disposition_is_explicit_in_cumulative_prompt(
    disposition, compaction, lifecycle,
):
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
    ])
    session = make_session(gateway)

    session.request_checkpoint(
        "rendered request pressure", disposition=disposition,
    )

    prompt = json.loads(gateway.requests[0].messages)[-1]["content"]
    assert "Checkpoint reason: rendered request pressure." in prompt
    assert compaction in prompt
    assert lifecycle in prompt
    assert (
        "This checkpoint must be cumulative and self-contained because it may "
        "become a future epoch boundary."
    ) in prompt
    assert "remaining budget" not in prompt.lower()
    assert "Tool access is disabled for this checkpoint turn." in prompt
    assert "Do not emit native tool calls or XML/function-call markup." in prompt
    if disposition == "compact_resume":
        assert "tool access will be re-enabled" in prompt
    required = set(gateway.requests[0].response_schema["required"])
    if disposition == "compact_resume":
        assert required >= {"unresolved", "working_summary", "completed_steps"}
    else:
        assert required == {"unresolved"}


def test_initial_compact_resume_repairs_missing_working_memory_before_compaction():
    sparse = checkpoint_response(
        inspected=["old.py"], unresolved=["OB-tests"],
        working_summary="", completed_steps=[],
    )
    repaired = checkpoint_response(
        inspected=["old.py"],
        unresolved=["OB-tests"],
        working_summary="The prior epoch established the controller data flow.",
        completed_steps=["Read old.py and confirmed the reachable branch."],
    )
    gateway = ScriptedGateway([sparse, repaired])
    session = make_session(gateway, max_context_tokens=100_000)
    seed_successful_tool_exchange(
        session,
        call_id="working-memory-repair",
        path="old.py",
        content="full evidence retained until repair validates",
        reasoning="material conclusion retained until repair validates",
    )

    result = session.request_checkpoint(
        "context-pressure", disposition="compact_resume",
    )

    assert len(gateway.requests) == 2
    initial_prompt = json.loads(gateway.requests[0].messages)[-1]["content"]
    repair_prompt = json.loads(gateway.requests[1].messages)[-1]["content"]
    for prompt in (initial_prompt, repair_prompt):
        assert "non-empty working_summary" in prompt
        assert "non-empty completed_steps" in prompt
    assert set(gateway.requests[0].response_schema["required"]) >= {
        "unresolved", "working_summary", "completed_steps",
    }
    compact_properties = gateway.requests[0].response_schema["properties"]
    assert compact_properties["working_summary"]["minLength"] == 1
    assert compact_properties["completed_steps"]["minItems"] == 1
    assert result.degraded is False
    assert result.checkpoint.working_summary.startswith("The prior epoch")
    assert result.checkpoint.completed_steps == (
        "Read old.py and confirmed the reachable branch.",
    )
    assert any(
        event.get("epoch_continuation")
        for event in session.conversation.events
    )
    continuation = next(
        event["content"] for event in session.conversation.events
        if event.get("epoch_continuation")
    )
    assert "Tool access is re-enabled for exploration." in continuation
    continuation_payload = json.loads(continuation.split("catalogued IDs:\n", 1)[1])
    checkpoint_memory = continuation_payload["cumulative_checkpoint"]
    assert "coverage" not in checkpoint_memory
    assert "evidence_metadata" not in checkpoint_memory
    assert "obligation_statuses" not in checkpoint_memory
    assert "candidate_statuses" not in checkpoint_memory


def test_context_pressure_checkpoint_compacts_and_resumes_same_specialist():
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=["old.py"], unresolved=["OB-tests"],
            proposed_next_actions=["Inspect the remaining behavioral test."],
        ),
        checkpoint_response(inspected=["old.py"], unresolved=[]),
    ])
    session = make_session(
        gateway, max_context_tokens=100_000, max_tokens=1_024,
        model_turns=8, tool_calls=8,
    )
    pressure_checks = iter((True, False, False))
    session._checkpoint_pressure_due = lambda: next(pressure_checks, False)

    result = session.explore()

    assert result.degraded is False
    assert result.state.value == "checkpoint"
    assert [request.tools_enabled for request in gateway.requests] == [False, True]
    assert gateway.requests[1].messages_contain(
        "Tool access is re-enabled for exploration."
    )


def test_checkpoint_wrapper_is_accepted_without_repair():
    nested = json.loads(checkpoint_response(
        inspected=["a.py"], unresolved=["OB-tests"],
    ).text)
    gateway = ScriptedGateway([
        invalid_response(json.dumps({"checkpoint": nested})),
    ])
    session = make_session(gateway)

    result = session.request_checkpoint("controller-request")

    assert result.degraded is False
    assert len(gateway.requests) == 1
    assert "OB-tests" in result.checkpoint.unknowns


def test_no_tools_checkpoint_reports_model_emitted_tool_call():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway)

    result = session.request_checkpoint("context-pressure")

    assert [request.tools_enabled for request in gateway.requests] == [False, False]
    assert "tool calls returned while checkpoint tools were disabled" in (
        result.finalization_diagnostics[-1]["initial_error"]
    )


def test_checkpoint_projection_degradation_survives_finalization():
    gateway = ScriptedGateway([
        invalid_response("not-json"),
        invalid_response("still-not-json"),
    ])
    session = make_session(gateway)

    checkpoint = session.request_checkpoint("context-pressure")
    finalized = session.finalize()

    assert checkpoint.degraded is True
    assert finalized.degraded is True


def test_initial_compact_resume_without_working_memory_never_compacts():
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=["old.py"], unresolved=["OB-tests"],
            working_summary="", completed_steps=[],
        ),
        checkpoint_response(
            inspected=["old.py"], unresolved=["OB-tests"],
            working_summary="", completed_steps=[],
        ),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    seed_successful_tool_exchange(
        session,
        call_id="missing-working-memory",
        path="old.py",
        content="irreplaceable evidence before invalid checkpoint",
        reasoning="irreplaceable working conclusion before invalid checkpoint",
    )

    result = session.request_checkpoint(
        "context-pressure", disposition="compact_resume",
    )

    transcript = json.dumps(session.conversation.events, sort_keys=True)
    assert len(gateway.requests) == 2
    assert result.degraded is True
    assert "irreplaceable evidence before invalid checkpoint" in transcript
    assert "irreplaceable working conclusion before invalid checkpoint" in transcript
    assert session._checkpoint_spans == []
    assert not any(
        event.get("epoch_continuation")
        for event in session.conversation.events
    )


def test_sparse_pause_checkpoint_cannot_be_reused_for_destructive_compaction():
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=["0.py", "1.py", "2.py"],
            unresolved=["OB-tests"],
            working_summary="",
            completed_steps=[],
        ),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    for index in range(3):
        seed_successful_tool_exchange(
            session,
            call_id=f"sparse-pause-{index}",
            path=f"{index}.py",
            content=f"full sparse-pause evidence {index}",
            reasoning=f"unexternalized sparse-pause reasoning {index}",
        )
    paused = session.explore()
    before = json.loads(json.dumps(session.conversation.events))

    stats = session._compact_validated_epoch()

    assert paused.degraded is False
    assert stats.removed_reasoning == 0
    assert stats.replaced_results == 0
    assert session.conversation.events == before
    assert session._checkpoint_spans[-1].compacted is False
    assert paused.finalization_diagnostics[-1]["reason"] == "normal-completion"
    assert paused.finalization_diagnostics[-1]["compaction_level"] == "none"


def test_pause_checkpoint_retains_full_epoch_after_validation():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=["old.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    record = seed_successful_tool_exchange(
        session,
        call_id="call-old",
        path="old.py",
        content="full prior evidence",
    )

    result = session.request_checkpoint("normal-completion", disposition="pause")

    transcript = json.dumps(session.conversation.events, sort_keys=True)
    assert result.degraded is False
    assert "private analysis" in transcript
    assert "full prior evidence" in transcript
    assert '"status": "compacted"' not in transcript
    assert record.id not in session._compacted_evidence
    assert len(session._checkpoint_spans) == 1


def test_epoch_compaction_runs_only_after_compact_resume_checkpoint_validates():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=["0.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    records = [
        seed_successful_tool_exchange(
            session,
            call_id=f"call-{index}",
            path=f"{index}.py",
            content=f"complete evidence {index}",
            reasoning=f"private analysis {index}",
        )
        for index in range(4)
    ]

    session.request_checkpoint(
        "context-pressure", disposition="compact_resume",
    )

    request_messages = gateway.requests[0].messages
    assert "private analysis 0" in request_messages
    assert "complete evidence 0" in request_messages
    transcript = json.dumps(session.conversation.events, sort_keys=True)
    assert "private analysis 0" not in transcript
    assert "complete evidence 0" not in transcript
    assert "complete evidence 2" in transcript
    assert "complete evidence 3" in transcript
    assert records[0].id in session._compacted_evidence
    assert records[1].id in session._compacted_evidence
    assert records[2].id not in session._compacted_evidence
    assert records[3].id not in session._compacted_evidence
    continuation = [
        event for event in session.conversation.events
        if event.get("epoch_continuation")
    ]
    assert len(continuation) == 1
    assert records[0].id in continuation[0]["content"]
    assert '"proposed_next_actions"' in continuation[0]["content"]


def test_checkpoint_diagnostic_projects_admission_and_regular_compaction_counts():
    gateway = EstimatingGateway(
        [
            checkpoint_response(inspected=[], unresolved=["OB-tests"]),
            checkpoint_response(inspected=[], unresolved=["OB-tests"]),
        ],
        rendered_bytes=24_000,
        usages=(
            {"prompt_tokens": 9_000, "completion_tokens": 200},
            {"prompt_tokens": 8_500, "completion_tokens": 180},
        ),
    )
    session = make_session(gateway, max_context_tokens=100_000)
    seed_successful_tool_exchange(
        session,
        call_id="diagnostic-prior-epoch",
        path="prior.py",
        content="prior diagnostic evidence",
        reasoning="prior private diagnostic reasoning",
    )
    session.request_checkpoint("controller-request", disposition="pause")
    for index in range(4):
        seed_successful_tool_exchange(
            session,
            call_id=f"diagnostic-{index}",
            path=f"{index}.py",
            content=f"diagnostic evidence {index}",
            reasoning=f"private diagnostic reasoning {index}",
        )

    result = session.request_checkpoint(
        "context-pressure", disposition="compact_resume",
    )
    diagnostic = result.finalization_diagnostics[-1]

    assert diagnostic["reason"] == "context-pressure"
    assert diagnostic["disposition"] == "compact_resume"
    assert diagnostic["estimated_input_tokens"] >= 9_000
    assert diagnostic["provider_calibrated_input_tokens"] >= 9_000
    assert diagnostic["response_reserve_tokens"] == session.checkpoint_max_tokens
    assert diagnostic["repair_response_reserve_tokens"] == session.checkpoint_max_tokens
    assert diagnostic["admission_source"] == "provider-calibrated"
    assert diagnostic["compaction_level"] == "regular"
    assert diagnostic["compaction_input_tokens_before"] > 0
    assert diagnostic["compaction_input_tokens_after"] > 0
    assert diagnostic["removed_reasoning_messages"] == 5
    assert diagnostic["placeholder_replaced_results"] == 2
    assert diagnostic["removed_old_exchanges"] >= 1
    assert diagnostic["retained_full_results"] == 2
    assert diagnostic["emergency_outcome"] == "not_attempted"
    serialized = json.dumps(diagnostic, sort_keys=True)
    assert "private diagnostic reasoning" not in serialized
    assert "diagnostic evidence" not in serialized


def test_direct_completion_diagnostic_owns_later_pressure_compaction():
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=[],
            unresolved=["OB-tests"],
            working_summary="The initial controller checkpoint is complete.",
            completed_steps=["Recorded the initial controller boundary."],
        ),
        checkpoint_response(
            inspected=["0.py", "1.py", "2.py", "3.py"],
            unresolved=["OB-tests"],
            working_summary="The direct model completion retained the new epoch.",
            completed_steps=["Read four implementation paths and compared them."],
        ),
        checkpoint_response(
            inspected=["0.py", "1.py", "2.py", "3.py"],
            unresolved=["OB-tests"],
            working_summary="The resumed model completion retained the compacted epoch.",
            completed_steps=["Resumed from the compacted checkpoint boundary."],
        ),
    ])
    session = make_session(
        gateway, max_context_tokens=100_000, model_turns=8, tool_calls=8,
    )
    session.request_checkpoint("controller-request", disposition="pause")
    for index in range(4):
        seed_successful_tool_exchange(
            session,
            call_id=f"direct-diagnostic-{index}",
            path=f"{index}.py",
            content="large direct-completion evidence " + (str(index) * 2_000),
            reasoning=f"large direct-completion reasoning {index} " + ("r" * 2_000),
        )

    first_completion = session.explore()

    assert [item["reason"] for item in first_completion.finalization_diagnostics] == [
        "controller-request", "normal-completion",
    ]
    assert first_completion.finalization_diagnostics[0]["compaction_level"] == "none"
    assert first_completion.finalization_diagnostics[1]["disposition"] == "pause"

    session.max_context_tokens = 8_000
    resumed = session.explore()
    diagnostics = resumed.finalization_diagnostics

    assert [item["reason"] for item in diagnostics] == [
        "controller-request", "normal-completion", "normal-completion",
    ]
    assert diagnostics[0]["compaction_level"] == "none"
    assert diagnostics[1]["disposition"] == "pause"
    assert diagnostics[1]["compaction_level"] == "regular"
    assert diagnostics[1]["compaction_input_tokens_before"] > 0
    assert diagnostics[1]["compaction_input_tokens_after"] > 0
    assert diagnostics[2]["disposition"] == "pause"
    assert diagnostics[2]["compaction_level"] == "none"


def test_checkpoint_diagnostic_admission_keeps_initial_and_repair_reserves():
    gateway = EstimatingGateway(
        [
            invalid_response("invalid initial checkpoint"),
            checkpoint_response(inspected=[], unresolved=["OB-tests"]),
        ],
        rendered_bytes=12_000,
    )
    session = make_session(gateway, max_context_tokens=100_000)

    result = session.request_checkpoint("controller-request")
    diagnostic = result.finalization_diagnostics[-1]

    assert diagnostic["repair_attempted"] is True
    assert diagnostic["response_reserve_tokens"] == session.checkpoint_max_tokens
    assert diagnostic["repair_response_reserve_tokens"] == session.checkpoint_max_tokens


def test_epoch_compaction_rejects_invalid_checkpoint_and_failed_repair():
    gateway = ScriptedGateway([
        invalid_response("invalid checkpoint"),
        invalid_response("invalid repair"),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    seed_successful_tool_exchange(
        session,
        call_id="call-old",
        path="old.py",
        content="irreplaceable full evidence",
    )
    epoch_before = json.loads(json.dumps(session.conversation.events))

    result = session.request_checkpoint(
        "context-pressure", disposition="compact_resume",
    )

    assert result.degraded is True
    assert session.conversation.events[:len(epoch_before)] == epoch_before
    assert "irreplaceable full evidence" in json.dumps(session.conversation.events)
    assert session._checkpoint_spans == []
    assert not any(
        event.get("epoch_continuation")
        for event in session.conversation.events
    )


def test_resumed_safe_pause_checkpoint_keeps_full_prior_epoch():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=["old.py"], unresolved=["OB-tests"]),
        checkpoint_response(inspected=["old.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    seed_successful_tool_exchange(
        session,
        call_id="call-old",
        path="old.py",
        content="full paused evidence",
    )
    session.request_checkpoint("controller-request", disposition="pause")

    session.explore()

    assert len(gateway.requests) == 2
    assert gateway.requests[1].tools_enabled is True
    assert "full paused evidence" in json.dumps(session.conversation.events)
    assert not any(
        event.get("epoch_continuation")
        for event in session.conversation.events
    )


def test_resumed_pressure_pause_checkpoint_compacts_existing_boundary():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=["0.py"], unresolved=["OB-tests"]),
        checkpoint_response(inspected=["0.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    for index in range(4):
        seed_successful_tool_exchange(
            session,
            call_id=f"call-{index}",
            path=f"{index}.py",
            content="large paused evidence " + (str(index) * 2_000),
            reasoning=f"large private analysis {index} " + ("r" * 2_000),
        )
    session.request_checkpoint("controller-request", disposition="pause")
    request_count = len(gateway.requests)
    session.max_context_tokens = 8_000

    session.explore()

    assert len(gateway.requests) == request_count + 1
    assert gateway.requests[-1].tools_enabled is True
    assert any(
        event.get("epoch_continuation")
        for event in session.conversation.events
    )
    assert len(session._checkpoint_spans) >= 1


def test_resumed_compacted_checkpoint_does_not_bypass_unrelieved_repair_reserve():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    session.request_checkpoint(
        "context-pressure", disposition="compact_resume",
    )
    assert session._checkpoint_spans[-1].compacted is True
    request_count = len(gateway.requests)
    continuation = session._estimate_admission(
        tools_enabled=True,
        max_tokens=session.max_tokens,
    )
    session.max_context_tokens = continuation.admission_tokens + 1
    assert continuation.admission_tokens < session.max_context_tokens
    assert session._checkpoint_pressure_due() is True

    result = session.explore()

    assert len(gateway.requests) == request_count
    assert result.state.value == "checkpoint"
    assert result.degraded is True
    assert session._checkpoint_pressure_due() is True


def test_epoch_compaction_prunes_only_noncheckpoint_events_before_prior_boundary():
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=["first.py"],
            unresolved=["OB-tests"],
            working_summary="first cumulative checkpoint",
        ),
        checkpoint_response(
            inspected=["second.py"],
            unresolved=["OB-tests"],
            working_summary="second cumulative checkpoint",
        ),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    session.conversation.add_user("discardable epoch-zero note")
    removed_record = seed_successful_tool_exchange(
        session,
        call_id="call-epoch-zero",
        path="epoch-zero.py",
        content="evidence removed with the oldest epoch",
    )
    session.request_checkpoint("first-boundary", disposition="pause")
    session.conversation.add_user("previous epoch investigation")
    seed_successful_tool_exchange(
        session,
        call_id="call-between",
        path="between.py",
        content="previous epoch full evidence",
    )

    session.request_checkpoint("second-boundary", disposition="compact_resume")

    transcript = json.dumps(session.conversation.events, sort_keys=True)
    assert "discardable epoch-zero note" not in transcript
    assert "Immutable specialist assignment" in transcript
    assert "Checkpoint reason: first-boundary." in transcript
    assert "first cumulative checkpoint" in transcript
    assert "Checkpoint reason: second-boundary." in transcript
    assert "second cumulative checkpoint" in transcript
    assert removed_record.id in session._compacted_evidence
    assert session.conversation.open_tool_call_ids() == set()
    assert len(session._checkpoint_spans) == 2
    for span in session._checkpoint_spans:
        protected = session.conversation.events[
            span.request_start:span.response_end
        ]
        assert protected[0]["kind"] == "user"
        assert protected[-1]["kind"] == "assistant_turn_boundary"


def test_emergency_reconstruction_keeps_checkpoint_ledger_and_newest_exchange():
    seed_paths = ("a.py", "1.py", "2.py", "3.py")
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": path}, call_id=f"seed-{index}")
        for index, path in enumerate(seed_paths)
    ] + [
        candidate_checkpoint_response(("candidate-code",)),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(
        gateway,
        tool_calls=8,
        model_turns=10,
        max_context_tokens=100_000,
    )
    session.explore()
    filler_record = session.evidence_store.snapshot().records[0]
    for index in range(20):
        session._compacted_evidence[f"evidence:000-filler-{index:02d}"] = filler_record
    omitted_record = seed_successful_tool_exchange(
        session,
        call_id="post-checkpoint-old",
        path="oversized.py",
        content="oversized result " + ("y" * 8_000),
        reasoning="oversized new epoch reasoning " + ("x" * 40_000),
    )
    session.conversation.add_assistant_turn(
        content="newest complete analysis",
        calls=[{
            "id": "post-checkpoint-new",
            "name": "git_grep",
            "arguments": '{"pattern":"latest"}',
        }],
    )
    session.conversation.add_tool_result(
        "post-checkpoint-new", "newest fitting result",
    )
    session.max_context_tokens = 8_000

    session.explore()

    rendered = gateway.requests[-1].messages
    assert gateway.requests[-1].tools_enabled is True
    assert "Gather evidence and checkpoint progress." == session.conversation.system
    assert "Immutable specialist assignment" in rendered
    assert "candidate-code" in rendered
    assert "The changed branch exposes issue candidate-code." in rendered
    assert "compacted_evidence" in rendered
    assert "evidence:" in rendered
    assert "post-checkpoint-new" in rendered
    assert "newest fitting result" in rendered
    assert "post-checkpoint-old" not in rendered
    assert omitted_record.id in session._compacted_evidence
    assert omitted_record.id in rendered
    assert rendered.index("candidate-code") < rendered.index("post-checkpoint-new")
    assert rendered.index("post-checkpoint-new") < rendered.index(
        "Continue the same specialist assignment"
    )
    assert sum(
        bool(event.get("emergency_reconstruction"))
        for event in session.conversation.events
    ) == 1


def test_emergency_reconstruction_bounds_retained_evidence_metadata():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    session.request_checkpoint("controller-request", disposition="pause")
    for index in range(30):
        session.evidence_store.add_tool_result_with_collection(
            session_id=session.session_id,
            tool="read_file",
            arguments={"path": f"retained-{index}.py"},
            result={"status": "ok", "content": f"retained metadata {index}"},
        )
    session.recovery_evidence_bytes = 500

    assert session._reconstruct_from_valid_checkpoint() is True

    snapshot_event = next(
        event
        for event in session.conversation.events
        if event.get("kind") == "assistant_text"
        and '"cumulative_checkpoint"' in event.get("content", "")
    )
    snapshot = json.loads(snapshot_event["content"])
    metadata = snapshot["cumulative_checkpoint"]["evidence_metadata"]
    assert len(json.dumps(metadata, sort_keys=True).encode("utf-8")) <= 500
    assert len(metadata) < 30


def test_emergency_catalogue_prioritizes_newest_of_many_omitted_results():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    session.request_checkpoint("controller-request", disposition="pause")
    omitted_records = [
        seed_successful_tool_exchange(
            session,
            call_id=f"omitted-{index:02d}",
            path=f"omitted-{index:02d}.py",
            content=f"successful omitted evidence {index}",
            reasoning=f"oversized omitted reasoning {index} " + ("x" * 9_000),
        )
        for index in range(25)
    ]

    assert session._reconstruct_from_valid_checkpoint() is True

    snapshot_event = next(
        event
        for event in session.conversation.events
        if event.get("kind") == "assistant_text"
        and '"compacted_evidence"' in event.get("content", "")
    )
    snapshot = json.loads(snapshot_event["content"])
    catalogue_ids = [
        item["evidence_id"] for item in snapshot["compacted_evidence"]
    ]
    expected_newest_first = [
        record.id for record in reversed(omitted_records[-20:])
    ]
    assert catalogue_ids == expected_newest_first
    assert len(catalogue_ids) == 20
    assert omitted_records[-1].id in catalogue_ids
    assert all(
        record.id not in catalogue_ids for record in omitted_records[:5]
    )


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


def test_pressure_requests_checkpoint_before_exploration():
    gateway = EstimatingGateway(
        [
            checkpoint_response(inspected=[], unresolved=["OB-code", "OB-tests"]),
            checkpoint_response(inspected=[], unresolved=[]),
        ],
        rendered_bytes=12_000,
    )
    session = make_session(
        gateway,
        max_tokens=2_048,
        recovery_max_tokens=1_024,
        max_context_tokens=7_000,
    )
    attempts = RequestAttemptJournal()
    session.bind_request_attempt_journal(attempts, "assignment-1")

    old_coarse_admission = (
        session.conversation.approx_tokens()
        + session.max_tokens
        + session.wire_safety_tokens
    )
    exploration_admission = session._estimate_admission(
        tools_enabled=True, max_tokens=session.max_tokens,
    )
    assert old_coarse_admission < session.max_context_tokens
    assert exploration_admission.admission_tokens < session.max_context_tokens

    result = session.explore()

    assert result.state.value == "checkpoint"
    assert len(gateway.requests) == 2
    assert gateway.requests[0].tools_enabled is False
    assert gateway.requests[1].tools_enabled is True
    assert gateway.requests[0].max_tokens == 2_048
    assert gateway.requests[0].reasoning_effort == "none"
    assert gateway.requests[0].messages_contain(
        "After validation, resume the specialist session."
    )
    assert [item.purpose for item in attempts.close_since(0)] == [
        "checkpoint", "exploration",
    ]


def test_coarse_context_overflow_preserves_history_until_checkpoint_validates():
    session = make_session(
        ScriptedGateway([]), max_context_tokens=300, max_tokens=256,
    )
    retained_content = "coarse-overflow conclusion: " + ("x" * 5_000)
    session.conversation.add_assistant_text(retained_content)

    result = session.explore()

    assert result.degraded is True
    assert any(
        event.get("kind") == "assistant_text"
        and event.get("content") == retained_content
        for event in session.conversation.events
    )
    assert not any(
        event.get("compaction_note")
        for event in session.conversation.events
    )
    checkpoint_prompt = session.conversation.events[-1]["content"]
    assert "Checkpoint reason: context-pressure." in checkpoint_prompt
    assert "Immediate compaction after validation: yes." in checkpoint_prompt
    assert "After validation, resume the specialist session." in checkpoint_prompt


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
    assert '"candidate_finding_ids"' not in checkpoint_request
    assert '"candidate_findings"' in checkpoint_request
    assert "controller derives internal candidate handles" in checkpoint_request
    assert (
        "evidence_by_obligation"
        not in gateway.requests[1].response_schema["properties"]
    )


def test_truncated_empty_candidate_checkpoint_is_not_a_material_retention_signal():
    text = json.dumps({
        "candidate_findings": [],
        "candidate_updates": [],
        "new_candidates": [],
        "completed_steps": [
            "Reviewed contributor_candidate_ids propagation in adjudication.py",
        ],
    })[:-1]

    signal = _candidate_retention_signal(text)

    assert signal.is_material is False


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


def test_checkpoint_context_admission_failure_records_actionable_diagnostics():
    session = make_session(
        ScriptedGateway([]), max_context_tokens=300, max_tokens=256,
    )

    result = session.request_checkpoint("context-pressure")

    diagnostic = result.finalization_diagnostics[-1]
    assert diagnostic["initial_error"].startswith(
        "BudgetExhausted: model context limit"
    )
    assert diagnostic["context_tokens_before"] >= diagnostic["context_tokens_after"]
    assert diagnostic["max_context_tokens"] == 300
    assert diagnostic["requested_output_tokens"] == 256


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
    assert result.finalization_diagnostics
    diagnostic = result.finalization_diagnostics[-1]
    assert diagnostic["reason"] == "checkpoint-retention-reserve"
    assert diagnostic["initial_parse"] == "invalid"
    assert diagnostic["repair_attempted"] is True
    assert diagnostic["repair_parse"] == "invalid"
    assert diagnostic["material_candidate_signal"] is True
    assert "response" not in diagnostic


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

    evidence_id = canonical_evidence_key(
        "read_file", {"path": "a.py"},
        {"tool": "read_file", "status": "ok", "result": {"content": "implementation"}},
    )
    gateway = ScriptedGateway([
        tool_call_response(
            "read_file",
            {"path": "a.py", "obligation_ids": ["OB-code"]},
        ),
        checkpoint_response(
            inspected=["a.py"], unresolved=["OB-tests"],
            obligation_updates=[{
                "target": "O1", "disposition": "covered", "reason": "Verified.",
                "evidence_ids": [evidence_id], "next_actions": [],
            }],
        ),
    ])

    result = make_session(gateway, execute_tool=execute_tool).explore()

    assert seen[0][1] == {"path": "a.py"}
    assert dict(result.checkpoint.obligation_statuses)["OB-code"].value == "pending"
    assert result.checkpoint.obligation_assessments[0].disposition.value == "covered"
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


def test_denied_gh_api_repo_creates_durable_repository_access_request():
    seen = []
    revision = "a" * 40

    def execute_tool(name, arguments, **kwargs):
        seen.append((name, arguments, kwargs))
        return {
            "tool": name,
            "status": "error",
            "result": {"error": "Repo not allowed: 125m125/pr-reviewer-action"},
        }

    session = make_session(ScriptedGateway([]), execute_tool=execute_tool)
    call = lambda call_id: {
        "id": call_id,
        "name": "gh_api",
        "arguments": json.dumps({
            "endpoint": (
                f"repos/125m125/pr-reviewer-action/commits/{revision}"
            ),
            "purpose": "Verify token ghp_abcdefghijklmnopqrstuvwxyz1234567890",
            "targets": ["O1"],
        }),
    }

    session._execute_calls((call("gh-1"),))
    session._execute_calls((call("gh-2"),))

    assert len(seen) == 2
    assert all("purpose" not in arguments for _name, arguments, _kw in seen)
    assert len(session.source_access_requests) == 1
    request = session.source_access_requests[0]
    assert request.repository == "125m125/pr-reviewer-action"
    assert request.revision == revision
    assert request.obligation_id == "OB-code"
    assert "ghp_" not in request.model_purpose


@pytest.mark.parametrize("error", (
    "Missing GH_TOKEN",
    "Endpoint prefix not allowed: /repos/a/b/actions",
    "HTTP 404: not found",
))
def test_non_allowlist_gh_api_errors_do_not_create_access_requests(error):
    def execute_tool(name, arguments, **kwargs):
        return {"tool": name, "status": "error", "result": {"error": error}}

    session = make_session(ScriptedGateway([]), execute_tool=execute_tool)
    session._execute_calls(({
        "id": "gh-1",
        "name": "gh_api",
        "arguments": json.dumps({
            "endpoint": "repos/a/b/commits/" + "a" * 40,
            "targets": ["O1"],
        }),
    },))

    assert session.source_access_requests == ()


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


def test_recovery_rebases_checkpoint_span_before_later_pressure_compaction():
    gateway = ScriptedGateway([
        checkpoint_response(
            inspected=["a.py"],
            unresolved=["OB-tests"],
            working_summary="checkpoint before recovery",
        ),
        checkpoint_response(
            inspected=["after.py"],
            unresolved=["OB-tests"],
            working_summary="checkpoint after recovery",
        ),
    ])
    session = make_session(gateway, max_context_tokens=100_000)
    session.request_checkpoint("controller-request", disposition="pause")

    session.recover("repetitive-transcript")

    assert len(session._checkpoint_spans) == 1
    recovered_span = session._checkpoint_spans[0]
    assert recovered_span.response_end <= len(session.conversation.events)
    protected = session.conversation.events[
        recovered_span.request_start:recovered_span.response_end
    ]
    assert len(protected) == 1
    assert protected[0]["kind"] == "user"
    assert protected[0]["content"].startswith("Recovery reconstruction.")

    seed_successful_tool_exchange(
        session,
        call_id="after-recovery",
        path="after.py",
        content="evidence collected after recovery",
    )
    session.request_checkpoint(
        "context-pressure", disposition="compact_resume",
    )

    assert len(session._checkpoint_spans) == 2
    transcript = json.dumps(session.conversation.events, sort_keys=True)
    assert "Recovery reconstruction." in transcript
    assert "checkpoint after recovery" in transcript


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
        checkpoint_response(inspected=["a.py"], unresolved=[]),
    ])
    session = make_session(gateway, tool_calls=4, model_turns=8)

    result = session.explore()

    assert result.state.value == "checkpoint"
    assert result.budget.tool_calls == 1
    assert result.budget.model_turns == 5
    assert gateway.requests[3].tools_enabled is False
    assert "not a final report" in gateway.requests[3].messages.lower()
    assert gateway.requests[3].messages_contain(
        "Immediate compaction after validation: yes."
    )
    assert gateway.requests[3].messages_contain(
        "After validation, resume the specialist session."
    )


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
    assert "[REDACTED_VALUE]" in retained


def test_multi_path_diff_retains_separate_path_evidence_slices():
    def execute_tool(name, arguments):
        return {
            "tool": name,
            "status": "ok",
            "result": {"patches": [
                {"path": path, "status": "ok", "patch": f"+changed {path}"}
                for path in arguments["paths"]
            ]},
        }

    gateway = ScriptedGateway([
        tool_call_response("read_pr_diff", {"paths": ["a.py", "tests/test_a.py"]}),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
    ])
    session = make_session(gateway, execute_tool=execute_tool)

    session.explore()

    records = session.evidence_store.snapshot().records
    assert {record.source_path for record in records} == {"a.py", "tests/test_a.py"}
    tool_events = [
        item for item in session.conversation.events if item.get("kind") == "tool_result"
    ]
    assert len(json.loads(tool_events[-1]["content"])["evidence_slices"]) == 2


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


def test_inspected_path_alone_cannot_cover_any_obligation_scope():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=[]),
    ])
    session = make_session(gateway)

    result = session.explore()
    statuses = dict(result.checkpoint.obligation_statuses)

    assert statuses["OB-code"].value == "pending"
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
    raw["obligation_updates"] = [{
        "target": "O1", "disposition": "covered", "reason": "Verified.",
        "evidence_ids": [evidence_id], "next_actions": [],
    }]
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

    assert dict(result.checkpoint.obligation_statuses)["OB-unscoped"].value == "pending"
    assert result.checkpoint.obligation_assessments[0].disposition.value == "covered"


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


def test_finalization_uses_checkpoint_state_and_ignores_extra_provider_responses():
    gateway = ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["OB-code"]),
        invalid_response("this must never be requested"),
    ])
    session = make_session(gateway)
    session.explore()

    result = session.finalize()

    assert result.degraded is False
    assert result.report["source"] == "checkpoint-finalization"
    assert result.report["unknowns"] == ["OB-code", "OB-tests"]
    assert len(gateway.requests) == 1


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


def test_finalization_preserves_retention_unknown_as_degraded_checkpoint_state():
    session, _gateway = _session_with_retained_candidate([])
    session.latest_checkpoint = session._checkpoint_with_retention_unknown(
        session.latest_checkpoint,
    )

    result = session.finalize()

    assert result.degraded is True
    assert result.report["source"] == "checkpoint-finalization"
    assert "candidate-retention-unknown" in result.report["unknowns"]


def test_expired_lease_refuses_exploration_and_finalization_requests():
    lease = SessionLease(RunPhase.FOLLOWUP, deadline_at=0.0)
    explore_gateway = ScriptedGateway([])
    explore_session = make_session(explore_gateway, lease=lease)

    with pytest.raises(TimeoutError, match="session lease expired"):
        explore_session.explore()

    finalize_gateway = ScriptedGateway([])
    finalize_session = make_session(finalize_gateway, lease=lease)
    with pytest.raises(TimeoutError, match="session lease expired"):
        finalize_session.finalize()

    assert explore_gateway.requests == []
    assert finalize_gateway.requests == []
