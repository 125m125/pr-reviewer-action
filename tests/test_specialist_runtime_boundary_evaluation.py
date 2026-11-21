import json

import pytest

from pr_reviewer.specialist_runtime.boundary_evaluation import (
    BoundaryEvaluation,
    build_boundary_context,
    validate_boundary_evaluation,
)
from pr_reviewer.specialist_runtime.evidence import EvidenceProvenance, EvidenceStore
from pr_reviewer.specialist_runtime.obligation_assessment import (
    ObligationAssessment,
    ObligationDisposition,
)
from pr_reviewer.specialist_runtime.policy import BoundaryPolicy


HEAD_SHA = "a" * 40


def _runtime_input_obligations():
    from pathlib import Path
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import load_review_policy

    policy = load_review_policy(Path(__file__).parents[1] / ".github/ai-review-policy.json")
    obligations = derive_obligations({
        "changed_files": ["action.yml"],
        "components": [{"id": "orchestration", "changed_files": ["action.yml"]}],
    }, {}, policy)
    return (
        next(item for item in policy.boundaries if item.id == "action-runtime-inputs"),
        tuple(item for item in obligations if item.boundary_id == "action-runtime-inputs" and not item.evaluator_owned),
    )


def test_boundary_accepts_unchanged_inspected_runtime_sources_without_changed_coverage():
    from pr_reviewer.specialist_runtime.coverage import _assessment_evidence_satisfies
    from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessmentLedger

    boundary, obligations = _runtime_input_obligations()
    store = EvidenceStore()
    contract = _record(store, path="action.yml", content="specialist_allow_approve: false")
    config = _record(store, path="scripts/sections/config.sh", content="export SPECIALIST_ALLOW_APPROVE")
    parser = _record(store, path="pr_reviewer/specialist_runtime/policy.py", content="_boolean(env, 'SPECIALIST_ALLOW_APPROVE', False)")
    assessments = []
    for obligation in obligations:
        endpoint = parser if obligation.participant_id == "specialist-runtime" else config
        ledger = ObligationAssessmentLedger(session_id="session-1", obligations=(obligation,), obligation_ids=(obligation.id,))
        result = ledger.propose(
            target="O1", disposition="covered", reason="Input default and runtime parser preserve the same boolean value.",
            evidence_ids=(contract.id, endpoint.id), next_actions=(), evidence=store.snapshot(),
            eligible=_assessment_evidence_satisfies,
            assessed_paths=("action.yml", endpoint.source_path), omitted_paths=(),
        )
        assert result.accepted, result.reason
        assert ledger.assessment("O1").assessed_paths == ("action.yml",)
        assert endpoint.id in ledger.assessment("O1").evidence_ids
        assessments.append(ledger.assessment("O1"))
    context = build_boundary_context(boundary, assessments, store.snapshot(), max_bytes=30_000,
        obligations={item.id: item.participant_id for item in obligations}, expected_head_sha=HEAD_SHA)
    assert context["incomplete"] is False
    assert context["participant_evidence_ids"]["specialist-runtime"] == [parser.id]


@pytest.mark.parametrize("path,head_sha", [
    ("action.yml", HEAD_SHA),
    ("pr_reviewer/specialist_runtime/session.py", HEAD_SHA),
    ("pr_reviewer/specialist_runtime/cli.py", None),
])
def test_boundary_covered_rejects_evidence_without_usable_participant_endpoint(path, head_sha):
    from pr_reviewer.specialist_runtime.coverage import _assessment_evidence_satisfies
    from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessmentLedger

    _boundary_policy, obligations = _runtime_input_obligations()
    obligation = next(item for item in obligations if item.participant_id == "specialist-runtime")
    store = EvidenceStore()
    record = _record(store, path=path, content="input plumbing", head_sha=head_sha)
    ledger = ObligationAssessmentLedger(session_id="session-1", obligations=(obligation,), obligation_ids=(obligation.id,))
    result = ledger.propose(
        target="O1", disposition="covered", reason="Runtime accepts the action input values.",
        evidence_ids=(record.id,), next_actions=(), evidence=store.snapshot(), eligible=_assessment_evidence_satisfies,
        assessed_paths=("action.yml",), omitted_paths=(),
    )
    assert not result.accepted
    assert "participant source" in result.reason
    assert "cli.py" in result.reason


