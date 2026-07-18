import pytest

from pr_reviewer.specialist_runtime.policy import RuntimeConfig
from pr_reviewer.specialist_runtime.types import BudgetLimits, CoverageObligation

from pr_reviewer.specialist_runtime.assignments import (
    AssignmentPlanError,
    fallback_assignment_plan,
    planner_prompt,
    repair_prompt,
    validate_assignment_plan,
)


@pytest.fixture
def topology():
    return {
        "changed_files": ["worker/a.py", "queue/consumer.py", "contracts/event.proto"],
        "components": [
            {"id": "worker", "changed_files": ["worker/a.py"]},
            {"id": "queue", "changed_files": ["queue/consumer.py"]},
        ],
        "relationships": [{"source": "worker", "target": "queue"}],
    }


@pytest.fixture
def runtime_config():
    return RuntimeConfig(
        review_deadline_sec=60,
        model_request_timeout_sec=1,
        concurrency=2,
        max_sessions=4,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )


@pytest.fixture
def obligations():
    return (
        CoverageObligation(
            obligation_id="topology:worker:implementation", origin="topology", subject="worker",
            required_evidence_categories=("implementation",), risk_tier="high",
            scope=("worker/a.py",), seed_hints=("worker/a.py",),
        ),
        CoverageObligation(
            obligation_id="recipe:delivery:consumer", origin="recipe", subject="delivery",
            required_evidence_categories=("consumer",), recipe_id="delivery",
            recipe_execution="coverage", scope=("queue/consumer.py",),
            seed_hints=("queue/consumer.py",),
        ),
        CoverageObligation(
            obligation_id="recipe:security:boundary", origin="recipe", subject="security",
            required_evidence_categories=("boundary",), recipe_id="security",
            recipe_execution="independent", requires_independent_verification=True,
            risk_tier="critical", scope=("worker/a.py",), seed_hints=("worker/a.py",),
        ),
        CoverageObligation(
            obligation_id="topology:worker-to-queue:interaction", origin="topology",
            subject="worker-to-queue", required_evidence_categories=("interaction",),
            scope=("worker/a.py", "queue/consumer.py"),
        ),
        CoverageObligation(
            obligation_id="recipe:release:artifact", origin="recipe", subject="release",
            required_evidence_categories=("artifact",), recipe_id="release",
            recipe_execution="dedicated", scope=("contracts/event.proto",),
            seed_hints=("contracts/event.proto",),
        ),
        # Task 3's private recipe-accounting marker is intentionally not assignable.
        CoverageObligation(
            obligation_id="recipe-marker:disabled", origin="recipe-accounting", subject="disabled",
            required_evidence_categories=(), recipe_id="disabled", mandatory=False,
        ),
    )


def assignment(*obligation_ids, id="worker-flow", **overrides):
    raw = {
        "id": id,
        "title": "Worker flow",
        "objective": "Trace the changed behavior",
        "obligation_ids": list(obligation_ids),
        "lenses": ["delivery"],
        "seed_paths": ["worker/a.py"],
        "boundary_paths": ["queue/consumer.py"],
        "expected_evidence": ["implementation", "consumer", "boundary", "interaction", "artifact"],
        "estimated_turns": 3,
        "priority": "high",
        "overlap_justification": "",
    }
    raw.update(overrides)
    return raw


def complete_plan_for(obligations, id="queue-loss-boundary"):
    return {"assignments": [
        assignment("topology:worker:implementation", "recipe:delivery:consumer",
                   "topology:worker-to-queue:interaction", id=id,
                   expected_evidence=["implementation", "consumer", "interaction"]),
        assignment("recipe:security:boundary", id="security-independent",
                   expected_evidence=["boundary"], lenses=["security"],
                   seed_paths=["worker/a.py"], boundary_paths=[], priority="critical"),
        assignment("recipe:release:artifact", id="release-dedicated",
                   expected_evidence=["artifact"], lenses=["release"],
                   seed_paths=["contracts/event.proto"], boundary_paths=[], priority="normal"),
    ]}


