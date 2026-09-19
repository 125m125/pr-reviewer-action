from dataclasses import replace

import pytest

from pr_reviewer.specialist_runtime.coverage import CoverageLedger, reconcile_wave, session_ownership_for_assignment
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessment, ObligationDisposition
from pr_reviewer.specialist_runtime.types import CoverageObligation, ObligationStatus, SessionCheckpoint, SessionState, SpecialistAssignment


def test_component_evidence_does_not_claim_full_coverage_without_an_assessment():
    obligation = CoverageObligation("owner", "component", "backend", owner_component_id="backend", scope=("a.py", "b.py"))
    ledger = CoverageLedger((obligation,))
    ledger.attach_evidence("owner", "E1")
    assert ledger.obligation_statuses()["owner"] is ObligationStatus.PENDING


def test_inspection_globs_do_not_widen_finding_proof():
    from pr_reviewer.specialist_runtime.coverage import evidence_satisfies_obligation, _assessment_evidence_satisfies
    obligation = CoverageObligation("owner", "component", "backend", owner_component_id="backend", scope=("a.py",), seed_hints=("consumers/**",), required_evidence_categories=("tool-result",))
    store = EvidenceStore()
    record = store.add_tool_result(session_id="S1", tool="read_file", arguments={"path": "consumers/other.py"}, result={"status": "ok", "content": "reference"})
    assert _assessment_evidence_satisfies(record, obligation)
    assert not evidence_satisfies_obligation(record, obligation)


def test_component_reconciliation_preserves_partial_work_and_rejects_false_completion():
    obligation = CoverageObligation("owner", "component", "backend", owner_component_id="backend", scope=("a.py", "b.py"), required_evidence_categories=("tool-result",))
    assignment = SpecialistAssignment("A1", "Review backend", primary_obligation_ids=("owner",))
    ownership = session_ownership_for_assignment(assignment, (obligation,), session_id="S1")
    store = EvidenceStore()
    record = store.add_tool_result(session_id="S1", tool="read_file", arguments={"path": "a.py"}, result={"status": "ok", "content": "changed behavior"})
    assessment = ObligationAssessment("O1", "owner", ObligationDisposition.COVERED, "Checked the changed request handling.", (record.id,), assessed_paths=("a.py",))
    checkpoint = SessionCheckpoint("S1", SessionState.CHECKPOINT, evidence_ids=(record.id,), obligation_assessments=(assessment,))
    ledger = CoverageLedger((obligation,))

    def reconcile(checkpoint):
        return reconcile_wave(ledger, wave_start_coverage=ledger.snapshot(), checkpoints=(checkpoint,), evidence=store.snapshot(), assignments=(assignment,), session_ownership=(ownership,))

    result = reconcile(checkpoint)
    assert dict(result.snapshot.obligation_statuses)["owner"] is ObligationStatus.PARTIALLY_COVERED
    assert result.uncovered_obligation_ids == ("owner",)
    assert dict(result.snapshot.evidence_by_obligation)["owner"] == (record.id,)
    invalid = replace(assessment, assessed_paths=("outside.py",))
    result = reconcile(replace(checkpoint, obligation_assessments=(invalid,)))
    assert dict(result.snapshot.obligation_statuses)["owner"] is ObligationStatus.PARTIALLY_COVERED
    complete = replace(assessment, assessed_paths=("a.py", "b.py"))
    result = reconcile(replace(checkpoint, obligation_assessments=(complete,)))
    assert dict(result.snapshot.obligation_statuses)["owner"] is ObligationStatus.COVERED
    assert result.uncovered_obligation_ids == ()


def test_component_legacy_checkpoint_cannot_close_group_from_evidence_alone():
    obligation = CoverageObligation("owner", "component", "backend", owner_component_id="backend", scope=("a.py",), required_evidence_categories=("tool-result",))
    assignment = SpecialistAssignment("A1", "Review backend", primary_obligation_ids=("owner",))
    store = EvidenceStore()
    record = store.add_tool_result(session_id="S1", tool="read_file", arguments={"path": "a.py"}, result={"status": "ok", "content": "source"})
    ledger = CoverageLedger((obligation,))
    result = reconcile_wave(ledger, wave_start_coverage=ledger.snapshot(), checkpoints=(SessionCheckpoint("S1", SessionState.CHECKPOINT, evidence_ids=(record.id,)),), evidence=store.snapshot(), assignments=(assignment,), session_ownership=(session_ownership_for_assignment(assignment, (obligation,), session_id="S1"),))
    assert dict(result.snapshot.obligation_statuses)["owner"] is ObligationStatus.PENDING


