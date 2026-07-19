from __future__ import annotations

from dataclasses import replace

from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.types import CandidateFinding, CoverageObligation
from pr_reviewer.specialist_runtime.adjudication import (
    AdjudicatedReview,
    adjudicate_candidates,
    apply_runtime_verdict_policy,
    build_review_handoff,
    build_review_notes,
)
from pr_reviewer.specialist_runtime.types import ReviewNoteKind


def _candidate(
    candidate_id: str = "candidate-1",
    *,
    claim: str = "A retry can duplicate the write",
    location: str = "./src\\store.py:41",
    category: str = "correctness",
    evidence_ids: tuple[str, ...] = (),
    contradicting_ids: tuple[str, ...] = (),
    obligation_ids: tuple[str, ...] = ("obligation-1",),
    severity: str = "major",
) -> CandidateFinding:
    return CandidateFinding(
        candidate_id=candidate_id,
        root_cause_fingerprint="model-controlled-value",
        claim=claim,
        affected_location=location,
        causal_chain="The retry re-enters the write after an ambiguous result.",
        severity=severity,
        category=category,
        supporting_evidence_ids=evidence_ids,
        contradicting_evidence_ids=contradicting_ids,
        related_obligation_ids=obligation_ids,
        collector_session_id="session-1",
        model_identity="specialist-model",
    )


def _evidence_store(*, status: str = "ok") -> tuple[EvidenceStore, str]:
    store = EvidenceStore()
    record = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "src/store.py"},
        result={"status": status, "content": "write_with_retry()"},
        category="implementation",
    )
    return store, record.id


def test_critic_cannot_publish_candidate_without_retained_evidence():
    candidate = _candidate(evidence_ids=("MISSING",))

    review = adjudicate_candidates(
        [candidate],
        [{"candidate_id": candidate.candidate_id, "action": "keep"}],
        EvidenceStore().snapshot(),
    )

    assert review.accepted == ()
    assert review.rejected[0].reason == "missing-retained-evidence"


def test_accepted_candidate_uses_deterministic_normalized_root_cause_fingerprint():
    store, evidence_id = _evidence_store()
    first = _candidate(evidence_ids=(evidence_id,))
    equivalent = replace(
        first,
        candidate_id="candidate-2",
        root_cause_fingerprint="different-model-value",
        claim="  A RETRY can duplicate   the write. ",
        affected_location="src/store.py:99",
        category="Correctness",
    )

    first_review = adjudicate_candidates(
        [first], [{"candidate_id": first.candidate_id, "action": "keep"}], store.snapshot()
    )
    second_review = adjudicate_candidates(
        [equivalent],
        [{"candidate_id": equivalent.candidate_id, "action": "keep"}],
        store.snapshot(),
    )

    assert first_review.accepted[0].root_cause_fingerprint == second_review.accepted[0].root_cause_fingerprint
    assert first_review.accepted[0].affected_location == "src/store.py:41"
    assert second_review.accepted[0].affected_location == "src/store.py:99"


def test_fingerprint_uses_normalized_claim_not_explanatory_causal_chain_wording():
    store, evidence_id = _evidence_store()
    first = _candidate("candidate-1", evidence_ids=(evidence_id,))
    second = replace(
        first,
        candidate_id="candidate-2",
        causal_chain="A differently worded explanation of the same concrete claim.",
    )

    first_review = adjudicate_candidates(
        [first], {first.candidate_id: "keep"}, store
    )
    second_review = adjudicate_candidates(
        [second], {second.candidate_id: "keep"}, store
    )

    assert first_review.accepted[0].root_cause_fingerprint == second_review.accepted[0].root_cause_fingerprint


def test_critic_action_vocabulary_is_closed():
    store, evidence_id = _evidence_store()
    candidate = _candidate(evidence_ids=(evidence_id,))

    review = adjudicate_candidates(
        [candidate],
        [{"candidate_id": candidate.candidate_id, "action": "rewrite"}],
        store.snapshot(),
    )

    assert review.accepted == ()
    assert review.rejected[0].reason == "invalid-critic-action"