def test_planner_cannot_omit_recipe_obligation(obligations, topology, runtime_config):
    raw = {"assignments": [assignment(obligations[0].id)]}

    with pytest.raises(AssignmentPlanError, match="unassigned mandatory"):
        validate_assignment_plan(raw, obligations, topology, runtime_config)


def test_mandatory_obligation_without_evidence_rejects_prompt_and_plan(topology, runtime_config):
    obligations = (CoverageObligation(
        obligation_id="mandatory:missing-evidence", origin="risk-rule", subject="unknown",
        required_evidence_categories=(), mandatory=True,
    ),)

    with pytest.raises(AssignmentPlanError, match="mandatory obligation has no required evidence"):
        planner_prompt(obligations, topology, runtime_config)
    with pytest.raises(AssignmentPlanError, match="mandatory obligation has no required evidence"):
        validate_assignment_plan({"assignments": []}, obligations, topology, runtime_config)


def test_model_created_focus_preserves_recipe_identity(obligations, topology, runtime_config):
    plan = validate_assignment_plan(complete_plan_for(obligations), obligations, topology, runtime_config)

    assert "delivery" in plan.assignments[0].recipe_ids
    assert "disabled" not in plan.assignments[0].recipe_ids


def test_schema_requires_every_assignment_field(obligations, topology, runtime_config):
    raw = complete_plan_for(obligations)
    del raw["assignments"][0]["boundary_paths"]

    with pytest.raises(AssignmentPlanError, match="boundary_paths"):
        validate_assignment_plan(raw, obligations, topology, runtime_config)


def test_assignment_paths_cannot_widen_immutable_obligation_scope(obligations, topology, runtime_config):
    raw = complete_plan_for(obligations)
    raw["assignments"][0]["seed_paths"] = ["unrelated/admin.py"]

    with pytest.raises(AssignmentPlanError, match="outside immutable obligation scope"):
        validate_assignment_plan(raw, obligations, topology, runtime_config)


def test_shared_obligation_requires_overlap_justification(obligations, topology, runtime_config):
    raw = complete_plan_for(obligations)
    raw["assignments"][1]["obligation_ids"].append("topology:worker:implementation")
    raw["assignments"][1]["expected_evidence"].append("implementation")

    with pytest.raises(AssignmentPlanError, match="shared obligation.*overlap"):
        validate_assignment_plan(raw, obligations, topology, runtime_config)


def test_shared_high_risk_obligation_has_deterministic_primary_owner(obligations, topology, runtime_config):
    raw = complete_plan_for(obligations)
    raw["assignments"][0]["overlap_justification"] = "Independent delivery perspective"
    raw["assignments"].append(assignment(
        "topology:worker:implementation", id="a-independent-worker",
        title="Independent worker risk", objective="Challenge worker failure handling",
        lenses=["failure-analysis"], expected_evidence=["implementation"],
        boundary_paths=[], overlap_justification="Independent delivery perspective",
    ))

    plan = validate_assignment_plan(raw, obligations, topology, runtime_config)
    by_id = {item.id: item for item in plan.assignments}

    assert by_id["a-independent-worker"].primary_obligation_ids == (
        "topology:worker:implementation",
    )
    assert "topology:worker:implementation" not in by_id["queue-loss-boundary"].primary_obligation_ids