@pytest.mark.parametrize("assessed,omitted,cite_endpoint,accepted", [
    (("pr_reviewer/specialist_runtime/policy.py",), (), True, True),
    (("action.yml", "pr_reviewer/specialist_runtime/policy.py"), (), False, False),
    (("action.yml", "unrelated.py"), (), True, False),
    (("action.yml", "pr_reviewer/specialist_runtime/*.py"), (), True, False),
    (("action.yml",), ("pr_reviewer/specialist_runtime/policy.py",), True, False),
])
def test_boundary_supporting_paths_require_exact_cited_sources_and_do_not_cover_changed_paths(
    assessed, omitted, cite_endpoint, accepted,
):
    from pr_reviewer.specialist_runtime.coverage import _assessment_evidence_satisfies
    from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessmentLedger

    _boundary_policy, obligations = _runtime_input_obligations()
    obligation = next(item for item in obligations if item.participant_id == "specialist-runtime")
    store = EvidenceStore()
    contract = _record(store, path="action.yml", content="input default: false")
    parser = _record(store, path="pr_reviewer/specialist_runtime/policy.py", content="boolean input parser")
    ledger = ObligationAssessmentLedger(session_id="session-1", obligations=(obligation,), obligation_ids=(obligation.id,))
    result = ledger.propose(
        target="O1", disposition="covered", reason="Runtime parsing preserves the action input values.",
        evidence_ids=(contract.id, parser.id) if cite_endpoint else (contract.id,),
        next_actions=(), evidence=store.snapshot(), eligible=_assessment_evidence_satisfies,
        assessed_paths=assessed, omitted_paths=omitted,
    )
    assert result.accepted is accepted
    if accepted:
        assert ledger.assessment("O1").disposition is ObligationDisposition.PARTIALLY_COVERED
        assert ledger.assessment("O1").assessed_paths == ()
        assert ledger.assessment("O1").omitted_paths == ("action.yml",)


def test_boundary_admission_and_evaluator_accept_pathless_negative_search():
    from pr_reviewer.specialist_runtime.coverage import _assessment_evidence_satisfies
    from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessmentLedger

    boundary, obligations = _runtime_input_obligations()
    obligation = next(item for item in obligations if item.participant_id == "specialist-runtime")
    store = EvidenceStore()
    lookup = store.add_tool_result(
        session_id="session-1", tool="git_grep", arguments={"pattern": "REMOVED_INPUT"},
        result={"status": "ok", "result": {"matches": []}},
        provenance=EvidenceProvenance(head_sha=HEAD_SHA),
    )
    ledger = ObligationAssessmentLedger(session_id="session-1", obligations=(obligation,), obligation_ids=(obligation.id,))
    result = ledger.propose(
        target="O1", disposition="not_applicable", reason="No runtime usage of the removed input remains.",
        evidence_ids=(lookup.id,), next_actions=(), evidence=store.snapshot(), eligible=_assessment_evidence_satisfies,
    )
    assert result.accepted, result.reason
    context = build_boundary_context(boundary, ledger.assessments(), store.snapshot(), max_bytes=20_000,
        obligations={obligation.id: obligation.participant_id}, expected_head_sha=HEAD_SHA)
    assert context["participant_evidence_ids"]["specialist-runtime"] == [lookup.id]


