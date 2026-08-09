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
from pr_reviewer.specialist_runtime.session import (
    COMPACTED_EVIDENCE_TOOL_NAME,
    SpecialistSession,
    _resolve_retained_evidence_id,
    _rewrite_rationale_evidence_ids,
)
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


def checkpoint_response(*, inspected, unresolved, **overrides):
    payload = {
        "inspected": inspected,
        "unresolved": unresolved,
        "hypotheses": [],
        "candidate_finding_ids": [],
        "invariants_evaluated": [],
        "unknowns": unresolved,
        "proposed_next_actions": [],
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


def test_compaction_registers_shrunk_tool_results_for_bounded_retrieval():
    session = make_session(ScriptedGateway([]), max_context_tokens=10)
    record, _collection = session.evidence_store.add_tool_result_with_collection(
        session_id=session.session_id,
        tool="read_file",
        arguments={"path": "a.py"},
        result={"status": "ok", "content": "important-tail\n" + ("x" * 5_000)},
    )
    session.conversation.add_assistant_tool_calls(({
        "id": "call-old",
        "name": "read_file",
        "arguments": json.dumps({"path": "a.py"}),
    },))
    session._tool_call_evidence_ids["call-old"] = record.id
    session.conversation.add_tool_result(
        "call-old",
        {"evidence_id": record.id, "content": record.content},
        max_bytes=100,
    )

    session._compact_conversation()

    assert record.id in session._compacted_evidence
    assert any(
        event.get("compaction_note") and record.id in event["content"]
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
            "evidence_id": record.id, "offset": 0, "limit": 100,
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
        "arguments": json.dumps({"evidence_id": "evidence:not-compacted"}),
    }
    session._execute_calls((rejected,))
    assert "not marked as compacted" in session.conversation.events[-1]["content"]


def test_compaction_marks_old_assistant_analysis_instead_of_prefix_truncating():
    session = make_session(ScriptedGateway([]), max_context_tokens=1_000)
    session.conversation.add_assistant_text("old conclusion: " + ("x" * 5_000))
    session.conversation.add_assistant_text("new conclusion: " + ("y" * 5_000))

    session._compact_conversation()

    assert any(
        event.get("compaction_note")
        and "Older assistant analysis was compacted" in event["content"]
        for event in session.conversation.events
    )


@dataclass(frozen=True)
class RecordedRequest:
    messages: str
    tools_enabled: bool
    deadline_at: float | None
    max_tokens: int
    ephemeral_user_note: str | None
    reasoning_effort: str | None

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
    assert payload["coverage"]["obligation_statuses"]["OB-code"] == "covered"
    assert payload["evidence_metadata"][0]["id"].startswith("evidence:")
    assert "content" not in payload["evidence_metadata"][0]


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
    assert '"candidate_finding_ids"' not in checkpoint_request
    assert '"candidate_findings"' in checkpoint_request
    assert "controller derives internal candidate handles" in checkpoint_request


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
