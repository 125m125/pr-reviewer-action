from __future__ import annotations

from dataclasses import replace

import pytest

from pr_reviewer.specialist_runtime.adjudication import (
    AdjudicatedReview,
    CandidateDisposition,
    ReviewHandoffContext,
    ReviewOrientationTopic,
    adjudicate_candidates,
    apply_runtime_verdict_policy,
    build_review_handoff,
    build_review_notes,
    consolidate_candidates,
)
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.types import (
    CandidateFinding,
    CoverageObligation,
    ReviewNoteKind,
)


CHANGED_FILES = ("src/store.py",)


def _obligation(
    obligation_id: str = "obligation-1",
    *,
    category: str = "implementation",
    scope: tuple[str, ...] = ("src/store.py",),
    risk_tier: str = "normal",
    unresolved_policy: str = "record_unknown",
) -> CoverageObligation:
    return CoverageObligation(
        obligation_id=obligation_id,
        origin="topology",
        subject="store behavior",
        required_evidence_categories=(category,),
        satisfaction_predicates=("recorded_evidence",),
        risk_tier=risk_tier,
        unresolved_policy=unresolved_policy,
        scope=scope,
        mandatory=True,
    )


def _obligations(*items: CoverageObligation) -> dict[str, CoverageObligation]:
    values = items or (_obligation(),)
    return {item.id: item for item in values}


def _candidate(
    candidate_id: str = "candidate-1",
    *,
    claim: str = "A retry can duplicate the write",
    location: str = "src/store.py:41",
    category: str = "database",
    evidence_ids: tuple[str, ...] = (),
    contradicting_ids: tuple[str, ...] = (),
    obligation_ids: tuple[str, ...] = ("obligation-1",),
    severity: str = "major",
    causal_chain: str = "The retry repeats a write after an ambiguous response.",
    consequence: str = "A user action can be persisted twice.",
    manual_validation: str = "Force an ambiguous retry and verify exactly one write is persisted.",
    confidence_rationale: str | None = None,
) -> CandidateFinding:
    rationale = confidence_rationale or (
        "consequence_support:reachable_input_path; "
        f"evidence_ids={','.join(evidence_ids)}; "
        "input=ambiguous response; condition=retry repeats a write; "
        "outcome=A user action can be persisted twice"
    )
    return CandidateFinding(
        candidate_id=candidate_id,
        root_cause_fingerprint="model-controlled-value",
        claim=claim,
        affected_location=location,
        causal_chain=causal_chain,
        severity=severity,
        category=category,
        supporting_evidence_ids=evidence_ids,
        contradicting_evidence_ids=contradicting_ids,
        related_obligation_ids=obligation_ids,
        collector_session_id="session-1",
        model_identity="specialist-model",
        confidence_rationale=rationale,
        user_visible_consequence=consequence,
        manual_validation=manual_validation,
    )


def _store(*, status: str = "ok") -> tuple[EvidenceStore, str]:
    store = EvidenceStore()
    record = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "src/store.py"},
        result={"status": status, "content": "write_with_retry()"},
        category="implementation",
    )
    return store, record.id


def _adjudicate(
    candidates: tuple[CandidateFinding, ...],
    decisions: object,
    store: EvidenceStore,
    *,
    obligations: dict[str, CoverageObligation] | None = None,
) -> AdjudicatedReview:
    return adjudicate_candidates(
        candidates,
        decisions,
        store,
        obligations=obligations or _obligations(),
        changed_files=CHANGED_FILES,
    )