@pytest.mark.parametrize("assessed_paths", [
    ("action.yml",),
    ("action.yml", "pr_reviewer/specialist_runtime/cli.py"),
])
def test_boundary_admission_rejects_wrong_head_endpoint_and_supporting_paths(assessed_paths):
    from pr_reviewer.specialist_runtime.coverage import _assessment_evidence_satisfies
    from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessmentLedger

    _boundary_policy, obligations = _runtime_input_obligations()
    obligation = next(item for item in obligations if item.participant_id == "specialist-runtime")
    store = EvidenceStore()
    source = _record(store, path="pr_reviewer/specialist_runtime/cli.py", content="input parser", head_sha="b" * 40)
    ledger = ObligationAssessmentLedger(
        session_id="session-1", obligations=(obligation,), obligation_ids=(obligation.id,),
        expected_head_sha=HEAD_SHA,
    )
    result = ledger.propose(
        target="O1", disposition="covered", reason="Runtime parsing matches the input default.",
        evidence_ids=(source.id,), next_actions=(), evidence=store.snapshot(),
        eligible=_assessment_evidence_satisfies, assessed_paths=assessed_paths,
    )
    assert not result.accepted
    assert ledger.assessment("O1").disposition is ObligationDisposition.PENDING


def _boundary():
    return BoundaryPolicy(
        id="backend-worker-messages",
        contract_paths=("contracts/**",),
        participants=("backend", "worker"),
        contract_change_owner="backend",
        endpoint_paths={},
        objective="Does showId retain database identity from producer to consumer?",
    )


def _record(store, *, path, content, tool="read_file", start_line=1, end_line=4, **kwargs):
    return store.add_tool_result(
        session_id="session-1",
        tool=tool,
        arguments={"path": path, "start_line": start_line, "end_line": end_line},
        result={"status": "ok", "content": content},
        provenance=EvidenceProvenance(
            head_sha=kwargs.get("head_sha", HEAD_SHA),
            retrieved_at=kwargs.get("retrieved_at"),
        ),
        contradicts=kwargs.get("contradicts", ()),
    )


def _assessment(obligation_id, evidence_id, *, disposition=ObligationDisposition.COVERED, reason="Checked behavior", version=1):
    return ObligationAssessment(
        target=obligation_id,
        obligation_id=obligation_id,
        disposition=disposition,
        reason=reason,
        evidence_ids=(evidence_id,),
        assessment_version=version,
    )


def test_partial_boundary_assessment_does_not_request_source_already_retained():
    from dataclasses import replace

    store = EvidenceStore()
    _record(store, path="contracts/message.proto", content="string showId = 1;")
    producer = _record(store, path="backend/Producer.java", content="send(show.id)")
    consumer = _record(store, path="worker/consumer.py", content="load(message.showId)")
    partial = replace(
        _assessment("OB-backend", producer.id,
                    disposition=ObligationDisposition.PARTIALLY_COVERED),
        next_actions=("Confirm the changed identifier conversion.",),
    )
    context = build_boundary_context(
        _boundary(), (partial, _assessment("OB-worker", consumer.id)),
        store.snapshot(), max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
    )
    assert context["incomplete"] is True
    assert context["participant_evidence_ids"]["backend"] == [producer.id]
    assert "missing usable source evidence for participant backend" not in context["diagnostics"]
    assert "Confirm the changed identifier conversion." in context["suggested_investigation"]
    assert "assessment" in context["suggested_investigation"]


def test_build_boundary_context_retains_actual_source_and_revision_provenance():
    store = EvidenceStore()
    schema = _record(
        store,
        path="contracts/message.proto",
        content="string showId = 1;",
        start_line=10,
        end_line=10,
    )
    producer = _record(
        store,
        path="backend/Producer.java",
        content="builder.setShowId(show.getDatabaseId());",
        start_line=40,
        end_line=42,
    )
    consumer = _record(
        store,
        path="worker/consumer.py",
        content="load_show(primary_key=message.showId)",
        start_line=20,
        end_line=22,
    )

    context = build_boundary_context(
        _boundary(),
        (
            _assessment("OB-backend", producer.id),
            _assessment("OB-worker", consumer.id),
        ),
        store.snapshot(),
        max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
    )

    assert context["incomplete"] is False
    assert context["boundary"]["contract_paths"] == ["contracts/**"]
    assert context["boundary"]["contract_change_owner"] == "backend"
    assert context["missing_participants"] == []
    assert context["required_participants"] == ["backend", "worker"]
    assert {item["id"] for item in context["evidence"]} == {
        schema.id,
        producer.id,
        consumer.id,
    }
    producer_packet = next(
        item for item in context["evidence"] if item["id"] == producer.id
    )
    assert producer_packet["content"] == "builder.setShowId(show.getDatabaseId());"
    assert producer_packet["arguments"] == {
        "end_line": 42,
        "path": "backend/Producer.java",
        "start_line": 40,
    }
    assert producer_packet["provenance"]["head_sha"] == HEAD_SHA
    assert producer_packet["content_hash"] == producer.content_hash
    assert producer_packet["truncated"] is False
    assert context["input_fingerprint"].startswith("boundary:")


