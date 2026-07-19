from __future__ import annotations

from dataclasses import replace

import pytest

from pr_reviewer.specialist_runtime.adjudication import (
    AdjudicatedReview,
    ReviewHandoffContext,
    adjudicate_candidates,
    apply_runtime_verdict_policy,
    build_review_handoff,
    build_review_notes,
)
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.types import (
    CandidateFinding,
    CoverageObligation,
    ReviewNoteKind,
)
from pr_reviewer.specialist_runtime.web_evidence import SourceAccessRequest


CHANGED_FILES = ("src/store.py",)


def _obligation(
    obligation_id: str = "obligation-store",
    *,
    scope: tuple[str, ...] = ("src/store.py",),
    category: str = "implementation",
    risk_tier: str = "normal",
    unresolved_policy: str = "record_unknown",
) -> CoverageObligation:
    return CoverageObligation(
        obligation_id=obligation_id,
        origin="topology",
        subject="store behavior",
        required_evidence_categories=(category,),
        satisfaction_predicates=("recorded_evidence",),
        scope=scope,
        risk_tier=risk_tier,
        unresolved_policy=unresolved_policy,
        mandatory=True,
    )


def _controller_obligations(*items: CoverageObligation) -> dict[str, CoverageObligation]:
    values = items or (_obligation(),)
    return {item.id: item for item in values}


def _store(
    *,
    path: str = "src/store.py",
    category: str = "implementation",
    content: str = "write_with_retry()",
) -> tuple[EvidenceStore, str]:
    store = EvidenceStore()
    record = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": path},
        result={"status": "ok", "content": content},
        category=category,
    )
    return store, record.id


def _candidate(
    candidate_id: str = "candidate-1",
    *,
    evidence_ids: tuple[str, ...] = (),
    claim: str = "A failed retry duplicates the write",
    causal_chain: str = "The retry repeats a write after an ambiguous response.",
    location: str = "src/store.py:41",
    severity: str = "major",
    category: str = "database",
    obligation_ids: tuple[str, ...] = ("obligation-store",),
) -> CandidateFinding:
    return CandidateFinding(
        candidate_id=candidate_id,
        root_cause_fingerprint="model-value",
        claim=claim,
        affected_location=location,
        causal_chain=causal_chain,
        severity=severity,
        category=category,
        supporting_evidence_ids=evidence_ids,
        related_obligation_ids=obligation_ids,
        collector_session_id="session-1",
        model_identity="specialist",
    )


def _adjudicate(
    candidates: tuple[CandidateFinding, ...],
    store: EvidenceStore,
    *,
    obligations: dict[str, CoverageObligation] | None = None,
    changed_files: tuple[str, ...] = CHANGED_FILES,
) -> AdjudicatedReview:
    return adjudicate_candidates(
        candidates,
        {candidate.candidate_id: "keep" for candidate in candidates},
        store,
        obligations=obligations or _controller_obligations(),
        changed_files=changed_files,
    )


def test_factual_adjudication_requires_controller_authority_inputs():
    store, evidence_id = _store()
    candidate = _candidate(evidence_ids=(evidence_id,))

    with pytest.raises(TypeError):
        adjudicate_candidates([candidate], {candidate.candidate_id: "keep"}, store)


def test_unrelated_evidence_id_cannot_launder_candidate_into_acceptance():
    store, evidence_id = _store(path="tests/test_other.py", category="tests")
    candidate = _candidate(evidence_ids=(evidence_id,))

    review = _adjudicate((candidate,), store)

    assert review.accepted == ()
    assert review.verification_requests[0].candidate.candidate_id == candidate.candidate_id
    assert review.verification_requests[0].reason == "evidence-does-not-satisfy-related-obligation"


@pytest.mark.parametrize("location", ["", "src/other.py:8", "C:\\repo\\src\\store.py:41"])
def test_missing_or_off_change_location_becomes_verification_not_factual(location: str):
    store, evidence_id = _store()
    candidate = _candidate(evidence_ids=(evidence_id,), location=location)

    review = _adjudicate((candidate,), store)

    assert review.accepted == ()
    assert tuple(item.candidate.candidate_id for item in review.verification_requests) == (
        candidate.candidate_id,
    )


def test_direct_forged_review_cannot_publish_or_block():
    store, evidence_id = _store(path="tests/not_related.py", category="tests")
    candidate = _candidate(evidence_ids=(evidence_id,))
    forged = AdjudicatedReview(accepted=(candidate,))
    obligations = _controller_obligations()

    notes = build_review_notes(
        forged,
        store,
        "review_comment",
        obligations=obligations,
        changed_files=CHANGED_FILES,
    )
    verdict = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=forged,
        unresolved=(),
        allow_approve=True,
        evidence=store,
        obligations=obligations,
        changed_files=CHANGED_FILES,
    )

    assert all(note.kind is not ReviewNoteKind.FINDING for note in notes)
    assert verdict.verdict == "approve"
    assert verdict.blocking_finding_ids == ()


