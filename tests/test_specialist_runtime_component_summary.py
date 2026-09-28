from pr_reviewer.specialist_runtime.cli import _component_coverage_summary, _runtime_event_line
from pr_reviewer.specialist_runtime.events import RunEvent


def test_component_summary_keeps_incomplete_boundary_and_queued_work_visible():
    artifact = {
        "coverage": {
            "owner": {"owner_component_id": "backend", "status": "partial", "scope": ["a.py", "b.py"], "assessed_paths": ["a.py"]},
            "boundary": {"boundary_id": "http-api", "evaluator_owned": True, "status": "unresolved", "scope": []},
        },
        "boundary_evaluations": [{"outcome": "insufficient_evidence"}],
        "boundary_model_turns": 2,
        "investigation_leads": [{"kind": "delegation", "status": "open"}],
        "ownership_warnings": ["Unmatched changed paths: misc.txt"],
    }
    text = "\n".join(_component_coverage_summary(artifact))
    assert "backend | partial | 1/2" in text
    assert "http\\-api | unresolved" in text
    assert "insufficient\\_evidence=1" in text
    assert "open=1" in text
    assert "misc\\.txt" in text
    assert "2 model turns" in text


def test_boundary_event_reports_reason_without_packet_content():
    event = RunEvent(1, "boundary_evaluation", {"boundary_id": "http-api", "outcome": "insufficient_evidence", "reason": "Participant source missing", "content": "do not log"})
    line = _runtime_event_line(event)
    assert "http-api" in line
    assert "Participant source missing" in line
    assert "do not log" not in line