def test_validate_supported_evaluation_binds_question_fingerprint_and_source_citations():
    store = EvidenceStore()
    schema = _record(store, path="contracts/message.proto", content="string showId = 1;")
    producer = _record(store, path="backend/Producer.java", content="showId = databaseId")
    consumer = _record(store, path="worker/consumer.py", content="lookup(pk=showId)")
    context = build_boundary_context(
        _boundary(),
        (
            _assessment("OB-backend", producer.id),
            _assessment("OB-worker", consumer.id),
        ),
        store.snapshot(),
        max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
        expected_head_sha=HEAD_SHA,
    )

    result = validate_boundary_evaluation({
        "question": context["question"],
        "outcome": "supported",
        "reason": "Producer and consumer both use the database identity.",
        "evidence_ids": [schema.id, producer.id, consumer.id],
    }, context)

    assert result == BoundaryEvaluation(
        boundary_id="backend-worker-messages",
        input_fingerprint=context["input_fingerprint"],
        outcome="supported",
        reason="Producer and consumer both use the database identity.",
        evidence_ids=(schema.id, producer.id, consumer.id),
    )


def test_schema_valid_identity_mismatch_can_only_be_reported_as_potential_contradiction():
    store = EvidenceStore()
    schema = _record(store, path="contracts/message.proto", content="string showId = 1;")
    producer = _record(
        store,
        path="backend/Producer.java",
        content="showId = externalProviderId",
    )
    consumer = _record(
        store,
        path="worker/consumer.py",
        content="load_show(primary_key=message.showId)",
    )
    context = build_boundary_context(
        _boundary(),
        (
            _assessment("OB-backend", producer.id),
            _assessment("OB-worker", consumer.id),
        ),
        store.snapshot(),
        max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
    )

    result = validate_boundary_evaluation({
        "outcome": "potential_contradiction",
        "reason": "Producer supplies an external ID but consumer performs a primary-key lookup.",
        "evidence_ids": [producer.id, consumer.id, schema.id],
        "missing_fact": "Confirm whether external and database IDs are identical.",
        "suggested_investigation": "Trace ID translation before message construction.",
    }, context)

    assert result.outcome == "potential_contradiction"
    assert result.missing_fact == "Confirm whether external and database IDs are identical."
    assert producer.content in next(
        item["content"] for item in context["evidence"] if item["id"] == producer.id
    )
    assert consumer.content in next(
        item["content"] for item in context["evidence"] if item["id"] == consumer.id
    )


def test_not_applicable_lookup_is_usable_until_new_evidence_contradicts_it():
    store = EvidenceStore()
    _record(store, path="contracts/message.proto", content="string showId = 1;")
    lookup = _record(
        store,
        path="backend/search.txt",
        content="No affected producer usage found",
        tool="git_grep",
    )
    worker = _record(store, path="worker/consumer.py", content="lookup(pk=showId)")
    assessments = (
        _assessment(
            "OB-backend",
            lookup.id,
            disposition=ObligationDisposition.NOT_APPLICABLE,
            reason="No affected producer usage found in the checked paths.",
        ),
        _assessment("OB-worker", worker.id),
    )
    mapping = {"OB-backend": "backend", "OB-worker": "worker"}

    before = build_boundary_context(
        _boundary(), assessments, store.snapshot(), max_bytes=20_000,
        obligations=mapping,
    )
    _record(
        store,
        path="backend/new_producer.java",
        content="send(showId)",
        contradicts=(lookup.id,),
    )
    after = build_boundary_context(
        _boundary(), assessments, store.snapshot(), max_bytes=20_000,
        obligations=mapping,
    )

    assert before["incomplete"] is False
    assert after["incomplete"] is True
    assert "backend" in after["missing_participants"]


