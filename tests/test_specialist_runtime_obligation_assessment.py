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


def _ledger(
    *,
    risk_tier="normal",
    scope=("workflow.yml",),
    owner_component_id="",
    boundary_id="",
    evaluator_owned=False,
    evidence_requirements=(),
):
    from pr_reviewer.specialist_runtime.obligation_assessment import (
        ObligationAssessmentLedger,
    )

    obligation = CoverageObligation(
        "OB-workflow", "recipe", "delivery:workflow",
        required_evidence_categories=("workflow",),
        scope=scope, seed_hints=("pom.xml",),
        explanation="Trace workflow behavior.", risk_tier=risk_tier,
        owner_component_id=owner_component_id,
        boundary_id=boundary_id,
        evaluator_owned=evaluator_owned,
        evidence_requirements=evidence_requirements,
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


def test_closed_disposition_rejects_next_actions():
    for disposition in ("covered", "not_applicable"):
        ledger = _ledger()
        store, record = _store_with_path()

        result = ledger.propose(
            target="O1", disposition=disposition,
            reason="The changed state closes this obligation.",
            evidence_ids=(record.id,), next_actions=("Inspect another file.",),
            evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
        )

        assert result.accepted is False
        assert result.reason == (
            "closed disposition cannot carry next_actions; "
            "use an empty next_actions array"
        )


def test_open_dispositions_retain_next_actions():
    for disposition in ("unresolved", "blocked", "exhausted"):
        ledger = _ledger(risk_tier="high")
        store, _record = _store_with_path()

        result = ledger.propose(
            target="O1", disposition=disposition,
            reason="Additional external evidence is required.",
            evidence_ids=(), next_actions=("Inspect the external contract.",),
            evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
        )

        assert result.accepted is True
        assert ledger.assessment("O1").next_actions == (
            "Inspect the external contract.",
        )


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


def test_component_group_assessment_records_two_of_five_paths_without_per_path_evidence():
    paths = tuple(f"backend/{name}.py" for name in "abcde")
    ledger = _ledger(scope=paths, owner_component_id="backend")
    store, record = _store_with_path(paths[0])

    result = ledger.propose(
        target="O1", disposition="covered",
        reason="The handlers preserve the validated request identity.",
        assessed_paths=paths[:2], omitted_paths=(), evidence_ids=(record.id,),
        next_actions=(), evidence=store.snapshot(),
        eligible=lambda evidence, _obligation: evidence.id == record.id,
    )

    assessment = ledger.assessment("O1")
    assert result.accepted is True
    assert assessment.disposition.value == "partially_covered"
    assert assessment.assessed_paths == paths[:2]
    assert assessment.omitted_paths == paths[2:]
    assert assessment.assessment_version == 1
    assert assessment.attempts[-1].assessment_version == 1
    assert ledger.open_targets() == ("O1",)


def test_component_supported_assessment_rejects_empty_assessed_scope():
    ledger = _ledger(
        scope=("backend/a.py", "backend/b.py"), owner_component_id="backend",
    )
    store, record = _store_with_path("backend/a.py")

    result = ledger.propose(
        target="O1", disposition="covered", reason="The change is correct.",
        assessed_paths=(), omitted_paths=(), evidence_ids=(record.id,),
        next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert result.accepted is False
    assert "assessed_paths" in result.reason
    assert ledger.assessment("O1").assessment_version == 0


def test_boundary_participant_without_changed_paths_can_record_supported_inspection():
    ledger = _ledger(scope=(), boundary_id="messages")
    store, record = _store_with_path("unchanged/consumer.py")

    result = ledger.propose(
        target="O1", disposition="covered",
        reason="The unchanged consumer still accepts the producer contract.",
        assessed_paths=(), omitted_paths=(), evidence_ids=(record.id,),
        next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert result.accepted is True
    assert ledger.assessment("O1").disposition.value == "covered"


def test_component_group_accepts_eligible_owner_evidence_and_ignores_supplemental_evidence():
    paths = ("backend/a.py", "backend/b.py")
    ledger = _ledger(scope=paths, owner_component_id="backend")
    store, direct = _store_with_path(paths[0])
    supplemental, _collection = store.add_tool_result_with_collection(
        session_id="session-1", tool="read_file",
        arguments={"path": "frontend/view.py"},
        result={"status": "ok", "content": "supplementary context"},
    )

    result = ledger.propose(
        target="O1", disposition="covered",
        reason="Both handlers preserve the validated request identity.",
        assessed_paths=paths, omitted_paths=(),
        evidence_ids=(direct.id, supplemental.id), next_actions=(),
        evidence=store.snapshot(),
        eligible=lambda record, _obligation: record.id == direct.id,
    )

    assert result.accepted is True
    assert result.eligible_evidence_ids == (direct.id,)
    assert result.ignored_supplemental_evidence_ids == (supplemental.id,)
    assert ledger.assessment("O1").disposition.value == "covered"


def test_component_group_rejects_paths_outside_ownership_and_overlap():
    paths = ("backend/a.py", "backend/b.py")
    store, record = _store_with_path(paths[0])

    invalid_ledger = _ledger(scope=paths, owner_component_id="backend")
    invalid = invalid_ledger.propose(
        target="O1", disposition="covered", reason="The path was inspected.",
        assessed_paths=("frontend/view.py",), omitted_paths=(),
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )
    overlap_ledger = _ledger(scope=paths, owner_component_id="backend")
    overlap = overlap_ledger.propose(
        target="O1", disposition="covered", reason="The path was inspected.",
        assessed_paths=(paths[0],), omitted_paths=(paths[0],),
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )
    glob_ledger = _ledger(scope=paths, owner_component_id="backend")
    glob = glob_ledger.propose(
        target="O1", disposition="covered", reason="The path was inspected.",
        assessed_paths=("backend/*.py",), omitted_paths=(),
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert invalid.accepted is False
    assert "outside owned changed scope" in invalid.reason
    assert overlap.accepted is False
    assert "both assessed and omitted" in overlap.reason
    assert glob.accepted is False
    assert "exact paths" in glob.reason


def test_component_partial_revisions_accumulate_and_explicit_omission_withdraws_scope():
    paths = tuple(f"backend/{name}.py" for name in "abcde")
    ledger = _ledger(scope=paths, owner_component_id="backend")
    store, record = _store_with_path(paths[0])

    first = ledger.propose(
        target="O1", disposition="partially_covered",
        reason="The first handlers preserve request identity.",
        assessed_paths=paths[:2], omitted_paths=(), evidence_ids=(record.id,),
        next_actions=("Inspect retry behavior.",), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )
    second = ledger.propose(
        target="O1", disposition="partially_covered",
        reason="The next handler is sound; the first needs renewed review.",
        assessed_paths=(paths[2],), omitted_paths=(paths[0],),
        evidence_ids=(record.id,), next_actions=("Recheck backend/a.py.",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )

    assessment = ledger.assessment("O1")
    assert first.accepted is True
    assert second.accepted is True
    assert assessment.assessed_paths == (paths[1], paths[2])
    assert assessment.omitted_paths == (paths[0], paths[3], paths[4])
    assert assessment.assessment_version == 2


def test_component_partial_revision_can_only_withdraw_previously_assessed_scope():
    paths = ("backend/a.py", "backend/b.py")
    ledger = _ledger(scope=paths, owner_component_id="backend")
    store, record = _store_with_path(paths[0])
    ledger.propose(
        target="O1", disposition="partially_covered",
        reason="The first handler preserves request identity.",
        assessed_paths=(paths[0],), omitted_paths=(), evidence_ids=(record.id,),
        next_actions=("Inspect the second handler.",), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    withdrawn = ledger.propose(
        target="O1", disposition="partially_covered",
        reason="The earlier conclusion is withdrawn pending another inspection.",
        assessed_paths=(), omitted_paths=(paths[0],), evidence_ids=(record.id,),
        next_actions=("Reinspect backend/a.py.",), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assessment = ledger.assessment("O1")
    assert withdrawn.accepted is True
    assert assessment.assessed_paths == ()
    assert assessment.omitted_paths == paths
    assert assessment.assessment_version == 2


def test_evaluator_owned_obligation_rejects_local_closure_without_losing_prior_work():
    paths = ("backend/a.py", "worker/b.py")
    ledger = _ledger(
        scope=paths, boundary_id="messages", evaluator_owned=True,
    )
    store, record = _store_with_path(paths[0])
    partial = ledger.propose(
        target="O1", disposition="partially_covered",
        reason="The producer preserves the message identity.",
        assessed_paths=(paths[0],), omitted_paths=(), evidence_ids=(record.id,),
        next_actions=("Evaluator must inspect the consumer.",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )
    rejected = ledger.propose(
        target="O1", disposition="covered",
        reason="The combined requirement is complete.", assessed_paths=paths,
        omitted_paths=(), evidence_ids=(record.id,), next_actions=(),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )

    assessment = ledger.assessment("O1")
    assert partial.accepted is True
    assert rejected.accepted is False
    assert "evaluator-owned" in rejected.reason
    assert assessment.assessed_paths == (paths[0],)
    assert assessment.assessment_version == 1
    assert len(assessment.attempts) == 2
    assert assessment.attempts[-1].assessment_version == 1


def test_boundary_not_applicable_requires_relevant_successful_untruncated_inspection():
    ledger = _ledger(scope=("backend/a.py",), boundary_id="messages")
    failed_store = EvidenceStore()
    failed, _collection = failed_store.add_tool_result_with_collection(
        session_id="session-1", tool="git_grep", arguments={"path": "backend/a.py"},
        result={"status": "failed", "error": "search failed"},
    )
    failed_result = ledger.propose(
        target="O1", disposition="not_applicable",
        reason="No affected boundary usage was found in the completed checks.",
        assessed_paths=(), omitted_paths=(), evidence_ids=(failed.id,),
        next_actions=(), evidence=failed_store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    store, successful = _store_with_path("backend/a.py")
    accepted = ledger.propose(
        target="O1", disposition="not_applicable",
        reason="No affected boundary usage was found in the completed checks.",
        assessed_paths=(), omitted_paths=(), evidence_ids=(successful.id,),
        next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert failed_result.accepted is False
    assert "successful untruncated" in failed_result.reason
    assert accepted.accepted is True


def test_boundary_not_applicable_rejects_truncated_or_contradicted_inspection():
    reason = "No affected boundary usage was found in the completed checks."
    truncated_ledger = _ledger(scope=("backend/a.py",), boundary_id="messages")
    truncated_store = EvidenceStore(max_content_bytes=8)
    truncated, _collection = truncated_store.add_tool_result_with_collection(
        session_id="session-1", tool="git_grep",
        arguments={"path": "backend/a.py"},
        result={"status": "ok", "content": "a result too long to retain"},
    )
    truncated_result = truncated_ledger.propose(
        target="O1", disposition="not_applicable", reason=reason,
        evidence_ids=(truncated.id,), next_actions=(),
        evidence=truncated_store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    contradicted_ledger = _ledger(
        scope=("backend/a.py",), boundary_id="messages",
    )
    contradicted_store, inspected = _store_with_path("backend/a.py")
    contradicted_store.add_tool_result(
        session_id="session-1", tool="git_grep",
        arguments={"path": "backend/a.py", "pattern": "publish"},
        result={"status": "ok", "content": "affected publish call"},
        contradicts=(inspected.id,),
    )
    contradicted_result = contradicted_ledger.propose(
        target="O1", disposition="not_applicable", reason=reason,
        evidence_ids=(inspected.id,), next_actions=(),
        evidence=contradicted_store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert truncated_result.accepted is False
    assert "successful untruncated" in truncated_result.reason
    assert contradicted_result.accepted is False
    assert "conflicts with retained evidence" in contradicted_result.reason


def test_boundary_not_applicable_rejects_unknown_evidence_without_crashing():
    ledger = _ledger(scope=("backend/a.py",), boundary_id="messages")
    store = EvidenceStore()

    result = ledger.propose(
        target="O1", disposition="not_applicable",
        reason="No affected boundary usage was found in the completed checks.",
        evidence_ids=("evidence:missing",), next_actions=(),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )

    assert result.accepted is False
    assert result.reason == "proposal references unknown retained evidence"


def test_component_covered_downgrades_until_collective_evidence_requirements_are_met():
    requirements = (
        {"id": "implementation", "category": "implementation", "mode": "required"},
        {"id": "tests", "category": "tests", "mode": "required"},
        {"id": "artifact", "category": "generated output", "mode": "one_of:proof"},
        {"id": "manual", "category": "manual check", "mode": "one_of:proof"},
        {"id": "docs", "category": "documentation", "mode": "optional"},
    )
    paths = ("backend/a.py", "backend/b.py")
    ledger = _ledger(
        scope=paths, owner_component_id="backend",
        evidence_requirements=requirements,
    )
    store = EvidenceStore()
    implementation, implementation_collection = store.add_tool_result_with_collection(
        session_id="session-1", tool="read_file", arguments={"path": paths[0]},
        result={"status": "ok", "content": "implementation behavior"},
    )
    store.associate_collection(
        implementation_collection.id, obligation_id="OB-workflow",
        categories=("implementation",),
    )

    partial = ledger.propose(
        target="O1", disposition="covered",
        reason="Both handlers preserve the validated request identity.",
        assessed_paths=paths, omitted_paths=(), evidence_ids=(implementation.id,),
        next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assessment = ledger.assessment("O1")
    assert partial.accepted is True
    assert assessment.disposition.value == "partially_covered"
    assert any("tests" in action for action in assessment.next_actions)
    assert any("one_of:proof" in action for action in assessment.next_actions)
    assert all("documentation" not in action for action in assessment.next_actions)

    tests_record, tests_collection = store.add_tool_result_with_collection(
        session_id="session-1", tool="read_file", arguments={"path": paths[1]},
        result={"status": "ok", "content": "behavioral tests"},
    )
    store.associate_collection(
        tests_collection.id, obligation_id="OB-workflow", categories=("tests",),
    )
    proof_record, proof_collection = store.add_tool_result_with_collection(
        session_id="session-1", tool="read_file", arguments={"path": paths[0]},
        result={"status": "ok", "content": "generated artifact proof"},
    )
    store.associate_collection(
        proof_collection.id, obligation_id="OB-workflow",
        categories=("generated output",),
    )
    covered = ledger.propose(
        target="O1", disposition="covered",
        reason="Implementation, tests, and generated proof support the conclusion.",
        assessed_paths=paths, omitted_paths=(),
        evidence_ids=(implementation.id, tests_record.id, proof_record.id),
        next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assessment = ledger.assessment("O1")
    assert covered.accepted is True
    assert assessment.disposition.value == "covered"
    assert assessment.next_actions == ()
    assert assessment.assessment_version == 2


def test_component_partial_retains_missing_collective_evidence_hints():
    paths = ("backend/a.py", "backend/b.py")
    ledger = _ledger(
        scope=paths,
        owner_component_id="backend",
        evidence_requirements=({
            "id": "tests", "category": "tests", "mode": "required",
        },),
    )
    store, record = _store_with_path(paths[0])

    result = ledger.propose(
        target="O1", disposition="partially_covered",
        reason="The first handler preserves the validated request identity.",
        assessed_paths=(paths[0],), omitted_paths=(),
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    assert result.accepted is True
    assessment = ledger.assessment("O1")
    assert assessment.omitted_paths == (paths[1],)
    assert assessment.next_actions == (
        "Collect evidence requirement 'tests' (tests).",
    )


def test_component_partial_with_no_omitted_paths_requires_explicit_next_action_and_preserves_state():
    paths = ("backend/a.py", "backend/b.py")
    ledger = _ledger(scope=paths, owner_component_id="backend")
    store, record = _store_with_path(paths[0])
    accepted = ledger.propose(
        target="O1", disposition="partially_covered",
        reason="The first handler preserves the validated request identity.",
        assessed_paths=(paths[0],), omitted_paths=(),
        evidence_ids=(record.id,), next_actions=("Inspect backend/b.py.",),
        evidence=store.snapshot(), eligible=lambda _record, _obligation: True,
    )
    before = ledger.assessment("O1")

    rejected = ledger.propose(
        target="O1", disposition="partially_covered",
        reason="Both handlers were inspected but a behavioral gap remains.",
        assessed_paths=(paths[1],), omitted_paths=(),
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(),
        eligible=lambda _record, _obligation: True,
    )

    after = ledger.assessment("O1")
    assert accepted.accepted is True
    assert rejected.accepted is False
    assert rejected.reason == (
        "partially_covered with no omitted paths requires a concrete next action"
    )
    assert after.disposition == before.disposition
    assert after.reason == before.reason
    assert after.evidence_ids == before.evidence_ids
    assert after.next_actions == before.next_actions
    assert after.assessed_paths == before.assessed_paths
    assert after.omitted_paths == before.omitted_paths
    assert after.assessment_version == before.assessment_version