@pytest.mark.parametrize(
    ("path", "anchor_field", "anchor", "category", "first_claim", "second_claim"),
    (
        (
            "scripts/sections/config.sh",
            "symbols",
            "validate_budget",
            "budget-validation",
            "validate_budget accepts an output budget larger than the model window",
            "The validate_budget guard does not cap output tokens to the context window",
        ),
        (
            ".github/workflows/ai-pr-review.yaml",
            "workflow_keys",
            "pull_request_target",
            "workflow-trigger",
            "pull_request_target runs the reviewer with target-repository privileges",
            "The pull_request_target trigger exposes the privileged review path",
        ),
        (
            "pr_reviewer/specialist_runtime/adjudication.py",
            "symbols",
            "_consequence_support_reason",
            "rationale-format",
            "_consequence_support_reason rejects a valid confidence_rationale layout",
            "A supported confidence_rationale is misparsed by _consequence_support_reason",
        ),
        (
            "pr_reviewer/specialist_runtime/adjudication.py",
            "symbols",
            "_exact_changed_location",
            "location-normalization",
            "_exact_changed_location rejects normalized changed paths",
            "Changed-path normalization is lost at _exact_changed_location",
        ),
    ),
)
def test_controller_root_identity_consolidates_production_shaped_specialist_clusters(
    path,
    anchor_field,
    anchor,
    category,
    first_claim,
    second_claim,
):
    candidates = (
        _candidate(
            "specialist-a",
            claim=first_claim,
            location=f"{path}:41",
            category=category,
            evidence_ids=("evidence:a",),
            causal_chain=f"{anchor} applies the changed contract incorrectly.",
        ),
        _candidate(
            "specialist-b",
            claim=second_claim,
            location=path,
            category=category,
            evidence_ids=("evidence:b",),
            causal_chain=f"The changed {anchor} contract takes the same faulty branch.",
        ),
    )

    result = consolidate_candidates(
        candidates,
        changed_files=(path,),
        change_facts={path: {anchor_field: (anchor,)}},
        obligations=_obligations(),
        valid_evidence_ids=("evidence:a", "evidence:b"),
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].contributor_candidate_ids == (
        "specialist-a",
        "specialist-b",
    )
    assert result.dispositions == (
        CandidateDisposition(
            candidate_id="specialist-b",
            action="merge",
            reason="controller-root-identity",
            target_id="specialist-a",
        ),
    )


def test_controller_root_merge_retains_precise_location_valid_evidence_obligations_and_severity():
    implementation = _obligation("obligation-implementation")
    contract = _obligation("obligation-contract")
    first = _candidate(
        "budget-a",
        claim="validate_budget permits an oversized output allowance",
        location="src/store.py:41",
        category="budget-validation",
        evidence_ids=("evidence:a", "missing:evidence"),
        obligation_ids=(implementation.id,),
        severity="minor",
        causal_chain="validate_budget compares the wrong remaining-token value.",
    )
    second = _candidate(
        "budget-b",
        claim="The output-token guard can exceed the remaining model context",
        location="src/store.py",
        category="budget-validation",
        evidence_ids=("evidence:b",),
        contradicting_ids=("evidence:contradiction", "missing:contradiction"),
        obligation_ids=(contract.id,),
        severity="major",
        causal_chain="The changed validate_budget contract uses the same incorrect comparison.",
    )

    result = consolidate_candidates(
        (second, first),
        changed_files=CHANGED_FILES,
        change_facts={"src/store.py": {"symbols": ("validate_budget",)}},
        obligations=_obligations(implementation, contract),
        valid_evidence_ids=(
            "evidence:a",
            "evidence:b",
            "evidence:contradiction",
        ),
    )

    assert len(result.candidates) == 1
    merged = result.candidates[0]
    assert merged.candidate_id == "budget-a"
    assert merged.affected_location == "src/store.py:41"
    assert merged.supporting_evidence_ids == ("evidence:a", "evidence:b")
    assert merged.contradicting_evidence_ids == ("evidence:contradiction",)
    assert merged.related_obligation_ids == (
        "obligation-contract",
        "obligation-implementation",
    )
    assert merged.severity == "major"
    assert merged.root_cause_fingerprint.startswith("root:")


