from __future__ import annotations

from dataclasses import replace

import pytest

from pr_reviewer.specialist_runtime.adjudication import (
    AdjudicatedReview,
    CandidateVerificationRequest,
    ReviewHandoffContext,
    ReviewOrientationTopic,
    adjudicate_candidates,
    apply_runtime_verdict_policy,
    build_review_handoff,
    build_review_notes,
)
from pr_reviewer.specialist_runtime.evidence import (
    EvidenceProvenance,
    EvidenceSnapshot,
    EvidenceStore,
)
from pr_reviewer.specialist_runtime.types import (
    CandidateFinding,
    CoverageObligation,
    ReviewNoteKind,
)
from pr_reviewer.specialist_runtime.web_evidence import (
    SearchCandidate,
    SourceAccessRequest,
    source_access_request,
)


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
    consequence: str = "A user action can be persisted twice.",
    manual_validation: str = "Force an ambiguous retry and verify only one write and audit event.",
    confidence_rationale: str | None = None,
) -> CandidateFinding:
    rationale = confidence_rationale
    if rationale is None:
        rationale = (
            "consequence_support:reachable_input_path; "
            f"evidence_ids={','.join(evidence_ids)}; "
            "input=ambiguous response; condition=retry repeats a write; "
            "outcome=A user action can be persisted twice"
        )
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
        confidence_rationale=rationale,
        user_visible_consequence=consequence,
        manual_validation=manual_validation,
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


@pytest.mark.parametrize(
    ("claim", "causal_chain"),
    (
        (
            "GitHub comment preview may use a zero-based array index",
            "The renderer uses index 0 to select a diff line from a local array.",
        ),
        (
            (
                "`_exact_changed_location` rejects line 0. If an input parser uses a "
                "zero-based list index, its first entry is skipped."
            ),
            "The concern is limited to local parser indexing.",
        ),
    ),
)
def test_zero_based_non_coordinate_concerns_remain_verification_requests(
    claim: str,
    causal_chain: str,
):
    candidate = _candidate(claim=claim, causal_chain=causal_chain)

    review = adjudicate_candidates(
        (candidate,),
        {candidate.candidate_id: "request_verification"},
        EvidenceStore(),
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert review.rejected == ()
    assert review.verification_requests[0].reason == "critic-requested-verification"


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


@pytest.mark.parametrize(
    ("claim", "causal_chain", "consequence", "rationale"),
    (
        (
            "The changed regex may classify an additional topic.",
            "The new alternation is present in the changed classifier.",
            "Unrelated reviews may silently receive the wrong specialist.",
            "The retained source confirms the regex changed.",
        ),
        (
            "Derived expected evidence may reduce specialist rigor.",
            "The controller now derives evidence labels from obligations.",
            "Specialists may miss material defects because their instructions are weaker.",
            "The retained source confirms expected_evidence is controller-derived.",
        ),
    ),
)
def test_changed_mechanism_alone_cannot_authorize_hypothetical_consequence(
    claim: str,
    causal_chain: str,
    consequence: str,
    rationale: str,
):
    store, evidence_id = _store(content="the cited implementation mechanism changed")
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        claim=claim,
        causal_chain=causal_chain,
        consequence=consequence,
        confidence_rationale=rationale,
    )

    review = _adjudicate((candidate,), store)

    assert review.accepted == ()
    assert review.verification_requests[0].reason == "consequence-not-supported"


