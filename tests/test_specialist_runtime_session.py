import json
import time
from dataclasses import dataclass

import pytest

from pr_reviewer.conversation import Conversation
from pr_reviewer.specialist_runtime.budget import BudgetLedger, SessionLease
from pr_reviewer.specialist_runtime.assignments import Assignment
from pr_reviewer.specialist_runtime.coverage import CoverageLedger
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.model_gateway import ModelTurnResult
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


@dataclass(frozen=True)
class RecordedRequest:
    messages: str
    tools_enabled: bool
    deadline_at: float | None

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
        ))
        assert self.responses, "model called more times than scripted"
        return self.responses.pop(0)


def final_response(summary="reviewed", recommendation="approve"):
    text = json.dumps({
        "summary": summary,
        "recommendation": recommendation,
        "candidate_finding_ids": [],
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


def make_session(
    gateway, *, tool_calls=4, model_turns=8, recoveries=1,
    execute_tool=None, lease=None, assignment=None,
):
    obligations = (
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
        budget=BudgetLedger(BudgetLimits(
            model_turns=model_turns, tool_calls=tool_calls, recoveries=recoveries,
        )),
        lease=lease, request_timeout_sec=30.0, max_tokens=1024,
    )


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
    assert result.report["unknowns"] == ["OB-code"]


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