def test_unsupported_duplicate_cannot_supply_a_more_precise_location():
    store, evidence_id = _store()
    supported = _candidate(
        "supported",
        claim="validate_budget permits an oversized output allowance",
        location="src/store.py",
        category="budget-validation",
        evidence_ids=(evidence_id,),
        causal_chain="validate_budget compares the wrong remaining-token value.",
    )
    unsupported = _candidate(
        "unsupported",
        claim="The validate_budget output guard can overrun remaining context",
        location="src/store.py:1",
        category="budget-validation",
        evidence_ids=("missing:evidence",),
        causal_chain="The changed validate_budget contract uses the same comparison.",
    )

    result = consolidate_candidates(
        (unsupported, supported),
        changed_files=CHANGED_FILES,
        change_facts={"src/store.py": {"symbols": ("validate_budget",)}},
        obligations=_obligations(),
        evidence=store,
    )

    assert result.candidates[0].affected_location == "src/store.py"


def test_obligation_irrelevant_evidence_cannot_promote_merged_severity():
    store = EvidenceStore()
    supporting = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "src/store.py"},
        result={"status": "ok", "content": "validate_budget()"},
        category="implementation",
    )
    unrelated = store.add_tool_result(
        session_id="session-2",
        tool="read_file",
        arguments={"path": "README.md"},
        result={"status": "ok", "content": "budget documentation"},
        category="documentation",
    )
    supported = _candidate(
        "supported",
        claim="validate_budget permits an oversized output allowance",
        category="budget-validation",
        evidence_ids=(supporting.id,),
        severity="minor",
        causal_chain="validate_budget compares the wrong remaining-token value.",
    )
    unsupported_blocker = _candidate(
        "unsupported-blocker",
        claim="The validate_budget output guard can overrun remaining context",
        category="budget-validation",
        evidence_ids=(unrelated.id,),
        severity="blocker",
        causal_chain="The changed validate_budget contract uses the same comparison.",
    )

    result = consolidate_candidates(
        (unsupported_blocker, supported),
        changed_files=CHANGED_FILES,
        change_facts={"src/store.py": {"symbols": ("validate_budget",)}},
        obligations=_obligations(),
        evidence=store,
    )

    assert result.candidates[0].severity == "minor"


def test_shared_valid_evidence_cannot_launder_location_or_severity():
    store, evidence_id = _store()
    first = _candidate(
        "candidate-a",
        claim="validate_budget permits an oversized output allowance",
        location="src/store.py:41",
        category="budget-validation",
        evidence_ids=(evidence_id,),
        severity="minor",
        causal_chain="validate_budget compares the wrong remaining-token value.",
    )
    hostile_duplicate = _candidate(
        "candidate-b",
        claim="The validate_budget output guard causes total service failure",
        location="src/store.py:1",
        category="budget-validation",
        evidence_ids=(evidence_id,),
        severity="blocker",
        causal_chain="The changed validate_budget contract uses the same comparison.",
    )

    result = consolidate_candidates(
        (first, hostile_duplicate),
        changed_files=CHANGED_FILES,
        change_facts={"src/store.py": {"symbols": ("validate_budget",)}},
        obligations=_obligations(),
        evidence=store,
    )

    assert result.candidates[0].affected_location == "src/store.py"
    assert result.candidates[0].severity == "minor"


def test_unique_unrelated_evidence_cannot_unlock_shared_support_donation():
    store, shared_id = _store()
    unrelated = store.add_tool_result(
        session_id="session-2",
        tool="read_file",
        arguments={"path": "README.md"},
        result={"status": "ok", "content": "budget documentation"},
        category="documentation",
    )
    first = _candidate(
        "candidate-a",
        claim="validate_budget permits an oversized output allowance",
        location="src/store.py:41",
        category="budget-validation",
        evidence_ids=(shared_id,),
        severity="minor",
        causal_chain="validate_budget compares the wrong remaining-token value.",
    )
    hostile_duplicate = _candidate(
        "candidate-b",
        claim="The validate_budget output guard causes total service failure",
        location="src/store.py:1",
        category="budget-validation",
        evidence_ids=(shared_id, unrelated.id),
        severity="blocker",
        causal_chain="The changed validate_budget contract uses the same comparison.",
    )

    result = consolidate_candidates(
        (first, hostile_duplicate),
        changed_files=CHANGED_FILES,
        change_facts={"src/store.py": {"symbols": ("validate_budget",)}},
        obligations=_obligations(),
        evidence=store,
    )

    assert result.candidates[0].affected_location == "src/store.py"
    assert result.candidates[0].severity == "minor"