def test_disjoint_parent_child_assessments_cover_only_their_combined_owned_paths():
    obligation = CoverageObligation("owner", "component", "backend", owner_component_id="backend", scope=("a.py", "b.py"), required_evidence_categories=("tool-result",))
    parent = SpecialistAssignment("parent", "Parent", primary_obligation_ids=("owner",), owned_changed_paths=("a.py",))
    child = SpecialistAssignment("child", "Child", primary_obligation_ids=("owner",), owned_changed_paths=("b.py",), parent_assignment_id="parent", delegation_depth=1)
    store = EvidenceStore()
    checkpoints = []
    for assignment, path in ((parent, "a.py"), (child, "b.py")):
        record = store.add_tool_result(session_id=assignment.assignment_id, tool="read_file", arguments={"path": path}, result={"status": "ok", "content": "changed behavior"})
        checkpoints.append(SessionCheckpoint(assignment.assignment_id, SessionState.CHECKPOINT, obligation_assessments=(ObligationAssessment("O1", "owner", ObligationDisposition.COVERED, "Checked the owned changed behavior.", (record.id,), assessed_paths=(path,)),)))
    ledger = CoverageLedger((obligation,))
    kwargs = dict(evidence=store.snapshot(), assignments=(parent, child), session_ownership=tuple(session_ownership_for_assignment(a, (obligation,), session_id=a.assignment_id) for a in (parent, child)))
    first = reconcile_wave(ledger, wave_start_coverage=ledger.snapshot(), checkpoints=checkpoints[:1], **kwargs)
    assert dict(first.snapshot.obligation_statuses)["owner"] is ObligationStatus.PARTIALLY_COVERED
    completed = reconcile_wave(ledger, wave_start_coverage=ledger.snapshot(), checkpoints=checkpoints, **kwargs)
    assert dict(completed.snapshot.obligation_statuses)["owner"] is ObligationStatus.COVERED
def test_integrated_recipe_status_tracks_its_owner_without_extra_obligations():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
    from pr_reviewer.specialist_runtime.policy import parse_review_policy
    from pr_reviewer.specialist_runtime.types import ObligationStatus
    policy = parse_review_policy({"version": 3, "components": [{"id": "app", "paths": ["app/**"]}], "recipes": [{"id": "lifecycle", "execution": "integrated", "match": {"component_ids_any": ["app"]}}]})
    obligations = derive_obligations({"changed_files": ["app/a.py"], "components": [{"id": "app", "changed_files": ["app/a.py"]}]}, {}, policy)
    assert len(obligations) == 1
    ledger = CoverageLedger(obligations)
    assert ledger.recipe_statuses()["lifecycle"] == "assigned"
    ledger.replace_reconciled_state({}, (), {obligations[0].id: ObligationStatus.PARTIALLY_COVERED})
    assert ledger.recipe_statuses()["lifecycle"] == "partially_covered"
    ledger.replace_reconciled_state({}, (), {obligations[0].id: ObligationStatus.COVERED})
    assert ledger.recipe_statuses()["lifecycle"] == "covered"


@pytest.mark.parametrize("latest", ["covered", "unresolved"])
@pytest.mark.parametrize("split", [False, True])
def test_latest_accepted_checkpoint_wins_over_reverse_lexical_session_ids(latest, split):
    obligation = CoverageObligation(
        "owner", "component", "backend", owner_component_id="backend",
        scope=("a.py", "b.py"), required_evidence_categories=("tool-result",),
    )
    parent = SpecialistAssignment(
        "parent", "Review backend", primary_obligation_ids=("owner",),
        owned_changed_paths=("a.py",) if split else obligation.scope,
    )
    child = SpecialistAssignment(
        "child", "Review b.py", primary_obligation_ids=("owner",),
        owned_changed_paths=("b.py",), parent_assignment_id="parent", delegation_depth=1,
    )
    store = EvidenceStore()
    records = {
        path: store.add_tool_result(
            session_id="z-initial", tool="read_file", arguments={"path": path},
            result={"status": "ok", "content": "changed behavior"},
        ) for path in obligation.scope
    }
    paths = parent.owned_changed_paths

    def checkpoint(session_id, disposition, assessed):
        return SessionCheckpoint(
            session_id, SessionState.CHECKPOINT,
            obligation_assessments=(ObligationAssessment(
                "O1", "owner", ObligationDisposition(disposition),
                "Checked behavior" if disposition == "covered" else "Retry behavior remains uncertain",
                tuple(records[path].id for path in assessed),
                assessed_paths=assessed,
                next_actions=() if disposition == "covered" else ("Inspect retry handling",),
            ),),
        )

    initial = "unresolved" if latest == "covered" else "covered"
    checkpoints = [
        checkpoint("z-initial", initial, paths),
        checkpoint("a-followup", latest, paths),
    ]
    ownership = [
        session_ownership_for_assignment(parent, (obligation,), session_id=session_id)
        for session_id in ("z-initial", "a-followup")
    ]
    if split:
        checkpoints.insert(1, checkpoint("m-child", "covered", ("b.py",)))
        ownership.append(session_ownership_for_assignment(child, (obligation,), session_id="m-child"))
    ledger = CoverageLedger((obligation,))
    result = reconcile_wave(
        ledger, wave_start_coverage=ledger.snapshot(), checkpoints=checkpoints,
        evidence=store.snapshot(), assignments=(parent, child) if split else (parent,),
        session_ownership=ownership,
    )
    expected = "partially_covered" if split and latest == "unresolved" else latest
    assert dict(result.snapshot.obligation_statuses)["owner"].value == expected
    assert records["a.py"].id in dict(result.snapshot.evidence_by_obligation)["owner"]