def test_fingerprint_preserves_operators_negation_and_non_latin_claims():
    store, evidence_id = _store()
    base = _candidate(evidence_ids=(evidence_id,), claim="値が 1 == 1 ではない")
    opposite = replace(base, candidate_id="opposite", claim="値が 1 != 1 ではない")
    non_latin = replace(base, candidate_id="non-latin", claim="значение не равно единице")
    fingerprints = []
    for candidate in (base, opposite, non_latin):
        review = _adjudicate((candidate,), store)
        fingerprints.append(review.accepted[0].root_cause_fingerprint)

    assert len(set(fingerprints)) == 3


def test_exact_dedup_records_contributor_to_representative_disposition():
    store, evidence_id = _store()
    first = _candidate("candidate-a", evidence_ids=(evidence_id,))
    duplicate = replace(first, candidate_id="candidate-b")

    review = _adjudicate((duplicate, first), store)

    assert len(review.accepted) == 1
    assert review.accepted[0].candidate_id == "candidate-a"
    assert review.accepted[0].contributor_candidate_ids == ("candidate-a", "candidate-b")
    dispositions = {item.candidate_id: item for item in review.dispositions}
    assert dispositions["candidate-a"].action == "keep"
    assert dispositions["candidate-b"].action == "merge"
    assert dispositions["candidate-b"].target_id == "candidate-a"


def test_same_claim_with_opposite_causal_root_is_not_exact_dedup():
    store, evidence_id = _store()
    first = _candidate("candidate-a", evidence_ids=(evidence_id,))
    opposite = replace(
        first,
        candidate_id="candidate-b",
        causal_chain="The retry does not repeat a write after an ambiguous response.",
    )

    review = _adjudicate((opposite, first), store)

    assert tuple(item.candidate_id for item in review.accepted) == (
        "candidate-a", "candidate-b",
    )
    assert len({item.root_cause_fingerprint for item in review.accepted}) == 2


