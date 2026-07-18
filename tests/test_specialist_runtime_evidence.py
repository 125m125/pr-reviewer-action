"""Evidence provenance and immutable wave-snapshot behavior."""

from pr_reviewer.specialist_runtime.evidence import EvidenceStore, canonical_evidence_key


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
    )
    snapshot = store.snapshot()

    store.import_into_session("S2", first.id)

    assert snapshot.records[0].imported_by == ("S1",)
    assert store.lookup_canonical(first.id).imported_by == ("S1", "S2")