def test_fingerprint_excludes_timestamps_and_unrelated_assessment_updates():
    def context(retrieved_at, *, worker_version=1, include_unrelated=False):
        store = EvidenceStore()
        _record(
            store,
            path="contracts/message.proto",
            content="string showId = 1;",
            retrieved_at=retrieved_at,
        )
        producer = _record(
            store,
            path="backend/Producer.java",
            content="showId = databaseId",
            retrieved_at=retrieved_at,
        )
        consumer = _record(
            store,
            path="worker/consumer.py",
            content="lookup(pk=showId)",
            retrieved_at=retrieved_at,
        )
        assessments = [
            _assessment("OB-backend", producer.id),
            _assessment("OB-worker", consumer.id, version=worker_version),
        ]
        mapping = {"OB-backend": "backend", "OB-worker": "worker"}
        if include_unrelated:
            unrelated = _record(
                store,
                path="docs/readme.md",
                content="metadata-only update",
                retrieved_at=retrieved_at,
            )
            assessments.append(_assessment("OB-docs", unrelated.id, version=99))
            mapping["OB-docs"] = "docs"
        return build_boundary_context(
            _boundary(), assessments, store.snapshot(), max_bytes=20_000,
            obligations=mapping,
        )

    first = context(1.0)
    metadata_only = context(999.0, include_unrelated=True)
    changed_assessment = context(999.0, worker_version=2)

    assert metadata_only["input_fingerprint"] == first["input_fingerprint"]
    assert changed_assessment["input_fingerprint"] != first["input_fingerprint"]


def test_oversized_required_evidence_returns_bounded_incomplete_packet():
    store = EvidenceStore(max_content_bytes=20_000)
    _record(store, path="contracts/message.proto", content="schema " + "x" * 5_000)
    producer = _record(store, path="backend/Producer.java", content="producer " + "x" * 5_000)
    consumer = _record(store, path="worker/consumer.py", content="consumer " + "x" * 5_000)

    context = build_boundary_context(
        _boundary(),
        (
            _assessment("OB-backend", producer.id),
            _assessment("OB-worker", consumer.id),
        ),
        store.snapshot(),
        max_bytes=3_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
    )

    assert context["incomplete"] is True
    assert context["evidence"] == []
    assert context["missing_participants"] == ["backend", "worker"]
    assert any("exceeds max_bytes" in item for item in context["diagnostics"])
    assert len(json.dumps(context, sort_keys=True).encode("utf-8")) <= 3_000


def test_missing_or_mixed_revision_provenance_marks_packet_incomplete():
    store = EvidenceStore()
    schema = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "contracts/message.proto"},
        result={"status": "ok", "content": "string showId = 1;"},
    )
    producer = _record(store, path="backend/Producer.java", content="showId = databaseId")
    consumer = _record(store, path="worker/consumer.py", content="lookup(pk=showId)")

    missing = build_boundary_context(
        _boundary(),
        (_assessment("OB-backend", producer.id), _assessment("OB-worker", consumer.id)),
        store.snapshot(),
        max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
        expected_head_sha=HEAD_SHA,
    )
    assert missing["incomplete"] is True
    assert schema.id not in missing["source_evidence_ids"]
    assert any("missing immutable head_sha" in item for item in missing["diagnostics"])

    mixed_store = EvidenceStore()
    _record(mixed_store, path="contracts/message.proto", content="string showId = 1;")
    mixed_producer = _record(
        mixed_store, path="backend/Producer.java", content="showId = databaseId",
    )
    mixed_consumer = _record(
        mixed_store,
        path="worker/consumer.py",
        content="lookup(pk=showId)",
        head_sha="b" * 40,
    )
    mixed = build_boundary_context(
        _boundary(),
        (
            _assessment("OB-backend", mixed_producer.id),
            _assessment("OB-worker", mixed_consumer.id),
        ),
        mixed_store.snapshot(),
        max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
    )

    assert mixed["incomplete"] is True
    assert any("mixed head_sha" in item for item in mixed["diagnostics"])