def test_critic_cannot_publish_candidate_without_retained_evidence():
    candidate = _candidate(evidence_ids=("MISSING",))

    review = _adjudicate((candidate,), {candidate.candidate_id: "keep"}, EvidenceStore())

    assert review.accepted == ()
    assert review.verification_requests[0].reason == "missing-retained-evidence"


def test_fingerprint_normalizes_file_category_claim_and_ignores_line():
    store, evidence_id = _store()
    first = _candidate(evidence_ids=(evidence_id,))
    equivalent = replace(
        first,
        candidate_id="candidate-2",
        claim="  A RETRY can duplicate   the write ",
        affected_location="src/store.py:99",
        category="Database",
        causal_chain="  THE retry repeats a write after an ambiguous response. ",
    )

    first_review = _adjudicate((first,), {first.candidate_id: "keep"}, store)
    second_review = _adjudicate((equivalent,), {equivalent.candidate_id: "keep"}, store)

    assert first_review.accepted[0].root_cause_fingerprint == second_review.accepted[0].root_cause_fingerprint
    assert first_review.accepted[0].affected_file == "src/store.py"
    assert second_review.accepted[0].line == 99


def test_non_exact_candidate_location_stays_general_without_inferred_anchor():
    store, evidence_id = _store()
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        location="./src\\store.py:41",
    )

    review = _adjudicate(
        (candidate,), {candidate.candidate_id: "keep"}, store,
    )
    notes = build_review_notes(
        review,
        store,
        "review_comment",
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert review.accepted == ()
    assert len(notes) == 1
    assert notes[0].kind is ReviewNoteKind.VERIFICATION_REQUEST
    assert notes[0].file is None
    assert notes[0].line is None


def test_critic_action_vocabulary_is_closed():
    store, evidence_id = _store()
    candidate = _candidate(evidence_ids=(evidence_id,))

    review = _adjudicate(
        (candidate,), [{"candidate_id": candidate.candidate_id, "action": "rewrite"}], store
    )

    assert review.accepted == ()
    assert review.rejected[0].reason == "invalid-critic-action"


def test_critic_cannot_request_verification_that_github_review_lines_may_be_zero_based():
    candidate = _candidate(
        claim=(
            "The `_exact_changed_location` function rejects line 0. If a system "
            "uses zero-based locations, this will be treated as invalid."
        ),
        causal_chain=(
            "The concern assumes GitHub review comments may accept zero-based "
            "diff locations."
        ),
        consequence="A valid GitHub inline review comment could be discarded.",
        manual_validation="Check whether the GitHub review API accepts line 0.",
    )

    review = _adjudicate(
        (candidate,),
        {candidate.candidate_id: "request_verification"},
        EvidenceStore(),
    )

    assert review.verification_requests == ()
    assert review.rejected[0].reason == "deterministic-platform-contradiction"


def test_authorization_failure_cannot_publish_zero_based_github_line_hypothesis():
    candidate = _candidate(
        claim="GitHub review comment lines might be zero-based",
        location="src/store.py:0",
        causal_chain="GitHub may accept line 0 for a review comment location.",
        consequence="A valid inline review comment could be discarded.",
        manual_validation="Check whether the GitHub review API accepts line 0.",
    )

    review = _adjudicate(
        (candidate,),
        {candidate.candidate_id: "keep"},
        EvidenceStore(),
    )

    assert review.verification_requests == ()
    assert review.rejected[0].reason == "deterministic-platform-contradiction"


def test_raw_candidate_verification_cannot_publish_zero_based_github_line_hypothesis():
    candidate = _candidate(
        claim="GitHub review comment lines might be zero-based",
        location="src/store.py:0",
        causal_chain="GitHub may accept line 0 for a review comment location.",
        manual_validation="Check whether the GitHub review API accepts line 0.",
    )
    review = AdjudicatedReview(verification_requests=(candidate,))

    notes = build_review_notes(
        review,
        EvidenceStore(),
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert notes == ()


def test_defensive_revalidation_cannot_publish_zero_based_github_line_hypothesis():
    store, evidence_id = _store()
    candidate = _candidate(
        claim="GitHub review comment lines might be zero-based",
        causal_chain="GitHub may accept line 0 for a review comment location.",
        evidence_ids=(evidence_id,),
        confidence_rationale=(
            "consequence_support:reachable_input_path; "
            f"evidence_ids={evidence_id}; input=line 0; "
            "condition=GitHub may accept line 0; "
            "outcome=A user action can be persisted twice"
        ),
        manual_validation="Check whether the GitHub review API accepts line 0.",
    )
    accepted = _adjudicate(
        (candidate,),
        {candidate.candidate_id: "keep"},
        store,
    ).accepted[0]
    review = AdjudicatedReview(accepted=(replace(accepted, line=0),))

    notes = build_review_notes(
        review,
        store,
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert notes == ()


def test_critic_preserves_genuine_github_diff_side_location_ambiguity():
    candidate = _candidate(
        claim="The requested line exists on both sides of the GitHub diff",
        causal_chain=(
            "The candidate names a changed line but retained evidence does not identify "
            "whether the old or new side is intended."
        ),
        consequence="The review comment could attach to the wrong side of the diff.",
        manual_validation="Inspect the patch and select LEFT or RIGHT for the location.",
    )

    review = _adjudicate(
        (candidate,),
        {candidate.candidate_id: "request_verification"},
        EvidenceStore(),
    )

    assert review.rejected == ()
    assert review.verification_requests[0].reason == "critic-requested-verification"


def test_high_risk_unresolved_controller_obligation_blocks_by_policy():
    obligation = _obligation(
        "obligation-high-risk",
        risk_tier="high",
        unresolved_policy="block_when_unresolved",
    )
    store = EvidenceStore()

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=AdjudicatedReview(),
        unresolved=(obligation,),
        allow_approve=True,
        evidence=store,
        obligations=_obligations(obligation),
        changed_files=CHANGED_FILES,
    )

    assert result.verdict == "request_changes"
    assert result.source == "incomplete-high-risk-coverage"
    assert result.blocking_obligation_ids == (obligation.id,)


def test_lower_risk_gap_is_unknown_and_does_not_invent_a_finding():
    obligation = _obligation("obligation-normal")

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=AdjudicatedReview(),
        unresolved=(obligation,),
        allow_approve=True,
        evidence=EvidenceStore(),
        obligations=_obligations(obligation),
        changed_files=CHANGED_FILES,
    )

    assert result.verdict == "approve"
    assert result.unknown_obligation_ids == (obligation.id,)
    assert result.blocking_finding_ids == ()


def test_merge_preserves_retained_provenance_contradictions_and_highest_severity():
    store, first_evidence_id = _store()
    second = store.add_tool_result(
        session_id="session-2",
        tool="read_file",
        arguments={"path": "src/store.py"},
        result={"status": "ok", "content": "retry introduced here"},
        category="implementation",
    )
    contradiction = store.add_tool_result(
        session_id="session-3",
        tool="read_file",
        arguments={"path": "src/store.py"},
        result={"status": "ok", "content": "test claims retry is idempotent"},
        category="tests",
    )
    target = _candidate("candidate-a", evidence_ids=(first_evidence_id,), severity="minor")
    merged = _candidate(
        "candidate-b",
        evidence_ids=(second.id,),
        contradicting_ids=(contradiction.id,),
        severity="major",
    )
    review = _adjudicate(
        (merged, target),
        (
            {"candidate_id": target.candidate_id, "action": "keep"},
            {"candidate_id": merged.candidate_id, "action": "merge", "target_id": target.candidate_id},
        ),
        store,
    )

    assert len(review.accepted) == 1
    finding = review.accepted[0]
    assert finding.candidate_id == target.candidate_id
    assert finding.supporting_evidence_ids == tuple(sorted((first_evidence_id, second.id)))
    assert finding.contradicting_evidence_ids == (contradiction.id,)
    assert finding.severity == "major"
    assert finding.contributor_candidate_ids == ("candidate-a", "candidate-b")


def test_merge_cannot_launder_missing_evidence():
    store, evidence_id = _store()
    target = _candidate("candidate-a", evidence_ids=(evidence_id,))
    merged = _candidate("candidate-b", evidence_ids=("MISSING",))

    review = _adjudicate(
        (target, merged),
        (
            {"candidate_id": target.candidate_id, "action": "keep"},
            {"candidate_id": merged.candidate_id, "action": "merge", "target_id": target.candidate_id},
        ),
        store,
    )

    assert tuple(item.candidate_id for item in review.accepted) == (target.candidate_id,)
    assert review.verification_requests[0].candidate.candidate_id == merged.candidate_id


def test_handoff_is_sparse_and_uses_only_genuine_structured_theme():
    store, evidence_id = _store()
    findings = (
        _candidate("db-1", evidence_ids=(evidence_id,), claim="DB connection leak"),
        _candidate("db-2", evidence_ids=(evidence_id,), claim="Transaction retries duplicate writes"),
    )
    review = _adjudicate(findings, {item.candidate_id: "keep" for item in findings}, store)
    context = ReviewHandoffContext(
        recommendation="request_changes",
        status="complete",
        change_topics=(ReviewOrientationTopic.DATABASE,),
        component_ids=("store",),
        specialist_topics=(ReviewOrientationTopic.DATABASE,),
        recipe_ids=("transaction-boundaries",),
        coverage_boundary_topics=(ReviewOrientationTopic.TEST_COVERAGE,),
        review_emphasis_topics=(
            ReviewOrientationTopic.DATABASE,
            ReviewOrientationTopic.FAILURE_RECOVERY,
            ReviewOrientationTopic.CROSS_COMPONENT_CONTRACTS,
            ReviewOrientationTopic.SECURITY,
        ),
    )

    handoff = build_review_handoff(
        context,
        review=review,
        evidence=store,
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert "DB connection leak" not in handoff.markdown
    assert "Transaction retries duplicate writes" not in handoff.markdown
    assert handoff.finding_theme == "database"
    assert len(handoff.review_emphasis) == 3
    assert "review the complete change" in handoff.markdown
    assert "Source access requests" not in handoff.markdown


def test_handoff_renders_concise_behavioral_sections_without_component_inventory():
    handoff = build_review_handoff(
        ReviewHandoffContext(
            what_changed=(
                "`src/store.py` changes database persistence behavior.",
                "`src/store.py` changes database persistence behavior.",
            ),
            ai_reviewed=(
                "Reviewed database persistence behavior in `src/store.py`.",
            ),
            component_ids=("store",),
            review_emphasis_topics=(
                ReviewOrientationTopic.DATABASE,
                ReviewOrientationTopic.FAILURE_RECOVERY,
                ReviewOrientationTopic.CROSS_COMPONENT_CONTRACTS,
                ReviewOrientationTopic.SECURITY,
            ),
        ),
        review=AdjudicatedReview(),
        evidence=EvidenceStore(),
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert "### What changed" in handoff.markdown
    assert "### What the AI reviewed" in handoff.markdown
    assert "### Human focus" in handoff.markdown
    assert handoff.what_changed == (
        "`src/store.py` changes database persistence behavior.",
    )
    assert handoff.ai_reviewed == (
        "Reviewed database persistence behavior in `src/store.py`.",
    )
    assert len(handoff.human_focus) == 3
    assert "Component: store" not in handoff.markdown
    assert "### Change map" not in handoff.markdown
    assert "### AI focus and coverage" not in handoff.markdown


def test_handoff_drops_behavioral_summaries_without_an_authorized_changed_path():
    handoff = build_review_handoff(
        ReviewHandoffContext(
            what_changed=(
                "`src/store.py` changes database persistence behavior.",
                "`src/invented.py` changes authorization behavior.",
                "A path-free behavioral claim.",
            ),
            ai_reviewed=(
                "Reviewed retry behavior using evidence:evidence-forged.",
                "Reviewed persistence behavior in `src/store.py`.",
            ),
        ),
        review=AdjudicatedReview(),
        evidence=EvidenceStore(),
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert handoff.what_changed == (
        "`src/store.py` changes database persistence behavior.",
    )
    assert handoff.ai_reviewed == (
        "Reviewed persistence behavior in `src/store.py`.",
    )
    assert "invented.py" not in handoff.markdown
    assert "path-free" not in handoff.markdown
    assert "evidence-forged" not in handoff.markdown


def test_handoff_caps_behavioral_summary_count_and_length():
    changed_files = tuple(f"src/module-{index}.py" for index in range(7))
    summaries = (
        "`src/module-0.py` " + ("x" * 145),
    ) + tuple(
        f"`{path}` changes runtime implementation behavior."
        for path in changed_files
    )
    handoff = build_review_handoff(
        ReviewHandoffContext(what_changed=summaries),
        review=AdjudicatedReview(),
        evidence=EvidenceStore(),
        obligations=_obligations(),
        changed_files=changed_files,
    )

    assert len(handoff.what_changed) == 5
    assert all(len(item) <= 160 for item in handoff.what_changed)


def test_handoff_compactly_names_distinct_degraded_stages_without_details():
    handoff = build_review_handoff(
        ReviewHandoffContext(
            status="degraded",
            material_coverage_limited=True,
            degraded_stages=(
                "planner",
                "negotiator",
                "planner",
                "specialist_hook:private-session-id:private-hook-id",
            ),
        ),
        review=AdjudicatedReview(),
        evidence=EvidenceStore(),
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert handoff.coverage_warning == (
        "Material evidence or session coverage is incomplete. "
        "Affected stages: negotiator, planner, specialist."
    )
    assert handoff.markdown.count("planner") == 1
    assert "private-session-id" not in handoff.markdown
    assert "private-hook-id" not in handoff.markdown
    assert "exception" not in handoff.markdown.lower()


def test_handoff_prepared_note_status_omits_severity_without_a_material_finding():
    handoff = build_review_handoff(
        ReviewHandoffContext(
            unresolved_thread_count=2,
            highest_thread_severity=None,
        ),
        review=AdjudicatedReview(),
        evidence=EvidenceStore(),
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert handoff.thread_status == "2 detail review notes prepared for publication."
    assert "**Prepared detail notes:**" in handoff.markdown
    assert "highest proposed finding severity" not in handoff.markdown
    assert "unresolved" not in handoff.markdown.casefold()
    assert "thread status" not in handoff.markdown.casefold()


def test_disparate_or_generic_findings_do_not_get_artificial_theme():
    store, evidence_id = _store()
    disparate = (
        _candidate("auth", evidence_ids=(evidence_id,), category="authorization"),
        _candidate("cache", evidence_ids=(evidence_id,), category="caching"),
    )
    generic = tuple(replace(item, category="correctness") for item in disparate)
    for candidates in (disparate, generic):
        review = _adjudicate(candidates, {item.candidate_id: "keep" for item in candidates}, store)
        handoff = build_review_handoff(
            ReviewHandoffContext(),
            review=review,
            evidence=store,
            obligations=_obligations(),
            changed_files=CHANGED_FILES,
        )
        assert handoff.finding_theme is None


def test_review_comment_builds_typed_detailed_finding_note():
    store, evidence_id = _store()
    candidate = _candidate(evidence_ids=(evidence_id,))
    review = _adjudicate((candidate,), {candidate.candidate_id: "keep"}, store)

    notes = build_review_notes(
        review,
        store,
        "review_comment",
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert len(notes) == 1
    note = notes[0]
    assert note.kind is ReviewNoteKind.FINDING
    assert note.fingerprint == review.accepted[0].root_cause_fingerprint
    assert note.related_obligation_ids == ("obligation-1",)
    assert note.file == "src/store.py"
    assert note.line == 41
    assert "Supporting evidence provenance" in note.markdown
    assert "Suggested validation" in note.markdown
    assert "A user action can be persisted twice" in note.markdown
    assert "Force an ambiguous retry" in note.markdown


def test_missing_structured_consequence_or_validation_downgrades_to_verification():
    store, evidence_id = _store()
    complete = _candidate(evidence_ids=(evidence_id,))
    candidates = (
        replace(complete, candidate_id="missing-consequence", user_visible_consequence=""),
        replace(complete, candidate_id="missing-validation", manual_validation=""),
    )

    review = _adjudicate(candidates, {item.candidate_id: "keep" for item in candidates}, store)

    assert review.accepted == ()
    assert tuple(item.reason for item in review.verification_requests) == (
        "missing-required-finding-detail", "missing-required-finding-detail",
    )


def test_comment_mode_does_not_move_detailed_findings_into_handoff_or_notes():
    store, evidence_id = _store()
    candidate = _candidate(evidence_ids=(evidence_id,))
    review = _adjudicate((candidate,), {candidate.candidate_id: "keep"}, store)

    notes = build_review_notes(
        review,
        store,
        "comment",
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )
    handoff = build_review_handoff(
        ReviewHandoffContext(),
        review=review,
        evidence=store,
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert notes == ()
    assert candidate.claim not in handoff.markdown


def test_unanchored_keep_becomes_typed_verification_request_note():
    store, evidence_id = _store()
    candidate = _candidate(evidence_ids=(evidence_id,), location="")
    review = _adjudicate((candidate,), {candidate.candidate_id: "keep"}, store)

    assert review.accepted == ()
    notes = build_review_notes(
        review,
        store,
        "review_verdict",
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert len(notes) == 1
    assert notes[0].kind is ReviewNoteKind.VERIFICATION_REQUEST
    assert notes[0].file is None
    assert "Why human input is needed" in notes[0].markdown


def test_exact_changed_file_without_line_stays_file_anchored():
    store, evidence_id = _store()
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        location="src/store.py",
    )
    review = _adjudicate(
        (candidate,), {candidate.candidate_id: "keep"}, store,
    )

    notes = build_review_notes(
        review,
        store,
        "review_comment",
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert len(review.accepted) == 1
    assert len(notes) == 1
    assert notes[0].kind is ReviewNoteKind.FINDING
    assert notes[0].file == "src/store.py"
    assert notes[0].line is None


def test_runtime_supported_severity_policy_and_approval_gate():
    store, evidence_id = _store()
    candidate = _candidate(evidence_ids=(evidence_id,), severity="major")
    review = _adjudicate((candidate,), {candidate.candidate_id: "keep"}, store)

    default_result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=review,
        unresolved=(),
        allow_approve=True,
        evidence=store,
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )
    configured_result = apply_runtime_verdict_policy(
        model_verdict="request_changes",
        review=review,
        unresolved=(),
        allow_approve=True,
        evidence=store,
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
        policy={"blocking_severities": ("blocker",)},
    )
    disabled_result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=AdjudicatedReview(),
        unresolved=(),
        allow_approve=False,
        evidence=store,
        obligations=_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert default_result.verdict == "request_changes"
    assert default_result.source == "supported-findings"
    assert configured_result.verdict == "approve"
    assert disabled_result.source == "approval-disabled"


def test_set_inputs_are_canonicalized_for_deterministic_output():
    store, evidence_id = _store()
    candidates = {
        _candidate("candidate-b", evidence_ids=(evidence_id,), claim="Second claim"),
        _candidate("candidate-a", evidence_ids=(evidence_id,), claim="First claim"),
    }
    decisions = {item.candidate_id: "keep" for item in candidates}

    review = adjudicate_candidates(
        candidates,
        decisions,
        store,
        obligations=_obligations(),
        changed_files={"src/store.py"},
    )

    assert tuple(item.candidate_id for item in review.accepted) == ("candidate-a", "candidate-b")
