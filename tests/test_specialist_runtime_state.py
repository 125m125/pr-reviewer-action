from dataclasses import FrozenInstanceError, replace

import pytest

from pr_reviewer.specialist_runtime.budget import BudgetExceeded, BudgetLedger, RunDeadline
from pr_reviewer.specialist_runtime.events import RunArtifactProjector, RunEvent
from pr_reviewer.specialist_runtime.types import BudgetLimits, PhaseShares, RunPhase


def test_lifetime_budget_never_resets_on_recovery():
    ledger = BudgetLedger(BudgetLimits(model_turns=4, tool_calls=3, recoveries=1))
    ledger.record_model_turn(input_tokens=10, output_tokens=5)
    ledger.reserve_tool_calls(2)
    ledger.record_recovery("repetitive-transcript")
    snapshot = ledger.snapshot()
    assert snapshot.model_turns == 1
    assert snapshot.tool_calls == 2
    assert snapshot.recoveries == 1
    with pytest.raises(BudgetExceeded):
        ledger.record_recovery("context-pressure")


def test_phase_shares_must_total_one_hundred():
    with pytest.raises(ValueError, match="total 100"):
        PhaseShares(planning=10, initial=60, followup=20, finalization=9)


def test_artifact_projection_is_event_order_deterministic():
    events = [
        RunEvent(sequence=1, kind="run_started", payload={"head_sha": "abc"}),
        RunEvent(sequence=2, kind="phase_changed", payload={"phase": RunPhase.PLANNING.value}),
    ]
    assert RunArtifactProjector().project(events)["phase"] == "planning"


def test_model_turn_token_limit_is_atomic():
    ledger = BudgetLedger(
        BudgetLimits(model_turns=2, tool_calls=2, recoveries=1, input_tokens=10)
    )
    ledger.record_model_turn(input_tokens=7, output_tokens=3)

    with pytest.raises(BudgetExceeded):
        ledger.record_model_turn(input_tokens=4, output_tokens=1)

    assert ledger.snapshot() == replace(
        ledger.snapshot(), model_turns=1, input_tokens=7, output_tokens=3
    )


def test_tool_reservation_rejects_an_unavailable_batch_without_mutation():
    ledger = BudgetLedger(BudgetLimits(model_turns=1, tool_calls=3, recoveries=1))
    ledger.reserve_tool_calls(2)

    with pytest.raises(BudgetExceeded):
        ledger.reserve_tool_calls(2)

    assert ledger.snapshot().tool_calls == 2
    assert ledger.remaining_tool_calls() == 1


def test_budget_diagnostics_keep_normalized_reasons_and_reset_only_the_streak():
    ledger = BudgetLedger(BudgetLimits(model_turns=1, tool_calls=1, recoveries=1))
    assert ledger.record_no_progress() == 1
    assert ledger.record_no_progress() == 2
    ledger.record_tool_rejection("  out   of scope ")
    ledger.reset_no_progress_streak(" controller   feedback ")

    snapshot = ledger.snapshot()
    assert snapshot.tool_rejections == 1
    assert snapshot.tool_rejection_reasons == ("out of scope",)
    assert snapshot.no_progress_streak == 0
    assert snapshot.no_progress_reset_reasons == ("controller feedback",)


def test_deadline_uses_absolute_cutoffs_and_preserves_finalization_reserve():
    deadline = RunDeadline(
        started_at=100.0,
        deadline_sec=100.0,
        phase_shares=PhaseShares(planning=10, initial=60, followup=20, finalization=10),
    )

    assert deadline.cutoff_for(RunPhase.PLANNING) == 110.0
    assert deadline.cutoff_for(RunPhase.INITIAL) == 170.0
    assert deadline.cutoff_for(RunPhase.FOLLOWUP) == 190.0
    assert deadline.cutoff_for(RunPhase.FINALIZATION) == 200.0
    assert deadline.exploration_allowed(now=189.9)
    assert not deadline.exploration_allowed(now=190.0)


def test_deadline_is_an_immutable_value():
    deadline = RunDeadline(100.0, 10.0, PhaseShares())

    with pytest.raises(FrozenInstanceError):
        deadline.deadline_sec = 20.0


def test_artifact_projection_rejects_duplicate_and_missing_sequences():
    projector = RunArtifactProjector()
    with pytest.raises(ValueError, match="duplicate"):
        projector.project(
            [
                RunEvent(sequence=1, kind="run_started"),
                RunEvent(sequence=1, kind="phase_changed"),
            ]
        )
    with pytest.raises(ValueError, match="missing"):
        projector.project([RunEvent(sequence=2, kind="run_started")])


def test_event_payload_deep_snapshot_isolated_from_caller_and_artifact_mutation():
    caller_payload = {"nested": {"values": ["original"]}}
    event = RunEvent(sequence=1, kind="run_started", payload=caller_payload)
    artifact = RunArtifactProjector().project([event])

    caller_payload["nested"]["values"].append("caller mutation")

    assert event.payload["nested"]["values"] == ("original",)
    assert artifact["events"][0]["payload"]["nested"]["values"] == ["original"]


def test_budget_snapshot_returns_a_fresh_immutable_value():
    ledger = BudgetLedger(BudgetLimits(model_turns=2, tool_calls=1, recoveries=1))

    first = ledger.snapshot()
    second = ledger.snapshot()
    ledger.record_model_turn()

    assert first == second
    assert first is not second
    assert first.model_turns == 0