def test_missing_relevant_participant_stays_explicitly_incomplete():
    store = EvidenceStore()
    _record(store, path="contracts/message.proto", content="string showId = 1;")
    producer = _record(store, path="backend/Producer.java", content="showId = databaseId")

    context = build_boundary_context(
        _boundary(),
        (_assessment("OB-backend", producer.id),),
        store.snapshot(),
        max_bytes=20_000,
        obligations={"OB-backend": "backend"},
    )

    assert context["incomplete"] is True
    assert context["missing_participants"] == ["worker"]
    assert any("participant worker" in item for item in context["diagnostics"])


def test_failed_supplemental_lookup_does_not_block_complete_source_proof():
    store = EvidenceStore()
    schema = _record(store, path="contracts/message.proto", content="string showId = 1;")
    producer = _record(store, path="backend/Producer.java", content="showId = databaseId")
    failed_lookup = store.add_tool_result(
        session_id="session-1",
        tool="git_grep",
        arguments={"path": "backend", "query": "showId"},
        result={"status": "error", "content": "lookup timed out"},
        provenance=EvidenceProvenance(head_sha=HEAD_SHA),
    )
    consumer = _record(store, path="worker/consumer.py", content="lookup(pk=showId)")
    backend = ObligationAssessment(
        target="OB-backend",
        obligation_id="OB-backend",
        disposition=ObligationDisposition.COVERED,
        reason="Producer implementation establishes the identity.",
        evidence_ids=(producer.id, failed_lookup.id),
        assessment_version=1,
    )
    context = build_boundary_context(
        _boundary(),
        (backend, _assessment("OB-worker", consumer.id)),
        store.snapshot(),
        max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
    )

    assert context["incomplete"] is False
    assert any(failed_lookup.id in item for item in context["diagnostics"])
    result = validate_boundary_evaluation({
        "question": context["question"],
        "outcome": "supported",
        "reason": "The retained implementation and contract excerpts agree.",
        "evidence_ids": [schema.id, producer.id, consumer.id],
    }, context)
    assert result.outcome == "supported"


def test_contract_fallback_ignores_uncited_failed_historical_reads():
    store = EvidenceStore()
    contract = _record(store, path="contracts/message.proto", content="string showId = 1;")
    failed_contract = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "contracts/message.proto", "start_line": 100},
        result={"status": "error", "content": "past end of file"},
        provenance=EvidenceProvenance(head_sha=HEAD_SHA),
    )
    producer = _record(store, path="backend/Producer.java", content="showId = databaseId")
    consumer = _record(store, path="worker/consumer.py", content="lookup(pk=showId)")

    context = build_boundary_context(
        _boundary(),
        (_assessment("OB-backend", producer.id), _assessment("OB-worker", consumer.id)),
        store.snapshot(),
        max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
    )

    assert context["contract_evidence_ids"] == [contract.id]
    assert failed_contract.id not in {item["id"] for item in context["evidence"]}
    assert context["incomplete"] is False