@pytest.mark.parametrize(
    ("claim", "causal_chain", "consequence", "rationale"),
    (
        (
            "Repository paths can escape the checkout.",
            "An input of ../outside enters read_repository_file without containment validation.",
            "A reviewer can read a file outside the checked-out repository.",
            (
                "consequence_support:reachable_input_path; input=../outside; "
                "condition=without containment validation; "
                "outcome=A reviewer can read a file outside the checked-out repository"
            ),
        ),
        (
            "Delivery diagnostics disclose the caller token.",
            "An invalid endpoint reaches delivery_failure_diagnostic with the raw token.",
            "The secret is returned in the failure diagnostic.",
            (
                "consequence_support:reachable_input_path; input=invalid endpoint; "
                "condition=with the raw token; "
                "outcome=The secret is returned in the failure diagnostic"
            ),
        ),
    ),
)
def test_concrete_reachable_security_consequence_remains_actionable(
    claim: str,
    causal_chain: str,
    consequence: str,
    rationale: str,
):
    store, evidence_id = _store(content=causal_chain)
    rationale = rationale.replace(
        "consequence_support:reachable_input_path;",
        f"consequence_support:reachable_input_path; evidence_ids={evidence_id};",
    )
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        claim=claim,
        causal_chain=causal_chain,
        consequence=consequence,
        confidence_rationale=rationale,
    )

    review = _adjudicate((candidate,), store)

    assert tuple(item.candidate_id for item in review.accepted) == ("candidate-1",)


def test_model_cannot_invent_an_invariant_as_consequence_authority():
    store, evidence_id = _store()
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        confidence_rationale=(
            "consequence_support:violated_invariant; "
            f"evidence_ids={evidence_id}; obligation_id=obligation-store; "
            "contract=predicate_index:99; violation=the code is slower"
        ),
    )

    review = _adjudicate((candidate,), store)

    assert review.accepted == ()
    assert review.verification_requests[0].reason == "consequence-not-supported"


def test_controller_owned_obligation_subject_can_authorize_violated_invariant():
    store, evidence_id = _store()
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        confidence_rationale=(
            "consequence_support:violated_invariant; "
            f"evidence_ids={evidence_id}; obligation_id=obligation-store; "
            "contract=subject; violation=an ambiguous retry repeats the write"
        ),
    )

    review = _adjudicate((candidate,), store)

    assert tuple(item.candidate_id for item in review.accepted) == ("candidate-1",)


def test_typed_words_without_explicit_retained_evidence_cannot_authorize_consequence():
    store, evidence_id = _store()
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        confidence_rationale=(
            "consequence_support:reachable_input_path; "
            "input=anything; condition=anything; outcome=anything"
        ),
    )

    review = _adjudicate((candidate,), store)

    assert review.accepted == ()
    assert review.verification_requests[0].reason == "consequence-not-supported"


def test_retained_id_plus_arbitrary_typed_words_cannot_game_reachable_path():
    store, evidence_id = _store()
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        confidence_rationale=(
            "consequence_support:reachable_input_path; "
            f"evidence_ids={evidence_id}; input=anything; "
            "condition=something; outcome=some result"
        ),
    )

    review = _adjudicate((candidate,), store)

    assert review.accepted == ()
    assert review.verification_requests[0].reason == "consequence-not-supported"


def test_critic_cannot_rescue_unavailable_evidence_by_repeating_specialist_prose():
    store, evidence_id = _store(content="")
    rationale = (
        "consequence_support:reachable_input_path; "
        f"evidence_ids={evidence_id}; input=../outside; "
        "condition=unchecked join; outcome=read outside checkout"
    )
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        causal_chain="../outside reaches an unchecked join before the file read.",
        consequence="A file outside the checkout can be read.",
        confidence_rationale=rationale.replace(
            "condition=unchecked join; outcome=read outside checkout",
            "condition=unchecked join; outcome=A file outside the checkout can be read",
        ),
    )
    rationale = candidate.confidence_rationale
    obligations = _controller_obligations()

    unconfirmed = adjudicate_candidates(
        (candidate,),
        {"actions": [{"candidate_id": candidate.candidate_id, "action": "keep"}]},
        store,
        obligations=obligations,
        changed_files=CHANGED_FILES,
    )
    confirmed = adjudicate_candidates(
        (candidate,),
        {"actions": [{
            "candidate_id": candidate.candidate_id,
            "action": "keep",
            "confidence_rationale": rationale,
        }]},
        store,
        obligations=obligations,
        changed_files=CHANGED_FILES,
    )

    assert unconfirmed.accepted == ()
    assert unconfirmed.verification_requests[0].reason == "consequence-not-supported"
    assert confirmed.accepted == ()
    assert confirmed.verification_requests[0].reason == "consequence-not-supported"


