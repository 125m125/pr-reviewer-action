"""Session-side delegation authority and retained ownership."""
import json
from dataclasses import replace
import pytest

from pr_reviewer.specialist_runtime.assignments import Assignment
from pr_reviewer.specialist_runtime.obligation_assessment import ObligationDisposition
from pr_reviewer.specialist_runtime.types import CoverageObligation
from test_specialist_runtime_session import (
    ScriptedGateway, checkpoint_response, make_component_group_session, make_session,
)


def owner_session(*, depth=0, paths=("a.py", "b.py")):
    obligation = CoverageObligation(
        "OB-owner", "component", "backend", owner_component_id="backend",
        scope=("a.py", "b.py"), required_evidence_categories=("implementation",),
    )
    assignment = Assignment(
        id="owner", title="Backend", objective="Review backend behavior",
        obligation_ids=(obligation.id,), recipe_ids=(), lenses=(), seed_paths=(),
        boundary_paths=("a.py", "b.py"), expected_evidence=(), estimated_turns=1,
        priority="normal", owner_component_id="backend", owned_changed_paths=paths,
        delegation_depth=depth, parent_assignment_id="parent" if depth else None,
    )
    return make_session(ScriptedGateway([]), assignment=assignment, obligations=(obligation,))


def tool_names(session):
    return {item["name"] for item in session.conversation.tool_schemas}


def test_global_budget_admission_rejects_before_local_reservation():
    from pr_reviewer.specialist_runtime.budget import BudgetExhausted
    session = owner_session()
    calls = []
    def reject(kind, count):
        calls.append((kind, count))
        raise BudgetExhausted("global cap exhausted")
    session.bind_global_budget_admission_handler(reject)
    with pytest.raises(BudgetExhausted, match="global cap"):
        session._reserve_model_turn()
    with pytest.raises(BudgetExhausted, match="global cap"):
        session._reserve_tool_calls(2)
    assert calls == [("model_turn", 1), ("tool_calls", 2)]
    assert session.budget.snapshot().model_turns == 0
    assert session.budget.snapshot().tool_calls == 0


def test_local_exhaustion_does_not_charge_global_budget():
    from pr_reviewer.specialist_runtime.budget import BudgetExhausted
    session = owner_session()
    calls = []
    session.bind_global_budget_admission_handler(lambda kind, count: calls.append((kind, count)))
    session.budget.reserve_tool_calls(session.budget.remaining_tool_calls())
    with pytest.raises(BudgetExhausted):
        session._reserve_tool_calls(1)
    assert calls == []


def test_delegation_requires_controller_binding_and_primary_owner():
    session = owner_session()
    assert "request_delegation" not in tool_names(session)
    session.bind_delegation_request_handler(lambda request: {"status": "rejected", "reason": "capacity"})
    assert "request_delegation" in tool_names(session)
    child = owner_session(depth=1, paths=("b.py",))
    child.bind_delegation_request_handler(lambda request: {"status": "queued"})
    assert "request_delegation" not in tool_names(child)


def test_queued_delegation_updates_local_scope_and_keeps_global_inventory():
    session = owner_session()
    parent = replace(session.assignment, owned_changed_paths=("a.py",))
    session.bind_delegation_request_handler(lambda request: {
        "status": "queued", "request_id": "delegation-1", "reason": "admitted",
        "parent_assignment": parent,
    })
    assert session._execute_obligation_tool("d1", "request_delegation", {
        "question": "Check independent b behavior", "reason": "Separate operation",
        "changed_paths": ["b.py"],
    })
    receipt = json.loads(session.conversation.events[-1]["content"])
    assert receipt["status"] == "queued"
    assert receipt["owned_changed_paths"] == ["a.py"]
    assert "parent_assignment" not in receipt
    assert session.obligation_assessments.explain("O1")["scope"] == ["a.py"]
    assert session.coverage.obligations()[0].scope == ("a.py", "b.py")
    assert '"owned_changed_paths": ["a.py"]' in session._assignment_prompt()
    assert "delegation-1" in session._assignment_prompt()


def test_child_local_assessment_scope_does_not_claim_parent_paths():
    session = owner_session(depth=1, paths=("b.py",))
    assert session.obligation_assessments.explain("O1")["scope"] == ["b.py"]
    assert session.coverage.obligations()[0].scope == ("a.py", "b.py")


@pytest.mark.parametrize("child", [False, True])
def test_checkpoint_contract_only_advertises_current_owned_scope(child):
    session = owner_session(depth=1 if child else 0, paths=("b.py",) if child else ("a.py", "b.py"))
    if not child:
        parent = replace(session.assignment, owned_changed_paths=("a.py",))
        session.bind_delegation_request_handler(lambda request: {
            "status": "queued", "request_id": "delegation-1", "parent_assignment": parent,
        })
        assert session._execute_obligation_tool("d1", "request_delegation", {
            "question": "Check b behavior", "reason": "Separate operation", "changed_paths": ["b.py"],
        })
    contract, _ = json.JSONDecoder().raw_decode(
        session._checkpoint_obligation_contract().split(": ", 1)[1],
    )
    assert contract["pending_obligations"][0]["owned_changed_paths"] == (["b.py"] if child else ["a.py"])
    assert session.coverage.obligations()[0].scope == ("a.py", "b.py")


