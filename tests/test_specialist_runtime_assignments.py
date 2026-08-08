import pytest

from pr_reviewer.specialist_runtime.policy import RuntimeConfig
from pr_reviewer.specialist_runtime.types import BudgetLimits, CoverageObligation

from pr_reviewer.specialist_runtime.assignments import (
    AssignmentPlanError,
    apply_planner_transformations,
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


def test_planner_transformations_apply_valid_items_independently(
    obligations, topology, runtime_config,
):
    base = fallback_assignment_plan(obligations, topology, runtime_config)
    ordinary = next(item for item in base.assignments if not item.recipe_ids)
    result = apply_planner_transformations({
        "transformations": [
            {
                "kind": "improve",
                "assignment_id": ordinary.id,
                "objective": "Trace worker delivery from producer to consumer.",
                "lenses": ["delivery", "failure-recovery"],
                "seed_paths": ["worker/a.py"],
            },
            {
                "kind": "reorder",
                "assignment_ids": [ordinary.id],
            },
        ],
    }, base, obligations, runtime_config, topology=topology)

    assert result.plan.assignments[0].id == ordinary.id
    assert result.plan.assignments[0].objective == (
        "Trace worker delivery from producer to consumer."
    )
    assert result.plan.assignments[0].lenses == ("delivery", "failure-recovery")
    assert result.ignored == ()


def test_planner_transformations_ignore_invalid_item_but_keep_valid_item(
    obligations, topology, runtime_config,
):
    base = fallback_assignment_plan(obligations, topology, runtime_config)
    ordinary = next(item for item in base.assignments if not item.recipe_ids)
    result = apply_planner_transformations({
        "transformations": [
            {
                "kind": "improve",
                "assignment_id": ordinary.id,
                "seed_paths": ["outside/immutable/scope.py"],
            },
            {
                "kind": "improve",
                "assignment_id": ordinary.id,
                "objective": "Inspect the reachable worker behavior.",
            },
        ],
    }, base, obligations, runtime_config, topology=topology)

    transformed = next(item for item in result.plan.assignments if item.id == ordinary.id)
    assert transformed.objective == "Inspect the reachable worker behavior."
    assert transformed.seed_paths == ordinary.seed_paths
    assert len(result.ignored) == 1
    assert "outside immutable obligation scope" in result.ignored[0]


def test_planner_transformation_diagnostics_are_bounded(
    obligations, topology, runtime_config,
):
    base = fallback_assignment_plan(obligations, topology, runtime_config)
    result = apply_planner_transformations({
        "transformations": [
            {"kind": "unsupported", "payload": "x" * 10_000}
            for _ in range(200)
        ],
    }, base, obligations, runtime_config, topology=topology)

    assert len(result.ignored) <= 65
    assert all(len(item) <= 330 for item in result.ignored)


def test_repeated_split_allocates_unique_controller_owned_assignment_ids():
    obligations = tuple(
        CoverageObligation(
            obligation_id=f"topology:item-{index}:implementation",
            origin="topology",
            subject=f"item-{index}",
            required_evidence_categories=("implementation",),
            scope=(f"src/item_{index}.py",),
            seed_hints=(f"src/item_{index}.py",),
        )
        for index in range(4)
    )
    topology = {
        "changed_files": [path for item in obligations for path in item.scope],
        "components": [{
            "id": "all-items",
            "changed_files": [path for item in obligations for path in item.scope],
        }],
        "relationships": [],
    }
    config = RuntimeConfig(
        max_sessions=6,
        session_limits=BudgetLimits(model_turns=8, tool_calls=8, recoveries=1),
    )
    base = fallback_assignment_plan(obligations, topology, config)
    original = base.assignments[0]
    result = apply_planner_transformations({
        "transformations": [
            {
                "kind": "split",
                "assignment_id": original.id,
                "obligation_groups": [
                    list(original.obligation_ids[:2]),
                    [original.obligation_ids[2]],
                ],
            },
            {
                "kind": "split",
                "assignment_id": original.id,
                "obligation_groups": [
                    [original.obligation_ids[0]],
                    [original.obligation_ids[1]],
                ],
            },
        ],
    }, base, obligations, config, topology=topology)

    assignment_ids = [item.id for item in result.plan.assignments]
    owned = [
        obligation_id
        for item in result.plan.assignments
        for obligation_id in item.obligation_ids
    ]
    assert result.ignored == ()
    assert len(assignment_ids) == len(set(assignment_ids))
    assert sorted(owned) == sorted(item.id for item in obligations)


def test_planner_omissions_never_remove_base_ownership(
    obligations, topology, runtime_config,
):
    base = fallback_assignment_plan(obligations, topology, runtime_config)
    result = apply_planner_transformations(
        {"transformations": []}, base, obligations, runtime_config,
        topology=topology,
    )

    assert result.plan == base
    assert {
        obligation_id
        for item in result.plan.assignments
        for obligation_id in item.obligation_ids
    } == {
        item.id for item in obligations
        if item.mandatory and item.required_evidence_categories
    }


def test_planner_cannot_merge_or_split_isolated_recipe_assignments(
    obligations, topology, runtime_config,
):
    base = fallback_assignment_plan(obligations, topology, runtime_config)
    isolated = next(
        item for item in base.assignments
        if "security" in item.recipe_ids
    )
    ordinary = next(item for item in base.assignments if not item.recipe_ids)
    result = apply_planner_transformations({
        "transformations": [
            {
                "kind": "merge",
                "target_assignment_id": ordinary.id,
                "source_assignment_ids": [isolated.id],
            },
            {
                "kind": "split",
                "assignment_id": isolated.id,
                "obligation_groups": [[isolated.obligation_ids[0]]],
            },
        ],
    }, base, obligations, runtime_config, topology=topology)

    assert result.plan == base
    assert len(result.ignored) == 2
    assert all("isolated recipe" in reason for reason in result.ignored)


def test_planner_can_merge_and_split_ordinary_assignments_on_existing_boundaries(
    obligations, topology, runtime_config,
):
    roomy = RuntimeConfig(
        review_deadline_sec=runtime_config.review_deadline_sec,
        model_request_timeout_sec=runtime_config.model_request_timeout_sec,
        concurrency=runtime_config.concurrency,
        max_sessions=5,
        session_limits=runtime_config.session_limits,
    )
    base = fallback_assignment_plan(obligations, topology, roomy)
    ordinary = [item for item in base.assignments if not any(
        obligation.recipe_execution in {"dedicated", "independent"}
        or obligation.requires_independent_verification
        for obligation in obligations
        if obligation.id in item.obligation_ids
    )]
    assert len(ordinary) >= 2
    merged = apply_planner_transformations({
        "transformations": [{
            "kind": "merge",
            "target_assignment_id": ordinary[0].id,
            "source_assignment_ids": [ordinary[1].id],
        }],
    }, base, obligations, roomy, topology=topology)
    merged_item = next(
        item for item in merged.plan.assignments if item.id == ordinary[0].id
    )
    expected_ids = set(ordinary[0].obligation_ids + ordinary[1].obligation_ids)
    assert set(merged_item.obligation_ids) == expected_ids

    merged_alias = apply_planner_transformations({
        "transformations": [{
            "kind": "merge",
            "assignment_ids": [ordinary[0].id, ordinary[1].id],
        }],
    }, base, obligations, roomy, topology=topology)
    merged_alias_item = next(
        item for item in merged_alias.plan.assignments if item.id == ordinary[0].id
    )
    assert set(merged_alias_item.obligation_ids) == expected_ids

    split = apply_planner_transformations({
        "transformations": [{
            "kind": "split",
            "assignment_id": merged_item.id,
            "obligation_groups": [[item] for item in sorted(expected_ids)],
        }],
    }, merged.plan, obligations, roomy, topology=topology)
    assert split.ignored == ()
    assert {
        obligation_id
        for item in split.plan.assignments
        for obligation_id in item.obligation_ids
    } == {
        obligation_id
        for item in base.assignments
        for obligation_id in item.obligation_ids
    }


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


@pytest.mark.parametrize("planner_evidence", [None, ["code behavior", "downstream effect"]])
def test_planner_expected_evidence_is_derived_from_immutable_obligations(
    obligations, topology, runtime_config, planner_evidence,
):
    raw = complete_plan_for(obligations)
    if planner_evidence is None:
        del raw["assignments"][0]["expected_evidence"]
    else:
        raw["assignments"][0]["expected_evidence"] = planner_evidence

    plan = validate_assignment_plan(raw, obligations, topology, runtime_config)

    assert plan.assignments[0].expected_evidence == (
        "consumer",
        "implementation",
        "interaction",
    )


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


def test_deadline_budget_does_not_pre_reject_owned_work(obligations, topology):
    raw = complete_plan_for(obligations)
    deadline_only = RuntimeConfig(
        review_deadline_sec=900, model_request_timeout_sec=300, concurrency=1, max_sessions=4,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    plan = validate_assignment_plan(raw, obligations, topology, deadline_only)

    assert not plan.unassigned_obligation_ids


def test_model_estimate_is_normalized_instead_of_enforcing_per_lane_capacity(topology):
    obligation = CoverageObligation(
        obligation_id="topology:worker:high", origin="topology", subject="worker",
        required_evidence_categories=("implementation",), risk_tier="high",
        scope=("worker/a.py",), seed_hints=("worker/a.py",),
    )
    raw = {"assignments": [assignment(
        obligation.id, expected_evidence=["implementation"], boundary_paths=[], estimated_turns=3,
    )]}
    two_lanes = RuntimeConfig(
        review_deadline_sec=750, model_request_timeout_sec=300, concurrency=2, max_sessions=4,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    plan = validate_assignment_plan(raw, (obligation,), topology, two_lanes)

    assert plan.assignments[0].estimated_turns == 1


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


def test_fallback_preserves_ownership_despite_small_estimated_deadline_capacity(topology, obligations):
    config = RuntimeConfig(
        review_deadline_sec=900, model_request_timeout_sec=300, concurrency=1, max_sessions=4,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, config)

    assert len(plan.assignments) <= config.max_sessions
    assert plan.unassigned_obligation_ids == ()


def test_fallback_keeps_one_ordinary_group_independent_of_deadline_estimates(topology):
    obligations = tuple(CoverageObligation(
        obligation_id=f"topology:worker:{index}", origin="topology", subject="worker",
        required_evidence_categories=(f"implementation-{index}",), risk_tier="high",
        scope=("worker/a.py",), seed_hints=("worker/a.py",),
    ) for index in range(3))
    two_lanes = RuntimeConfig(
        review_deadline_sec=750, model_request_timeout_sec=300, concurrency=2, max_sessions=2,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, two_lanes)

    assert [item.estimated_turns for item in plan.assignments] == [3]
    assert not plan.unassigned_obligation_ids


def test_fallback_keeps_dedicated_recipe_isolated_without_estimated_turn_overflow(topology):
    obligations = tuple(CoverageObligation(
        obligation_id=f"recipe:release:{index}", origin="recipe", subject="release",
        required_evidence_categories=(f"artifact-{index}",), risk_tier="high",
        recipe_id="release", recipe_execution="dedicated", scope=("worker/a.py",),
    ) for index in range(3))
    two_lanes = RuntimeConfig(
        review_deadline_sec=750, model_request_timeout_sec=300, concurrency=2, max_sessions=2,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, two_lanes)

    assert len(plan.assignments) == 1
    assert plan.assignments[0].obligation_ids == tuple(item.id for item in obligations)
    assert not plan.unassigned_obligation_ids


def test_fallback_globally_prioritizes_groups_without_dropping_lower_risk_ownership(topology):
    obligations = (
        CoverageObligation(
            obligation_id="topology:worker:a-critical", origin="topology", subject="worker",
            required_evidence_categories=("critical",), risk_tier="critical",
            scope=("worker/a.py",),
        ),
        CoverageObligation(
            obligation_id="topology:worker:z-low", origin="topology", subject="worker",
            required_evidence_categories=("low",), risk_tier="low",
            scope=("worker/a.py",),
        ),
        CoverageObligation(
            obligation_id="topology:queue:m-high", origin="topology", subject="queue",
            required_evidence_categories=("high",), risk_tier="high",
            scope=("queue/consumer.py",),
        ),
    )
    two_lanes = RuntimeConfig(
        review_deadline_sec=375, model_request_timeout_sec=300, concurrency=2, max_sessions=2,
        session_limits=BudgetLimits(model_turns=12, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, two_lanes)
    assigned_ids = {item_id for item in plan.assignments for item_id in item.obligation_ids}

    assert [item.priority for item in plan.assignments] == ["critical", "high"]
    assert "topology:queue:m-high" in assigned_ids
    assert "topology:worker:z-low" in assigned_ids
    assert plan.unassigned_obligation_ids == ()


def test_fallback_reports_obligations_impossible_under_isolation_session_cap(topology):
    obligations = tuple(CoverageObligation(
        obligation_id=f"recipe:isolated-{index}:boundary",
        origin="recipe",
        subject=f"isolated-{index}",
        required_evidence_categories=("boundary",),
        recipe_id=f"isolated-{index}",
        recipe_execution="independent",
        requires_independent_verification=True,
        risk_tier="high",
        scope=("worker/a.py",),
    ) for index in range(3))
    two_sessions = RuntimeConfig(
        review_deadline_sec=3_600,
        model_request_timeout_sec=300,
        concurrency=1,
        max_sessions=2,
        session_limits=BudgetLimits(model_turns=32, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, two_sessions)

    assert len(plan.assignments) == 2
    assert plan.unassigned_obligation_ids == ("recipe:isolated-2:boundary",)
    assert plan.unassigned_obligation_reasons == ((
        "recipe:isolated-2:boundary",
        "max_sessions exhausted after deterministic risk and tie-break ordering",
    ),)


def test_fallback_admits_critical_ordinary_work_before_low_risk_isolated_recipe(topology):
    obligations = (
        CoverageObligation(
            obligation_id="recipe:low-isolated:boundary",
            origin="recipe",
            subject="low-isolated",
            required_evidence_categories=("boundary",),
            recipe_id="low-isolated",
            recipe_execution="independent",
            requires_independent_verification=True,
            risk_tier="low",
            scope=("worker/a.py",),
        ),
        CoverageObligation(
            obligation_id="topology:queue:critical",
            origin="topology",
            subject="queue",
            required_evidence_categories=("implementation",),
            risk_tier="critical",
            scope=("queue/consumer.py",),
        ),
    )
    one_session = RuntimeConfig(
        review_deadline_sec=3_600,
        model_request_timeout_sec=300,
        concurrency=1,
        max_sessions=1,
        session_limits=BudgetLimits(model_turns=32, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, one_session)

    assert plan.assignments[0].obligation_ids == ("topology:queue:critical",)
    assert plan.unassigned_obligation_ids == ("recipe:low-isolated:boundary",)


def test_fallback_coalesces_topology_groups_to_own_every_obligation_within_capacity():
    paths = [f"canaries/canary_{index}.py" for index in range(4)]
    topology = {
        "changed_files": paths,
        "components": [
            {"id": f"canary-{index}", "changed_files": [path]}
            for index, path in enumerate(paths)
        ],
        "relationships": [],
    }
    obligations = tuple(
        CoverageObligation(
            obligation_id=f"topology:canary-{index}:implementation",
            origin="topology",
            subject=f"canary-{index}",
            required_evidence_categories=("implementation",),
            risk_tier="high",
            scope=(path,),
            seed_hints=(path,),
        )
        for index, path in enumerate(paths)
    )
    one_lane = RuntimeConfig(
        review_deadline_sec=600,
        model_request_timeout_sec=100,
        concurrency=1,
        max_sessions=1,
        session_limits=BudgetLimits(model_turns=6, tool_calls=20, recoveries=1),
    )

    plan = fallback_assignment_plan(obligations, topology, one_lane)

    assert len(plan.assignments) == 1
    assert set(plan.assignments[0].obligation_ids) == {item.id for item in obligations}
    assert plan.unassigned_obligation_ids == ()


def test_model_estimated_turns_do_not_control_assignment_validity_or_capacity(
    obligations, topology, runtime_config,
):
    raw = complete_plan_for(obligations)
    for assignment_item in raw["assignments"]:
        assignment_item["estimated_turns"] = 10_000

    plan = validate_assignment_plan(raw, obligations, topology, runtime_config)

    assert all(
        item.estimated_turns <= runtime_config.session_limits.model_turns
        for item in plan.assignments
    )
    assert not plan.unassigned_obligation_ids


def test_dogfood_shape_assigns_all_topology_obligations_without_estimated_turn_arithmetic():
    paths = [f"canaries/canary_{index}.py" for index in range(40)]
    topology = {
        "changed_files": paths,
        "components": [
            {"id": f"canary-{index}", "changed_files": [path]}
            for index, path in enumerate(paths)
        ],
        "relationships": [],
    }
    obligations = tuple(
        CoverageObligation(
            obligation_id=f"topology:canary-{index}:implementation",
            origin="topology",
            subject=f"canary-{index}",
            required_evidence_categories=("implementation",),
            risk_tier="high" if index < 2 else "normal",
            scope=(path,),
            seed_hints=(path,),
        )
        for index, path in enumerate(paths)
    )
    dogfood = RuntimeConfig(
        review_deadline_sec=3_600,
        model_request_timeout_sec=300,
        concurrency=1,
        max_sessions=8,
        session_limits=BudgetLimits(model_turns=32, tool_calls=40, recoveries=2),
    )

    plan = fallback_assignment_plan(obligations, topology, dogfood)

    assigned = {
        obligation_id
        for assignment_item in plan.assignments
        for obligation_id in assignment_item.obligation_ids
    }
    assert assigned == {item.id for item in obligations}
    assert len(plan.assignments) <= dogfood.max_sessions
    assert plan.unassigned_obligation_ids == ()


def test_planner_prompt_includes_immutable_obligation_ids_only(obligations, topology, runtime_config):
    prompt = planner_prompt(obligations, topology, runtime_config)

    assert "recipe:delivery:consumer" in prompt["obligations"]
    assert "recipe-marker:disabled" not in prompt["obligations"]
    assert prompt["authority"]["obligation_ids"] == "exact immutable identifiers; do not invent or paraphrase"
    assert prompt["authority"]["paths"] == (
        "seed_paths and boundary_paths may contain only paths from each assigned "
        "obligation's scope or seed_hints"
    )
    assert prompt["authority"]["expected_evidence"] == (
        "derived by the controller from assigned obligation_ids; planner values are ignored"
    )


def test_assignment_brief_explains_each_owned_obligation(runtime_config):
    obligations = (
        CoverageObligation(
            obligation_id="topology:worker:delivery",
            origin="topology",
            subject="worker delivery",
            explanation="The worker now acknowledges messages after persistence.",
            required_evidence_categories=("implementation", "test"),
            satisfaction_predicates=(
                "The changed acknowledgement order is traced.",
                "Failure behavior is covered by a test.",
            ),
            risk_tier="high",
            scope=("worker/delivery.py", "tests/test_delivery.py"),
            seed_hints=("worker/delivery.py",),
        ),
    )
    topology = {
        "changed_files": ["worker/delivery.py", "tests/test_delivery.py"],
        "components": [{
            "id": "worker",
            "changed_files": ["worker/delivery.py", "tests/test_delivery.py"],
        }],
        "changed_contract_facts": {},
    }

    assignment_item = fallback_assignment_plan(
        obligations, topology, runtime_config,
    ).assignments[0]

    assert len(assignment_item.obligation_briefs) == 1
    brief = assignment_item.obligation_briefs[0]
    assert brief.obligation_id == "topology:worker:delivery"
    assert brief.subject == "worker delivery"
    assert brief.explanation == (
        "The worker now acknowledges messages after persistence."
    )
    assert brief.risk_tier == "high"
    assert brief.required_evidence == ("implementation", "test")
    assert brief.satisfaction_predicates == (
        "The changed acknowledgement order is traced.",
        "Failure behavior is covered by a test.",
    )


def test_assignment_brief_contains_scoped_changed_behavior(runtime_config):
    obligations = (
        CoverageObligation(
            obligation_id="topology:worker:delivery",
            origin="topology",
            subject="worker delivery",
            required_evidence_categories=("implementation",),
            scope=("worker/delivery.py",),
            seed_hints=("worker/delivery.py",),
        ),
    )
    topology = {
        "changed_files": ["worker/delivery.py", "docs/unrelated.md"],
        "components": [{
            "id": "worker",
            "changed_files": ["worker/delivery.py"],
        }],
        "changed_contract_facts": {
            "worker/delivery.py": {
                "change_type": "modifies",
                "symbols": ["deliver"],
                "hunk_summaries": [
                    "new lines 18-24: def deliver(message):",
                ],
                "action_inputs": [],
                "workflow_steps": [],
            },
            "docs/unrelated.md": {
                "change_type": "modifies",
                "symbols": [],
                "hunk_summaries": ["new lines 1-3"],
                "action_inputs": [],
                "workflow_steps": [],
            },
        },
    }

    assignment_item = fallback_assignment_plan(
        obligations, topology, runtime_config,
    ).assignments[0]

    assert tuple(item.path for item in assignment_item.changed_context) == (
        "worker/delivery.py",
    )
    assert assignment_item.changed_context[0].symbols == ("deliver",)
    assert assignment_item.changed_context[0].hunk_summaries == (
        "new lines 18-24: def deliver(message):",
    )


def test_merged_deterministic_assignment_retains_each_scoped_change_context(
    runtime_config,
):
    obligations = (
        CoverageObligation(
            obligation_id="topology:worker:implementation",
            origin="topology",
            subject="worker delivery",
            required_evidence_categories=("implementation",),
            scope=("worker/delivery.py",),
        ),
        CoverageObligation(
            obligation_id="topology:queue:test",
            origin="topology",
            subject="queue retry tests",
            required_evidence_categories=("test",),
            scope=("tests/test_queue.py",),
        ),
    )
    topology = {
        "changed_files": ["worker/delivery.py", "tests/test_queue.py"],
        "components": [
            {"id": "worker", "changed_files": ["worker/delivery.py"]},
            {"id": "queue", "changed_files": ["tests/test_queue.py"]},
        ],
        "changed_contract_facts": {
            "worker/delivery.py": {
                "change_type": "modifies",
                "symbols": ["deliver"],
                "hunk_summaries": [],
            },
            "tests/test_queue.py": {
                "change_type": "modifies",
                "symbols": ["test_retry"],
                "hunk_summaries": [],
            },
        },
    }
    base = fallback_assignment_plan(obligations, topology, runtime_config)
    assert len(base.assignments) == 2
    target, source = base.assignments

    transformed = apply_planner_transformations(
        {
            "transformations": [{
                "kind": "merge",
                "target_assignment_id": target.id,
                "source_assignment_ids": [source.id],
            }],
        },
        base,
        obligations,
        runtime_config,
        topology=topology,
    ).plan

    assert {
        item.path for item in transformed.assignments[0].changed_context
    } == {"worker/delivery.py", "tests/test_queue.py"}


def test_merged_changed_context_reports_paths_omitted_by_the_bound():
    paths = tuple(f"components/c{index}/behavior.py" for index in range(14))
    obligations = tuple(
        CoverageObligation(
            obligation_id=f"topology:c{index}:implementation",
            origin="topology",
            subject=f"component c{index}",
            required_evidence_categories=("implementation",),
            scope=(path,),
        )
        for index, path in enumerate(paths)
    )
    topology = {
        "changed_files": list(paths),
        "components": [
            {"id": f"c{index}", "changed_files": [path]}
            for index, path in enumerate(paths)
        ],
        "changed_contract_facts": {},
    }
    two_sessions = RuntimeConfig(
        review_deadline_sec=600,
        model_request_timeout_sec=100,
        concurrency=1,
        max_sessions=2,
        session_limits=BudgetLimits(model_turns=6, tool_calls=20, recoveries=1),
    )
    base = fallback_assignment_plan(obligations, topology, two_sessions)
    target, source = base.assignments

    merged = apply_planner_transformations(
        {
            "transformations": [{
                "kind": "merge",
                "target_assignment_id": target.id,
                "source_assignment_ids": [source.id],
            }],
        },
        base,
        obligations,
        two_sessions,
        topology=topology,
    ).plan.assignments[0]

    assert len(merged.changed_context) == 12
    assert merged.changed_context_omitted_paths == 2


def test_split_recomputes_context_after_parent_context_was_truncated():
    paths = tuple(f"component/behavior_{index}.py" for index in range(14))
    obligations = tuple(
        CoverageObligation(
            obligation_id=f"topology:component:behavior-{index}",
            origin="topology",
            subject=f"behavior {index}",
            required_evidence_categories=("implementation",),
            scope=(path,),
        )
        for index, path in enumerate(paths)
    )
    topology = {
        "changed_files": list(paths),
        "components": [{
            "id": "component",
            "changed_files": list(paths),
        }],
        "changed_contract_facts": {},
    }
    two_sessions = RuntimeConfig(
        review_deadline_sec=600,
        model_request_timeout_sec=100,
        concurrency=1,
        max_sessions=2,
        session_limits=BudgetLimits(model_turns=8, tool_calls=20, recoveries=1),
    )
    base = fallback_assignment_plan(obligations, topology, two_sessions)
    original = base.assignments[0]
    assert len(original.changed_context) == 12
    assert original.changed_context_omitted_paths == 2

    split = apply_planner_transformations(
        {
            "transformations": [{
                "kind": "split",
                "assignment_id": original.id,
                "obligation_groups": [
                    list(original.obligation_ids[:7]),
                    list(original.obligation_ids[7:]),
                ],
            }],
        },
        base,
        obligations,
        two_sessions,
        topology=topology,
    ).plan.assignments

    paths_by_obligation = {
        obligation.id: obligation.scope[0] for obligation in obligations
    }
    for assignment_item in split:
        assert {
            item.path for item in assignment_item.changed_context
        } == {
            paths_by_obligation[item]
            for item in assignment_item.obligation_ids
        }
        assert assignment_item.changed_context_omitted_paths == 0


def test_merge_recomputes_overlapping_context_omission_once():
    paths = tuple(f"shared/behavior_{index}.py" for index in range(14))
    obligations = (
        CoverageObligation(
            obligation_id="topology:producer:interaction",
            origin="topology",
            subject="producer interaction",
            required_evidence_categories=("implementation",),
            scope=paths,
        ),
        CoverageObligation(
            obligation_id="topology:consumer:interaction",
            origin="topology",
            subject="consumer interaction",
            required_evidence_categories=("implementation",),
            scope=paths,
        ),
    )
    topology = {
        "changed_files": list(paths),
        "components": [
            {"id": "producer", "changed_files": list(paths[:7])},
            {"id": "consumer", "changed_files": list(paths[7:])},
        ],
        "changed_contract_facts": {},
    }
    two_sessions = RuntimeConfig(
        review_deadline_sec=600,
        model_request_timeout_sec=100,
        concurrency=1,
        max_sessions=2,
        session_limits=BudgetLimits(model_turns=8, tool_calls=20, recoveries=1),
    )
    base = fallback_assignment_plan(obligations, topology, two_sessions)
    assert len(base.assignments) == 2
    assert all(item.changed_context_omitted_paths == 2 for item in base.assignments)
    target, source = base.assignments

    merged = apply_planner_transformations(
        {
            "transformations": [{
                "kind": "merge",
                "target_assignment_id": target.id,
                "source_assignment_ids": [source.id],
            }],
        },
        base,
        obligations,
        two_sessions,
        topology=topology,
    ).plan.assignments[0]

    assert len(merged.changed_context) == 12
    assert merged.changed_context_omitted_paths == 2


def test_split_regenerates_semantic_title_and_objective_for_each_child():
    obligations = (
        CoverageObligation(
            obligation_id="topology:worker:implementation",
            origin="topology",
            subject="worker delivery",
            required_evidence_categories=("implementation",),
            scope=("worker/delivery.py",),
        ),
        CoverageObligation(
            obligation_id="topology:queue:test",
            origin="topology",
            subject="queue retry tests",
            required_evidence_categories=("test",),
            scope=("tests/test_queue.py",),
        ),
    )
    topology = {
        "changed_files": ["worker/delivery.py", "tests/test_queue.py"],
        "components": [{
            "id": "repository",
            "changed_files": ["worker/delivery.py", "tests/test_queue.py"],
        }],
        "changed_contract_facts": {},
    }
    two_sessions = RuntimeConfig(
        max_sessions=2,
        session_limits=BudgetLimits(model_turns=6, tool_calls=20, recoveries=1),
    )
    base = fallback_assignment_plan(obligations, topology, two_sessions)
    original = base.assignments[0]

    split = apply_planner_transformations(
        {
            "transformations": [{
                "kind": "split",
                "assignment_id": original.id,
                "obligation_groups": [
                    [obligations[0].id],
                    [obligations[1].id],
                ],
            }],
        },
        base,
        obligations,
        two_sessions,
        topology=topology,
    ).plan.assignments

    by_obligation = {
        item.obligation_ids[0]: f"{item.title} {item.objective}".lower()
        for item in split
    }
    assert "worker delivery" in by_obligation[obligations[0].id]
    assert "queue retry tests" not in by_obligation[obligations[0].id]
    assert "queue retry tests" in by_obligation[obligations[1].id]
    assert "worker delivery" not in by_obligation[obligations[1].id]


def test_merge_regenerates_semantic_title_and_objective_for_combined_ownership(
    runtime_config,
):
    obligations = (
        CoverageObligation(
            obligation_id="topology:worker:implementation",
            origin="topology",
            subject="worker delivery",
            required_evidence_categories=("implementation",),
            scope=("worker/delivery.py",),
        ),
        CoverageObligation(
            obligation_id="topology:queue:test",
            origin="topology",
            subject="queue retry tests",
            required_evidence_categories=("test",),
            scope=("tests/test_queue.py",),
        ),
    )
    topology = {
        "changed_files": ["worker/delivery.py", "tests/test_queue.py"],
        "components": [
            {"id": "worker", "changed_files": ["worker/delivery.py"]},
            {"id": "queue", "changed_files": ["tests/test_queue.py"]},
        ],
        "changed_contract_facts": {},
    }
    base = fallback_assignment_plan(obligations, topology, runtime_config)
    target, source = base.assignments

    merged = apply_planner_transformations(
        {
            "transformations": [{
                "kind": "merge",
                "target_assignment_id": target.id,
                "source_assignment_ids": [source.id],
            }],
        },
        base,
        obligations,
        runtime_config,
        topology=topology,
    ).plan.assignments[0]
    semantic_text = f"{merged.title} {merged.objective}".lower()

    assert "worker delivery" in semantic_text
    assert "queue retry tests" in semantic_text


def test_deterministic_group_titles_describe_behavior_not_capacity_bucket():
    obligations = (
        CoverageObligation(
            obligation_id="topology:worker:implementation",
            origin="topology",
            subject="worker delivery",
            required_evidence_categories=("implementation",),
            scope=("worker/delivery.py",),
        ),
        CoverageObligation(
            obligation_id="topology:queue:test",
            origin="topology",
            subject="queue retry tests",
            required_evidence_categories=("test",),
            scope=("tests/test_queue.py",),
        ),
    )
    topology = {
        "changed_files": ["worker/delivery.py", "tests/test_queue.py"],
        "components": [
            {"id": "worker", "changed_files": ["worker/delivery.py"]},
            {"id": "queue", "changed_files": ["tests/test_queue.py"]},
        ],
        "changed_contract_facts": {},
    }
    one_session = RuntimeConfig(
        review_deadline_sec=600,
        model_request_timeout_sec=100,
        concurrency=1,
        max_sessions=1,
        session_limits=BudgetLimits(model_turns=6, tool_calls=20, recoveries=1),
    )

    assignment_item = fallback_assignment_plan(
        obligations, topology, one_session,
    ).assignments[0]
    semantic_text = f"{assignment_item.title} {assignment_item.objective}".lower()

    assert "worker delivery" in semantic_text
    assert "queue retry tests" in semantic_text
    assert "combined:" not in semantic_text
    assert "capacity" not in semantic_text
    assert "deterministically cover" not in semantic_text
