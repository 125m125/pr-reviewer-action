"""Evidence provenance and immutable wave-snapshot behavior."""

from dataclasses import replace

import pytest

from pr_reviewer.specialist_runtime.evidence import (
    EvidenceProvenance,
    EvidenceStore,
    canonical_evidence_key,
)


def test_duplicate_success_reuses_evidence_without_claiming_independence():
    store = EvidenceStore()

    first = store.add_tool_result(
        session_id="S1",
        tool="read_file",
        arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
    )
    second = store.add_tool_result(
        session_id="S2",
        tool="read_file",
        arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
    )

    assert second.id == first.id
    assert second.collector_session_id == "S1"
    assert "S2" in second.imported_by
    assert second.is_usable_for_coverage is True


def test_canonical_key_normalizes_request_identity_and_redacts_before_hashing():
    first = canonical_evidence_key(
        "read_file",
        {"path": "./src\\main.py", "limit": 5},
        {"status": "ok", "result": {"content": "token=supersecretvalue"}},
    )
    second = canonical_evidence_key(
        "read_file",
        {"limit": 5, "path": "src/main.py"},
        {"status": "ok", "result": {"content": "token=anothersecretvalue"}},
    )

    assert first == second


def test_failed_tool_calls_are_retained_but_not_canonical_coverage_evidence():
    store = EvidenceStore()

    failed = store.add_tool_result(
        session_id="S1",
        tool="read_file",
        arguments={"path": "missing.py"},
        result={"status": "error", "error": "not found"},
    )

    assert failed.is_usable_for_coverage is False
    assert store.lookup_canonical(failed.canonical_key) is None
    assert store.snapshot().records == (failed,)


def test_evidence_retains_redaction_and_truncation_state():
    store = EvidenceStore(max_content_bytes=10)

    record = store.add_tool_result(
        session_id="S1",
        tool="read_file",
        arguments={"path": "secrets.txt"},
        result={"status": "ok", "result": {"content": "token=supersecretvalue\nmore"}},
    )

    assert record.redacted is True
    assert record.truncated is True
    assert "supersecretvalue" not in record.content


def test_wave_snapshot_does_not_change_when_store_grows():
    store = EvidenceStore()
    store.add_tool_result(
        session_id="S1",
        tool="read_file",
        arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
    )
    snapshot = store.snapshot()

    store.add_tool_result(
        session_id="S3",
        tool="read_file",
        arguments={"path": "b.py"},
        result={"status": "ok", "result": {"content": "y = 2"}},
    )

    assert snapshot.get_by_path("b.py") == ()


def test_snapshot_remains_immutable_when_later_session_imports_existing_evidence():
    store = EvidenceStore()
    first = store.add_tool_result(
        session_id="S1",
        tool="read_file",
        arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(head_sha="head-a", retrieved_at=100.0, max_age_hours=1),
    )
    snapshot = store.snapshot()

    store.add_tool_result(
        session_id="S2",
        tool="read_file",
        arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(head_sha="head-a", retrieved_at=200.0, max_age_hours=1),
        now=200.0,
    )

    assert snapshot.records[0].imported_by == ("S1",)
    assert snapshot.records[0].provenance.retrieved_at == 100.0
    assert store.lookup_canonical(first.id, now=200.0).imported_by == ("S1", "S2")


def test_retained_evidence_redacts_nested_arguments_and_url_metadata():
    store = EvidenceStore()
    secret = "supersecretvalue"
    record = store.add_tool_result(
        session_id="S1",
        tool="web_fetch",
        arguments={
            "credentials": {"token": secret},
            "url": f"https://alice:{secret}@Docs.Example.com/api?access_token={secret}&page=1",
        },
        result={"status": "ok", "result": {"content": "public"}},
        provenance=EvidenceProvenance(
            original_url=f"https://alice:{secret}@docs.example.com/original?token={secret}",
            final_url=f"https://bob:{secret}@docs.example.com/final?api_key={secret}",
        ),
    )

    retained = "\n".join((
        record.arguments,
        record.source_identity,
        record.provenance.original_url or "",
        record.provenance.final_url or "",
        record.canonical_key,
        repr(store.snapshot()),
    ))
    assert secret not in retained
    assert "alice@" not in retained
    assert "bob@" not in retained
    assert record.redacted is True


def test_head_policy_rule_and_final_source_changes_prevent_canonical_reuse():
    store = EvidenceStore()
    baseline = EvidenceProvenance(
        head_sha="head-a",
        policy_hash="policy-a",
        policy_rule_id="rule-a",
        final_url="https://docs.example.com/v1",
    )
    first = store.add_tool_result(
        session_id="S1", tool="web_fetch", arguments={"url": "https://docs.example.com/v1"},
        result={"status": "ok", "result": {"content": "same"}}, provenance=baseline,
    )

    for index, provenance in enumerate((
        replace(baseline, head_sha="head-b"),
        replace(baseline, policy_hash="policy-b"),
        replace(baseline, policy_rule_id="rule-b"),
        replace(baseline, final_url="https://docs.example.com/v2"),
    ), start=2):
        record = store.add_tool_result(
            session_id=f"S{index}", tool="web_fetch", arguments={"url": "https://docs.example.com/v1"},
            result={"status": "ok", "result": {"content": "same"}}, provenance=provenance,
        )
        assert record.id != first.id
        assert record.collector_session_id == f"S{index}"


