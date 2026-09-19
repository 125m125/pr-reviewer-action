"""Pure delegation admission and ownership-transfer preparation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from .assignments import Assignment
from .types import InvestigationLead


_PROPOSAL_KEYS = frozenset({
    "question", "changed_paths", "targets", "reference_paths", "reason",
    "evidence_ids", "observations", "expected_result",
})


@dataclass(frozen=True)
class DelegationLimits:
    remaining_session_capacity: int
    accepted_evidence_ids: frozenset[str] = field(default_factory=frozenset)
    authorized_reference_paths: frozenset[str] = field(default_factory=frozenset)
    existing_request_fingerprints: frozenset[str] = field(default_factory=frozenset)
    transferred_paths: frozenset[str] = field(default_factory=frozenset)
    transferred_obligation_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DelegationProposal:
    status: str
    question: str = ""
    changed_paths: tuple[str, ...] = ()
    obligation_ids: tuple[str, ...] = ()
    reference_paths: tuple[str, ...] = ()
    reason: str = ""
    evidence_ids: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    expected_result: str = ""
    request_fingerprint: str = ""


@dataclass(frozen=True)
class DelegationDecision:
    status: str
    request_id: str
    child_assignment_id: str | None
    reason: str


@dataclass(frozen=True)
class DelegationTransfer:
    proposal: DelegationProposal
    decision: DelegationDecision
    parent_assignment: Assignment
    child_assignment: Assignment
    lead: InvestigationLead


def validate_delegation(
    raw: object,
    assignment: Assignment,
    owned_paths,
    remaining_paths,
    limits: DelegationLimits,
) -> DelegationProposal:
    if not isinstance(raw, Mapping):
        return _rejected("delegation request must be an object")
    if not isinstance(assignment, Assignment):
        raise TypeError("assignment must be an Assignment")
    if not isinstance(limits, DelegationLimits):
        raise TypeError("limits must be DelegationLimits")
    unknown = set(raw) - _PROPOSAL_KEYS
    if unknown:
        return _rejected(
            "delegation request contains unknown keys: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    question = raw.get("question")
    reason = raw.get("reason")
    if not isinstance(question, str) or not question.strip():
        return _rejected("delegation question must be non-empty")
    if not isinstance(reason, str) or not reason.strip():
        return _rejected("delegation reason must be non-empty")
    question = question.strip()
    reason = reason.strip()
    raw_expected_result = raw.get("expected_result")
    if raw_expected_result is None:
        expected_result = "Report scoped findings or confirm the delegated question."
    elif not isinstance(raw_expected_result, str) or not raw_expected_result.strip():
        return _rejected("delegation expected_result must be non-empty")
    else:
        expected_result = raw_expected_result.strip()
    changed_paths, error = _paths(raw.get("changed_paths", []), "changed_paths")
    if error:
        return _rejected(error)
    reference_paths, error = _paths(
        raw.get("reference_paths", []), "reference_paths",
    )
    if error:
        return _rejected(error)
    targets, error = _strings(raw.get("targets", []), "targets")
    if error:
        return _rejected(error)
    evidence_ids, error = _strings(raw.get("evidence_ids", []), "evidence_ids")
    if error:
        return _rejected(error)
    observations, error = _strings(raw.get("observations", []), "observations")
    if error:
        return _rejected(error)

    target_map = {
        f"O{index}": obligation_id
        for index, obligation_id in enumerate(assignment.obligation_ids, start=1)
    }
    obligation_ids = []
    for target in targets:
        obligation_id = target_map.get(target, target)
        if obligation_id not in assignment.obligation_ids:
            return _rejected(f"delegation target is not owned: {target}")
        if obligation_id not in obligation_ids:
            obligation_ids.append(obligation_id)
    if not changed_paths and not obligation_ids:
        return _rejected("delegation must transfer changed paths or a separable requirement")

    owned = frozenset(_normalized_paths(owned_paths))
    remaining = frozenset(_normalized_paths(remaining_paths))
    requested_paths = frozenset(changed_paths)
    transferred_paths = set(limits.transferred_paths)
    already_paths = sorted(requested_paths.intersection(transferred_paths))
    if already_paths:
        return _rejected(
            "delegation paths were already transferred: " + ", ".join(already_paths)
        )
    unowned = sorted(requested_paths - owned)
    if unowned:
        return _rejected("delegation paths are not owned: " + ", ".join(unowned))
    unavailable = sorted(requested_paths - remaining)
    if unavailable:
        return _rejected(
            "delegation paths are no longer remaining: " + ", ".join(unavailable)
        )

    requested_obligations = frozenset(obligation_ids)
    transferred_obligations = set(limits.transferred_obligation_ids)
    already_obligations = sorted(
        requested_obligations.intersection(transferred_obligations)
    )
    if already_obligations:
        return _rejected(
            "delegation requirements were already transferred: "
            + ", ".join(already_obligations)
        )
    remaining_obligations = set(assignment.obligation_ids) - transferred_obligations
    unavailable_obligations = sorted(requested_obligations - remaining_obligations)
    if unavailable_obligations:
        return _rejected(
            "delegation requirements are no longer remaining: "
            + ", ".join(unavailable_obligations)
        )

    unauthorized_references = sorted(
        set(reference_paths) - set(limits.authorized_reference_paths)
    )
    if unauthorized_references:
        return _rejected(
            "delegation reference paths are not authorized: "
            + ", ".join(unauthorized_references)
        )
    unauthorized_evidence = sorted(
        set(evidence_ids) - set(limits.accepted_evidence_ids)
    )
    if unauthorized_evidence:
        return _rejected(
            "delegation evidence is not accepted retained evidence: "
            + ", ".join(unauthorized_evidence)
        )
    if assignment.delegation_depth > 0:
        return _rejected("delegation children cannot delegate")
    if limits.remaining_session_capacity <= 0:
        return _rejected("delegation session capacity is exhausted")
    if requested_paths and requested_paths == remaining:
        return _rejected("delegation cannot transfer the whole remaining scope")
    if not requested_paths and requested_obligations == remaining_obligations:
        return _rejected("delegation cannot transfer the whole remaining scope")

    fingerprint = "delegation:" + hashlib.sha256(json.dumps(
        {
            "parent_assignment_id": assignment.id,
            "changed_paths": sorted(requested_paths),
            "obligation_ids": sorted(requested_obligations),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if fingerprint in limits.existing_request_fingerprints:
        return _rejected("duplicate delegation request")
    return DelegationProposal(
        status="accepted",
        question=question,
        changed_paths=tuple(changed_paths),
        obligation_ids=tuple(obligation_ids),
        reference_paths=tuple(reference_paths),
        reason=reason,
        evidence_ids=tuple(evidence_ids),
        observations=tuple(observations),
        expected_result=expected_result,
        request_fingerprint=fingerprint,
    )


def prepare_delegation_transfer(
    proposal: DelegationProposal,
    assignment: Assignment,
    *,
    request_id: str,
    child_assignment_id: str,
    origin_session_id: str,
) -> DelegationTransfer:
    if not isinstance(proposal, DelegationProposal) or proposal.status != "accepted":
        raise ValueError("only an accepted delegation proposal can transfer ownership")
    if not isinstance(assignment, Assignment):
        raise TypeError("assignment must be an Assignment")
    request_id = str(request_id).strip()
    child_assignment_id = str(child_assignment_id).strip()
    origin_session_id = str(origin_session_id).strip()
    if not request_id or not child_assignment_id or not origin_session_id:
        raise ValueError("delegation transfer ids must be non-empty")
    if assignment.delegation_depth > 0:
        raise ValueError("delegation children cannot delegate")
    unowned_paths = set(proposal.changed_paths) - set(assignment.owned_changed_paths)
    if unowned_paths:
        raise ValueError(
            "delegation proposal contains paths not owned by the parent: "
            + ", ".join(sorted(unowned_paths))
        )
    unowned_obligations = set(proposal.obligation_ids) - set(assignment.obligation_ids)
    if unowned_obligations:
        raise ValueError(
            "delegation proposal contains requirements not owned by the parent: "
            + ", ".join(sorted(unowned_obligations))
        )

    delegated_paths = set(proposal.changed_paths)
    target_only = not delegated_paths
    delegated_obligations = set(proposal.obligation_ids)
    if delegated_paths and not delegated_obligations:
        delegated_obligations.update(
            item.obligation_id for item in assignment.obligation_briefs
            if set(item.scope).intersection(delegated_paths)
        )
        if not delegated_obligations:
            delegated_obligations.update(
                assignment.primary_obligation_ids or assignment.obligation_ids
            )
    parent_obligations = tuple(
        obligation_id for obligation_id in assignment.obligation_ids
        if not target_only or obligation_id not in delegated_obligations
    )
    parent = replace(
        assignment,
        obligation_ids=parent_obligations,
        primary_obligation_ids=tuple(
            obligation_id for obligation_id in assignment.primary_obligation_ids
            if not target_only or obligation_id not in delegated_obligations
        ),
        obligation_briefs=tuple(
            item for item in assignment.obligation_briefs
            if not target_only or item.obligation_id not in delegated_obligations
        ),
        owned_changed_paths=tuple(
            path for path in assignment.owned_changed_paths
            if path not in delegated_paths
        ),
    )
    lead = InvestigationLead(
        lead_id=request_id,
        summary=proposal.question,
        affected_paths=proposal.changed_paths,
        evidence_ids=proposal.evidence_ids,
        next_action=(
            proposal.expected_result
            + (
                " Parent observations: " + "; ".join(proposal.observations)
                if proposal.observations else ""
            )
        )[:1000],
        required_capability="repository",
        origin_session_id=origin_session_id,
        kind="delegation",
        parent_assignment_id=assignment.id,
        child_assignment_id=child_assignment_id,
        delegated_paths=proposal.changed_paths,
        delegated_obligation_ids=tuple(
            item for item in assignment.obligation_ids
            if item in delegated_obligations
        ),
    )
    child_objective = proposal.question
    if proposal.observations:
        child_objective += (
            "\n\nUntrusted parent orientation (not evidence): "
            + "; ".join(proposal.observations)
        )
    child = Assignment(
        id=child_assignment_id,
        title=f"Delegated: {proposal.question}"[:160],
        objective=child_objective[:2000],
        obligation_ids=tuple(
            item for item in assignment.obligation_ids
            if item in delegated_obligations
        ),
        primary_obligation_ids=tuple(
            obligation_id for obligation_id in assignment.primary_obligation_ids
            if obligation_id in delegated_obligations
        ),
        recipe_ids=assignment.recipe_ids,
        lenses=assignment.lenses,
        seed_paths=proposal.reference_paths,
        boundary_paths=assignment.boundary_paths,
        expected_evidence=assignment.expected_evidence,
        estimated_turns=0,
        priority=assignment.priority,
        overlap_justification=(
            f"Delegated by {assignment.id}: {proposal.reason}"
        )[:1000],
        obligation_briefs=tuple(
            item for item in assignment.obligation_briefs
            if item.obligation_id in delegated_obligations
        ),
        changed_context=tuple(
            item for item in assignment.changed_context
            if item.path in delegated_paths
        ),
        investigation_leads=(lead,),
        owner_component_id=assignment.owner_component_id,
        owned_changed_paths=proposal.changed_paths,
        parent_assignment_id=assignment.id,
        delegation_depth=assignment.delegation_depth + 1,
    )
    decision = DelegationDecision(
        status="queued",
        request_id=request_id,
        child_assignment_id=child_assignment_id,
        reason="Delegation admitted and queued for scheduling.",
    )
    return DelegationTransfer(
        proposal=proposal,
        decision=decision,
        parent_assignment=parent,
        child_assignment=child,
        lead=lead,
    )


def _rejected(reason: str) -> DelegationProposal:
    return DelegationProposal(status="rejected", reason=reason)


def _strings(value: object, field_name: str) -> tuple[tuple[str, ...], str]:
    if not isinstance(value, list):
        return (), f"delegation {field_name} must be an array"
    values = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return (), f"delegation {field_name} must contain non-empty strings"
        item = item.strip()
        if item not in values:
            values.append(item)
    return tuple(values), ""


def _paths(value: object, field_name: str) -> tuple[tuple[str, ...], str]:
    values, error = _strings(value, field_name)
    if error:
        return (), error
    paths = []
    for value in values:
        normalized = value.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if (
            not normalized
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or ".." in normalized.split("/")
        ):
            return (), f"delegation {field_name} must contain repository-relative paths"
        paths.append(normalized.strip("/"))
    return tuple(paths), ""


def _normalized_paths(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        str(value).replace("\\", "/").removeprefix("./").strip("/")
        for value in values
    )