def test_explicit_critic_merge_combines_distinct_claims_under_same_causal_root():
    store, evidence_id = _store()
    target = _candidate(
        "candidate-a", evidence_ids=(evidence_id,), claim="The retry duplicates the write"
    )
    consequence = replace(
        target,
        candidate_id="candidate-b",
        claim="The duplicate write emits two audit events",
    )

    review = adjudicate_candidates(
        (consequence, target),
        (
            {"candidate_id": target.candidate_id, "action": "keep"},
            {
                "candidate_id": consequence.candidate_id,
                "action": "merge",
                "target_id": target.candidate_id,
            },
        ),
        store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert len(review.accepted) == 1
    assert review.accepted[0].candidate_id == target.candidate_id
    assert review.accepted[0].contributor_candidate_ids == ("candidate-a", "candidate-b")
    assert next(
        item for item in review.dispositions if item.candidate_id == consequence.candidate_id
    ).action == "merge"


def test_real_source_access_requests_include_context_and_distinguish_url():
    store, _ = _store()
    obligations = _controller_obligations()
    first = SourceAccessRequest(
        host="docs.example.com",
        candidate_url="https://docs.example.com/schema/v1",
        obligation_id="obligation-store",
        purpose="Confirm the deployed schema.",
        authority_reason="The source is not allowlisted.",
    )
    second = replace(first, candidate_url="https://docs.example.com/schema/v2")

    notes = build_review_notes(
        AdjudicatedReview(),
        store,
        "review_comment",
        obligations=obligations,
        changed_files=CHANGED_FILES,
        source_access_requests=(first, second),
    )

    assert len(notes) == 2
    assert all(note.kind is ReviewNoteKind.SOURCE_ACCESS_REQUEST for note in notes)
    assert len({note.fingerprint for note in notes}) == 2
    assert all("docs.example.com" in note.markdown for note in notes)
    assert all("not allowlisted" in note.markdown for note in notes)
    assert all(note.evidence_ids == () for note in notes)


def test_handoff_counts_only_controller_valid_source_access_requests():
    store, _ = _store()
    valid = SourceAccessRequest(
        host="docs.example.com",
        candidate_url="https://docs.example.com/schema/v1",
        obligation_id="obligation-store",
        purpose="Confirm the deployed schema.",
    )
    unknown_obligation = replace(valid, obligation_id="not-controller-owned")
    mismatched_host = replace(valid, host="other.example.com")
    context = ReviewHandoffContext(
        source_access_requests=(mismatched_host, unknown_obligation, valid),
        access_request_url="https://github.example.test/artifacts/source-access",
    )

    handoff = build_review_handoff(
        context,
        review=AdjudicatedReview(),
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert handoff.access_request_count == 1
    assert "[1 open](https://github.example.test/artifacts/source-access)" in handoff.markdown


def test_sparse_handoff_rejects_multiline_markdown_and_detail_injection():
    store, evidence_id = _store()
    review = _adjudicate((_candidate(evidence_ids=(evidence_id,)),), store)
    context = ReviewHandoffContext(
        recommendation="request_changes",
        status="complete",
        change_map=("src/store.py", "# Finding\nA failed retry duplicates the write"),
        specialist_focuses=("database", "Evidence: secret detail"),
        recipe_ids=("transaction-boundaries",),
        coverage_boundaries=("repository-only",),
        review_emphasis=("persistence", "unknown: private detail"),
    )

    handoff = build_review_handoff(
        context,
        review=review,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert "duplicates the write" not in handoff.markdown
    assert "secret detail" not in handoff.markdown
    assert "private detail" not in handoff.markdown
    assert "# Finding" not in handoff.markdown
    assert handoff.change_map == ("src/store.py",)
    assert "review the complete change" in handoff.markdown


def test_sparse_handoff_rejects_single_line_finding_and_unknown_injection():
    store, evidence_id = _store()
    finding = _candidate(evidence_ids=(evidence_id,))
    unknown = replace(
        finding,
        candidate_id="unknown-1",
        claim="The production schema version could not be confirmed",
    )
    review = adjudicate_candidates(
        (finding, unknown),
        {finding.candidate_id: "keep", unknown.candidate_id: "downgrade_unknown"},
        store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )
    context = ReviewHandoffContext(
        change_map=(finding.claim, "src/store.py"),
        specialist_focuses=(unknown.claim, "database"),
        review_emphasis=(finding.claim,),
    )

    handoff = build_review_handoff(
        context,
        review=review,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert finding.claim not in handoff.markdown
    assert unknown.claim not in handoff.markdown
    assert handoff.change_map == ("src/store.py",)


def test_notes_quote_bounded_single_line_values_and_never_raw_evidence():
    raw_evidence = "# Evidence heading\n[click](javascript:alert(1))\n" + "x" * 2000
    store, evidence_id = _store(content=raw_evidence)
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        claim="# Finding\nA failed retry duplicates the write",
        causal_chain="* markdown\nThe retry repeats the write.",
    )
    review = _adjudicate((candidate,), store)

    notes = build_review_notes(
        review,
        store,
        "review_comment",
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert len(notes) == 1
    markdown = notes[0].markdown
    assert "javascript:" not in markdown
    assert raw_evidence not in markdown
    assert "Evidence provenance" in markdown
    assert "User-visible consequence" in markdown
    assert "Causal chain" in markdown
    assert "Suggested validation" in markdown
    assert "\n# Finding" not in markdown


@pytest.mark.parametrize("line", [True, False, 0, -1, "7", object()])
def test_malformed_optional_request_line_is_safe_file_or_general_note(line: object):
    store, _ = _store()
    request = {
        "question": "Can a maintainer verify the retry contract?",
        "related_obligation_ids": ("obligation-store",),
        "file": "src/store.py",
        "line": line,
    }

    notes = build_review_notes(
        AdjudicatedReview(),
        store,
        "review_comment",
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
        verification_requests=(request,),
    )

    assert len(notes) == 1
    assert notes[0].file == "src/store.py"
    assert notes[0].line is None


@pytest.mark.parametrize(
    "configured",
    ["minor", ("minor",), ("arbitrary", "major"), {"major", "minor"}],
)
def test_only_supported_blocking_severities_can_block(configured: object):
    store, evidence_id = _store()
    review = _adjudicate((_candidate(evidence_ids=(evidence_id,), severity="minor"),), store)

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=review,
        unresolved=(),
        allow_approve=True,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
        policy={"blocking_severities": configured},
    )

    assert result.verdict == "approve"
    assert result.blocking_finding_ids == ()


@pytest.mark.parametrize("configured", [42, {"major": True}, [None], object()])
def test_malformed_severity_configuration_fails_closed_without_crashing(configured: object):
    store, evidence_id = _store()
    review = _adjudicate((_candidate(evidence_ids=(evidence_id,)),), store)

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=review,
        unresolved=(),
        allow_approve=True,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
        policy={"blocking_severities": configured},
    )

    assert result.verdict == "approve"
    assert result.blocking_finding_ids == ()
