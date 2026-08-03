"""Validate bounded follow-up proposals without transferring controller authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

from .assignments import Assignment
from .coverage import CoverageSnapshot, SessionOwnership
from .types import (
    CoverageObligation,
    ObligationStatus,
    SessionCheckpoint,
    SpecialistAssignment,
)


_ACTION_KINDS = frozenset({"resume", "consult", "new_session", "record_unknown"})
_ACTION_ALIASES = {
    "record-unknown": "record_unknown",
    "new-session": "new_session",
}
_ACTION_RANK = {"resume": 0, "consult": 1, "new_session": 2, "record_unknown": 3}
_ACTION_FIELDS = frozenset({
    "kind", "session_id", "obligation_ids", "expected_evidence",
    "estimated_turns", "reason",
})
_RISK_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}


def _assignment_id(assignment: Assignment | SpecialistAssignment) -> str:
    return str(
        assignment.id if isinstance(assignment, Assignment)
        else assignment.assignment_id
    ).strip()


def _assignment_ownership(
    assignment: Assignment | SpecialistAssignment,
    obligation_by_id: Mapping[str, CoverageObligation],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    primary = set(assignment.primary_obligation_ids)
    if isinstance(assignment, SpecialistAssignment):
        return (
            tuple(sorted(primary)), (),
            tuple(sorted(assignment.independent_obligation_ids)),
        )
    assigned = set(assignment.obligation_ids)
    non_primary = assigned - primary
    independent = {
        obligation_id for obligation_id in assigned
        if obligation_id in obligation_by_id
        and obligation_by_id[obligation_id].requires_independent_verification
    }
    return (
        tuple(sorted(primary)),
        tuple(sorted(non_primary)),
        tuple(sorted(independent)),
    )


class NegotiationError(ValueError):
    """A negotiator proposal exceeded its bounded advisory authority."""

    def __init__(self, errors: tuple[str, ...] | list[str] | str) -> None:
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class SessionResources:
    """Detached remaining budget and lease for one existing session."""

    session_id: str
    remaining_model_turns: int
    lease_remaining_sec: float
    remaining_tool_calls: int

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be non-empty")
        if (
            isinstance(self.remaining_model_turns, bool)
            or not isinstance(self.remaining_model_turns, int)
            or self.remaining_model_turns < 0
        ):
            raise ValueError("remaining_model_turns must be a non-negative integer")
        if (
            isinstance(self.remaining_tool_calls, bool)
            or not isinstance(self.remaining_tool_calls, int)
            or self.remaining_tool_calls < 0
        ):
            raise ValueError("remaining_tool_calls must be a non-negative integer")
        if not isfinite(self.lease_remaining_sec) or self.lease_remaining_sec < 0:
            raise ValueError("lease_remaining_sec must be non-negative and finite")


@dataclass(frozen=True)
class NegotiationState:
    """Immutable controller-owned facts available to proposal validation."""

    obligations: tuple[CoverageObligation, ...]
    coverage: CoverageSnapshot
    assignments: tuple[Assignment | SpecialistAssignment, ...]
    checkpoints: tuple[SessionCheckpoint, ...]
    session_ownership: tuple[SessionOwnership, ...]
    session_resources: tuple[SessionResources, ...]
    remaining_deadline_sec: float
    seconds_per_turn: float
    current_session_count: int
    max_sessions: int
    followup_sessions_started: int
    max_followup_sessions: int
    new_session_turns_remaining: int
    new_session_turn_cap: int
    new_session_lease_remaining_sec: float
    new_session_tool_call_cap: int

    def __post_init__(self) -> None:
        obligation_ids = [item.id for item in self.obligations]
        assignment_ids = [_assignment_id(item) for item in self.assignments]
        ownership_session_ids = [item.session_id for item in self.session_ownership]
        resource_ids = [item.session_id for item in self.session_resources]
        checkpoint_ids = [item.session_id for item in self.checkpoints]
        if len(set(obligation_ids)) != len(obligation_ids):
            raise ValueError("obligation ids must be unique")
        if len(set(assignment_ids)) != len(assignment_ids):
            raise ValueError("assignment ids must be unique")
        if len(set(ownership_session_ids)) != len(ownership_session_ids):
            raise ValueError("session ownership ids must be unique")
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("session resource ids must be unique")
        if len(set(checkpoint_ids)) != len(checkpoint_ids):
            raise ValueError("checkpoint session ids must be unique")
        coverage_ids = [item[0] for item in self.coverage.obligation_statuses]
        if len(set(coverage_ids)) != len(coverage_ids):
            raise ValueError("coverage obligation ids must be unique")
        unknown_coverage = sorted(set(coverage_ids) - set(obligation_ids))
        if unknown_coverage:
            raise ValueError("coverage contains unknown obligations: " + ", ".join(unknown_coverage))
        assignment_by_id = {
            _assignment_id(item): item for item in self.assignments
        }
        obligation_by_id = {item.id: item for item in self.obligations}
        for ownership in self.session_ownership:
            assignment = assignment_by_id.get(ownership.assignment_id)
            if assignment is None:
                raise ValueError("session ownership must reference an existing assignment")
            expected_primary, expected_secondary, expected_independent = _assignment_ownership(
                assignment, obligation_by_id
            )
            if tuple(sorted(ownership.primary_obligation_ids)) != expected_primary:
                raise ValueError("session primary ownership differs from its assignment")
            if tuple(sorted(ownership.secondary_obligation_ids)) != expected_secondary:
                raise ValueError("session secondary ownership differs from its assignment")
            if tuple(sorted(ownership.independent_obligation_ids)) != expected_independent:
                raise ValueError("session independent ownership differs from its assignment")
        if set(resource_ids) - set(ownership_session_ids):
            raise ValueError("session resources must belong to durable session ownership")
        if set(checkpoint_ids) - set(ownership_session_ids):
            raise ValueError("checkpoints must belong to durable session ownership")
        numeric_counts = (
            self.current_session_count, self.max_sessions,
            self.followup_sessions_started, self.max_followup_sessions,
            self.new_session_turns_remaining,
            self.new_session_turn_cap,
            self.new_session_tool_call_cap,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in numeric_counts):
            raise ValueError("session counts and remaining turns must be non-negative integers")
        if self.current_session_count < len(self.session_ownership):
            raise ValueError("current_session_count cannot be smaller than durable sessions")
        if self.current_session_count > self.max_sessions:
            raise ValueError("current_session_count cannot exceed max_sessions")
        if self.followup_sessions_started > self.max_followup_sessions:
            raise ValueError("followup_sessions_started cannot exceed max_followup_sessions")
        for name in (
            "remaining_deadline_sec", "seconds_per_turn", "new_session_lease_remaining_sec",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be non-negative and finite")
        if self.seconds_per_turn == 0:
            raise ValueError("seconds_per_turn must be positive")


@dataclass(frozen=True)
class NegotiationAction:
    """One validated, controller-feasible advisory action."""

    kind: str
    obligation_ids: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    estimated_turns: int
    reason: str
    expected_coverage_gain: int
    session_id: str | None = None
    resolution_policies: tuple[tuple[str, str], ...] = ()

    @property
    def resolution_policy(self) -> str | None:
        policies = {policy for _, policy in self.resolution_policies}
        return next(iter(policies)) if len(policies) == 1 else None


@dataclass(frozen=True)
class NegotiationProposal:
    """Stable immutable collection of validated follow-up actions."""

    actions: tuple[NegotiationAction, ...]


def compact_negotiation_context(state: NegotiationState) -> dict[str, object]:
    """Project controller state into a small, model-facing target catalogue.

    The model only needs to choose *which* unresolved target and *what kind* of
    bounded action to take.  Obligation/session IDs, evidence categories and
    budgets remain controller-owned and are deliberately omitted from this
    projection.
    """
    statuses = dict(state.coverage.obligation_statuses)
    obligations = sorted(
        (
            item for item in state.obligations
            if item.mandatory
            and item.required_evidence_categories
            and statuses.get(item.id, ObligationStatus.PENDING)
            is not ObligationStatus.COVERED
        ),
        key=lambda item: (_RISK_RANK.get(item.risk_tier, 2), item.id),
    )
    ownership = tuple(state.session_ownership)
    targets: list[dict[str, object]] = []
    for index, item in enumerate(obligations, start=1):
        owners = tuple(
            owner for owner in ownership if item.id in owner.obligation_ids
        )
        primary = any(item.id in owner.primary_obligation_ids for owner in owners)
        actions = ["record_unknown", "new_session"]
        if primary:
            actions.insert(0, "resume")
        elif owners:
            actions.insert(0, "consult")
        targets.append({
            "handle": f"U{index}",
            "risk_tier": item.risk_tier,
            "subject": item.subject,
            "summary": (
                f"Investigate the {item.risk_tier}-risk obligation concerning "
                f"{item.subject}."
            ),
            "allowed_actions": tuple(actions),
        })
    return {
        "protocol": "choose exactly one action for one target handle",
        "targets": tuple(targets),
    }


def _string_list(value: Any, field: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        errors.append(f"{field} must be an array")
        return ()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field} must contain non-empty strings")
            continue
        result.append(item.strip())
    if not result:
        errors.append(f"{field} must not be empty")
    if len(set(result)) != len(result):
        errors.append(f"{field} must not contain duplicates")
    return tuple(sorted(set(result)))


def _normalise_action_kind(value: Any, *, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        return None
    kind = value.strip()
    normalised = _ACTION_ALIASES.get(kind, kind)
    if normalised != kind:
        errors.append(f"normalized action kind alias {kind!r} to {normalised!r}")
    if normalised not in _ACTION_KINDS:
        return None
    return normalised


def _compact_target_obligations(
    state: NegotiationState,
) -> dict[str, CoverageObligation]:
    statuses = dict(state.coverage.obligation_statuses)
    obligations = sorted(
        (
            item for item in state.obligations
            if item.mandatory
            and item.required_evidence_categories
            and statuses.get(item.id, ObligationStatus.PENDING)
            is not ObligationStatus.COVERED
        ),
        key=lambda item: (_RISK_RANK.get(item.risk_tier, 2), item.id),
    )
    return {f"U{index}": item for index, item in enumerate(obligations, start=1)}


def _compact_session_for(
    kind: str,
    obligation: CoverageObligation,
    state: NegotiationState,
) -> str | None:
    owners = tuple(
        owner for owner in state.session_ownership
        if obligation.id in owner.obligation_ids
    )
    if kind == "resume":
        owners = tuple(
            owner for owner in owners
            if obligation.id in owner.primary_obligation_ids
        )
    if not owners:
        return None
    resources = {item.session_id: item for item in state.session_resources}
    # Prefer a feasible owner, then stable controller order.  Validation still
    # rechecks ownership, budget and lease after this projection.
    feasible = tuple(
        owner for owner in sorted(owners, key=lambda item: item.session_id)
        if (
            resources.get(owner.session_id) is not None
            and resources[owner.session_id].remaining_model_turns > 0
            and resources[owner.session_id].remaining_tool_calls > 0
            and resources[owner.session_id].lease_remaining_sec >= state.seconds_per_turn
        )
    )
    return (feasible or tuple(sorted(owners, key=lambda item: item.session_id)))[0].session_id


def _compact_raw_to_legacy(
    raw: Mapping[str, Any],
    state: NegotiationState,
) -> dict[str, Any]:
    fields = set(raw)
    unsupported = sorted(fields - {"kind", "target", "reason"})
    if unsupported:
        raise NegotiationError(
            "compact proposal has unsupported fields: " + ", ".join(unsupported)
        )
    errors: list[str] = []
    kind = _normalise_action_kind(raw.get("kind"), errors=[])
    if kind is None:
        errors.append(
            "kind must be exactly resume, consult, new_session, or record_unknown"
        )
    target = raw.get("target")
    if not isinstance(target, str) or not target.strip():
        errors.append("target must be a non-empty controller target handle")
        target = ""
    else:
        target = target.strip()
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("reason must be a non-empty string")
        reason = ""
    else:
        reason = " ".join(reason.split())
    obligation = _compact_target_obligations(state).get(target)
    if obligation is None:
        errors.append(f"target {target!r} is not an unresolved controller target")
    if errors:
        # Alias diagnostics are useful in the event journal but are not errors.
        diagnostics = tuple(
            item for item in errors if item.startswith("normalized action kind alias")
        )
        fatal = tuple(item for item in errors if item not in diagnostics)
        if fatal:
            raise NegotiationError(fatal)
    assert obligation is not None
    assert kind is not None
    estimated_turns = 0 if kind == "record_unknown" else 1
    action: dict[str, Any] = {
        "kind": kind,
        "obligation_ids": [obligation.id],
        "expected_evidence": list(obligation.required_evidence_categories),
        "estimated_turns": estimated_turns,
        "reason": reason,
    }
    if kind in {"resume", "consult"}:
        session_id = _compact_session_for(kind, obligation, state)
        if session_id is None:
            raise NegotiationError(
                f"target {target!r} has no controller-owned session for {kind}"
            )
        action["session_id"] = session_id
    return {"actions": [action]}


def _parse_action(
    raw: Any,
    index: int,
    state: NegotiationState,
) -> tuple[NegotiationAction | None, list[str]]:
    label = f"action {index}"
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        return None, [f"{label} must be an object"]
    extra = sorted(set(raw) - _ACTION_FIELDS)
    if extra:
        errors.append(f"{label} has unsupported fields: {', '.join(extra)}")
    kind = _normalise_action_kind(raw.get("kind"), errors=errors)
    if kind is None:
        errors.append(
            f"{label} kind must be exactly resume, consult, new_session, or record_unknown"
        )
        return None, errors

    obligation_ids = _string_list(raw.get("obligation_ids"), f"{label} obligation_ids", errors)
    expected_evidence = _string_list(
        raw.get("expected_evidence"), f"{label} expected_evidence", errors
    )
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append(f"{label} reason must be a non-empty string")
        reason = ""
    else:
        reason = " ".join(reason.split())
    estimated_turns = raw.get("estimated_turns")
    valid_turns = isinstance(estimated_turns, int) and not isinstance(estimated_turns, bool)
    if not valid_turns or estimated_turns < 0 or (kind != "record_unknown" and estimated_turns == 0):
        errors.append(
            f"{label} estimated_turns must be a positive integer"
            + (" or zero for record_unknown" if kind == "record_unknown" else "")
        )
        estimated_turns = 0
    if kind == "record_unknown" and estimated_turns != 0:
        errors.append(f"{label} record_unknown must not consume model turns")

    obligation_by_id = {item.id: item for item in state.obligations}
    statuses = dict(state.coverage.obligation_statuses)
    unknown = sorted(set(obligation_ids) - set(obligation_by_id))
    if unknown:
        errors.append(f"{label} contains unknown obligations: {', '.join(unknown)}")
    completed = sorted(
        obligation_id for obligation_id in obligation_ids
        if statuses.get(obligation_id, ObligationStatus.PENDING) is ObligationStatus.COVERED
    )
    if completed:
        errors.append(f"{label} repeats already covered obligations: {', '.join(completed)}")
    non_mandatory = sorted(
        obligation_id for obligation_id in obligation_ids
        if obligation_id in obligation_by_id and not obligation_by_id[obligation_id].mandatory
    )
    if non_mandatory:
        errors.append(f"{label} targets non-mandatory obligations: {', '.join(non_mandatory)}")

    required_union: set[str] = set()
    no_gain: list[str] = []
    for obligation_id in obligation_ids:
        obligation = obligation_by_id.get(obligation_id)
        if obligation is None:
            continue
        required = set(obligation.required_evidence_categories)
        required_union.update(required)
        if not required.intersection(expected_evidence):
            no_gain.append(obligation_id)
    unsupported_evidence = sorted(set(expected_evidence) - required_union)
    if no_gain or unsupported_evidence:
        detail = ", ".join(sorted(no_gain))
        errors.append(
            f"{label} expected new evidence provides no positive coverage gain"
            + (f" for: {detail}" if detail else "")
        )

    raw_session_id = raw.get("session_id")
    if kind in {"resume", "consult"}:
        if "session_id" not in raw:
            errors.append(f"{label} {kind} requires session_id field")
            session_id = None
        elif not isinstance(raw_session_id, str) or not raw_session_id.strip():
            errors.append(f"{label} {kind} session_id must be a non-empty string")
            session_id = None
        else:
            session_id = raw_session_id.strip()
    else:
        session_id = None
        if "session_id" in raw:
            errors.append(f"{label} {kind} must omit session_id")

    ownership_by_session = {
        item.session_id: item for item in state.session_ownership
    }
    if session_id is not None:
        owner = ownership_by_session.get(session_id)
        if owner is None:
            errors.append(f"{label} session has no controller-owned assignment projection")
        elif kind == "resume" and not set(obligation_ids).issubset(owner.primary_obligation_ids):
            errors.append(f"{label} resume session is not the primary owner of all obligations")
        elif kind == "consult" and not set(obligation_ids).issubset(owner.obligation_ids):
            errors.append(f"{label} consultation is outside the session's assignment ownership")

    policies = tuple(sorted(
        (obligation_id, obligation_by_id[obligation_id].unresolved_policy)
        for obligation_id in obligation_ids
        if obligation_id in obligation_by_id
    )) if kind == "record_unknown" else ()
    if errors:
        return None, errors
    return NegotiationAction(
        kind=kind,
        session_id=session_id,
        obligation_ids=obligation_ids,
        expected_evidence=expected_evidence,
        estimated_turns=estimated_turns,
        reason=reason,
        expected_coverage_gain=len(obligation_ids),
        resolution_policies=policies,
    ), []


def _validate_feasibility(
    actions: tuple[NegotiationAction, ...],
    state: NegotiationState,
) -> list[str]:
    errors: list[str] = []
    resources = {item.session_id: item for item in state.session_resources}
    turns_by_session: dict[str, int] = {}
    new_session_actions = tuple(item for item in actions if item.kind == "new_session")
    exploration_turns = sum(item.estimated_turns for item in actions)
    if exploration_turns * state.seconds_per_turn > state.remaining_deadline_sec:
        errors.append("proposal estimated turns exceed the remaining deadline")

    for action in actions:
        if action.kind not in {"resume", "consult"} or action.session_id is None:
            continue
        turns_by_session[action.session_id] = (
            turns_by_session.get(action.session_id, 0) + action.estimated_turns
        )
    for session_id, turns in sorted(turns_by_session.items()):
        resource = resources.get(session_id)
        if resource is None:
            errors.append(f"session '{session_id}' has no remaining budget/lease projection")
            continue
        if turns > resource.remaining_model_turns:
            errors.append(f"session '{session_id}' exceeds its remaining model-turn budget")
        if resource.remaining_tool_calls == 0:
            errors.append(f"session '{session_id}' has no remaining tool-call budget")
        if turns * state.seconds_per_turn > resource.lease_remaining_sec:
            errors.append(f"session '{session_id}' exceeds its remaining lease")

    new_count = len(new_session_actions)
    if state.current_session_count + new_count > state.max_sessions:
        errors.append("proposal exceeds hard session capacity")
    if state.followup_sessions_started + new_count > state.max_followup_sessions:
        errors.append("proposal exceeds follow-up session capacity")
    new_turns = sum(item.estimated_turns for item in new_session_actions)
    if new_turns > state.new_session_turns_remaining:
        errors.append("new sessions exceed remaining follow-up model-turn budget")
    if any(
        item.estimated_turns * state.seconds_per_turn > state.new_session_lease_remaining_sec
        for item in new_session_actions
    ):
        errors.append("new session exceeds its available lease")
    if any(item.estimated_turns > state.new_session_turn_cap for item in new_session_actions):
        errors.append("new session exceeds the controller-owned per-session turn cap")
    if new_session_actions and state.new_session_tool_call_cap == 0:
        errors.append("new session has no controller-owned tool-call budget")
    return errors


def _action_order(
    action: NegotiationAction,
    obligation_by_id: Mapping[str, CoverageObligation],
) -> tuple[int, tuple[str, ...], int, str]:
    risk_rank = min(
        (
            _RISK_RANK.get(obligation_by_id[obligation_id].risk_tier, _RISK_RANK["normal"])
            for obligation_id in action.obligation_ids
        ),
        default=_RISK_RANK["normal"],
    )
    return (
        risk_rank,
        action.obligation_ids,
        _ACTION_RANK[action.kind],
        action.session_id or "",
    )


def validate_negotiation(raw: Mapping[str, Any], state: NegotiationState) -> NegotiationProposal:
    """Validate model JSON while retaining all obligation and budget authority."""
    if not isinstance(state, NegotiationState):
        raise TypeError("state must be a NegotiationState")
    if not isinstance(raw, Mapping):
        raise NegotiationError("proposal must be an object")
    if "actions" not in raw and ({"kind", "target"} & set(raw)):
        return validate_compact_negotiation(raw, state)
    extra = sorted(set(raw) - {"actions"})
    if extra:
        raise NegotiationError("proposal has unsupported fields: " + ", ".join(extra))
    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise NegotiationError("proposal actions must be a non-empty array")

    errors: list[str] = []
    parsed: list[NegotiationAction] = []
    for index, item in enumerate(raw_actions):
        action, action_errors = _parse_action(item, index, state)
        errors.extend(action_errors)
        if action is not None:
            parsed.append(action)
    actions = tuple(parsed)
    targeted: set[str] = set()
    targeted_sessions: set[str] = set()
    for action in actions:
        duplicate = sorted(targeted.intersection(action.obligation_ids))
        if duplicate:
            errors.append("proposal repeats obligations across actions: " + ", ".join(duplicate))
        targeted.update(action.obligation_ids)
        if action.session_id is not None:
            if action.session_id in targeted_sessions:
                errors.append(
                    f"proposal has multiple actions for the same durable session: "
                    f"{action.session_id}"
                )
            targeted_sessions.add(action.session_id)
    errors.extend(_validate_feasibility(actions, state))
    if errors:
        raise NegotiationError(errors)
    obligation_by_id = {item.id: item for item in state.obligations}
    return NegotiationProposal(actions=tuple(sorted(
        actions, key=lambda action: _action_order(action, obligation_by_id)
    )))


def validate_compact_negotiation(
    raw: Mapping[str, Any], state: NegotiationState,
) -> NegotiationProposal:
    """Validate the one-action, handle-based negotiator response.

    The compact response deliberately contains no controller-owned IDs, evidence
    categories or budgets.  Those values are projected from ``state`` and then
    passed through the same legacy validator, preserving all semantic checks.
    """
    if not isinstance(raw, Mapping):
        raise NegotiationError("compact proposal must be an object")
    if "actions" in raw:
        actions = raw.get("actions")
        if isinstance(actions, list) and len(actions) != 1:
            raise NegotiationError("compact proposal must contain exactly one action")
        raise NegotiationError(
            "compact proposal must contain kind, target, and reason, not actions"
        )
    return validate_negotiation(_compact_raw_to_legacy(raw, state), state)


def _fallback_raw(
    kind: str,
    obligation: CoverageObligation,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "kind": kind,
        "obligation_ids": [obligation.id],
        "expected_evidence": list(obligation.required_evidence_categories),
        "estimated_turns": 0 if kind == "record_unknown" else 1,
        "reason": (
            "Deterministic fallback selected the highest-risk uncovered obligation."
            if kind != "record_unknown"
            else "No feasible bounded investigation remains; apply the obligation's unresolved policy."
        ),
    }
    if session_id is not None:
        action["session_id"] = session_id
    return {"actions": [action]}


def fallback_next_action(state: NegotiationState) -> NegotiationAction:
    """Choose one stable, narrow next action for the highest-risk uncovered work."""
    statuses = dict(state.coverage.obligation_statuses)
    uncovered = tuple(sorted(
        (
            item for item in state.obligations
            if item.mandatory
            and item.required_evidence_categories
            and statuses.get(item.id, ObligationStatus.PENDING) is not ObligationStatus.COVERED
        ),
        key=lambda item: (_RISK_RANK.get(item.risk_tier, 2), item.id),
    ))
    if not uncovered:
        raise NegotiationError("no uncovered mandatory obligations remain")
    obligation = uncovered[0]

    primary_owners = sorted(
        item.session_id for item in state.session_ownership
        if obligation.id in item.primary_obligation_ids
    )
    for session_id in primary_owners:
        try:
            return validate_negotiation(
                _fallback_raw("resume", obligation, session_id=session_id), state
            ).actions[0]
        except NegotiationError:
            continue

    try:
        return validate_negotiation(_fallback_raw("new_session", obligation), state).actions[0]
    except NegotiationError:
        return validate_negotiation(_fallback_raw("record_unknown", obligation), state).actions[0]