def test_cited_contract_keeps_historical_reads_out_of_packet_and_fingerprint():
    store = EvidenceStore()
    contract = _record(store, path="contracts/message.proto", content="string showId = 1;")
    producer = _record(store, path="backend/Producer.java", content="showId = databaseId")
    consumer = _record(store, path="worker/consumer.py", content="lookup(pk=showId)")
    backend = ObligationAssessment(
        target="OB-backend",
        obligation_id="OB-backend",
        disposition=ObligationDisposition.COVERED,
        reason="Producer and contract establish the identity.",
        evidence_ids=(producer.id, contract.id),
        assessment_version=1,
    )
    mapping = {"OB-backend": "backend", "OB-worker": "worker"}
    before = build_boundary_context(
        _boundary(), (backend, _assessment("OB-worker", consumer.id)),
        store.snapshot(), max_bytes=20_000, obligations=mapping,
    )
    historical = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "contracts/message.proto", "start_line": 100},
        result={"status": "error", "content": "past end of file"},
        provenance=EvidenceProvenance(head_sha=HEAD_SHA),
    )
    after = build_boundary_context(
        _boundary(), (backend, _assessment("OB-worker", consumer.id)),
        store.snapshot(), max_bytes=20_000, obligations=mapping,
    )

    assert historical.id not in {item["id"] for item in after["evidence"]}
    assert after["input_fingerprint"] == before["input_fingerprint"]


@pytest.mark.parametrize("outcome", ["approve", "unknown", ""])
def test_boundary_evaluation_rejects_unagreed_outcomes(outcome):
    with pytest.raises(ValueError, match="outcome"):
        validate_boundary_evaluation(
            {"outcome": outcome, "reason": "No", "evidence_ids": []},
            {"evidence": []},
        )


def test_supported_evaluation_rejects_summary_only_or_wrong_question():
    context_without_sources = {
        "boundary_id": "messages",
        "question": "Does the producer identity match the consumer lookup?",
        "input_fingerprint": "boundary:123",
        "required_participants": ["backend", "worker"],
        "participant_evidence_ids": {"backend": [], "worker": []},
        "contract_evidence_ids": [],
        "source_evidence_ids": [],
        "evidence": [],
        "incomplete": True,
        "diagnostics": ["model summaries are not source evidence"],
    }

    with pytest.raises(ValueError, match="source evidence"):
        validate_boundary_evaluation({
            "question": context_without_sources["question"],
            "outcome": "supported",
            "reason": "Both summaries agree",
            "evidence_ids": [],
        }, context_without_sources)

    complete = dict(context_without_sources, incomplete=False)
    with pytest.raises(ValueError, match="specific question"):
        validate_boundary_evaluation({
            "question": "Is the whole API correct?",
            "outcome": "supported",
            "reason": "Everything is fine",
            "evidence_ids": [],
        }, complete)


def test_contract_alone_cannot_stand_in_for_both_participant_implementations():
    store = EvidenceStore()
    contract = _record(store, path="contracts/message.proto", content="string showId = 1;")
    context = build_boundary_context(
        _boundary(), (_assessment("OB-backend", contract.id), _assessment("OB-worker", contract.id)),
        store.snapshot(), max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
    )
    assert context["incomplete"] is True
    assert set(context["missing_participants"]) == {"backend", "worker"}


def test_real_pathless_negative_search_can_support_not_affected():
    store = EvidenceStore()
    _record(store, path="contracts/message.proto", content="string showId = 1;")
    worker = _record(store, path="worker/consumer.py", content="lookup(pk=showId)")
    lookup = store.add_tool_result(
        session_id="S1", tool="git_grep", arguments={"pattern": "setShowId"},
        result={"status": "ok", "result": {"matches": []}},
        provenance=EvidenceProvenance(head_sha=HEAD_SHA),
    )
    context = build_boundary_context(
        _boundary(), (_assessment("OB-backend", lookup.id, disposition=ObligationDisposition.NOT_APPLICABLE), _assessment("OB-worker", worker.id)),
        store.snapshot(), max_bytes=20_000,
        obligations={"OB-backend": "backend", "OB-worker": "worker"},
        expected_head_sha=HEAD_SHA,
    )
    assert context["incomplete"] is False
    assert context["participant_evidence_ids"]["backend"] == [lookup.id]
