from dataclasses import replace

from pr_reviewer.specialist_runtime.assignments import Assignment, AssignmentPlan
from pr_reviewer.specialist_runtime.test_triage import assign_test_failure_triage


def assignment(name, paths):
    return Assignment(name, name, "Review changes", (), (), (), tuple(paths),
                      tuple(paths), (), 1, "normal")


def test_failure_triage_routes_to_component_owner_without_new_sessions():
    plan = AssignmentPlan((assignment("web", ["web/src.ts"]),
                           assignment("api", ["api/src.py"])))
    rows = ({"name": "broken", "file": "api/tests/test_x.py", "report": "pytest.xml", "status": "failed"},
            {"name": "fine", "report": "other.xml", "status": "passed"})
    updated, obligations = assign_test_failure_triage(
        plan, rows, ({"id": "api", "paths": ("api/**",)},),
    )
    assert len(updated.assignments) == 2
    assert len(obligations) == 1
    assert updated.assignments[0] == plan.assignments[0]
    assert obligations[0].id in updated.assignments[1].obligation_ids
    assert "broken" in obligations[0].explanation
    assert obligations[0].risk_tier == "normal"
    assert obligations[0].unresolved_policy == "record_unknown"
    assert updated.assignments[1].boundary_paths == ("api/src.py",)


def test_unknown_report_routing_is_stable_and_groups_errors():
    plan = AssignmentPlan((assignment("b", []), assignment("a", [])))
    rows = tuple({"name": str(i), "report": "suite.xml", "status": "errored"} for i in range(100))
    updated, obligations = assign_test_failure_triage(plan, rows, ())
    assert len(obligations) == 1
    assert "100" in obligations[0].explanation
    assert len(obligations[0].explanation) <= 500
    assert updated.assignments[0] == plan.assignments[0]
    assert updated.assignments[1].obligation_ids == (obligations[0].id,)
    assert assign_test_failure_triage(plan, (), ()) == (plan, ())


def test_exact_path_owner_wins_tie_and_preserves_existing_primary_ownership():
    plan = AssignmentPlan((
        assignment("component", ["api/src.py"]),
        replace(assignment("exact", ["api/tests/test_x.py"]), obligation_ids=("original",)),
    ))
    updated, obligations = assign_test_failure_triage(plan, ({
        "name": "broken", "file": "api/tests/test_x.py", "status": "failed",
    },), ({"id": "api", "paths": ("api/**",)},))
    assert updated.assignments[1].primary_obligation_ids == ("original", obligations[0].id)
    assert updated.assignments[0] == plan.assignments[0]


def test_empty_plan_records_unassigned_triage_and_many_reports_stay_bounded():
    rows = tuple({"name": "fails", "report": f"{i}.xml", "status": "failed"} for i in range(100))
    plan, obligations = assign_test_failure_triage(AssignmentPlan(()), rows, ())
    assert 0 < len(obligations) <= 8
    assert set(plan.unassigned_obligation_ids) == {o.id for o in obligations}