def test_expired_evidence_is_refetched_but_fresh_retrieval_time_does_not_change_identity():
    store = EvidenceStore()
    first = store.add_tool_result(
        session_id="S1", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(retrieved_at=1_000.0, max_age_hours=1), now=2_000.0,
    )
    reused = store.add_tool_result(
        session_id="S2", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(retrieved_at=2_000.0, max_age_hours=1), now=2_000.0,
    )
    before_stale_import = store.snapshot()
    assert store.lookup_canonical(first.id, now=5_000.0) is None
    with pytest.raises(ValueError, match="not reusable"):
        store.import_into_session("S3", first.id, now=5_000.0)
    refreshed = store.add_tool_result(
        session_id="S4", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(retrieved_at=5_000.0, max_age_hours=1), now=5_000.0,
    )

    assert reused.id == first.id
    assert before_stale_import.records[0].imported_by == ("S1", "S2")
    assert refreshed.id != first.id
    assert refreshed.collector_session_id == "S4"


def test_provenance_and_evidence_relationships_are_immutable_values():
    store = EvidenceStore()
    prior = store.add_tool_result(
        session_id="S0", tool="read_file", arguments={"path": "old.py"},
        result={"status": "ok", "result": {"content": "old"}},
    )
    supersedes = [prior.id]
    contradicts = [prior.id]
    record = store.add_tool_result(
        session_id="S1", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(head_sha="head-a", policy_hash="policy-a"),
        supersedes=supersedes, contradicts=contradicts,
    )
    supersedes.append("caller-mutation")
    contradicts.append("caller-mutation")

    assert record.supersedes == (prior.id,)
    assert record.contradicts == (prior.id,)
    assert record.provenance.head_sha == "head-a"


def test_age_governed_evidence_without_timestamp_is_not_reusable_from_any_path():
    store = EvidenceStore()
    original = store.add_tool_result(
        session_id="S1", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(max_age_hours=1), now=100.0,
    )
    snapshot = store.snapshot()

    assert store.lookup_canonical(original.id, now=100.0) is None
    with pytest.raises(ValueError, match="not reusable"):
        store.import_into_session("S2", original.id, now=100.0)
    refreshed = store.add_tool_result(
        session_id="S3", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(max_age_hours=1), now=100.0,
    )

    assert snapshot.records[0].imported_by == ("S1",)
    assert refreshed.id != original.id


def test_explicit_non_url_source_is_sanitized_before_storage_and_identity():
    store = EvidenceStore()
    secret = "supersecretvalue"
    record = store.add_tool_result(
        session_id="S1", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "public"}},
        source=f"token={secret}",
    )

    retained = "\n".join((
        record.arguments, record.source_identity, record.canonical_key, repr(store.snapshot()),
    ))
    assert secret not in retained
    assert record.redacted is True


def test_unknown_or_secret_bearing_relationship_ids_are_rejected_without_recording_them():
    store = EvidenceStore()
    with pytest.raises(ValueError, match="known record"):
        store.add_tool_result(
            session_id="S1", tool="read_file", arguments={"path": "a.py"},
            result={"status": "ok", "result": {"content": "public"}},
            supersedes=("evidence:supersecretvalue",),
        )

    assert store.snapshot().records == ()


def test_relationships_retain_exact_refresh_and_failed_attempt_ids_for_snapshot_lookup():
    store = EvidenceStore()
    initial = store.add_tool_result(
        session_id="S1", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(retrieved_at=0.0, max_age_hours=1), now=0.0,
    )
    refreshed = store.add_tool_result(
        session_id="S2", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
        provenance=EvidenceProvenance(retrieved_at=4_000.0, max_age_hours=1), now=4_000.0,
    )
    failed = store.add_tool_result(
        session_id="S3", tool="read_file", arguments={"path": "a.py"},
        result={"status": "error", "error": "not found"},
    )
    related = store.add_tool_result(
        session_id="S4", tool="read_file", arguments={"path": "b.py"},
        result={"status": "ok", "result": {"content": "y = 2"}},
        supersedes=(refreshed.id,), contradicts=(failed.id,),
    )
    snapshot = store.snapshot()

    assert refreshed.id != initial.id
    assert related.supersedes == (refreshed.id,)
    assert related.contradicts == (failed.id,)
    assert snapshot.get(related.supersedes[0]) == refreshed
    assert snapshot.get(related.contradicts[0]) == failed


def test_exported_canonical_key_sanitizes_non_url_source_before_hashing():
    first = canonical_evidence_key(
        "read_file", {"path": "a.py"}, {"status": "ok", "result": {"content": "x"}},
        source="token=supersecretvalue",
    )
    second = canonical_evidence_key(
        "read_file", {"path": "a.py"}, {"status": "ok", "result": {"content": "x"}},
        source="token=anothersecretvalue",
    )

    assert first == second
    assert "supersecretvalue" not in first
    assert "anothersecretvalue" not in first
