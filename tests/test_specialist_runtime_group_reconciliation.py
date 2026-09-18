from dataclasses import replace

from pr_reviewer.specialist_runtime.coverage import CoverageLedger, reconcile_wave, session_ownership_for_assignment
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessment, ObligationDisposition
from pr_reviewer.specialist_runtime.types import CoverageObligation, ObligationStatus, SessionCheckpoint, SessionState, SpecialistAssignment


def test_component_evidence_does_not_claim_full_coverage_without_an_assessment():
    obligation = CoverageObligation("owner", "component", "backend", owner_component_id="backend", scope=("a.py", "b.py"))
    ledger = CoverageLedger((obligation,))
    ledger.attach_evidence("owner", "E1")
    assert ledger.obligation_statuses()["owner"] is ObligationStatus.PENDING


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