def test_info_candidate_is_not_published_as_an_actionable_finding():
    store, evidence_id = _store()
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        severity="info",
    )

    review = _adjudicate((candidate,), store)

    assert review.accepted == ()
    assert review.verification_requests == ()
    assert review.rejected[0].reason == "non-actionable-info"


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


@pytest.mark.parametrize(
    ("first_claim", "second_claim"),
    [
        ("flags ^ mask", "flags mask"),
        ("balance + 1 overflows", "balance - 1 overflows"),
        ("count * 2 is persisted", "count / 2 is persisted"),
        ("count % 2 is zero", "count = 2 is zero"),
        ("ready && valid", "ready || valid"),
        ("!ready", "ready"),
        ("ready ? primary : fallback", "ready primary fallback"),
        ("size ≤ limit", "size limit"),
        ("size ≥ limit", "size limit"),
        ("size ≠ limit", "size limit"),
    ],
)
def test_public_fingerprint_preserves_arithmetic_and_boolean_operators(
    first_claim: str, second_claim: str,
):
    store, evidence_id = _store()
    first = _candidate("first", evidence_ids=(evidence_id,), claim=first_claim)
    second = _candidate("second", evidence_ids=(evidence_id,), claim=second_claim)

    first_review = _adjudicate((first,), store)
    second_review = _adjudicate((second,), store)

    assert first_review.accepted[0].root_cause_fingerprint != second_review.accepted[0].root_cause_fingerprint


def test_public_fingerprint_is_stable_across_causal_rewording():
    store, evidence_id = _store()
    first = _candidate("first", evidence_ids=(evidence_id,))
    reworded = replace(
        first,
        candidate_id="second",
        causal_chain="An ambiguous result causes the write operation to run again.",
        confidence_rationale=(
            "consequence_support:reachable_input_path; "
            f"evidence_ids={evidence_id}; input=ambiguous result; "
            "condition=write operation to run again; "
            "outcome=A user action can be persisted twice"
        ),
    )

    first_review = _adjudicate((first,), store)
    second_review = _adjudicate((reworded,), store)

    assert first_review.accepted[0].root_cause_fingerprint == second_review.accepted[0].root_cause_fingerprint
    assert first_review.accepted[0].deduplication_key != second_review.accepted[0].deduplication_key


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
        confidence_rationale=(
            "consequence_support:reachable_input_path; "
            f"evidence_ids={evidence_id}; input=ambiguous response; "
            "condition=retry does not repeat a write; "
            "outcome=A user action can be persisted twice"
        ),
    )

    review = _adjudicate((opposite, first), store)

    assert tuple(item.candidate_id for item in review.accepted) == (
        "candidate-a", "candidate-b",
    )
    assert len({item.root_cause_fingerprint for item in review.accepted}) == 1
    assert len({item.deduplication_key for item in review.accepted}) == 2