def test_high_risk_unresolved_obligation_blocks_by_policy():
    obligation = CoverageObligation(
        obligation_id="obligation-high-risk",
        origin="recipe",
        subject="authorization boundary",
        risk_tier="high",
        unresolved_policy="block_when_unresolved",
        mandatory=True,
    )

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        accepted=(),
        unresolved=(obligation,),
        allow_approve=True,
    )

    assert result.verdict == "request_changes"
    assert result.source == "incomplete-high-risk-coverage"
    assert result.blocking_obligation_ids == (obligation.obligation_id,)


def test_lower_risk_gap_is_unknown_and_does_not_invent_a_finding():
    obligation = CoverageObligation(
        obligation_id="obligation-normal",
        origin="topology",
        subject="documentation",
        risk_tier="normal",
        unresolved_policy="record_unknown",
        mandatory=True,
    )

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        accepted=(),
        unresolved=(obligation,),
        allow_approve=True,
    )

    assert result.verdict == "approve"
    assert result.source == "model"
    assert result.unknown_obligation_ids == (obligation.obligation_id,)


def test_merge_preserves_only_retained_provenance_and_contradictions():
    store, first_evidence_id = _evidence_store()
    second = store.add_tool_result(
        session_id="session-2",
        tool="git_blame",
        arguments={"path": "src/store.py", "line": 41},
        result={"status": "ok", "content": "retry introduced here"},
        category="history",
    )
    contradiction = store.add_tool_result(
        session_id="session-3",
        tool="read_file",
        arguments={"path": "tests/test_store.py"},
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

    review = adjudicate_candidates(
        [merged, target],
        [
            {"candidate_id": target.candidate_id, "action": "keep"},
            {"candidate_id": merged.candidate_id, "action": "merge", "target_id": target.candidate_id},
        ],
        store.snapshot(),
    )

    assert len(review.accepted) == 1
    assert review.accepted[0].candidate_id == target.candidate_id
    assert review.accepted[0].supporting_evidence_ids == tuple(sorted((first_evidence_id, second.id)))
    assert review.accepted[0].contradicting_evidence_ids == (contradiction.id,)
    assert review.accepted[0].severity == "major"


def test_merge_cannot_launder_missing_evidence_into_an_accepted_candidate():
    store, evidence_id = _evidence_store()
    target = _candidate("candidate-a", evidence_ids=(evidence_id,))
    merged = _candidate("candidate-b", evidence_ids=("MISSING",))

    review = adjudicate_candidates(
        [target, merged],
        [
            {"candidate_id": target.candidate_id, "action": "keep"},
            {"candidate_id": merged.candidate_id, "action": "merge", "target_id": target.candidate_id},
        ],
        store.snapshot(),
    )

    assert tuple(item.candidate_id for item in review.accepted) == (target.candidate_id,)
    assert review.accepted[0].supporting_evidence_ids == (evidence_id,)
    assert any(item.candidate_id == merged.candidate_id and item.reason == "missing-retained-evidence" for item in review.rejected)


def test_handoff_omits_per_finding_unknown_details_and_empty_sections():
    findings = (
        _candidate("db-1", claim="DB connection leak", category="database"),
        _candidate("db-2", claim="Transaction retries duplicate writes", category="database"),
    )
    handoff = build_review_handoff({
        "recommendation": "Request changes",
        "status": "AI review complete",
        "change_map": ("Persistence and retry behavior",),
        "specialist_focuses": ("database correctness",),
        "recipes": ("transaction-boundaries",),
        "coverage_boundaries": ("No production database was queried",),
        "findings": findings,
        "unknowns": ("Whether the remote schema matches",),
        "review_emphasis": ("Persistence", "Failure recovery", "Cross-service contract", "Extra"),
        "telemetry": {"recoveries": 9},
    })

    assert "DB connection leak" not in handoff.markdown
    assert "Transaction retries duplicate writes" not in handoff.markdown
    assert "Whether the remote schema matches" not in handoff.markdown
    assert "recoveries" not in handoff.markdown
    assert "Unknowns" not in handoff.markdown
    assert "database" in handoff.markdown.lower()
    assert handoff.finding_theme == "database"
    assert handoff.review_emphasis == ("Persistence", "Failure recovery", "Cross-service contract")
    assert "review the complete change" in handoff.markdown
    assert "Source access requests" not in handoff.markdown


def test_unrelated_findings_do_not_get_artificial_theme():
    handoff = build_review_handoff({
        "finding_theme": "security",
        "findings": (
            _candidate("auth", claim="Auth bypass", category="authorization"),
            _candidate("cache", claim="Stale cache", category="caching"),
        ),
    })

    assert handoff.finding_theme is None
    assert "Aggregate finding theme" not in handoff.markdown


def test_generic_shared_category_is_not_a_useful_aggregate_theme():
    handoff = build_review_handoff({
        "findings": (
            _candidate("auth", claim="Auth bypass", category="correctness"),
            _candidate("cache", claim="Stale cache", category="correctness"),
        ),
    })

    assert handoff.finding_theme is None


def test_changed_file_validation_keeps_unanchored_issue_for_verification():
    store, evidence_id = _evidence_store()
    candidate = _candidate(evidence_ids=(evidence_id,), location="")

    review = adjudicate_candidates(
        [candidate],
        [{"candidate_id": candidate.candidate_id, "action": "keep"}],
        store.snapshot(),
        changed_files=("src/store.py",),
    )

    assert tuple(item.candidate_id for item in review.accepted) == (candidate.candidate_id,)


def test_drive_absolute_location_cannot_be_a_changed_causal_file():
    store, evidence_id = _evidence_store()
    candidate = _candidate(evidence_ids=(evidence_id,), location="C:\\repo\\src\\store.py:41")

    review = adjudicate_candidates(
        [candidate],
        [{"candidate_id": candidate.candidate_id, "action": "keep"}],
        store.snapshot(),
        changed_files=("src/store.py",),
    )

    assert review.accepted == ()
    assert review.rejected[0].reason == "not-a-changed-causal-file"


def test_review_comment_builds_typed_finding_note_from_retained_evidence():
    store, evidence_id = _evidence_store()
    candidate = _candidate(evidence_ids=(evidence_id,))
    review = adjudicate_candidates(
        [candidate], [{"candidate_id": candidate.candidate_id, "action": "keep"}], store.snapshot()
    )

    notes = build_review_notes(review, store.snapshot(), publishing_mode="review_comment")

    assert len(notes) == 1
    note = notes[0]
    assert note.kind is ReviewNoteKind.FINDING
    assert note.fingerprint == review.accepted[0].root_cause_fingerprint
    assert note.related_obligation_ids == ("obligation-1",)
    assert note.evidence_ids == (evidence_id,)
    assert note.file == "src/store.py"
    assert note.line == 41
    assert "write_with_retry" in note.markdown
    assert review.accepted[0].claim in note.markdown


def test_comment_mode_does_not_move_detailed_findings_into_handoff_or_notes():
    store, evidence_id = _evidence_store()
    candidate = _candidate(evidence_ids=(evidence_id,))
    review = adjudicate_candidates(
        [candidate], [{"candidate_id": candidate.candidate_id, "action": "keep"}], store.snapshot()
    )

    notes = build_review_notes(review, store.snapshot(), publishing_mode="comment")
    handoff = build_review_handoff({"findings": review.accepted})

    assert notes == ()
    assert candidate.claim not in handoff.markdown


def test_unanchored_accepted_candidate_becomes_verification_request_not_finding():
    store, evidence_id = _evidence_store()
    candidate = _candidate(evidence_ids=(evidence_id,), location="")
    review = adjudicate_candidates(
        [candidate], [{"candidate_id": candidate.candidate_id, "action": "keep"}], store.snapshot()
    )

    notes = build_review_notes(review, store.snapshot(), publishing_mode="review_verdict")

    assert len(notes) == 1
    assert notes[0].kind is ReviewNoteKind.VERIFICATION_REQUEST
    assert notes[0].file is None
    assert "verify" in notes[0].markdown.lower()


def test_note_builder_rejects_forged_accepted_candidate_authority():
    store, evidence_id = _evidence_store()
    missing_contradiction = _candidate(
        evidence_ids=(evidence_id,), contradicting_ids=("MISSING",)
    )
    missing_obligation = _candidate(
        "candidate-2", evidence_ids=(evidence_id,), obligation_ids=()
    )
    review = AdjudicatedReview(accepted=(missing_contradiction, missing_obligation))

    notes = build_review_notes(review, store.snapshot(), publishing_mode="review_comment")

    assert notes == ()


def test_handoff_compacts_single_line_status_warning_and_source_link():
    handoff = build_review_handoff({
        "thread_status": "2 open threads\nHighest severity: major",
        "coverage_warning": "External schema unavailable\nConfidence is limited",
        "access_request_count": 2,
        "access_request_url": "https://example.test/artifacts/source-requests",
    })

    assert handoff.thread_status == "2 open threads Highest severity: major"
    assert handoff.coverage_warning == "External schema unavailable Confidence is limited"
    assert "[2 open](https://example.test/artifacts/source-requests)" in handoff.markdown


def test_note_builder_accepts_default_and_positional_publishing_mode():
    store, evidence_id = _evidence_store()
    candidate = _candidate(evidence_ids=(evidence_id,))
    review = adjudicate_candidates(
        [candidate], [{"candidate_id": candidate.candidate_id, "action": "keep"}], store
    )

    assert build_review_notes(review, store)[0].kind is ReviewNoteKind.FINDING
    assert build_review_notes(review, store, "comment") == ()


def test_typed_request_notes_have_stable_ids_and_related_authority():
    store, evidence_id = _evidence_store()
    review = AdjudicatedReview()
    verification = {
        "question": "Can a maintainer confirm the production retry contract?",
        "related_obligation_ids": ("obligation-1",),
        "evidence_ids": (evidence_id,),
        "file": "src/store.py",
        "line": 41,
    }
    source = {
        "purpose": "Grant access to the deployment schema used by this change.",
        "related_obligation_ids": ("obligation-2",),
        "file": "src/store.py",
    }

    first = build_review_notes(
        review,
        store,
        publishing_mode="review_comment",
        verification_requests=(verification,),
        source_access_requests=(source,),
    )
    second = build_review_notes(
        review,
        store,
        publishing_mode="review_comment",
        verification_requests=(verification,),
        source_access_requests=(source,),
    )

    assert {item.kind for item in first} == {
        ReviewNoteKind.VERIFICATION_REQUEST,
        ReviewNoteKind.SOURCE_ACCESS_REQUEST,
    }
    assert tuple(item.fingerprint for item in first) == tuple(item.fingerprint for item in second)
    assert next(item for item in first if item.kind is ReviewNoteKind.VERIFICATION_REQUEST).evidence_ids == (evidence_id,)
    assert next(item for item in first if item.kind is ReviewNoteKind.SOURCE_ACCESS_REQUEST).related_obligation_ids == ("obligation-2",)


def test_runtime_verdict_uses_only_configured_supported_severities():
    accepted = (_candidate(severity="major"),)

    default_result = apply_runtime_verdict_policy(
        model_verdict="approve", accepted=accepted, unresolved=(), allow_approve=True
    )
    configured_result = apply_runtime_verdict_policy(
        model_verdict="request_changes",
        accepted=accepted,
        unresolved=(),
        allow_approve=True,
        policy={"blocking_severities": ("blocker",)},
    )

    assert default_result.verdict == "request_changes"
    assert default_result.source == "supported-findings"
    assert configured_result.verdict == "approve"
    assert configured_result.source == "policy"


def test_runtime_approval_is_withheld_when_repository_policy_disables_it():
    result = apply_runtime_verdict_policy(
        model_verdict="approve", accepted=(), unresolved=(), allow_approve=False
    )

    assert result.verdict == "request_changes"
    assert result.source == "approval-disabled"


def test_unknown_obligation_and_unusable_evidence_cannot_be_accepted():
    store, failed_evidence_id = _evidence_store(status="error")
    usable = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "src/store.py"},
        result={"status": "ok", "content": "retained implementation"},
        category="implementation",
    )
    known = CoverageObligation(
        obligation_id="known", origin="topology", subject="store"
    )
    unusable = _candidate(
        "unusable", evidence_ids=(failed_evidence_id,), obligation_ids=(known.id,)
    )
    unknown = _candidate(
        "unknown", evidence_ids=(usable.id,), obligation_ids=("not-known",)
    )

    review = adjudicate_candidates(
        [unusable, unknown],
        {"unusable": "keep", "unknown": "keep"},
        store,
        obligations=(known,),
    )

    assert review.accepted == ()
    assert {item.reason for item in review.rejected} == {
        "unusable-retained-evidence", "unknown-related-obligation",
    }