@pytest.mark.parametrize("withdraw", [False, True])
def test_reconstruction_preserves_assessments_accepted_after_checkpoint(withdraw):
    session = make_component_group_session(ScriptedGateway([
        checkpoint_response(inspected=[], unresolved=["O1"]),
    ]))
    session._execute_calls(({
        "id": "read-a", "name": "read_file",
        "arguments": json.dumps({"path": "backend/a.py", "targets": ["O1"]}),
    },))
    evidence_id = json.loads(session.conversation.events[-1]["content"])["evidence_id"]
    proposal = {
        "target": "O1", "disposition": "partially_covered",
        "reason": "The first handler preserves validated request identity.",
        "assessed_paths": ["backend/a.py"], "omitted_paths": [],
        "evidence_ids": [evidence_id], "next_actions": ["Inspect backend/b.py retry behavior."],
        "defect_assessment": {
            "result": "none_observed", "summary": "No concrete defect observed.", "candidate_drafts": [],
        },
    }
    if withdraw:
        assert session._execute_obligation_tool("assess-a", "propose_obligation_resolution", proposal)
    session.request_checkpoint("controller-request", disposition="pause")
    if withdraw:
        proposal.update(
            reason="The first handler still needs its identity assumptions checked.",
            assessed_paths=[], omitted_paths=["backend/a.py"],
            next_actions=["Recheck backend/a.py identity assumptions."],
        )
    assert session._execute_obligation_tool("new-assessment", "propose_obligation_resolution", proposal)
    accepted = session.obligation_assessments.assessment("O1")

    assert session._reconstruct_from_valid_checkpoint()

    assert session.obligation_assessments.assessment("O1") == accepted
    memory = session._model_checkpoint_memory()["obligation_assessments"][0]
    assert memory["assessment_version"] == (2 if withdraw else 1)
    assert memory["assessed_paths"] == ([] if withdraw else ["backend/a.py"])
    assert memory["evidence_ids"] == [evidence_id]
    assert memory["next_actions"] == proposal["next_actions"]


def test_rebinding_keeps_handles_and_does_not_promote_partial_work():
    session = owner_session()
    ledger = session.obligation_assessments
    assessment = replace(ledger.assessment("O1"),
        disposition=ObligationDisposition.PARTIALLY_COVERED,
        assessed_paths=("a.py",), omitted_paths=("b.py",), assessment_version=1)
    ledger.restore((assessment,))
    local = replace(session.coverage.obligations()[0], scope=("a.py",))
    ledger.replace_owned_obligations((local,), (local.id,))
    assert ledger.handles() == ("O1",)
    assert ledger.assessment("O1").disposition is ObligationDisposition.PARTIALLY_COVERED
    assert ledger.assessment("O1").omitted_paths == ()


def test_rebinding_does_not_recycle_removed_target_handles():
    from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessmentLedger
    first = CoverageObligation("first", "component", "backend")
    second = CoverageObligation("second", "boundary", "contract")
    ledger = ObligationAssessmentLedger(session_id="owner", obligations=(first, second), obligation_ids=(first.id, second.id))
    ledger.replace_owned_obligations((second,), (second.id,))
    assert ledger.handles() == ("O2",)
    assert ledger.obligation_id("O1") is None
    assert ledger.obligation_id("O2") == "second"


def test_real_pathless_empty_grep_can_support_boundary_not_affected_only():
    from pr_reviewer.specialist_runtime.evidence import EvidenceProvenance, EvidenceStore
    from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessmentLedger
    store = EvidenceStore()
    record, _ = store.add_tool_result_with_collection(
        session_id="S", tool="git_grep", arguments={"pattern": "ChangedEvent"},
        result={"status": "ok", "result": {"matches": []}},
        provenance=EvidenceProvenance(head_sha="a" * 40),
    )
    obligation = CoverageObligation("B", "boundary-participant", "worker",
        boundary_id="messages", participant_id="worker", owner_component_id="worker",
        seed_hints=("worker/**",), required_evidence_categories=("implementation",))
    ledger = ObligationAssessmentLedger(session_id="S", obligations=(obligation,), obligation_ids=("B",))
    accepted = ledger.propose(target="O1", disposition="not_applicable",
        reason="Searched ChangedEvent references; no worker usage found in the checks performed.",
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(), eligible=lambda *_: False)
    assert accepted.accepted
    assert not ledger.propose(target="O1", disposition="covered", reason="Found compatible worker behavior.",
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(), eligible=lambda *_: False).accepted


def test_empty_search_requires_bound_successful_untruncated_source():
    from pr_reviewer.specialist_runtime.evidence import EvidenceProvenance, EvidenceStore
    from pr_reviewer.specialist_runtime.obligation_assessment import is_retained_empty_repository_search
    store = EvidenceStore()
    record, _ = store.add_tool_result_with_collection(session_id="S", tool="git_grep",
        arguments={"pattern": "ChangedEvent"}, result={"status": "ok", "matches": []},
        provenance=EvidenceProvenance(head_sha="a" * 40))
    assert is_retained_empty_repository_search(record)
    assert not is_retained_empty_repository_search(replace(record, truncated=True))
    assert not is_retained_empty_repository_search(replace(record, status="error"))
    assert not is_retained_empty_repository_search(replace(record, provenance=EvidenceProvenance()))
    assert not is_retained_empty_repository_search(replace(record, content='{"matches":["one match"]}'))