def test_explicit_critic_merge_combines_distinct_claims_under_same_causal_root():
    store, evidence_id = _store()
    target = _candidate(
        "candidate-a", evidence_ids=(evidence_id,), claim="The retry duplicates the write"
    )
    consequence = replace(
        target,
        candidate_id="candidate-b",
        claim="The duplicate write emits two audit events",
        user_visible_consequence="Operators see duplicate audit entries for one action.",
        manual_validation="Trigger one retry and verify the audit log has one entry.",
        confidence_rationale=(
            "consequence_support:reachable_input_path; "
            f"evidence_ids={evidence_id}; input=ambiguous response; "
            "condition=retry repeats a write; "
            "outcome=Operators see duplicate audit entries for one action"
        ),
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
    finding = review.accepted[0]
    assert finding.candidate_id == target.candidate_id
    assert finding.contributor_candidate_ids == ("candidate-a", "candidate-b")
    assert set(finding.user_visible_consequences) == {
        target.user_visible_consequence,
        consequence.user_visible_consequence,
    }
    assert set(finding.manual_validations) == {
        target.manual_validation,
        consequence.manual_validation,
    }
    assert next(
        item for item in review.dispositions if item.candidate_id == consequence.candidate_id
    ).action == "merge"

    notes = build_review_notes(
        review,
        store,
        "review_comment",
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert len(notes) == 1
    assert all(
        detail in notes[0].markdown
        for detail in (*finding.user_visible_consequences, *finding.manual_validations)
    )


def test_duplicate_consequence_or_validation_placeholder_downgrades_to_verification():
    store, evidence_id = _store()
    complete = _candidate(evidence_ids=(evidence_id,))
    candidates = (
        replace(
            complete,
            candidate_id="claim-as-consequence",
            user_visible_consequence=f"  {complete.claim.upper()}!  ",
        ),
        replace(
            complete,
            candidate_id="claim-as-validation",
            manual_validation=f" {complete.claim.upper()} ",
        ),
        replace(
            complete,
            candidate_id="consequence-as-validation",
            manual_validation=complete.user_visible_consequence.upper(),
        ),
    )

    review = _adjudicate(candidates, store)

    assert review.accepted == ()
    assert tuple(item.reason for item in review.verification_requests) == (
        "non-distinct-required-finding-detail",
        "non-distinct-required-finding-detail",
        "non-distinct-required-finding-detail",
    )


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


def test_source_access_request_preserves_non_default_port_in_identity_and_display():
    store, _ = _store()
    first = source_access_request(
        SearchCandidate(None, "https://docs.example.com/schema/v1"),
        "obligation-store",
        "Confirm the deployed schema.",
    )
    second = source_access_request(
        SearchCandidate(None, "https://docs.example.com:8443/schema/v1"),
        "obligation-store",
        "Confirm the deployed schema.",
    )

    notes = build_review_notes(
        AdjudicatedReview(),
        store,
        "review_comment",
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
        source_access_requests=(first, second),
    )

    assert len(notes) == 2
    assert len({note.fingerprint for note in notes}) == 2
    assert any("docs.example.com:8443/schema/v1" in note.markdown for note in notes)


def test_source_access_request_removes_default_https_port_from_identity():
    store, _ = _store()
    first = source_access_request(
        SearchCandidate(None, "https://docs.example.com/schema/v1"),
        "obligation-store",
        "Confirm the deployed schema.",
    )
    second = source_access_request(
        SearchCandidate(None, "https://docs.example.com:443/schema/v1"),
        "obligation-store",
        "Confirm the deployed schema.",
    )

    notes = build_review_notes(
        AdjudicatedReview(),
        store,
        "review_comment",
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
        source_access_requests=(first, second),
    )

    assert len(notes) == 1
    assert ":443" not in notes[0].markdown


@pytest.mark.parametrize(
    "candidate_url",
    (
        "https://user:pass@docs.example.com/schema/v1",
        "https://docs.example.com:/schema/v1",
        "https://docs.example.com:bad/schema/v1",
        "https://docs.example.com:0/schema/v1",
        "https://docs.example.com:99999/schema/v1",
    ),
)
def test_source_access_request_rejects_userinfo_and_invalid_ports(candidate_url: str):
    store, _ = _store()
    request = SourceAccessRequest(
        host="docs.example.com",
        candidate_url=candidate_url,
        obligation_id="obligation-store",
        purpose="Confirm the deployed schema.",
    )

    notes = build_review_notes(
        AdjudicatedReview(),
        store,
        "review_comment",
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
        source_access_requests=(request,),
    )

    assert notes == ()


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
        change_topics=(ReviewOrientationTopic.DATABASE,),
        component_ids=("src/store.py", "# Finding\nA failed retry duplicates the write"),
        specialist_topics=(ReviewOrientationTopic.DATABASE,),
        recipe_ids=("transaction-boundaries", "Evidence: secret detail"),
        coverage_boundary_topics=(ReviewOrientationTopic.TEST_COVERAGE,),
        review_emphasis_topics=(ReviewOrientationTopic.FAILURE_RECOVERY,),
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
    assert handoff.change_map == ("Database and persistence",)
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
        component_ids=(finding.claim, unknown.claim, "src/store.py"),
        specialist_topics=(ReviewOrientationTopic.DATABASE,),
        review_emphasis_topics=(ReviewOrientationTopic.DATABASE,),
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
    assert handoff.change_map == ()


def test_handoff_taxonomy_excludes_short_claims_and_preserves_useful_topic():
    store, evidence_id = _store(content="database")
    auth = _candidate(
        "auth", evidence_ids=(evidence_id,), claim="Auth", category="authorization"
    )
    cache = _candidate(
        "cache", evidence_ids=(evidence_id,), claim="Cache", category="caching"
    )
    review = adjudicate_candidates(
        (auth, cache),
        {auth.candidate_id: "keep", cache.candidate_id: "downgrade_unknown"},
        store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )
    context = ReviewHandoffContext(
        change_topics=(ReviewOrientationTopic.DATABASE,),
        component_ids=("auth", "cache", "database-worker"),
        specialist_topics=(ReviewOrientationTopic.DATABASE,),
        review_emphasis_topics=(ReviewOrientationTopic.DATABASE,),
    )

    handoff = build_review_handoff(
        context,
        review=review,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert "Auth" not in handoff.markdown
    assert "Cache" not in handoff.markdown
    assert "database-worker" not in handoff.markdown
    assert "Database and persistence" in handoff.markdown
    assert handoff.human_focus == ("Database and persistence",)


def test_handoff_excludes_exact_evidence_content_source_id_and_hash():
    store, evidence_id = _store(content="sensitive-evidence-topic")
    record = store.snapshot().get(evidence_id)
    assert record is not None
    candidate = _candidate(evidence_ids=(evidence_id,))
    review = _adjudicate((candidate,), store)
    context = ReviewHandoffContext(
        component_ids=(
            "sensitive-evidence-topic",
            record.content_hash,
            evidence_id,
            "safe-component",
        ),
        recipe_ids=(record.source_path or "", "safe-recipe"),
    )

    handoff = build_review_handoff(
        context,
        review=review,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert "sensitive-evidence-topic" not in handoff.markdown
    assert record.content_hash not in handoff.markdown
    assert evidence_id not in handoff.markdown
    assert (record.source_path or "not-present") not in handoff.markdown
    assert "safe-component" not in handoff.markdown
    assert "safe-recipe" not in handoff.markdown
    assert handoff.what_changed == ()
    assert handoff.ai_reviewed == ()


def test_handoff_recursively_excludes_unknown_request_obligation_and_source_details():
    store, evidence_id = _store()
    accepted_review = _adjudicate((_candidate(evidence_ids=(evidence_id,)),), store)
    unknown = _candidate(
        "unknown-candidate",
        claim="unknown-claim",
        evidence_ids=(evidence_id,),
        obligation_ids=("unknown-obligation",),
        consequence="unknown-consequence",
        manual_validation="unknown-validation",
    )
    verification = _candidate(
        "verification-candidate",
        claim="verification-claim",
        evidence_ids=(evidence_id,),
        obligation_ids=("verification-obligation",),
        consequence="verification-consequence",
        manual_validation="verification-validation",
    )
    review = replace(
        accepted_review,
        unknowns=(unknown,),
        verification_requests=(
            CandidateVerificationRequest(verification, "verification-reason"),
        ),
    )
    source_request = SourceAccessRequest(
        host="source-host",
        candidate_url="https://source-host/source-path",
        obligation_id="source-obligation",
        purpose="Database and persistence",
        authority_reason="source-authority",
    )
    injected = (
        "unknown-candidate",
        "unknown-claim",
        "unknown-obligation",
        "unknown-consequence",
        "unknown-validation",
        "verification-candidate",
        "verification-claim",
        "verification-obligation",
        "verification-consequence",
        "verification-validation",
        "verification-reason",
        "source-host",
        "source-obligation",
        "source-authority",
    )
    context = ReviewHandoffContext(
        change_topics=(ReviewOrientationTopic.DATABASE,),
        component_ids=(*injected, "safe-component"),
        recipe_ids=injected,
        source_access_requests=(source_request,),
    )

    handoff = build_review_handoff(
        context,
        review=review,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert all(item not in handoff.markdown for item in injected)
    assert "Database and persistence" not in handoff.markdown
    assert "safe-component" not in handoff.markdown
    assert handoff.what_changed == ()
    assert handoff.ai_reviewed == ()
    assert handoff.access_request_count == 0


def test_handoff_recursively_excludes_every_evidence_record_and_provenance_field():
    store = EvidenceStore()
    record = store.add_tool_result(
        session_id="collector-metadata",
        model_identity="model-metadata",
        tool="tool-metadata",
        arguments={"path": "metadata-path", "selector": "argument-topic"},
        result={"status": "ok", "content": "evidence-content"},
        category="evidence-category",
        source="source-metadata",
        mime_type="mime-topic",
        provenance=EvidenceProvenance(
            head_sha="head-sha",
            policy_hash="policy-hash",
            policy_rule_id="Database and persistence",
            source_classification="source-classification",
            original_url="provenance-original",
            final_url="provenance-final",
            retrieved_at=1234.5,
            max_age_hours=6.5,
        ),
        now=1234.5,
    )
    obligation = _obligation(category="evidence-category")
    review = _adjudicate(
        (_candidate(evidence_ids=(record.id,)),),
        store,
        obligations=_controller_obligations(obligation),
    )
    record = replace(
        record,
        source_identity="source-identity",
        imported_by=("imported-session",),
        supersedes=("superseded-evidence",),
        contradicts=("contradicted-evidence",),
        truncated=True,
        redacted=False,
    )
    snapshot = EvidenceSnapshot((record,))
    injected = (
        "evidence-category",
        "collector-metadata",
        "model-metadata",
        "tool-metadata",
        "argument-topic",
        "source-identity",
        "metadata-path",
        "ok",
        "evidence-content",
        record.content_hash,
        "mime-topic",
        "true",
        "false",
        "imported-session",
        "superseded-evidence",
        "contradicted-evidence",
        "head-sha",
        "policy-hash",
        "source-classification",
        "provenance-original",
        "provenance-final",
        "1234.5",
        "6.5",
    )
    context = ReviewHandoffContext(
        change_topics=(ReviewOrientationTopic.DATABASE,),
        component_ids=(*injected, "safe-component"),
        recipe_ids=injected,
    )

    handoff = build_review_handoff(
        context,
        review=review,
        evidence=snapshot,
        obligations=_controller_obligations(obligation),
        changed_files=CHANGED_FILES,
    )

    assert all(item not in handoff.markdown for item in injected)
    assert "Database and persistence" not in handoff.markdown
    assert "safe-component" not in handoff.markdown
    assert handoff.what_changed == ()
    assert handoff.ai_reviewed == ()


def test_handoff_rechecks_every_dynamic_value_after_prefix_and_rendering():
    store = EvidenceStore()
    rendered_details = (
        "Database and persistence",
        "Component: safe-component",
        "Repository recipe: safe-recipe",
        "Approve",
        "AI review complete",
        "2 detail review notes prepared for publication; "
        "highest proposed finding severity: major.",
        "Material evidence or session coverage is incomplete.",
    )
    records = tuple(
        store.add_tool_result(
            session_id="session-1",
            tool="read_file",
            arguments={"path": "src/store.py" if index == 0 else f"metadata/{index}"},
            result={"status": "ok", "content": detail},
            category="implementation",
        )
        for index, detail in enumerate(rendered_details)
    )
    candidates = (
        _candidate("one", evidence_ids=(records[0].id,)),
        _candidate(
            "two",
            evidence_ids=(records[0].id,),
            claim="A separate retry path duplicates the write",
            causal_chain="A separate retry branch repeats the write.",
        ),
    )
    review = _adjudicate(candidates, store)
    context = ReviewHandoffContext(
        recommendation="approve",
        status="complete",
        change_topics=(ReviewOrientationTopic.DATABASE,),
        component_ids=("safe-component",),
        specialist_topics=(ReviewOrientationTopic.DATABASE,),
        recipe_ids=("safe-recipe",),
        coverage_boundary_topics=(ReviewOrientationTopic.DATABASE,),
        unresolved_thread_count=2,
        highest_thread_severity="major",
        review_emphasis_topics=(ReviewOrientationTopic.DATABASE,),
        material_coverage_limited=True,
    )

    handoff = build_review_handoff(
        context,
        review=review,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert all(detail not in handoff.markdown for detail in rendered_details)
    assert handoff.recommendation == ""
    assert handoff.change_map == ()
    assert handoff.reviewed_focuses == ()
    assert handoff.thread_status is None
    assert handoff.finding_theme is None
    assert handoff.review_emphasis == ()
    assert handoff.coverage_warning is None


def test_handoff_omits_detail_derived_diagnostics_and_candidate_access_urls():
    diagnostics_url = "https://artifacts.example.test/run/diagnostics"
    store = EvidenceStore()
    store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "src/store.py"},
        result={"status": "ok", "content": "implementation evidence"},
        category="implementation",
        provenance=EvidenceProvenance(
            original_url="https://ARTIFACTS.example.test:443/run/diagnostics"
        ),
    )
    source_url = "https://sources.example.test:8443/schema/v1"
    request = SourceAccessRequest(
        host="sources.example.test",
        candidate_url=source_url,
        obligation_id="obligation-store",
        purpose="Confirm the external schema.",
    )
    context = ReviewHandoffContext(
        material_coverage_limited=True,
        diagnostics_url=diagnostics_url,
        source_access_requests=(request,),
        access_request_url=source_url,
    )

    handoff = build_review_handoff(
        context,
        review=AdjudicatedReview(),
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert diagnostics_url not in handoff.markdown
    assert source_url not in handoff.markdown
    assert handoff.coverage_warning == "Material evidence or session coverage is incomplete."
    assert handoff.access_request_count == 1
    assert handoff.access_request_url is None
    assert "**Source access requests:** 1 open" in handoff.markdown


def test_category_disguised_as_claim_cannot_be_aggregate_theme():
    store, evidence_id = _store()
    candidates = (
        _candidate("one", evidence_ids=(evidence_id,), claim="credential-leak", category="credential-leak"),
        _candidate(
            "two",
            evidence_ids=(evidence_id,),
            claim="credential-leak",
            category="credential-leak",
            causal_chain="A separate causal explanation for the same disguised category.",
        ),
    )
    review = _adjudicate(candidates, store)

    handoff = build_review_handoff(
        ReviewHandoffContext(),
        review=review,
        evidence=store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert handoff.finding_theme is None
    assert "credential-leak" not in handoff.markdown


def test_notes_quote_bounded_single_line_values_and_never_raw_evidence():
    raw_evidence = "# Evidence heading\n[click](javascript:alert(1))\n" + "x" * 2000
    store, evidence_id = _store(content=raw_evidence)
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        claim="# Finding\nA failed retry duplicates the write",
        causal_chain="* markdown\nThe retry repeats the write.",
        confidence_rationale=(
            "consequence_support:reachable_input_path; "
            f"evidence_ids={evidence_id}; input=retry; "
            "condition=retry repeats the write; "
            "outcome=A user action can be persisted twice"
        ),
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
    assert "Supporting evidence provenance" in markdown
    assert "User-visible consequence" in markdown
    assert "Causal chain" in markdown
    assert "Suggested validation" in markdown
    assert "\n# Finding" not in markdown


def test_finding_note_separates_supporting_and_contradicting_provenance():
    store, supporting_id = _store()
    contradiction = store.add_tool_result(
        session_id="session-2",
        tool="read_file",
        arguments={"path": "src/store.py"},
        result={"status": "ok", "content": "A test suggests retries are idempotent."},
        category="tests",
    )
    candidate = _candidate(evidence_ids=(supporting_id,))
    candidate = replace(candidate, contradicting_evidence_ids=(contradiction.id,))
    review = _adjudicate((candidate,), store)

    notes = build_review_notes(
        review,
        store,
        "review_comment",
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert len(notes) == 1
    finding = review.accepted[0]
    assert tuple(item.evidence_id for item in finding.supporting_citations) == (supporting_id,)
    assert tuple(item.evidence_id for item in finding.contradicting_citations) == (contradiction.id,)
    assert "Supporting evidence provenance" in notes[0].markdown
    assert "Contradicting evidence provenance" in notes[0].markdown


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


@pytest.mark.parametrize("obligation_ids", [(), ("not-controller-owned",)])
def test_candidate_verification_without_known_obligation_stays_private(
    obligation_ids: tuple[str, ...],
):
    store, evidence_id = _store()
    candidate = _candidate(
        evidence_ids=(evidence_id,),
        location="",
        obligation_ids=obligation_ids,
    )
    review = adjudicate_candidates(
        (candidate,),
        {candidate.candidate_id: "keep"},
        store,
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    notes = build_review_notes(
        review,
        store,
        "review_comment",
        obligations=_controller_obligations(),
        changed_files=CHANGED_FILES,
    )

    assert review.accepted == ()
    assert notes == ()


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
    review = _adjudicate((
        _candidate(evidence_ids=(evidence_id,), severity="major"),
    ), store)

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

    assert result.verdict == "request_changes"
    assert result.blocking_finding_ids


@pytest.mark.parametrize(
    "configured",
    [
        "critical",
        42,
        ("unknown",),
        ("critical", "unknown"),
        {"unknown"},
        object(),
    ],
)
def test_invalid_high_risk_tier_configuration_uses_secure_default(configured: object):
    obligation = _obligation(
        risk_tier="high", unresolved_policy="block_when_unresolved"
    )

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=AdjudicatedReview(),
        unresolved=(obligation,),
        allow_approve=True,
        evidence=EvidenceStore(),
        obligations=_controller_obligations(obligation),
        changed_files=CHANGED_FILES,
        policy={"high_risk_tiers": configured},
    )

    assert result.verdict == "request_changes"
    assert result.source == "incomplete-high-risk-coverage"


def test_supported_high_risk_tier_subset_remains_configurable():
    obligation = _obligation(
        risk_tier="high", unresolved_policy="block_when_unresolved"
    )

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=AdjudicatedReview(),
        unresolved=(obligation,),
        allow_approve=True,
        evidence=EvidenceStore(),
        obligations=_controller_obligations(obligation),
        changed_files=CHANGED_FILES,
        policy={"high_risk_tiers": ("critical",)},
    )

    assert result.verdict == "approve"
    assert result.unknown_obligation_ids == (obligation.obligation_id,)