def test_normal_risk_obligation_cannot_have_shared_ownership(obligations, topology, runtime_config):
    normal = CoverageObligation(
        obligation_id="topology:queue:normal", origin="topology", subject="queue",
        required_evidence_categories=("queue",), scope=("queue/consumer.py",),
    )
    raw = complete_plan_for(obligations + (normal,))
    raw["assignments"][0]["obligation_ids"].append(normal.id)
    raw["assignments"][0]["expected_evidence"].append("queue")
    raw["assignments"][0]["overlap_justification"] = "Cross-check queue behavior"
    raw["assignments"].append(assignment(
        normal.id, id="queue-cross-check", title="Queue cross-check",
        objective="Independently inspect queue behavior", lenses=["queue"],
        seed_paths=["queue/consumer.py"], boundary_paths=[], expected_evidence=["queue"],
        priority="normal", overlap_justification="Cross-check queue behavior",
    ))

    with pytest.raises(AssignmentPlanError, match="only allowed for high or critical"):
        validate_assignment_plan(raw, obligations + (normal,), topology, runtime_config)


@pytest.mark.parametrize("priority", ["normal", "critical"])
def test_planner_priority_must_equal_immutable_obligation_risk(
    obligations, topology, runtime_config, priority,
):
    raw = complete_plan_for(obligations)
    raw["assignments"][0]["priority"] = priority

    with pytest.raises(AssignmentPlanError, match="priority must equal immutable risk"):
        validate_assignment_plan(raw, obligations, topology, runtime_config)


def test_dedicated_and_independent_recipes_are_isolated(obligations, topology, runtime_config):
    raw = complete_plan_for(obligations)
    raw["assignments"][0]["obligation_ids"].append("recipe:release:artifact")
    raw["assignments"][0]["expected_evidence"].append("artifact")

    with pytest.raises(AssignmentPlanError, match="dedicated recipe"):
        validate_assignment_plan(raw, obligations, topology, runtime_config)

    raw = complete_plan_for(obligations)
    raw["assignments"][1]["obligation_ids"].append("topology:worker:implementation")
    raw["assignments"][1]["expected_evidence"].append("implementation")
    with pytest.raises(AssignmentPlanError, match="independent recipe"):
        validate_assignment_plan(raw, obligations, topology, runtime_config)


def test_deadline_turn_capacity_rejects_plan_without_other_caps(obligations, topology):
    raw = complete_plan_for(obligations)
    deadline_only = RuntimeConfig(
        review_deadline_sec=900, model_request_timeout_sec=300, concurrency=1, max_sessions=4,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    with pytest.raises(AssignmentPlanError, match="deadline turn capacity"):
        validate_assignment_plan(raw, obligations, topology, deadline_only)


def test_repair_prompt_contains_only_errors_and_previous_plan(obligations):
    raw = {"assignments": [{"id": "invalid"}]}
    prompt = repair_prompt(("missing title", "unassigned mandatory: O1"), raw)

    assert prompt == {
        "errors": ["missing title", "unassigned mandatory: O1"],
        "previous_plan": raw,
    }
    assert "obligations" not in prompt


def test_fallback_prioritizes_high_risk_and_keeps_capacity_overflow_explicit(topology, obligations):
    config = RuntimeConfig(
        review_deadline_sec=60, model_request_timeout_sec=1, concurrency=1, max_sessions=2,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, config)

    assigned_ids = {item_id for item in plan.assignments for item_id in item.obligation_ids}
    assert plan.assignments[0].priority == "critical"
    assert "recipe:security:boundary" in assigned_ids
    assert set(plan.unassigned_obligation_ids).union(assigned_ids) == {
        item.id for item in obligations if item.mandatory and item.required_evidence
    }
    assert plan.unassigned_obligation_ids


def test_fallback_stops_at_deadline_turn_capacity(topology, obligations):
    config = RuntimeConfig(
        review_deadline_sec=900, model_request_timeout_sec=300, concurrency=1, max_sessions=4,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, config)

    assert len(plan.assignments) == 2
    assert plan.unassigned_obligation_ids


def test_planner_prompt_includes_immutable_obligation_ids_only(obligations, topology, runtime_config):
    prompt = planner_prompt(obligations, topology, runtime_config)

    assert "recipe:delivery:consumer" in prompt["obligations"]
    assert "recipe-marker:disabled" not in prompt["obligations"]
