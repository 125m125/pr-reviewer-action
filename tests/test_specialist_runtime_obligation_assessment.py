from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.types import CoverageObligation


def _store_with_path(path: str = "workflow.yml"):
    store = EvidenceStore()
    record, _collection = store.add_tool_result_with_collection(
        session_id="session-1",
        tool="read_pr_diff",
        arguments={"path": path},
        result={"status": "ok", "content": "changed behavior"},
    )
    return store, record


def _ledger(*, risk_tier="normal"):
    from pr_reviewer.specialist_runtime.obligation_assessment import (
        ObligationAssessmentLedger,
    )

    obligation = CoverageObligation(
        "OB-workflow", "recipe", "delivery:workflow",
        required_evidence_categories=("workflow",),
        scope=("workflow.yml",), seed_hints=("pom.xml",),
        explanation="Trace workflow behavior.", risk_tier=risk_tier,
    )
    return ObligationAssessmentLedger(
        session_id="session-1", obligations=(obligation,),
        obligation_ids=(obligation.id,),
    )


def test_not_applicable_requires_changed_state_evidence_and_closes_target():
    ledger = _ledger()
    store, record = _store_with_path()

    result = ledger.propose(
        target="O1", disposition="not_applicable",
        reason="No build manifest or build command changed.",
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert result.accepted is True
    assert ledger.assessment("O1").disposition.value == "not_applicable"
    assert ledger.open_targets() == ()


def test_covered_requires_eligible_retained_evidence():
    ledger = _ledger()
    store, record = _store_with_path()

    rejected = ledger.propose(
        target="O1", disposition="covered", reason="Wiring is correct.",
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: False,
    )
    accepted = ledger.propose(
        target="O1", disposition="covered", reason="Wiring is correct.",
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert rejected.accepted is False
    assert "eligible" in rejected.reason
    assert accepted.accepted is True
    assert ledger.assessment("O1").evidence_ids == (record.id,)


def test_covered_accepts_eligible_subset_and_ignores_supplemental_evidence():
    ledger = _ledger()
    store, direct = _store_with_path()
    supplemental, _collection = store.add_tool_result_with_collection(
        session_id="session-1",
        tool="read_pr_diff",
        arguments={"path": "tests/test_workflow.py"},
        result={"status": "ok", "content": "supporting test"},
    )

    result = ledger.propose(
        target="O1", disposition="covered", reason="Wiring is correct.",
        evidence_ids=(direct.id, supplemental.id), next_actions=(),
        evidence=store.snapshot(),
        eligible=lambda record, _obligation: record.id == direct.id,
    )

    assert result.accepted is True
    assert result.eligible_evidence_ids == (direct.id,)
    assert result.ignored_supplemental_evidence_ids == (supplemental.id,)
    assert ledger.assessment("O1").evidence_ids == (direct.id,)


def test_unresolved_requires_a_novel_concrete_next_action():
    ledger = _ledger()
    store, _record = _store_with_path()

    missing = ledger.propose(
        target="O1", disposition="unresolved", reason="More work remains.",
        evidence_ids=(), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )
    first = ledger.propose(
        target="O1", disposition="unresolved", reason="Trace the consumer.",
        evidence_ids=(), next_actions=("read consumer.py diff",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )
    repeated = ledger.propose(
        target="O1", disposition="unresolved", reason="Still trace it.",
        evidence_ids=(), next_actions=("read consumer.py diff",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )

    assert missing.accepted is False
    assert first.accepted is True
    assert repeated.accepted is False
    assert "novel" in repeated.reason


def test_unknown_or_unowned_target_is_rejected_without_state_change():
    ledger = _ledger()
    store, _record = _store_with_path()

    result = ledger.propose(
        target="O99", disposition="blocked", reason="Unavailable.",
        evidence_ids=(), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert result.accepted is False
    assert ledger.open_targets() == ("O1",)


def test_consuming_followup_action_prevents_same_gap_from_being_resumed_again():
    ledger = _ledger()
    store, _record = _store_with_path()
    result = ledger.propose(
        target="O1", disposition="unresolved", reason="Trace the consumer.",
        evidence_ids=(), next_actions=("read consumer.py diff",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )
    assert result.accepted is True

    ledger.consume_next_actions(("OB-workflow",))

    assert ledger.assessment("O1").next_actions == ()
    assert ledger.assessment("O1").attempts[0].next_actions == (
        "read consumer.py diff",
    )


def test_normal_risk_accepts_only_one_unresolved_followup_attempt():
    ledger = _ledger()
    store, _record = _store_with_path()

    first = ledger.propose(
        target="O1", disposition="unresolved", reason="Trace the consumer.",
        evidence_ids=(), next_actions=("read consumer.py diff",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )
    second = ledger.propose(
        target="O1", disposition="unresolved", reason="Inspect its test.",
        evidence_ids=(), next_actions=("read consumer test",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )

    assert first.accepted is True
    assert second.accepted is False
    assert "attempt limit" in second.reason


def test_high_risk_accepts_one_additional_distinct_followup_attempt():
    ledger = _ledger(risk_tier="high")
    store, _record = _store_with_path()

    results = [
        ledger.propose(
            target="O1", disposition="unresolved", reason=f"Attempt {index}.",
            evidence_ids=(), next_actions=(f"read path {index}",),
            evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
        )
        for index in range(1, 4)
    ]

    assert [item.accepted for item in results] == [True, True, False]
    assert "attempt limit" in results[-1].reason


def test_attempt_records_bounded_evidence_before_after_and_delta():
    ledger = _ledger()
    store, record = _store_with_path()

    ledger.propose(
        target="O1", disposition="unresolved", reason="Inspect the consumer.",
        evidence_ids=(record.id,), next_actions=("read consumer.py",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )

    attempt = ledger.assessment("O1").attempts[-1]
    assert attempt.evidence_before_count == 0
    assert attempt.evidence_after_count == 1
    assert attempt.evidence_delta == 1
