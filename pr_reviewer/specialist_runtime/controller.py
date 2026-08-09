"""Authoritative orchestration for one specialist review run.

The controller deliberately composes the runtime components implemented by Tasks
1--12.  It owns ordering, controller-authority projections, degradation, and the
terminal artifact; it does not reproduce their policy decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
import hashlib
import inspect
import json
from math import isfinite
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import urlsplit

from pr_reviewer.conversation import Conversation
from pr_reviewer.specialists import classify_file_roles
from .adjudication import (
    AdjudicatedReview,
    ReviewHandoffContext,
    ReviewOrientationTopic,
    adjudicate_candidates,
    apply_runtime_verdict_policy,
    build_review_handoff,
    build_review_notes,
    consolidate_candidates,
    review_orientation_label,
)
from .assignments import (
    Assignment,
    AssignmentPlan,
    apply_planner_transformations,
    fallback_assignment_plan,
    validate_assignment_plan,
)
from .budget import BudgetLedger, RunDeadline, SessionLease
from .callbacks import (
    CALLBACK_POOL,
    CallbackTimedOut,
    format_callback_error,
    freeze_callback_value,
    mask_runtime_text,
)
from .coverage import (
    CoverageLedger,
    CoverageReconciliation,
    SessionOwnership,
    derive_obligations,
    reconcile_wave,
    session_ownership_for_assignment,
)
from .evidence import EvidenceSnapshot, EvidenceStore
from .events import EventJournal, RunEvent
from .negotiation import (
    NegotiationAction,
    NegotiationError,
    NegotiationState,
    SessionResources,
    compact_negotiation_context,
    fallback_next_action,
    validate_compact_negotiation,
    validate_negotiation,
)
from .model_gateway import ModelGateway, ModelTurnRequest
from .policy import ReviewPolicy, RuntimeConfig
from .request_attempts import RequestAttempt, RequestAttemptJournal
from .scheduler import SessionScheduler, WaveResult, WaveSnapshot
from .types import (
    BudgetUsage,
    CandidateFinding,
    change_overview_orientation,
    CoverageObligation,
    ObligationStatus,
    ReviewHandoff,
    ReviewNote,
    RunPhase,
)
from .web_evidence import SourceAccessRequest


_SCHEMA_VERSION = 2
_MAX_ARTIFACT_STRING = 16 * 1024
_MAX_ARTIFACT_ITEMS = 2_000
_MAX_CHANGE_OVERVIEW_ITEMS = 12
_PROHIBITED_OVERVIEW_CLAIM = re.compile(
    r"(?i)\b(?:approve|approval|request[_ -]?changes|verdict|findings?|"
    r"blocker|bugs?|defects?|vulnerabilit(?:y|ies)|"
    r"unsafe|safe\s+to\s+merge|ready\s+to\s+merge|merge[- ]safe|"
    r"coverage\s+(?:is\s+)?(?:complete|sufficient|verified)|"
    r"(?:is|are|was|were|has\s+been|have\s+been)\s+"
    r"(?:fully\s+)?(?:verified|validated|tested|covered)|"
    r"every\s+(?:branch|path|case)\s+(?:is\s+)?tested|"
    r"(?:all|fully|completely)\b.{0,40}\b(?:covered|reviewed|verified|tested))\b"
)
_PATH_WITH_DIRECTORY = re.compile(
    r"(?<![A-Za-z0-9_./:-])"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
)
_ROOT_FILE_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./:-])"
    r"(?:[A-Za-z0-9_-]+\.[A-Za-z][A-Za-z0-9_-]{0,62}"
    r"|\.[A-Za-z0-9_-]+)\b"
)
_PROSE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_./-])[A-Za-z0-9_.-]+"
    r"(?:/[A-Za-z0-9_.-]+)*(?![A-Za-z0-9_./-])"
)
_URL_CANDIDATE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:"
    r"https?://[^\s<>()]+|"
    r"www\.(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}(?:/[^\s<>()]*)?|"
    r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}/[^\s<>()]*|"
    r"(?:[A-Za-z0-9-]+\.){2,}[A-Za-z]{2,63}"
    r")"
)
_HANDOFF_COMPONENT_REFERENCE = re.compile(
    r"(?i)\b(?:the\s+)?([a-z][a-z0-9_-]{1,63})\s+"
    r"(?:component|service|gateway|boundary)\b"
)
_DETERMINISTIC_CONTEXT_KEYS = frozenset({
    "context_paths", "related_paths", "affected_paths", "affected_consumers",
    "affected_producers", "affected_callees", "affected_callers",
    "consumer_paths", "producer_paths", "callee_paths", "caller_paths",
    "retained_evidence_paths", "evidence_paths", "dependency_paths",
})
_RELATION_PATH_KEYS = frozenset({
    "path", "paths", "source_path", "target_path", "producer_path",
    "consumer_path", "callee_path", "caller_path", "related_paths",
    "affected_paths", "context_paths", "consumer_paths", "producer_paths",
    "callee_paths", "caller_paths",
})
_SIGNAL_ORIENTATION_TOPICS = {
    "implementation": (ReviewOrientationTopic.IMPLEMENTATION,),
    "documentation": (ReviewOrientationTopic.DOCUMENTATION,),
    "other": (ReviewOrientationTopic.REPOSITORY_BEHAVIOR,),
    "test": (ReviewOrientationTopic.TEST_COVERAGE,),
    "tests": (ReviewOrientationTopic.TEST_COVERAGE,),
    "schema_contract": (ReviewOrientationTopic.API_CONTRACTS,),
    "producer": (ReviewOrientationTopic.API_CONTRACTS,),
    "consumer": (ReviewOrientationTopic.API_CONTRACTS,),
    "generated": (ReviewOrientationTopic.GENERATED_ARTIFACTS,),
    "migration": (ReviewOrientationTopic.DATABASE,),
    "persistence": (ReviewOrientationTopic.DATABASE,),
    "messaging": (ReviewOrientationTopic.CROSS_COMPONENT_CONTRACTS,),
    "delivery": (ReviewOrientationTopic.FAILURE_RECOVERY,),
    "deployment": (ReviewOrientationTopic.DEPLOYMENT,),
    "deployment_artifact": (ReviewOrientationTopic.DEPLOYMENT,),
    "build_manifest": (ReviewOrientationTopic.DEPLOYMENT,),
    "configuration": (ReviewOrientationTopic.DEPLOYMENT,),
    "trust_boundary": (
        ReviewOrientationTopic.AUTHORIZATION,
        ReviewOrientationTopic.SECURITY,
    ),
    "interaction": (ReviewOrientationTopic.CROSS_COMPONENT_CONTRACTS,),
    "auth_changes": (
        ReviewOrientationTopic.AUTHORIZATION,
        ReviewOrientationTopic.SECURITY,
    ),
    "public_route_changes": (
        ReviewOrientationTopic.AUTHORIZATION,
        ReviewOrientationTopic.API_CONTRACTS,
    ),
    "file_serving_changes": (ReviewOrientationTopic.SECURITY,),
    "path_handling_changes": (ReviewOrientationTopic.SECURITY,),
    "secret_handling_changes": (ReviewOrientationTopic.SECURITY,),
    "linked_security_issue": (ReviewOrientationTopic.SECURITY,),
    "linked_audit_issue": (ReviewOrientationTopic.SECURITY,),
    "dependency_changes": (ReviewOrientationTopic.DEPLOYMENT,),
}

_HANDOFF_RISK_ORDER = {
    "critical": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
}

_CANDIDATE_RETENTION_UNKNOWN = "candidate-retention-unknown"


def _behavioral_handoff_candidates(
    *,
    changed_files: Iterable[str],
    topology: Mapping[str, Any],
    obligations: Iterable[CoverageObligation],
    evidence_records: Iterable[object],
    reviewed_obligation_ids: Iterable[str],
    allow_role_fallback: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Build bounded, controller-backed behavioral summaries across the full PR."""
    changed = tuple(dict.fromkeys(str(path) for path in changed_files if str(path)))
    changed_set = set(changed)
    obligation_items = tuple(obligations)
    reviewed_ids = set(reviewed_obligation_ids)
    facts_value = topology.get("changed_contract_facts", {})
    facts = facts_value if isinstance(facts_value, Mapping) else {}
    evidence_paths = {
        str(getattr(record, "source_path", "") or "")
        for record in evidence_records
        if bool(getattr(record, "is_usable_for_coverage", False))
    }

    by_path: dict[str, list[CoverageObligation]] = {path: [] for path in changed}
    for obligation in obligation_items:
        paths = {
            path for path in (*obligation.scope, *obligation.seed_hints)
            if path in changed_set
        }
        if obligation.subject in changed_set:
            paths.add(obligation.subject)
        for path in paths:
            by_path[path].append(obligation)

    def file_kind(path: str) -> int:
        roles = set(classify_file_roles(path))
        if "documentation" in roles:
            return 2
        if "test" in roles:
            return 1
        return 0

    def behavior_priority(path: str) -> int:
        roles = set(classify_file_roles(path))
        if "trust-boundary" in roles:
            return 0
        if {"deployment", "build-manifest", "configuration"} & roles:
            return 2
        if "implementation" in roles and "test" not in roles:
            return 1
        return 2

    def path_priority(path: str) -> tuple[int, int, int, str]:
        return (
            file_kind(path),
            behavior_priority(path),
            min(
                (
                    _HANDOFF_RISK_ORDER.get(item.risk_tier, 2)
                    for item in by_path[path]
                ),
                default=2,
            ),
            path,
        )

    def fact_values(path: str, key: str) -> tuple[str, ...]:
        item = facts.get(path)
        if not isinstance(item, Mapping):
            return ()
        values = item.get(key, ())
        if not isinstance(values, (list, tuple)):
            return ()
        normalized = []
        for value in values:
            safe = re.sub(r"[^A-Za-z0-9 .:/+_-]+", " ", str(value))
            safe = " ".join(safe.split())[:120]
            if safe and safe not in normalized:
                normalized.append(safe)
        return tuple(normalized[:5])

    def change_type(path: str) -> str:
        item = facts.get(path)
        value = item.get("change_type") if isinstance(item, Mapping) else None
        return value if value in {"adds", "removes", "modifies", "changes"} else "changes"

    def contract_label(path: str) -> str | None:
        candidates = sorted(
            (
                item for item in by_path[path]
                if item.subject and item.subject != path
            ),
            key=lambda item: (
                _HANDOFF_RISK_ORDER.get(item.risk_tier, 2),
                item.subject,
            ),
        )
        return candidates[0].subject if candidates else None

    what_changed: list[str] = []
    ai_reviewed: list[str] = []
    summarized_paths: set[str] = set()

    def add_summary(path: str, *, fallback: bool) -> None:
        used_role_fallback = fallback
        verb = change_type(path)
        symbols = fact_values(path, "symbols")
        action_inputs = fact_values(path, "action_inputs")
        workflow_steps = fact_values(path, "workflow_steps")
        if action_inputs:
            summary = f"`{path}` {verb} the `{action_inputs[0]}` action input contract."
            reviewed_behavior = f"the `{action_inputs[0]}` action input contract"
        elif workflow_steps:
            summary = f"`{path}` {verb} the `{workflow_steps[0]}` workflow step."
            reviewed_behavior = f"the `{workflow_steps[0]}` workflow step contract"
        elif symbols:
            summary = f"`{path}` {verb} `{symbols[0]}()` behavior."
            reviewed_behavior = f"the `{symbols[0]}()` behavior"
        elif fallback:
            topics = _orientation_topics(classify_file_roles(path))
            topic = topics[0] if topics else ReviewOrientationTopic.REPOSITORY_BEHAVIOR
            label = (
                review_orientation_label(topic)
                or "Repository behavior and integration"
            ).lower()
            summary = f"`{path}` {verb} {label}."
            reviewed_behavior = f"{label} in `{path}`"
        else:
            return
        if len(what_changed) < 5:
            what_changed.append(summary)
            summarized_paths.add(path)
        path_reviewed = (
            path in evidence_paths
            or any(item.id in reviewed_ids for item in by_path[path])
        )
        if path_reviewed and len(ai_reviewed) < 5:
            contract = None if used_role_fallback else contract_label(path)
            suffix = f" and {contract} contract" if contract else ""
            location = "" if f"`{path}`" in reviewed_behavior else f" in `{path}`"
            ai_reviewed.append(
                f"Reviewed {reviewed_behavior}{location}{suffix}."
            )

    ordered_paths = sorted(changed, key=path_priority)
    for path in ordered_paths:
        add_summary(path, fallback=False)
    minimum = min(2, len(changed))
    if allow_role_fallback or len(what_changed) < minimum:
        for path in ordered_paths:
            if path not in summarized_paths and len(what_changed) < max(minimum, 5 if allow_role_fallback else minimum):
                add_summary(path, fallback=True)
    return tuple(what_changed), tuple(ai_reviewed)


def _orientation_topics(values: Iterable[object]) -> tuple[ReviewOrientationTopic, ...]:
    selected: set[ReviewOrientationTopic] = set()
    for value in values:
        normalized = str(value or "").strip().casefold().replace("-", "_")
        if not normalized:
            continue
        try:
            selected.add(ReviewOrientationTopic(normalized))
        except ValueError:
            pass
        selected.update(_SIGNAL_ORIENTATION_TOPICS.get(normalized, ()))
    return tuple(topic for topic in ReviewOrientationTopic if topic in selected)


class _FrozenArtifactDict(dict):
    """JSON-serializable mapping with an immutable public mutation surface."""

    def _immutable(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("review result artifact is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze_result_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenArtifactDict({
            str(key): _freeze_result_value(item) for key, item in value.items()
        })
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_result_value(item) for item in value)
    return value


def _mask_runtime_text(value: str) -> str:
    return mask_runtime_text(value)


_PHASES = (
    "precheck", "planning", "initial", "followup", "finalization",
    "publish_ready", "complete",
)
_LEGAL_TRANSITIONS = {
    None: "precheck",
    "precheck": "planning",
    "planning": "initial",
    "initial": "followup",
    "followup": "finalization",
    "finalization": "publish_ready",
    "publish_ready": "complete",
}


class ControllerPhase(str, Enum):
    PRECHECK = "precheck"
    PLANNING = "planning"
    INITIAL = "initial"
    FOLLOWUP = "followup"
    FINALIZATION = "finalization"
    PUBLISH_READY = "publish_ready"
    COMPLETE = "complete"


@dataclass
class PlannerRequestBudget:
    """Controller-owned cap for all provider requests in one planning attempt."""

    remaining: int = 4

    def consume(self) -> None:
        if self.remaining <= 0:
            raise ValueError("planner model-call budget exhausted")
        self.remaining -= 1


@dataclass(frozen=True)
class RoleRequest:
    """Controller-authoritative, lease-bearing input for one role request."""

    role: str
    request_id: str
    phase: RunPhase
    lease: SessionLease
    timeout_sec: float
    max_tokens: int
    context: Mapping[str, object]
    planner_request_budget: PlannerRequestBudget | None = None

    def __post_init__(self) -> None:
        if not self.role.strip() or not self.request_id.strip():
            raise ValueError("role and request_id must be non-empty")
        if self.timeout_sec <= 0 or self.max_tokens <= 0:
            raise ValueError("role timeout and token budget must be positive")
        if self.lease.phase is not self.phase:
            raise ValueError("role request phase must match its lease")

    @property
    def remaining_tokens(self) -> int:
        return self.max_tokens


class RoleAdapter(Protocol):
    def complete(self, request: RoleRequest) -> object: ...


@dataclass(frozen=True)
class FinalizerProposal:
    """Advisory orientation selections; never controller facts or policy."""

    change_topics: tuple[ReviewOrientationTopic, ...] = ()
    component_ids: tuple[str, ...] = ()
    specialist_topics: tuple[ReviewOrientationTopic, ...] = ()
    recipe_ids: tuple[str, ...] = ()
    coverage_boundary_topics: tuple[ReviewOrientationTopic, ...] = ()
    review_emphasis_topics: tuple[ReviewOrientationTopic, ...] = ()
    recommendation: str | None = None
    what_changed: tuple[str, ...] = ()
    ai_reviewed: tuple[str, ...] = ()


@dataclass(frozen=True)
class HandoffSummaryProposal:
    """Bounded reviewer-facing prose with explicit controller-owned references."""

    ai_reviewed_summary: str
    human_focus: str
    referenced_paths: tuple[str, ...] = ()
    referenced_component_ids: tuple[str, ...] = ()
    referenced_obligation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceSeed:
    """Explicit repository/head binding for intentionally reused evidence."""

    repository: str
    head_sha: str
    snapshot: EvidenceSnapshot

    def __post_init__(self) -> None:
        if not self.repository.strip() or not self.head_sha.strip():
            raise ValueError("evidence seed repository and head SHA are required")


@dataclass(frozen=True)
class _PrimitiveRunIdentity:
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    artifact_id: str
    policy_version: int


def _primitive_run_identity(inputs: ReviewInputs) -> _PrimitiveRunIdentity:
    def text_value(value: object, fallback: str) -> str:
        try:
            return mask_runtime_text(value, limit=1000) or fallback
        except BaseException:
            return fallback

    try:
        pr_number = int(inputs.pr_number)
    except BaseException:
        pr_number = 0
    try:
        policy_version = int(inputs.policy.version)
    except BaseException:
        policy_version = 0
    values = {
        "repository": text_value(inputs.repository, "unknown/unknown"),
        "pr_number": pr_number,
        "base_sha": text_value(inputs.base_sha, "unknown"),
        "head_sha": text_value(inputs.head_sha, "unknown"),
        "policy_version": policy_version,
        "schema_version": _SCHEMA_VERSION,
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return _PrimitiveRunIdentity(
        repository=values["repository"],
        pr_number=pr_number,
        base_sha=values["base_sha"],
        head_sha=values["head_sha"],
        artifact_id=hashlib.sha256(encoded).hexdigest()[:32],
        policy_version=policy_version,
    )


def _finalizer_proposal(value: object) -> FinalizerProposal:
    if isinstance(value, FinalizerProposal):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("finalizer must return FinalizerProposal")

    def topics(key: str) -> tuple[ReviewOrientationTopic, ...]:
        raw = value.get(key, ())
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
            return ()
        selected: list[ReviewOrientationTopic] = []
        for item in raw:
            try:
                topic = (
                    item
                    if isinstance(item, ReviewOrientationTopic)
                    else ReviewOrientationTopic(str(item).strip().casefold())
                )
            except ValueError:
                continue
            if topic not in selected:
                selected.append(topic)
        return tuple(selected)

    def identifiers(key: str) -> tuple[str, ...]:
        raw = value.get(key, ())
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
            raise TypeError(f"finalizer {key} must be an array")
        return tuple(str(item).strip() for item in raw if str(item).strip())

    recommendation = value.get("recommendation")
    if recommendation is not None:
        recommendation = str(recommendation).strip() or None
    return FinalizerProposal(
        change_topics=topics("change_topics"),
        component_ids=identifiers("component_ids"),
        specialist_topics=topics("specialist_topics"),
        recipe_ids=identifiers("recipe_ids"),
        coverage_boundary_topics=topics("coverage_boundary_topics"),
        review_emphasis_topics=topics("review_emphasis_topics"),
        recommendation=recommendation,
        what_changed=identifiers("what_changed"),
        ai_reviewed=identifiers("ai_reviewed"),
    )


def _handoff_summary_proposal(value: object) -> HandoffSummaryProposal:
    if isinstance(value, HandoffSummaryProposal):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("handoff summarizer must return an object")
    expected = {
        "ai_reviewed_summary", "human_focus", "referenced_paths",
        "referenced_component_ids", "referenced_obligation_ids",
    }
    if set(value) != expected:
        raise ValueError("handoff summarizer fields are invalid")

    def text(key: str) -> str:
        item = " ".join(str(value.get(key) or "").split())
        if not item or len(item) > 600:
            raise ValueError(f"handoff summarizer {key} is invalid")
        # Keep each section a summary rather than a hidden finding list.
        sentence_ends = list(re.finditer(r"[.!?](?:\s|$)", item))
        if len(sentence_ends) > 1:
            # Preserve the useful first sentence instead of discarding the
            # complete handoff when a compatible model appends explanation.
            item = item[:sentence_ends[0].end()].strip()
        elif not sentence_ends:
            item += "."
        return item

    def identifiers(key: str) -> tuple[str, ...]:
        raw = value.get(key)
        if isinstance(raw, (str, bytes)) or not isinstance(raw, (tuple, list)):
            raise TypeError(f"handoff summarizer {key} must be an array")
        selected = tuple(dict.fromkeys(
            str(item).strip() for item in raw if str(item).strip()
        ))
        # Reference lists are orientation metadata, not an authorization
        # surface. Keep the bounded prefix when a model over-produces them;
        # rejecting the whole handoff loses otherwise valid prose.
        return selected[:12]

    return HandoffSummaryProposal(
        ai_reviewed_summary=text("ai_reviewed_summary"),
        human_focus=text("human_focus"),
        referenced_paths=identifiers("referenced_paths"),
        referenced_component_ids=identifiers("referenced_component_ids"),
        referenced_obligation_ids=identifiers("referenced_obligation_ids"),
    )


_VALID_CRITIC_ACTIONS = frozenset({
    "keep", "reject", "merge", "request_verification", "downgrade_unknown",
})


def _critic_response_diagnostics(value: object) -> dict[str, object]:
    """Return bounded attachment-only diagnostics for a critic response.

    The model response itself can contain evidence text and is deliberately
    not copied into the workflow log.  These shape diagnostics are enough to
    explain a normalization or validation decision in the report artifact.
    """
    top_level_keys = (
        tuple(sorted(str(key) for key in value))
        if isinstance(value, Mapping) else ()
    )
    rows: object = (
        value.get("actions", value.get("decisions"))
        if isinstance(value, Mapping) else value
    )
    extra_fields: set[str] = set()
    action_count = 0
    if isinstance(rows, Iterable) and not isinstance(rows, (str, bytes, Mapping)):
        for row in rows:
            if isinstance(row, Mapping):
                action_count += 1
                extra_fields.update(
                    str(key) for key in row
                    if key not in {"candidate_id", "action", "target_id"}
                )
    return {
        "response_digest": _digest(value),
        "top_level_keys": top_level_keys,
        "action_count": action_count,
        "ignored_fields": tuple(sorted(extra_fields)),
    }


def _critic_rows(value: object) -> tuple[Mapping[str, object], ...]:
    """Extract mapping-shaped critic rows without trusting their contents."""
    rows: object = value
    if isinstance(value, Mapping):
        rows = value.get("actions", value.get("decisions"))
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Iterable):
        return ()
    return tuple(item for item in rows if isinstance(item, Mapping))


def _partial_critic_result(
    value: object,
    candidates: Iterable[CandidateFinding],
) -> tuple[tuple[Mapping[str, str], ...], tuple[str, ...]]:
    """Keep valid rows and identify candidate IDs needing a bounded repair."""
    allowed = {item.candidate_id for item in candidates}
    seen: set[str] = set()
    rows: list[Mapping[str, str]] = []
    for row in _critic_rows(value):
        candidate_id = str(row.get("candidate_id") or "").strip()
        action = str(row.get("action") or "").strip().lower()
        target_id = str(row.get("target_id") or "").strip()
        if candidate_id not in allowed or candidate_id in seen:
            continue
        if action not in _VALID_CRITIC_ACTIONS:
            continue
        if action == "merge":
            if target_id not in allowed or target_id == candidate_id:
                continue
        elif target_id:
            continue
        seen.add(candidate_id)
        rows.append({
            "candidate_id": candidate_id,
            "action": action,
            "target_id": target_id,
        })
    return tuple(rows), tuple(sorted(allowed - seen))


def _coverage_verification_requests(
    obligations: Iterable[CoverageObligation],
    blocking_obligation_ids: Iterable[str],
) -> tuple[Mapping[str, object], ...]:
    """Create explicit human checks for policy-blocking coverage gaps."""
    obligation_map = {item.id: item for item in obligations}
    unresolved: list[tuple[CoverageObligation, str, str]] = []
    for obligation_id in sorted({str(item).strip() for item in blocking_obligation_ids}):
        obligation = obligation_map.get(obligation_id)
        if obligation is None:
            continue
        subject = " ".join(str(obligation.subject).split()) or obligation.id
        reason = " ".join(str(obligation.explanation).split()) or (
            "The specialist review did not retain enough evidence to resolve "
            "this mandatory high-risk obligation."
        )
        unresolved.append((obligation, subject, reason))
    if not unresolved:
        return ()
    subjects = "; ".join(item[1] for item in unresolved[:6])
    reasons = " ".join(
        f"{subject}: {reason}"
        for _obligation, subject, reason in unresolved[:4]
    )
    if len(unresolved) > 4:
        reasons += f" (+{len(unresolved) - 4} additional coverage gaps.)"
    return ({
        "question": (
            "Can a human recheck the unresolved high-risk coverage areas "
            f"before merging? Focus areas: {subjects}."
        ),
        "reason": reasons,
        "related_obligation_ids": tuple(item[0].id for item in unresolved),
    },)


def _deterministic_handoff_focus(
    obligations: Iterable[CoverageObligation],
    blocking_obligation_ids: Iterable[str],
    degraded: bool,
) -> tuple[str, ...]:
    del degraded
    obligation_map = {item.id: item for item in obligations}
    subjects = tuple(dict.fromkeys(
        " ".join(str(obligation_map[item].subject).split())
        for item in sorted({str(value).strip() for value in blocking_obligation_ids})
        if item in obligation_map and str(obligation_map[item].subject).strip()
    ))
    if subjects:
        return (
            "Recheck the unresolved high-risk coverage questions in the handoff, "
            "especially " + "; ".join(subjects[:3]) + ".",
        )
    # Without a concrete unresolved obligation, keep the legacy orientation
    # labels; there is no specific human action to summarize here.
    return ()


def _deterministic_reviewed_summary(
    *,
    changed_files: Iterable[str],
    component_ids: Iterable[str],
    reviewed_obligations: Iterable[CoverageObligation],
) -> tuple[str, ...]:
    del changed_files
    if not tuple(reviewed_obligations):
        return ()
    components = tuple(dict.fromkeys(
        " ".join(str(item).split())
        for item in component_ids
        if str(item).strip()
    ))
    scope = " and ".join(components[:4]) or "the affected components"
    return (
        "The AI checked retained evidence across " + scope + " for the assigned "
        "review obligations; unresolved areas are listed in the detail notes.",
    )


def _deterministic_handoff_change_summary(
    change_overview: Mapping[str, object],
) -> tuple[str, ...]:
    """Render bounded behavioral prose when a run cannot trust model prose."""
    overview = " ".join(str(change_overview.get("overview") or "").split())
    if not overview:
        return ()
    result = [overview[:600]]
    key_changes = change_overview.get("key_changes", ())
    summaries: list[str] = []
    if isinstance(key_changes, (list, tuple)):
        for item in key_changes:
            if not isinstance(item, Mapping):
                continue
            summary = " ".join(str(item.get("summary") or "").split())
            if summary.casefold() in {"changes", "modifies"}:
                continue
            if summary and summary not in summaries:
                summaries.append(summary[:180])
            if len(summaries) == 3:
                break
    if summaries:
        result.append(
            "The main behavioral changes include "
            + "; ".join(summaries)
            + "."
        )
    effects = change_overview.get("cross_component_effects", ())
    effect_text: list[str] = []
    if isinstance(effects, (list, tuple)):
        for item in effects:
            if isinstance(item, Mapping):
                text = " ".join(str(item.get("summary") or "").split())
            else:
                text = " ".join(str(item).split())
            if text and text not in effect_text:
                effect_text.append(text[:180])
            if len(effect_text) == 2:
                break
    if effect_text and len(result) < 3:
        suffix = "" if effect_text[-1].endswith((".", "!", "?")) else "."
        result.append(
            "Cross-component effects to recheck include "
            + "; ".join(effect_text)
            + suffix
        )
    if len(result) == 1:
        result.append(
            "The bounded summary focuses on the behavioral areas represented by "
            "the retained change facts."
        )
    return tuple(result[:3])


def _validated_critic_result(
    value: object,
    candidates: Iterable[CandidateFinding],
) -> object:
    candidate_ids = tuple(item.candidate_id for item in candidates)
    allowed_ids = set(candidate_ids)
    rows: object = value
    if isinstance(value, Mapping):
        rows = value.get("actions", value.get("decisions"))
    if (
        isinstance(rows, (str, bytes))
        or not isinstance(rows, Iterable)
    ):
        raise ValueError("critic actions must be an array")
    admitted: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("critic actions must contain objects")
        candidate_id = str(row.get("candidate_id") or "").strip()
        action = str(row.get("action") or "").strip().lower()
        target_id = str(row.get("target_id") or "").strip()
        if candidate_id not in allowed_ids or candidate_id in seen:
            raise ValueError("critic candidate coverage is invalid")
        if action not in _VALID_CRITIC_ACTIONS:
            raise ValueError("critic action is invalid")
        if action == "merge":
            if target_id not in allowed_ids or target_id == candidate_id:
                raise ValueError("critic merge target is invalid")
        elif target_id:
            raise ValueError("critic target_id is only valid for merge")
        seen.add(candidate_id)
        # Models commonly add rationale/evidence fields despite the compact
        # decision schema.  Those fields cannot change adjudication and are
        # intentionally ignored; only the validated decision is admitted.
        admitted.append({
            "candidate_id": candidate_id,
            "action": action,
            "target_id": target_id,
        })
    if seen != allowed_ids:
        raise ValueError("critic omitted candidate decisions")
    return {"actions": admitted}


def _json_object(text: str) -> Mapping[str, object]:
    """Extract one unambiguous JSON object from a structured-role response.

    Compatible endpoints do not consistently honor response-format hints and
    may wrap an otherwise valid object in a fence or append a short
    explanation.  Scan balanced top-level object spans while respecting JSON
    strings, but fail closed if the response contains zero or multiple valid
    objects.
    """
    source = str(text or "")
    objects: list[Mapping[str, object]] = []
    start: int | None = None
    depth = 0
    array_depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(source):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if start is not None:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(source[start:index + 1])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                    else:
                        if isinstance(value, Mapping):
                            objects.append(value)
                    start = None
            continue
        if character == "[":
            array_depth += 1
        elif character == "]":
            array_depth = max(0, array_depth - 1)
        elif character == "{" and array_depth == 0:
            start = index
            depth = 1
    if len(objects) != 1:
        raise ValueError("role model response must contain exactly one JSON object")
    return objects[0]


@dataclass(frozen=True)
class GatewayRoleAdapter:
    """Force a controller role through Task 7's leased gateway contract."""

    gateway: ModelGateway
    system_prompt: str = "Return only the requested structured JSON object."
    response_format_override: str | None = None
    attempt_logger: Callable[[str], None] | None = None

    def complete(self, request: RoleRequest) -> object:
        return self._complete_recoverable_structured_role(request)

    def _complete_recoverable_structured_role(
        self,
        request: RoleRequest,
        *,
        max_attempts: int = 2,
        before_attempt: Callable[[int], None] | None = None,
        force_final: Callable[[int], bool] | None = None,
    ) -> object:
        """Continue one bounded structured role without losing private state."""
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        conversation = Conversation(system=self.system_prompt)
        conversation.add_user(json.dumps(
            _json_value(request.context),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ))
        for attempt in range(max_attempts):
            finalization = (
                force_final(attempt)
                if force_final is not None
                else attempt == max_attempts - 1
            )
            if self.attempt_logger is not None:
                self.attempt_logger(
                    f"role {request.role} attempt {attempt + 1}/{max_attempts} "
                    f"mode={'strict-json' if finalization else 'reasoning'}"
                )
            if before_attempt is not None:
                before_attempt(attempt)
            result = self.gateway.complete(ModelTurnRequest(
                role=request.role,
                conversation=conversation,
                max_tokens=request.max_tokens,
                response_schema=None,
                tools_enabled=False,
                timeout_sec=request.timeout_sec,
                deadline_at=request.lease.deadline_at,
                stream=False,
                response_schema_name=f"specialist_{request.role}",
                response_format_override=self.response_format_override,
                reasoning_effort="none" if finalization else None,
                ephemeral_user_note=(
                    "Return only the required JSON object."
                    if finalization else None
                ),
            ))
            content = result.content
            if not content and result.text_source == "content":
                # Compatibility for test/provider adapters built before
                # ModelTurnResult gained explicit ordinary-content state.
                content = result.text
            try:
                return _json_object(content)
            except (TypeError, ValueError) as exc:
                # Provider finish reasons are not trustworthy enough to decide
                # whether malformed structured output deserves its one bounded
                # repair.  In particular, local endpoints can report ``stop``
                # after filling the output budget with reasoning and cutting the
                # JSON content mid-object.
                continuation_allowed = attempt < max_attempts - 1
                if self.attempt_logger is not None:
                    status = (
                        "continuation scheduled"
                        if continuation_allowed
                        and attempt < max_attempts - 1
                        else "no continuation available"
                    )
                    self.attempt_logger(
                        f"role {request.role} structured response invalid; "
                        f"attempt={attempt + 1}/{max_attempts}; "
                        f"finish_reason={result.finish_reason or 'unknown'}; "
                        f"content_chars={len(content)}; "
                        f"reasoning_chars={len(result.reasoning)}; "
                        f"parse_error={type(exc).__name__}: {str(exc)[:160]}; "
                        f"{status}"
                    )
                if (
                    attempt == max_attempts - 1
                    or not continuation_allowed
                ):
                    raise
                conversation.add_assistant_turn(
                    reasoning=result.reasoning,
                    content=content,
                    calls=result.tool_calls,
                )
        raise AssertionError("structured-role continuation loop exhausted")


@dataclass(frozen=True)
class ReviewInputs:
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    topology: Mapping[str, Any]
    classification: Mapping[str, Any]
    policy: ReviewPolicy
    config: RuntimeConfig
    changed_files: tuple[str, ...]
    tracked_paths: tuple[str, ...] = ()
    artifact_path: Path | str = Path("specialist-review-artifact.json")
    allow_approve: bool = False
    publishing_mode: str = "review_comment"
    model_verdict: str = "approve"
    candidate_findings: tuple[CandidateFinding, ...] = ()
    source_access_requests: tuple[SourceAccessRequest, ...] = ()
    verification_requests: tuple[Mapping[str, Any], ...] = ()
    pr_metadata: Mapping[str, Any] = field(default_factory=dict)
    configuration_warnings: tuple[str, ...] = ()
    adapter_configuration: Mapping[str, Any] = field(default_factory=dict)


def _change_components(
    inputs: ReviewInputs,
) -> tuple[set[str], dict[str, str]]:
    component_ids: set[str] = set()
    path_components: dict[str, str] = {}
    explicit = inputs.topology.get("path_components", {})
    if isinstance(explicit, Mapping):
        for path, component in explicit.items():
            clean_path = str(path).replace("\\", "/").strip("/")
            clean_component = str(component).strip()
            if clean_path and clean_component:
                path_components[clean_path] = clean_component
                component_ids.add(clean_component)
    for item in inputs.topology.get("components", ()):
        if not isinstance(item, Mapping):
            continue
        component = str(item.get("id") or "").strip()
        if not component:
            continue
        component_ids.add(component)
        for path in item.get("changed_files", ()):
            clean_path = str(path).replace("\\", "/").strip("/")
            if clean_path:
                path_components.setdefault(clean_path, component)
    return component_ids, path_components


def _overview_text(value: object, label: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"change overview {label} must be non-empty text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ValueError(f"change overview {label} exceeds its bound")
    if _PROHIBITED_OVERVIEW_CLAIM.search(text):
        raise ValueError(
            f"change overview {label} claims verdict, findings, or coverage"
        )
    if label == "overview" and len(re.findall(r"[.!?](?:\s|$)", text)) != 1:
        raise ValueError("change overview must be exactly one sentence")
    return text


def _domain_name(value: str) -> bool:
    labels = value.rstrip(".").split(".")
    if len(labels) < 2 or not 2 <= len(labels[-1]) <= 63:
        return False
    if not labels[-1].isalpha():
        return False
    label_pattern = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    return all(bool(re.fullmatch(label_pattern, label)) for label in labels)


def _url_reference_spans(value: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for match in _URL_CANDIDATE.finditer(value):
        candidate = match.group(0).rstrip(".,;:!?)]}")
        parsed = urlsplit(
            candidate if "://" in candidate else f"https://{candidate}",
        )
        hostname = parsed.hostname or ""
        if hostname == "localhost" or _domain_name(hostname):
            spans.append((match.start(), match.start() + len(candidate)))
    return tuple(spans)


def _prose_path_references(
    value: str,
    tracked_paths: Iterable[str] = (),
) -> tuple[str, ...]:
    normalized = value.replace("\\", "/")
    references: list[str] = []
    occupied: list[tuple[int, int]] = []
    url_spans = _url_reference_spans(normalized)

    def is_url_span(start: int) -> bool:
        return any(span_start <= start < span_end for span_start, span_end in url_spans)

    tracked = {
        str(path).replace("\\", "/").strip("/")
        for path in tracked_paths
        if str(path).strip()
    }
    for match in _PROSE_TOKEN.finditer(normalized):
        if is_url_span(match.start()):
            continue
        reference = match.group(0).rstrip(".,;:!?)]}")
        if reference in tracked:
            if reference not in references:
                references.append(reference)
            occupied.append(match.span())
    for match in _PATH_WITH_DIRECTORY.finditer(normalized):
        if is_url_span(match.start()):
            continue
        reference = match.group(0).rstrip(".,;:!?)]}")
        if reference and reference not in references:
            references.append(reference)
        occupied.append(match.span())
    for match in _ROOT_FILE_REFERENCE.finditer(normalized):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        if is_url_span(match.start()):
            continue
        reference = match.group(0).rstrip(".,;:!?)]}")
        if reference.lower().startswith("www."):
            continue
        if reference and reference not in references:
            references.append(reference)
    return tuple(references)


def _normalize_repository_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _path_values(value: object) -> tuple[str, ...]:
    """Extract path-shaped values from a controller-owned context field."""
    if isinstance(value, str):
        path = _normalize_repository_path(value)
        return (path,) if path else ()
    if isinstance(value, Mapping):
        paths: list[str] = []
        for item in value.values():
            paths.extend(_path_values(item))
        return tuple(dict.fromkeys(paths))
    if isinstance(value, (tuple, list, set, frozenset)):
        paths: list[str] = []
        for item in value:
            paths.extend(_path_values(item))
        return tuple(dict.fromkeys(paths))
    return ()


def _deterministic_context_paths(
    topology: Mapping[str, Any],
    tracked_paths: Iterable[str],
    *,
    extra_paths: Iterable[str] = (),
) -> frozenset[str]:
    """Return unchanged paths explicitly supplied as deterministic context.

    This intentionally does not treat every tracked path as context.  Only
    controller-owned context fields and path-bearing topology relationships are
    admitted, and the result is bounded to the repository paths known to the
    controller.
    """
    tracked = {
        _normalize_repository_path(path)
        for path in tracked_paths
        if _normalize_repository_path(path)
    }
    candidates: list[str] = []
    for key in _DETERMINISTIC_CONTEXT_KEYS:
        candidates.extend(_path_values(topology.get(key)))
    for relationship in topology.get("relationships", ()):
        if not isinstance(relationship, Mapping):
            continue
        for key in _RELATION_PATH_KEYS:
            candidates.extend(_path_values(relationship.get(key)))
    for component in topology.get("components", ()):
        if not isinstance(component, Mapping):
            continue
        for key in _DETERMINISTIC_CONTEXT_KEYS | {"paths", "files"}:
            candidates.extend(_path_values(component.get(key)))
    for artifact in topology.get("generated_artifacts", ()):
        if not isinstance(artifact, Mapping):
            continue
        for key in ("source_of_truth", "generator_config", "output_paths"):
            candidates.extend(_path_values(artifact.get(key)))
    candidates.extend(_normalize_repository_path(path) for path in extra_paths)
    # Context must still refer to a known repository path.  This keeps an
    # arbitrary model-invented path from becoming authoritative merely because
    # it appeared in a topology-shaped object.
    return frozenset(path for path in candidates if path and path in tracked)


def _direct_change_references(
    value: str,
    references: Iterable[str],
) -> frozenset[str]:
    """Find references used as direct change claims, not contextual effects."""
    normalized = value.replace("\\", "/")
    direct: set[str] = set()
    for reference in references:
        path = _normalize_repository_path(reference)
        if not path:
            continue
        start = 0
        while True:
            index = normalized.find(path, start)
            if index < 0:
                break
            before = normalized[max(0, index - 40):index]
            after = normalized[index + len(path):index + len(path) + 64]
            before = before.rstrip(" `'\"(")
            after = after.lstrip(" `'\")(")
            before_claim = re.search(
                r"(?:^|[\s,;])(?:"
                r"(?:the\s+)?(?:add|change|modif(?:y|ies|ied)|update|"
                r"remov(?:e|es|ed)|introduc(?:e|es|ed)|"
                r"rewrit(?:e|es|ten)|refactor(?:s|ed|ing)?|renam(?:e|es|ed))"
                r"(?:s|d)?\s+(?:in|to|of)"
                r"|add(?:s|ed)?\s+(?:behavior|changes?)\s+(?:in|to|of)"
                r"|(?:the\s+)?(?:add|change|modif(?:y|ies|ied)|update|"
                r"remov(?:e|es|ed)|introduc(?:e|es|ed)|"
                r"rewrit(?:e|es|ten)|refactor(?:s|ed|ing)?|renam(?:e|es|ed))"
                r"(?:s|d)?"
                r")(?:\s+(?:the|its|a|an|new|updated|changed))?$",
                before,
                re.IGNORECASE,
            )
            after_claim = re.match(
                r"(?:(?:the\s+)?file\s+)?"
                r"(?:is|are|was|were|has been|have been)?\s*"
                r"(?:add(?:s|ed)?|change(?:s|d)?|modif(?:y|ies|ied)|"
                r"update(?:s|d)?|remov(?:e|es|ed)|introduc(?:e|es|ed)|"
                r"rewrit(?:e|es|ten)|refactor(?:s|ed)?|renam(?:e|es|ed))\b",
                after,
                re.IGNORECASE,
            )
            if before_claim or after_claim:
                direct.add(path)
                break
            start = index + len(path)
    return frozenset(direct)


def _authoritative_change_facts(
    topology: Mapping[str, Any],
) -> tuple[Mapping[str, object], Mapping[str, object] | None]:
    value = topology.get("change_facts")
    if isinstance(value, Mapping) and "facts" in value:
        facts = value.get("facts")
        return (
            facts if isinstance(facts, Mapping) else {},
            value,
        )
    if isinstance(value, Mapping):
        return value, None
    compatibility = topology.get("changed_contract_facts", {})
    return (
        compatibility if isinstance(compatibility, Mapping) else {},
        None,
    )


def _validated_change_overview(
    value: object,
    inputs: ReviewInputs,
) -> dict[str, object]:
    """Admit only bounded orientation backed by changed paths/components."""
    if not isinstance(value, Mapping):
        raise ValueError("change overview must be an object")
    fields = {
        "overview", "key_changes", "cross_component_effects", "uncertainties",
    }
    if not set(value).issubset(fields) or "overview" not in value:
        raise ValueError("change overview fields are invalid")
    changed_paths = {
        str(path).replace("\\", "/").strip("/")
        for path in inputs.changed_files
        if str(path).strip()
    }
    tracked_paths = {
        _normalize_repository_path(path)
        for path in (*inputs.tracked_paths, *inputs.changed_files)
        if _normalize_repository_path(path)
    }
    context_paths = _deterministic_context_paths(
        inputs.topology,
        tracked_paths,
    )
    component_ids, path_components = _change_components(inputs)

    def admitted_text(raw: object, label: str, *, limit: int) -> str:
        text = _overview_text(raw, label, limit=limit)
        prose_paths = _prose_path_references(text, tracked_paths)
        for reference in prose_paths:
            path = _normalize_repository_path(reference)
            if path not in changed_paths and path not in context_paths:
                raise ValueError(
                    "change overview contains an unchanged path reference"
                )
        direct_context_paths = _direct_change_references(text, context_paths)
        if direct_context_paths:
            raise ValueError("change overview contains an unchanged path reference")
        return text

    overview = admitted_text(value.get("overview"), "overview", limit=1000)

    raw_changes = value.get("key_changes", ())
    if (
        isinstance(raw_changes, (str, bytes))
        or not isinstance(raw_changes, (tuple, list))
        or len(raw_changes) > _MAX_CHANGE_OVERVIEW_ITEMS
    ):
        raise ValueError("change overview key_changes must be a bounded array")
    key_changes: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for row in raw_changes:
        if not isinstance(row, Mapping) or set(row) != {
            "path", "component", "summary",
        }:
            raise ValueError("change overview key change fields are invalid")
        path = str(row.get("path") or "").replace("\\", "/").strip("/")
        component = str(row.get("component") or "").strip()
        if path not in changed_paths or path in seen_paths:
            raise ValueError("change overview contains unchanged or duplicate path")
        if component not in component_ids:
            raise ValueError("change overview contains unknown component")
        expected_component = path_components.get(path)
        if expected_component and component != expected_component:
            raise ValueError("change overview path/component binding is invalid")
        summary = admitted_text(
            row.get("summary"), f"summary for {path}", limit=500,
        )
        key_changes.append({
            "path": path,
            "component": component,
            "summary": summary,
        })
        seen_paths.add(path)

    raw_effects = value.get("cross_component_effects", ())
    if (
        isinstance(raw_effects, (str, bytes))
        or not isinstance(raw_effects, (tuple, list))
        or len(raw_effects) > 8
    ):
        raise ValueError(
            "change overview cross_component_effects must be a bounded array"
        )
    effects: list[dict[str, object]] = []
    for row in raw_effects:
        if not isinstance(row, Mapping) or set(row) != {"components", "summary"}:
            raise ValueError("change overview cross-component fields are invalid")
        components = row.get("components")
        if (
            isinstance(components, (str, bytes))
            or not isinstance(components, (tuple, list))
        ):
            raise ValueError("change overview components must be an array")
        selected = tuple(dict.fromkeys(
            str(component).strip() for component in components
            if str(component).strip()
        ))
        if len(selected) < 2 or len(selected) > 6:
            raise ValueError("change overview cross-component effect is invalid")
        if any(component not in component_ids for component in selected):
            raise ValueError("change overview contains unknown component")
        effects.append({
            "components": selected,
            "summary": admitted_text(
                row.get("summary"), "cross-component summary", limit=500,
            ),
        })

    raw_uncertainties = value.get("uncertainties", ())
    if (
        isinstance(raw_uncertainties, (str, bytes))
        or not isinstance(raw_uncertainties, (tuple, list))
        or len(raw_uncertainties) > 8
    ):
        raise ValueError("change overview uncertainties must be a bounded array")
    uncertainties = tuple(
        admitted_text(item, "uncertainty", limit=400)
        for item in raw_uncertainties
    )
    return {
        "overview": overview,
        "key_changes": tuple(key_changes),
        "cross_component_effects": tuple(effects),
        "uncertainties": uncertainties,
    }


def _deterministic_change_overview(inputs: ReviewInputs) -> dict[str, object]:
    facts, metadata = _authoritative_change_facts(inputs.topology)
    component_ids, path_components = _change_components(inputs)
    fallback_component = sorted(component_ids)[0] if len(component_ids) == 1 else ""
    changes: list[dict[str, str]] = []
    for path in inputs.changed_files[:_MAX_CHANGE_OVERVIEW_ITEMS]:
        fact_value = facts.get(path, {})
        fact = fact_value if isinstance(fact_value, Mapping) else {}
        signals: list[str] = []
        for field_name, label in (
            ("symbols", "symbols"),
            ("action_inputs", "action inputs"),
            ("workflow_steps", "workflow steps"),
            ("workflow_keys", "workflow keys"),
            ("headings", "headings"),
            ("change_excerpts", "documentation excerpts"),
            ("hunk_summaries", "hunks"),
        ):
            values = fact.get(field_name, ())
            if (
                isinstance(values, (tuple, list))
                and values
                and len(signals) < 3
            ):
                selected = ", ".join(
                    " ".join(str(item).split())[:100]
                    for item in values[:3]
                    if str(item).strip()
                )
                if selected:
                    signals.append(f"{label}: {selected}")
        change_type = str(fact.get("change_type") or "changes").strip()
        summary = change_type.capitalize()
        if signals:
            summary += " " + "; ".join(signals)
        component = path_components.get(path, fallback_component)
        if not component:
            component = "repository"
            component_ids.add(component)
        changes.append({
            "path": path,
            "component": component,
            "summary": summary[:500],
        })
    omitted = max(0, len(inputs.changed_files) - len(changes))
    # A count is useful metadata but is not a human handoff. Build a bounded
    # behavioral sentence from the same immutable facts when the model
    # summarizer is unavailable or its proposal fails validation.
    changed_paths = tuple(str(path).replace("\\", "/") for path in inputs.changed_files)
    path_roles = {
        path: set(classify_file_roles(path)) for path in changed_paths
    }
    themes: list[str] = []

    def add_theme(value: str) -> None:
        if value not in themes:
            themes.append(value)

    if any(
        path == "action.yml" or ".github/workflows/" in path
        for path in changed_paths
    ):
        add_theme("workflow/action configuration")
    if any(
        "implementation" in path_roles[path] or path.startswith("pr_reviewer/")
        for path in changed_paths
    ):
        add_theme("review-runtime behavior")
    if any(
        "github_review_notes" in path or "publish" in path
        for path in changed_paths
    ):
        add_theme("publishing and handoff behavior")
    if any("test" in path_roles[path] or path.startswith("evals/") for path in changed_paths):
        add_theme("regression/evaluation coverage")
    if any("documentation" in path_roles[path] for path in changed_paths):
        add_theme("operator documentation")
    if not themes:
        themes.append("repository behavior")

    specifics: list[str] = []

    def add_specific(value: str) -> None:
        if value not in specifics:
            specifics.append(value)

    workflow_keys = {
        str(key).casefold()
        for fact in facts.values()
        if isinstance(fact, Mapping)
        for key in fact.get("workflow_keys", ())
        if isinstance(fact.get("workflow_keys", ()), (list, tuple))
    }
    if "specialist_max_tool_calls_per_session" in workflow_keys:
        add_specific("specialist tool-call budgeting")
    if any(
        path.endswith("conversation.py") or "reasoning" in path.casefold()
        for path in changed_paths
    ):
        add_specific("reasoning/session continuity")
    if any(
        path.endswith("controller.py") or path.endswith("session.py")
        for path in changed_paths
    ):
        add_specific("specialist-session recovery and candidate retention")
    if any(
        "github_review_notes" in path or "publish" in path
        for path in changed_paths
    ):
        add_specific("review handoff publishing")
    if any("test" in path_roles[path] or path.startswith("evals/") for path in changed_paths):
        add_specific("regression verification")
    if not specifics:
        specifics.append("the changed components and their integration contracts")

    component_text = ", ".join(sorted(component_ids)[:5]) or "the changed components"
    overview = (
        f"This change updates {component_text}; it covers "
        + ", ".join(themes[:5])
        + "; the main behavioral areas are "
        + ", ".join(specifics[:4])
        + "."
    )
    uncertainties: list[str] = []
    if omitted:
        uncertainties.append(
            f"{omitted} additional changed paths are omitted from this bounded overview."
        )
    if metadata is not None:
        fact_omitted = metadata.get("omitted_path_count", 0)
        if isinstance(fact_omitted, int) and fact_omitted > 0:
            uncertainties.append(
                f"Immutable semantic facts are unavailable for {fact_omitted} changed "
                f"{'path' if fact_omitted == 1 else 'paths'}."
            )
    return {
        "overview": overview,
        "key_changes": tuple(changes),
        "cross_component_effects": (),
        "uncertainties": tuple(uncertainties),
    }


@dataclass(frozen=True)
class ReviewResult:
    artifact: Mapping[str, Any]
    handoff: ReviewHandoff = field(default_factory=ReviewHandoff)
    notes: tuple[ReviewNote, ...] = ()
    verdict: str = "notice"
    verdict_source: str = "controller-degraded"
    events: tuple[RunEvent, ...] = ()
    artifact_path: Path | None = None
    artifact_write_error: str | None = None
    publishing_ready: bool = False


@dataclass
class _RunState:
    inputs: ReviewInputs
    journal: EventJournal
    deadline: RunDeadline
    evidence: EvidenceStore
    phase: str | None = None
    phase_outcomes: dict[str, str] = field(default_factory=dict)
    effective_allow_approve: bool = False
    effective_publishing_mode: str = "review_comment"
    required_note_count: int = 0
    obligations: tuple[CoverageObligation, ...] = ()
    coverage: CoverageLedger | None = None
    plan: AssignmentPlan = field(default_factory=lambda: AssignmentPlan(()))
    plan_source: str = "deterministic_fallback"
    planner_repaired: bool = False
    planner_diagnostics: list[str] = field(default_factory=list)
    change_overview: Mapping[str, object] = field(default_factory=dict)
    assignments: dict[str, Assignment] = field(default_factory=dict)
    ownership: dict[str, SessionOwnership] = field(default_factory=dict)
    sessions: dict[str, object] = field(default_factory=dict)
    assignment_sessions: dict[str, str] = field(default_factory=dict)
    session_generations: dict[str, int] = field(default_factory=dict)
    session_results: dict[tuple[str, str], object] = field(default_factory=dict)
    failed_session_budgets: dict[str, BudgetUsage] = field(default_factory=dict)
    quarantined_session_ids: set[str] = field(default_factory=set)
    admitted_specialist_request_events: set[tuple[str, str]] = field(default_factory=set)
    request_attempt_journal: RequestAttemptJournal | None = None
    request_attempts: dict[str, RequestAttempt] = field(default_factory=dict)
    request_attempt_ids_by_session: dict[str, set[str]] = field(default_factory=dict)
    candidates: dict[str, CandidateFinding] = field(default_factory=dict)
    candidate_occurrences: dict[str, CandidateFinding] = field(default_factory=dict)
    collision_dispositions: list[dict[str, object]] = field(default_factory=list)
    source_requests: list[SourceAccessRequest] = field(default_factory=list)
    retention_verification_requests: tuple[Mapping[str, object], ...] = ()
    coverage_verification_requests: tuple[Mapping[str, object], ...] = ()
    unknowns: list[dict[str, object]] = field(default_factory=list)
    degradations: list[dict[str, str]] = field(default_factory=list)
    review: AdjudicatedReview = field(default_factory=AdjudicatedReview)
    handoff: ReviewHandoff = field(default_factory=ReviewHandoff)
    notes: tuple[ReviewNote, ...] = ()
    verdict: str = "notice"
    verdict_source: str = "controller-degraded"
    blocking_finding_ids: tuple[str, ...] = ()
    blocking_obligation_ids: tuple[str, ...] = ()
    publishing_ready: bool = False


@dataclass
class _IsolatedSessionHandle:
    """Worker-owned mutable state admitted only after a completed result."""

    assignment: Assignment
    session: object
    session_id: str
    evidence: EvidenceStore
    coverage: CoverageLedger
    lease: SessionLease
    baseline_evidence_ids: frozenset[str] = frozenset()
    latest_result: object | None = None

    @property
    def candidate_findings(self) -> tuple[CandidateFinding, ...]:
        return tuple(
            item for item in getattr(self.session, "candidate_findings", ())
            if isinstance(item, CandidateFinding)
        )

    @property
    def budget(self) -> object:
        return getattr(self.session, "budget", None)

    @property
    def request_events(self) -> tuple[object, ...]:
        return tuple(getattr(self.session, "request_events", ()))

    @property
    def source_access_requests(self) -> tuple[SourceAccessRequest, ...]:
        return tuple(
            item for item in getattr(
                self.session, "source_access_requests", (),
            )
            if isinstance(item, SourceAccessRequest)
        )

    def explore(self) -> object:
        result = self.session.explore()
        self._validate_result(result)
        if result.state.value == "exploring":
            raise RuntimeError("specialist returned while still exploring")
        self._validate_owned_outputs(result)
        self.latest_result = result
        return result

    def apply_coverage_feedback(self, gaps: Iterable[str]) -> None:
        callback = getattr(self.session, "apply_coverage_feedback", None)
        if not callable(callback):
            raise TypeError("resumed session must support apply_coverage_feedback")
        callback(tuple(gaps))

    def recover(self, reason: str) -> object:
        callback = getattr(self.session, "recover", None)
        if not callable(callback):
            raise TypeError("failed session does not support reconstruction")
        result = callback(reason)
        self._validate_result(result)
        self.latest_result = result
        return result

    def update_lease(self, lease: SessionLease) -> None:
        callback = getattr(self.session, "update_lease", None)
        if not callable(callback):
            raise TypeError("resumed session must support update_lease")
        callback(lease)
        self.lease = lease

    def finalize(self) -> object:
        if self.latest_result is None or self.latest_result.state.value == "exploring":
            raise RuntimeError("an exploring or uncheckpointed session cannot be finalized")
        result = self.session.finalize()
        self._validate_result(result)
        self._validate_owned_outputs(result)
        self.latest_result = result
        return result

    def _validate_result(self, result: object) -> None:
        if getattr(result, "session_id", None) != self.session_id:
            raise ValueError("session identity differs from controller binding")
        checkpoint = getattr(result, "checkpoint", None)
        if checkpoint is None or checkpoint.session_id != self.session_id:
            raise ValueError("checkpoint identity differs from controller binding")

    def _validate_owned_outputs(self, result: object) -> None:
        retained = {record.id: record for record in self.evidence.snapshot().records}
        for evidence_id in getattr(result.checkpoint, "evidence_ids", ()):
            if evidence_id not in retained:
                raise ValueError("checkpoint references evidence outside its isolated store")
        for evidence_id, record in retained.items():
            if (
                evidence_id not in self.baseline_evidence_ids
                and record.collector_session_id != self.session_id
            ):
                raise ValueError("evidence collector identity differs from controller binding")
        for candidate in self.candidate_findings:
            if candidate.collector_session_id != self.session_id:
                raise ValueError("candidate collector identity differs from controller binding")


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else "[invalid-number]"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (set, frozenset)):
        value = tuple(sorted(value, key=str))
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(value)
        }
    return str(value)


def _artifact_value(value: object, *, depth: int = 0) -> object:
    """Return a bounded, secret-redacted, strict-JSON artifact value."""
    if depth > 12:
        return "[bounded]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else "[invalid-number]"
    if isinstance(value, str):
        try:
            return _mask_runtime_text(value)[:_MAX_ARTIFACT_STRING]
        except BaseException:
            return "[unserializable]"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key in sorted((str(item) for item in value))[:_MAX_ARTIFACT_ITEMS]:
            try:
                result[key] = _artifact_value(value[key], depth=depth + 1)
            except (KeyError, TypeError):
                # The mapping may not accept a normalized string key. Artifact
                # inputs are expected to use string keys, so omit hostile rows.
                continue
            except BaseException:
                result[key] = "[unserializable]"
        return result
    if isinstance(value, (set, frozenset)):
        value = tuple(sorted(value, key=str))
    if isinstance(value, (tuple, list)):
        return [
            _artifact_value(item, depth=depth + 1)
            for item in tuple(value)[:_MAX_ARTIFACT_ITEMS]
        ]
    if isinstance(value, Enum):
        return _artifact_value(value.value, depth=depth + 1)
    if is_dataclass(value):
        return _artifact_value({
            item.name: getattr(value, item.name) for item in fields(value)
        }, depth=depth + 1)
    try:
        return _mask_runtime_text(str(value))[:_MAX_ARTIFACT_STRING]
    except BaseException:
        return "[unserializable]"


def _checkpoint_projection(checkpoint: object) -> dict[str, object]:
    statuses = getattr(checkpoint, "obligation_statuses", ())
    return {
        "session_id": str(getattr(checkpoint, "session_id", "")),
        "state": getattr(getattr(checkpoint, "state", ""), "value", ""),
        "evidence_ids": list(getattr(checkpoint, "evidence_ids", ())),
        "imported_evidence_ids": list(getattr(checkpoint, "imported_evidence_ids", ())),
        "candidate_finding_ids": list(getattr(checkpoint, "candidate_finding_ids", ())),
        "obligation_statuses": [
            [str(obligation_id), getattr(status, "value", str(status))]
            for obligation_id, status in statuses
        ],
    }


def _evidence_projection(record: object) -> dict[str, object]:
    provenance = getattr(record, "provenance")
    return {
        "evidence_id": getattr(record, "id"),
        "category": getattr(record, "category"),
        "collector_session_id": getattr(record, "collector_session_id"),
        "model_identity": getattr(record, "model_identity"),
        "tool": getattr(record, "tool"),
        "source_identity": getattr(record, "source_identity"),
        "source_path": getattr(record, "source_path"),
        "provenance": {
            "head_sha": provenance.head_sha,
            "policy_hash": provenance.policy_hash,
            "policy_rule_id": provenance.policy_rule_id,
            "source_classification": provenance.source_classification,
            "original_url": provenance.original_url,
            "final_url": provenance.final_url,
            "max_age_hours": provenance.max_age_hours,
        },
        "status": getattr(record, "status"),
        "content_hash": getattr(record, "content_hash"),
        "mime_type": getattr(record, "mime_type"),
        "truncated": getattr(record, "truncated"),
        "redacted": getattr(record, "redacted"),
        "imported_by": list(getattr(record, "imported_by")),
        "supersedes": list(getattr(record, "supersedes")),
        "contradicts": list(getattr(record, "contradicts")),
    }


def _artifact_id(inputs: ReviewInputs) -> str:
    return _digest({
        "schema_version": _SCHEMA_VERSION,
        "repository": inputs.repository,
        "pr_number": inputs.pr_number,
        "base_sha": inputs.base_sha,
        "head_sha": inputs.head_sha,
        "policy_digest": _digest(inputs.policy),
        "config_digest": _digest(inputs.config),
    })[:32]


def _is_link_or_reparse(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _path_identity(path: Path) -> tuple[int, int]:
    result = path.stat(follow_symlinks=False)
    return int(result.st_dev), int(result.st_ino)


def _resolve_artifact_path(root_value: Path | str, requested_value: Path | str) -> Path:
    root = Path(root_value).absolute()
    requested = Path(requested_value)
    if (
        requested.is_absolute()
        or not requested.name
        or len(requested.parts) != 1
        or requested.name in {".", ".."}
    ):
        raise ValueError("artifact path must be one safe relative filename")
    if not root.exists() or not root.is_dir():
        raise ValueError("controller-owned artifact output root must already exist")
    if _is_link_or_reparse(root):
        raise ValueError("artifact output root must not be a link or reparse point")
    target = root / requested.name
    if _is_link_or_reparse(target):
        raise ValueError("artifact target must not be a link or reparse point")
    return target


def _digest(value: object) -> str:
    encoded = json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_error(exc: BaseException) -> str:
    return format_callback_error(exc)


def _budget_projection(value: BudgetUsage) -> dict[str, object]:
    return _json_value(value)  # type: ignore[return-value]


def _directory_fsync_status(directory_fd: int) -> str:
    try:
        os.fsync(directory_fd)
    except OSError:
        return "written_durability_warning"
    return "written"


def _atomic_write_json(
    path: Path,
    artifact: Mapping[str, object],
    *,
    directory_fd: int | None = None,
    root_identity: tuple[int, int] | None = None,
) -> str:
    """Write a private canonical JSON file without exposing a partial target."""
    payload = json.dumps(
        artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    if directory_fd is not None and os.name != "nt":
        if root_identity is None or _path_identity(path.parent) != root_identity:
            raise ValueError("artifact output root identity changed before create")
        temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if _path_identity(path.parent) != root_identity:
                raise ValueError("artifact output root identity changed before replace")
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            try:
                os.chmod(path.name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
            except (NotImplementedError, OSError, TypeError):
                pass
            return _directory_fsync_status(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
            raise

    if root_identity is not None and _path_identity(path.parent) != root_identity:
        raise ValueError("artifact output root identity changed before create")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if root_identity is not None and _path_identity(path.parent) != root_identity:
            raise ValueError("artifact output root identity changed before replace")
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        durability_warning = False
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    durability_warning = (
                        _directory_fsync_status(directory)
                        == "written_durability_warning"
                    )
                finally:
                    os.close(directory)
        return "written_durability_warning" if durability_warning else "written"
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class ReviewController:
    """Own one review run from precheck through terminal projection."""

    def __init__(
        self,
        *,
        change_summarizer: object | None = None,
        planner: object | None = None,
        planner_gateway: object | None = None,
        session_factory: Callable[..., object] | None = None,
        negotiator: object | None = None,
        critic: object | None = None,
        finalizer: object | None = None,
        evidence_store: EvidenceStore | None = None,
        evidence_seed: EvidenceSeed | None = None,
        evidence_store_factory: Callable[[], EvidenceStore] = EvidenceStore,
        artifact_output_root: Path | str = Path("."),
        event_sink: Callable[[RunEvent], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        artifact_writer: Callable[[Path, Mapping[str, object]], object] = _atomic_write_json,
        obligation_deriver: Callable[..., tuple[CoverageObligation, ...]] = derive_obligations,
        assignment_validator: Callable[..., AssignmentPlan] = validate_assignment_plan,
        scheduler_type: type[SessionScheduler] = SessionScheduler,
    ) -> None:
        if planner is not None and planner_gateway is not None:
            raise ValueError("provide planner or planner_gateway, not both")
        if evidence_store is not None and evidence_store.snapshot().records:
            raise ValueError(
                "non-empty evidence_store is unbound; provide an EvidenceSeed"
            )
        self.change_summarizer = change_summarizer
        self.planner = (
            planner if planner is not None
            else GatewayRoleAdapter(planner_gateway) if planner_gateway is not None
            else None
        )
        self.session_factory = session_factory
        self.negotiator = negotiator
        self.critic = critic
        self.finalizer = finalizer
        self._provided_evidence_store = evidence_store
        self._evidence_seed = evidence_seed
        self._evidence_store_factory = evidence_store_factory
        self.artifact_output_root = Path(artifact_output_root).absolute()
        if (
            not self.artifact_output_root.exists()
            or not self.artifact_output_root.is_dir()
            or _is_link_or_reparse(self.artifact_output_root)
        ):
            raise ValueError(
                "artifact_output_root must be an existing non-link directory"
            )
        self._artifact_root_identity = _path_identity(self.artifact_output_root)
        self._artifact_root_fd: int | None = None
        if os.name != "nt":
            flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
            flags |= int(getattr(os, "O_NOFOLLOW", 0))
            self._artifact_root_fd = os.open(self.artifact_output_root, flags)
            if (
                int(os.fstat(self._artifact_root_fd).st_dev),
                int(os.fstat(self._artifact_root_fd).st_ino),
            ) != self._artifact_root_identity:
                os.close(self._artifact_root_fd)
                self._artifact_root_fd = None
                raise ValueError("artifact output root identity changed during open")
        self.event_sink = event_sink
        self.clock = clock
        self.artifact_writer = artifact_writer
        self._uses_atomic_writer = artifact_writer is _atomic_write_json
        self.obligation_deriver = obligation_deriver
        self.assignment_validator = assignment_validator
        self.scheduler_type = scheduler_type

    def __del__(self) -> None:
        descriptor = getattr(self, "_artifact_root_fd", None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._artifact_root_fd = None

    def _transition(self, state: _RunState, phase: str) -> None:
        expected = _LEGAL_TRANSITIONS.get(state.phase)
        if phase != expected:
            raise RuntimeError(f"illegal controller transition {state.phase!r} -> {phase!r}")
        previous = state.phase
        state.phase = phase
        state.phase_outcomes[phase] = "running"
        state.journal.emit("phase_changed", {
            "phase": phase, "previous_phase": previous, "status": "running",
        })

    def _complete_phase(
        self, state: _RunState, *, status: str = "complete",
    ) -> None:
        if state.phase is None:
            raise RuntimeError("cannot complete a phase before it is entered")
        if status not in {"complete", "incomplete", "degraded"}:
            raise ValueError(
                "completed phase status must be complete, incomplete, or degraded"
            )
        state.phase_outcomes[state.phase] = status
        state.journal.emit("phase_completed", {
            "phase": state.phase, "status": status,
        })

    def _skip_phase(self, state: _RunState, phase: str) -> None:
        expected = _LEGAL_TRANSITIONS.get(state.phase)
        if phase != expected:
            raise RuntimeError(
                f"illegal controller phase skip {state.phase!r} -> {phase!r}"
            )
        previous = state.phase
        state.phase = phase
        state.phase_outcomes[phase] = "skipped"
        state.journal.emit("phase_skipped", {
            "phase": phase, "previous_phase": previous,
        })

    @staticmethod
    def _publishing_authority(inputs: ReviewInputs) -> tuple[str, bool]:
        modes = {"comment": 0, "review_comment": 1, "review_verdict": 2}
        allowed_modes = inputs.policy.publishing.get(
            "allowed_modes", ("review_verdict",),
        )
        policy_allow = inputs.policy.publishing.get("allow_approve", False)
        if (
            not isinstance(allowed_modes, (list, tuple))
            or not allowed_modes
            or any(mode not in modes for mode in allowed_modes)
        ):
            raise ValueError(
                "repository publishing policy has invalid allowed_modes"
            )
        if not isinstance(policy_allow, bool):
            raise ValueError("repository publishing policy allow_approve must be boolean")
        policy_mode = max(allowed_modes, key=lambda item: modes[item])
        mode = min(
            (policy_mode, inputs.publishing_mode),
            key=lambda item: modes[item],
        )
        return mode, policy_allow and inputs.allow_approve

    @staticmethod
    def _required_note_count(state: _RunState) -> int:
        return (
            len(state.review.accepted)
            + len(state.review.verification_requests)
            + len(state.inputs.verification_requests)
            + len(state.retention_verification_requests)
            + len(state.source_requests)
        )

    @staticmethod
    def _publication_is_ready(state: _RunState, path: Path | None) -> bool:
        if path is None or not state.handoff.markdown:
            return False
        if state.verdict not in {"approve", "request_changes", "notice"}:
            return False
        if state.effective_publishing_mode == "comment":
            return True
        return len(state.notes) == state.required_note_count

    def _degrade(self, state: _RunState, component: str, reason: str) -> None:
        item = {"component": component, "reason": _mask_runtime_text(reason)[:1000]}
        state.degradations.append(item)
        state.journal.emit("degradation", item)

    def _promote_degraded_session_result(
        self,
        state: _RunState,
        assignment_id: str,
        result: object,
    ) -> None:
        checkpoint = getattr(result, "checkpoint", None)
        retention_unknown = _CANDIDATE_RETENTION_UNKNOWN in tuple(
            getattr(checkpoint, "unknowns", ())
        )
        if not bool(getattr(result, "degraded", False)) and not retention_unknown:
            return
        component = f"specialist:{assignment_id}"
        if any(item.get("component") == component for item in state.degradations):
            return
        state.journal.emit("specialist_result_degraded", {
            "assignment_id": assignment_id,
            "session_id": str(getattr(result, "session_id", "")),
            "result_degraded": bool(getattr(result, "degraded", False)),
            "candidate_retention_unknown": retention_unknown,
            "checkpoint_state": str(
                getattr(checkpoint, "state", "")
            ),
            "candidate_count": len(tuple(
                getattr(checkpoint, "candidate_finding_ids", ())
            )),
        })
        self._degrade(
            state,
            component,
            "specialist completed with degraded retained state",
        )

    @staticmethod
    def _candidate_retention_limited(state: _RunState) -> bool:
        return any(
            _CANDIDATE_RETENTION_UNKNOWN
            in tuple(getattr(result.checkpoint, "unknowns", ()))
            for result in state.session_results.values()
        )

    @staticmethod
    def _review_status(state: _RunState) -> str:
        if state.degradations:
            return "degraded"
        if state.blocking_obligation_ids:
            return "incomplete"
        return "complete"

    def _candidate_retention_verification_requests(
        self,
        state: _RunState,
    ) -> tuple[Mapping[str, object], ...]:
        """Return one non-defect verification note for locatable retention loss."""
        changed = set(state.inputs.changed_files)
        snapshot = state.evidence.snapshot()
        records = {
            record.id: record
            for record in snapshot.records
            if record.is_usable_for_coverage
        }
        obligation_ids = {item.id for item in state.obligations}
        candidates: dict[
            tuple[str, str],
            tuple[set[str], set[str]],
        ] = {}
        for result in state.session_results.values():
            checkpoint = result.checkpoint
            if (
                _CANDIDATE_RETENTION_UNKNOWN
                not in tuple(getattr(checkpoint, "unknowns", ()))
            ):
                continue
            ownership = state.ownership.get(result.session_id)
            assignment = (
                state.assignments.get(ownership.assignment_id)
                if ownership is not None
                else None
            )
            assignment_obligation_ids = tuple(
                item for item in getattr(assignment, "obligation_ids", ())
                if item in obligation_ids
            )
            if not assignment_obligation_ids:
                continue
            for evidence_id in getattr(checkpoint, "evidence_ids", ()):
                record = records.get(evidence_id)
                path = str(getattr(record, "source_path", "") or "")
                if record is None or path not in changed:
                    continue
                associated_obligation_ids = {
                    obligation_id
                    for obligation_id in assignment_obligation_ids
                    if any(
                        collection.session_id == result.session_id
                        for collection, _association
                        in snapshot.associations_for(
                            evidence_id,
                            obligation_id,
                        )
                    )
                }
                if not associated_obligation_ids:
                    continue
                associated, evidence = candidates.setdefault(
                    (path, result.session_id),
                    (set(), set()),
                )
                associated.update(associated_obligation_ids)
                evidence.add(evidence_id)
        if not candidates:
            return ()
        path, session_id = sorted(candidates)[0]
        associated, evidence = candidates[(path, session_id)]
        return ({
            "question": (
                f"Verify the reviewed behavior in `{path}` because candidate "
                "retention was incomplete."
            ),
            "reason": (
                "A specialist reported material candidate-retention loss. "
                "This is a coverage check, not a confirmed defect."
            ),
            "related_obligation_ids": tuple(sorted(associated)),
            "evidence_ids": tuple(sorted(evidence)),
            "file": path,
        },)

    def _quarantine_session(
        self, state: _RunState, session_id: str, reason: str,
    ) -> None:
        handle = state.sessions.pop(session_id, None)
        state.quarantined_session_ids.add(session_id)
        if isinstance(handle, _IsolatedSessionHandle):
            if state.assignment_sessions.get(handle.assignment.id) == session_id:
                state.assignment_sessions.pop(handle.assignment.id, None)
        state.journal.emit("session_quarantined", {
            "session_id": session_id,
            "reason": reason,
        })

    def _admit_specialist_request_events(
        self, state: _RunState, source: object,
    ) -> None:
        for event in getattr(source, "request_events", ()):
            request_id = str(getattr(event, "request_id", "")).strip()
            status = str(getattr(event, "status", "")).strip()
            key = (request_id, status)
            if (
                not request_id
                or status not in {"started", "completed", "failed", "timed_out"}
                or key in state.admitted_specialist_request_events
            ):
                continue
            state.admitted_specialist_request_events.add(key)
            state.journal.emit(f"specialist_request_{status}", {
                "request_id": request_id,
                "session_id": getattr(source, "session_id", ""),
                "tools_enabled": bool(getattr(event, "tools_enabled", False)),
                "response_schema_name": getattr(event, "response_schema_name", None),
                "finish_reason": getattr(event, "finish_reason", ""),
                "text_source": getattr(event, "text_source", ""),
                "tool_call_count": int(getattr(event, "tool_call_count", 0) or 0),
                **({"error": getattr(event, "error", "")} if status == "failed" else {}),
            })

    def _admit_wave_request_attempts(
        self, state: _RunState, attempts: Iterable[RequestAttempt],
    ) -> None:
        for attempt in attempts:
            state.request_attempts.setdefault(attempt.request_id, attempt)
            state.request_attempt_ids_by_session.setdefault(
                attempt.session_id, set(),
            ).add(attempt.request_id)
            start_key = (attempt.request_id, "started")
            payload = {
                "request_id": attempt.request_id,
                "session_id": attempt.session_id,
                "assignment_id": attempt.assignment_id,
                "phase": attempt.phase,
                "turn": attempt.turn,
                "input_tokens": attempt.input_tokens,
                "max_output_tokens": attempt.max_output_tokens,
                "started_at": attempt.started_at,
            }
            if start_key not in state.admitted_specialist_request_events:
                state.admitted_specialist_request_events.add(start_key)
                state.journal.emit("specialist_request_started", payload)
            terminal_key = (attempt.request_id, attempt.status)
            if (
                attempt.status != "started"
                and terminal_key not in state.admitted_specialist_request_events
            ):
                state.admitted_specialist_request_events.add(terminal_key)
                state.journal.emit(f"specialist_request_{attempt.status}", {
                    **payload,
                    "terminal_at": attempt.terminal_at,
                    "in_flight": attempt.in_flight,
                })

    @staticmethod
    def _request_attempt_event_payload(attempt: RequestAttempt) -> dict[str, object]:
        return {
            # Keep this separate from the admitted specialist_request_* event
            # identity so older artifact consumers do not double-count the
            # immediate gateway telemetry as another specialist transition.
            "gateway_request_id": attempt.request_id,
            "session_id": attempt.session_id,
            "assignment_id": attempt.assignment_id,
            "phase": attempt.phase,
            "turn": attempt.turn,
            "input_tokens": attempt.input_tokens,
            "max_output_tokens": attempt.max_output_tokens,
            "status": attempt.status,
            "in_flight": attempt.in_flight,
            "purpose": attempt.purpose,
            "finish_reason": attempt.finish_reason,
            "text_source": attempt.text_source,
            "tool_call_count": attempt.tool_call_count,
            "error": attempt.error,
        }

    def _session_hook(
        self,
        state: _RunState,
        session_id: str,
        hook: str,
        phase: RunPhase,
        *args: object,
    ) -> tuple[bool, object | None]:
        handle = state.sessions.get(session_id)
        if not isinstance(handle, _IsolatedSessionHandle):
            return False, None
        lease = state.deadline.lease_for(phase)
        timeout = min(
            float(state.inputs.config.model_request_timeout_sec),
            lease.remaining(now=self.clock()),
        )
        detached_args = tuple(freeze_callback_value(args))
        state.journal.emit("specialist_hook_started", {
            "session_id": session_id, "hook": hook, "phase": phase.value,
        })
        try:
            callback = getattr(handle, hook)
            result = CALLBACK_POOL.run(
                lambda: callback(*detached_args),
                timeout_sec=timeout,
                name=f"session-{hook}",
            )
            if lease.remaining(now=self.clock()) <= 0:
                raise CallbackTimedOut(f"session {hook} completed after phase cutoff")
        except CallbackTimedOut as exc:
            reason = _bounded_error(exc)
            state.journal.emit("specialist_hook_timed_out", {
                "session_id": session_id, "hook": hook, "error": reason,
            })
            self._degrade(state, f"specialist_hook:{session_id}:{hook}", reason)
            self._quarantine_session(state, session_id, reason)
            return False, None
        except BaseException as exc:
            reason = _bounded_error(exc)
            state.journal.emit("specialist_hook_failed", {
                "session_id": session_id, "hook": hook, "error": reason,
            })
            self._degrade(state, f"specialist_hook:{session_id}:{hook}", reason)
            self._quarantine_session(state, session_id, reason)
            return False, None
        state.journal.emit("specialist_hook_completed", {
            "session_id": session_id, "hook": hook,
        })
        return True, result

    def _model_request(
        self,
        state: _RunState,
        *,
        role: str,
        request_id: str,
        phase: RunPhase,
        component: object,
        method: str,
        context: Mapping[str, object],
        planner_request_budget: PlannerRequestBudget | None = None,
    ) -> object:
        state.journal.emit("model_request_started", {
            "request_id": request_id,
            "role": role,
            "phase": phase.value,
        })
        try:
            lease = state.deadline.lease_for(phase)

            def remaining() -> float:
                return lease.remaining(now=self.clock())

            timeout = min(
                float(state.inputs.config.model_request_timeout_sec), remaining(),
            )
            frozen_context = freeze_callback_value(context)
            if not isinstance(frozen_context, Mapping):
                raise TypeError("role context must freeze to an object")
            request = RoleRequest(
                role=role,
                request_id=request_id,
                phase=phase,
                lease=lease,
                timeout_sec=timeout,
                max_tokens=state.inputs.config.session_limits.output_tokens or 4096,
                context=frozen_context,
                planner_request_budget=planner_request_budget,
            )
            value = CALLBACK_POOL.run(
                lambda: self._call_role_component(component, method, request),
                timeout_sec=timeout,
                name=f"role-{role}",
            )
            if remaining() <= 0:
                raise CallbackTimedOut(f"{role} result arrived after phase cutoff")
        except CallbackTimedOut as exc:
            state.journal.emit("model_request_timed_out", {
                "request_id": request_id, "role": role, "phase": phase.value,
                "error": _bounded_error(exc),
            })
            raise
        except BaseException as exc:
            state.journal.emit("model_request_failed", {
                "request_id": request_id,
                "role": role,
                "phase": phase.value,
                "error": _bounded_error(exc),
            })
            if isinstance(exc, Exception):
                raise
            raise RuntimeError(_bounded_error(exc)) from None
        state.journal.emit("model_request_completed", {
            "request_id": request_id,
            "role": role,
            "phase": phase.value,
        })
        return value

    @staticmethod
    def _call_role_component(
        component: object,
        method: str,
        request: RoleRequest,
    ) -> object:
        target = getattr(component, method, None)
        if target is None:
            target = getattr(component, "complete", None)
        if target is None:
            target = component
        if not callable(target):
            raise TypeError(f"{method} component is not callable")
        return target(request)

    def _plan(self, state: _RunState) -> AssignmentPlan:
        inputs = state.inputs
        base_plan = fallback_assignment_plan(
            state.obligations, inputs.topology, inputs.config,
        )
        state.plan_source = "deterministic_base"
        if self.planner is None or self.clock() >= state.deadline.cutoff_for(RunPhase.PLANNING):
            return base_plan
        request_budget = PlannerRequestBudget()
        try:
            raw = self._model_request(
                state,
                role="planner",
                request_id="planner:1",
                phase=RunPhase.PLANNING,
                component=self.planner,
                method="plan",
                planner_request_budget=request_budget,
                context={
                    "base_plan": base_plan,
                    "obligations": state.obligations,
                    "topology": inputs.topology,
                    "config": inputs.config,
                    "pr_metadata": inputs.pr_metadata,
                    "policy": inputs.policy,
                    "change_overview": change_overview_orientation(
                        state.change_overview,
                    ),
                },
            )
            result = apply_planner_transformations(
                raw if isinstance(raw, Mapping) else {},
                base_plan,
                state.obligations,
                inputs.config,
                topology=inputs.topology,
            )
            state.planner_diagnostics.extend(result.ignored)
            if result.plan != base_plan:
                state.plan_source = "deterministic_base_transformed"
            for diagnostic in result.ignored:
                state.journal.emit("planner_transformation_ignored", {
                    "reason": diagnostic,
                })
            return result.plan
        except Exception as exc:
            diagnostic = _bounded_error(exc)
            state.planner_diagnostics.append(diagnostic)
            state.journal.emit("planner_transformation_ignored", {
                "reason": diagnostic,
            })
            return base_plan

    def _summarize_changes(self, state: _RunState) -> Mapping[str, object]:
        fallback = _deterministic_change_overview(state.inputs)
        change_facts = state.inputs.topology.get("change_facts")
        if (
            isinstance(change_facts, Mapping)
            and change_facts.get("status") == "degraded"
        ):
            failures = change_facts.get("failures", ())
            first = (
                failures[0]
                if isinstance(failures, (tuple, list)) and failures
                else {}
            )
            reason = (
                str(first.get("reason") or "").strip()
                if isinstance(first, Mapping)
                else ""
            ) or "immutable local diff facts are unavailable"
            self._degrade(state, "change_facts", reason)
            return fallback
        if (
            self.change_summarizer is None
            or self.clock() >= state.deadline.cutoff_for(RunPhase.PLANNING)
        ):
            return fallback
        try:
            proposed = self._model_request(
                state,
                role="change_summarizer",
                request_id="change_summarizer:1",
                phase=RunPhase.PLANNING,
                component=self.change_summarizer,
                method="summarize",
                context={
                    "change_facts": (
                        change_facts
                        if isinstance(change_facts, Mapping)
                        else state.inputs.topology.get(
                            "changed_contract_facts", {},
                        )
                    ),
                    "changed_files": state.inputs.changed_files,
                    "components": state.inputs.topology.get("components", ()),
                    "path_components": state.inputs.topology.get(
                        "path_components", {},
                    ),
                    "relationships": state.inputs.topology.get(
                        "relationships", (),
                    ),
                    "pr_metadata": {
                        key: state.inputs.pr_metadata.get(key, "")
                        for key in ("title", "body")
                    },
                },
            )
            return _validated_change_overview(proposed, state.inputs)
        except Exception as exc:
            state.journal.emit("recovery", {
                "component": "change_summarizer",
                "action": "deterministic-fallback",
                "reason": _bounded_error(exc),
            })
            return fallback

    def _ownership(self, assignment: Assignment, session_id: str, state: _RunState) -> SessionOwnership:
        return session_ownership_for_assignment(
            assignment,
            state.obligations,
            session_id=session_id,
        )

    @staticmethod
    def _session_identity(state: _RunState, assignment: Assignment) -> str:
        existing = state.assignment_sessions.get(assignment.id)
        if existing is not None:
            return existing
        generation = state.session_generations.get(assignment.id, 0) + 1
        state.session_generations[assignment.id] = generation
        identity = _digest({
            "repository": state.inputs.repository,
            "head_sha": state.inputs.head_sha,
            "assignment_id": assignment.id,
            "generation": generation,
        })[:20]
        return f"session:{identity}:g{generation}"

    def _create_isolated_session(
        self,
        state: _RunState,
        assignment: Assignment,
        lease: SessionLease,
        snapshot: WaveSnapshot,
        expected_session_id: str,
        existing: _IsolatedSessionHandle | None,
    ) -> object:
        if existing is not None:
            if existing.session_id != expected_session_id:
                raise ValueError("existing session identity differs from controller binding")
            if existing.lease != lease:
                raise RuntimeError("resumed session lease was not activated by controller")
            return existing
        if self.session_factory is None:
            raise RuntimeError("no specialist session factory configured")
        local_evidence = EvidenceStore.from_snapshot(snapshot.evidence)
        local_coverage = CoverageLedger(state.obligations)
        local_coverage.replace_reconciled_state(
            dict(snapshot.coverage.evidence_by_obligation),
            tuple(
                obligation_id
                for obligation_id, status in snapshot.coverage.obligation_statuses
                if status is ObligationStatus.UNRESOLVED
            ),
        )
        factory = self.session_factory
        signature = inspect.signature(factory)
        positional = tuple(
            item for item in signature.parameters.values()
            if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        args = (
            assignment, lease, snapshot, local_evidence, local_coverage,
            state.obligations, expected_session_id, state.change_overview,
        )
        session = factory(*args if any(
            item.kind is inspect.Parameter.VAR_POSITIONAL
            for item in signature.parameters.values()
        ) else args[:len(positional)])
        session_id = str(getattr(session, "session_id", "")).strip()
        if session_id != expected_session_id:
            raise ValueError("session identity differs from controller binding")
        binder = getattr(session, "bind_request_attempt_journal", None)
        if callable(binder) and state.request_attempt_journal is not None:
            binder(state.request_attempt_journal, assignment.id)
        return _IsolatedSessionHandle(
            assignment=assignment,
            session=session,
            session_id=session_id,
            evidence=local_evidence,
            coverage=local_coverage,
            lease=lease,
            baseline_evidence_ids=frozenset(snapshot.evidence.evidence_ids),
        )

    def _run_wave(
        self,
        state: _RunState,
        assignments: Iterable[Assignment],
        phase: RunPhase,
    ) -> tuple[WaveResult, WaveSnapshot]:
        assert state.coverage is not None
        assignment_items = tuple(assignments)
        wave_snapshot = WaveSnapshot(state.evidence.snapshot(), state.coverage.snapshot())
        expected_ids = {
            assignment.id: self._session_identity(state, assignment)
            for assignment in assignment_items
        }
        existing_handles = {
            assignment.id: state.sessions.get(expected_ids[assignment.id])
            for assignment in assignment_items
        }
        for assignment in assignment_items:
            state.journal.emit("specialist_initializing", {
                "assignment_id": assignment.id,
                "session_id": expected_ids[assignment.id],
                "phase": phase.value,
                "resumed": isinstance(
                    existing_handles[assignment.id], _IsolatedSessionHandle,
                ),
            })
        scheduler = self.scheduler_type(
            deadline=state.deadline,
            session_factory=lambda assignment, lease, snapshot: self._create_isolated_session(
                state,
                assignment,
                lease,
                snapshot,
                expected_ids[assignment.id],
                existing_handles[assignment.id]
                if isinstance(existing_handles[assignment.id], _IsolatedSessionHandle)
                else None,
            ),
            wave_snapshot=wave_snapshot,
            concurrency=state.inputs.config.concurrency,
            event_sink=lambda kind, payload: state.journal.emit(kind, payload),
            request_attempt_journal=state.request_attempt_journal,
            clock=self.clock,
        )
        result = scheduler.run_wave(assignment_items, phase)
        self._admit_wave_request_attempts(state, result.request_attempts)
        assignment_by_id = state.assignments
        for item in result.results:
            if not isinstance(item.session, _IsolatedSessionHandle):
                raise TypeError("completed scheduler result lacks isolated session state")
            assignment = assignment_by_id[item.assignment_id]
            expected_session_id = expected_ids[item.assignment_id]
            if item.session_result.session_id != expected_session_id:
                raise ValueError("completed session identity differs from controller binding")
            state.evidence.merge_completed_snapshot(item.session.evidence.snapshot())
            state.sessions[expected_session_id] = item.session
            state.assignment_sessions[item.assignment_id] = expected_session_id
            state.session_results[(item.assignment_id, expected_session_id)] = item.session_result
            self._admit_specialist_request_events(state, item.session_result)
            state.ownership[item.session_result.session_id] = self._ownership(
                assignment, item.session_result.session_id, state,
            )
            for index, candidate in enumerate(item.session.candidate_findings):
                state.candidate_occurrences[
                    f"session:{item.session_result.session_id}:{index}"
                ] = candidate
            state.source_requests.extend(item.session.source_access_requests)
            state.journal.emit("session_transition", {
                "assignment_id": item.assignment_id,
                "session_id": item.session_result.session_id,
                "state": "created",
            })
            state.journal.emit("session_transition", {
                "assignment_id": item.assignment_id,
                "session_id": item.session_result.session_id,
                "state": item.session_result.state.value,
            })
            usage = item.session_result.budget
            state.journal.emit("budget_changed", {
                "session_id": item.session_result.session_id,
                "usage": _budget_projection(usage),
            })
        for failure in result.failures:
            if isinstance(failure.session, _IsolatedSessionHandle):
                failed_handle = failure.session
                self._admit_specialist_request_events(state, failed_handle)
                failed_usage = getattr(failed_handle.budget, "snapshot", None)
                if callable(failed_usage):
                    usage = failed_usage()
                    if isinstance(usage, BudgetUsage):
                        state.failed_session_budgets[
                            failed_handle.session_id
                        ] = usage
                        state.journal.emit("budget_changed", {
                            "session_id": failed_handle.session_id,
                            "usage": _budget_projection(usage),
                            "failed": True,
                        })
                self._quarantine_session(
                    state, failed_handle.session_id, failure.error,
                )
            self._degrade(state, f"specialist:{failure.assignment_id}", failure.error)
            state.journal.emit("recovery", {
                "component": "specialist", "assignment_id": failure.assignment_id,
                "action": "bounded_followup_or_unknown",
            })
        for assignment_id in result.in_flight_assignment_ids:
            session_id = state.assignment_sessions.pop(assignment_id, None)
            if session_id is not None:
                state.sessions.pop(session_id, None)
            state.journal.emit("session_quarantined", {
                "assignment_id": assignment_id,
                "reason": "in_flight_after_wave_cutoff",
            })
        before_ids = set(wave_snapshot.evidence.evidence_ids)
        for record in state.evidence.snapshot().records:
            if record.id not in before_ids:
                state.journal.emit("evidence_added", {
                    "evidence_id": record.id,
                    "category": record.category,
                    "collector_session_id": record.collector_session_id,
                    "content_hash": record.content_hash,
                    "status": record.status,
                })
        return result, wave_snapshot

    def _reconcile(
        self,
        state: _RunState,
        result: WaveResult,
        wave_snapshot: WaveSnapshot,
    ) -> CoverageReconciliation:
        assert state.coverage is not None
        checkpoints = tuple(item.session_result.checkpoint for item in result.results)
        try:
            reconciled = reconcile_wave(
                state.coverage,
                wave_start_coverage=wave_snapshot.coverage,
                checkpoints=checkpoints,
                evidence=state.evidence.snapshot(),
                assignments=tuple(state.assignments[key] for key in sorted(state.assignments)),
                session_ownership=tuple(
                    state.ownership[key] for key in sorted(state.ownership)
                    if state.ownership[key].assignment_id in state.assignments
                ),
            )
        except Exception as exc:
            self._degrade(state, "coverage_reconciliation", _bounded_error(exc))
            baseline_evidence = dict(wave_snapshot.coverage.evidence_by_obligation)
            unresolved = tuple(
                obligation_id for obligation_id, status in wave_snapshot.coverage.obligation_statuses
                if status is ObligationStatus.UNRESOLVED
            )
            state.coverage.replace_reconciled_state(baseline_evidence, unresolved)
            snapshot = state.coverage.snapshot()
            statuses = dict(snapshot.obligation_statuses)
            uncovered = tuple(sorted(
                item.id for item in state.obligations
                if item.mandatory and statuses.get(item.id) is not ObligationStatus.COVERED
            ))
            reconciled = CoverageReconciliation(snapshot, (), uncovered, (), uncovered)
        for obligation_id, status in reconciled.snapshot.obligation_statuses:
            state.journal.emit("coverage_decision", {
                "obligation_id": obligation_id,
                "status": status.value,
                "evidence_ids": dict(reconciled.snapshot.evidence_by_obligation).get(
                    obligation_id, (),
                ),
            })
        for recipe_id, status in reconciled.snapshot.recipe_statuses:
            state.journal.emit("recipe_status", {
                "recipe_id": recipe_id, "status": status,
            })
        return reconciled

    def _negotiation_state(
        self,
        state: _RunState,
        reconciliation: CoverageReconciliation,
        followup_started: int,
    ) -> NegotiationState:
        resources: list[SessionResources] = []
        for session_id, ownership in sorted(state.ownership.items()):
            session = state.sessions.get(session_id)
            result = state.session_results.get((ownership.assignment_id, session_id))
            if session is None or result is None:
                continue
            ledger = getattr(session, "budget", None)
            if isinstance(ledger, BudgetLedger):
                remaining_turns = ledger.remaining_model_turns()
                remaining_tools = ledger.remaining_tool_calls()
            else:
                remaining_turns = max(
                    0, state.inputs.config.session_limits.model_turns - result.budget.model_turns,
                )
                remaining_tools = max(
                    0, state.inputs.config.session_limits.tool_calls - result.budget.tool_calls,
                )
            resources.append(SessionResources(
                session_id=session_id,
                remaining_model_turns=remaining_turns,
                remaining_tool_calls=remaining_tools,
                lease_remaining_sec=session.lease.remaining(now=self.clock()),
            ))
        checkpoints = tuple(
            state.session_results[key].checkpoint for key in sorted(state.session_results)
        )
        return NegotiationState(
            obligations=state.obligations,
            coverage=reconciliation.snapshot,
            assignments=tuple(state.assignments[key] for key in sorted(state.assignments)),
            checkpoints=checkpoints,
            session_ownership=tuple(state.ownership[key] for key in sorted(state.ownership)),
            session_resources=tuple(resources),
            remaining_deadline_sec=state.deadline.remaining_for_exploration(now=self.clock()),
            seconds_per_turn=float(state.inputs.config.model_request_timeout_sec),
            current_session_count=len(state.assignments),
            max_sessions=state.inputs.config.max_sessions,
            followup_sessions_started=followup_started,
            max_followup_sessions=state.inputs.config.max_followup_sessions,
            new_session_turns_remaining=max(
                0,
                (state.inputs.config.max_followup_sessions - followup_started)
                * state.inputs.config.session_limits.model_turns,
            ),
            new_session_turn_cap=state.inputs.config.session_limits.model_turns,
            new_session_tool_call_cap=state.inputs.config.session_limits.tool_calls,
            new_session_lease_remaining_sec=state.deadline.remaining_for_exploration(
                now=self.clock(),
            ),
        )

    def _negotiate(
        self, state: _RunState, reconciliation: CoverageReconciliation,
        *, attempt: int = 1,
    ) -> tuple[NegotiationAction, ...]:
        if not reconciliation.uncovered_obligation_ids:
            return ()
        followup_started = sum(
            1 for assignment in state.assignments.values()
            if "-followup-" in assignment.id
        )
        negotiation_state = self._negotiation_state(
            state, reconciliation, followup_started,
        )
        if not state.deadline.exploration_allowed(now=self.clock()):
            self._degrade(state, "deadline", "exploration cutoff reached before follow-up")
            try:
                return (fallback_next_action(negotiation_state),)
            except NegotiationError:
                return ()
        if self.negotiator is not None:
            try:
                compact_context = compact_negotiation_context(negotiation_state)
                role_context: dict[str, object] = {
                    "negotiation_state": compact_context,
                    "negotiation_targets": compact_context["targets"],
                    "pr_metadata": state.inputs.pr_metadata,
                    "policy": state.inputs.policy,
                    "change_overview": change_overview_orientation(
                        state.change_overview,
                    ),
                }
                # Keep the typed state available to in-process integrations for
                # backwards compatibility.  Gateway-backed model roles receive
                # only the compact target projection above.
                if not isinstance(self.negotiator, GatewayRoleAdapter):
                    role_context["negotiation_state"] = negotiation_state
                raw = self._model_request(
                    state,
                    role="negotiator",
                    request_id=f"negotiator:{max(1, attempt)}",
                    phase=RunPhase.FOLLOWUP,
                    component=self.negotiator,
                    method="propose",
                    context=role_context,
                )
                if not isinstance(raw, Mapping):
                    raise NegotiationError("negotiator result must be an object")
                raw_kind = raw.get("kind")
                if raw_kind is None and isinstance(raw.get("actions"), list):
                    action_items = raw["actions"]
                    if len(action_items) == 1 and isinstance(action_items[0], Mapping):
                        raw_kind = action_items[0].get("kind")
                if isinstance(raw_kind, str) and raw_kind in {"record-unknown", "new-session"}:
                    state.journal.emit("negotiation_alias_normalized", {
                        "from": raw_kind,
                        "to": {"record-unknown": "record_unknown", "new-session": "new_session"}[raw_kind],
                    })
                if isinstance(self.negotiator, GatewayRoleAdapter):
                    proposal = validate_compact_negotiation(raw, negotiation_state)
                else:
                    # Trusted in-process adapters may still return the legacy
                    # shape, but never more than one action is executable.
                    if "actions" in raw and isinstance(raw.get("actions"), list) \
                            and len(raw["actions"]) != 1:
                        raise NegotiationError(
                            "negotiator proposal must contain exactly one action"
                        )
                    proposal = validate_negotiation(raw, negotiation_state)
                return proposal.actions
            except Exception as exc:
                # Negotiation is an optimization layer. The deterministic
                # fallback below can still produce a bounded follow-up, so a
                # malformed optional proposal must not make the whole review
                # appear materially degraded.
                state.journal.emit("recovery", {
                    "component": "negotiator",
                    "action": "deterministic-fallback",
                    "reason": _bounded_error(exc),
                })
        try:
            action = fallback_next_action(negotiation_state)
            state.journal.emit("recovery", {
                "component": "negotiator", "action": action.kind,
                "obligation_ids": action.obligation_ids,
            })
            return (action,)
        except NegotiationError as exc:
            self._degrade(state, "negotiator", _bounded_error(exc))
            return ()

    def _followup_assignments(
        self, state: _RunState, actions: Iterable[NegotiationAction],
    ) -> tuple[Assignment, ...]:
        result: list[Assignment] = []
        obligation_by_id = {item.id: item for item in state.obligations}
        for index, action in enumerate(actions, start=1):
            state.journal.emit("negotiation_action", {
                "kind": action.kind,
                "session_id": action.session_id,
                "obligation_ids": action.obligation_ids,
                "estimated_turns": action.estimated_turns,
                "reason": action.reason,
            })
            if action.kind == "record_unknown":
                for obligation_id in action.obligation_ids:
                    assert state.coverage is not None
                    state.coverage.mark_unresolved(obligation_id)
                    state.unknowns.append({
                        "obligation_id": obligation_id,
                        "reason": "bounded investigation recorded no further feasible evidence",
                        "resolution_policy": dict(action.resolution_policies).get(obligation_id),
                    })
                state.journal.emit("negotiation_action_applied", {
                    "kind": action.kind,
                    "obligation_ids": action.obligation_ids,
                    "outcome": "recorded_unresolved",
                })
                continue
            if action.kind in {"resume", "consult"} and action.session_id:
                ownership = state.ownership.get(action.session_id)
                if ownership is None:
                    state.journal.emit("negotiation_action_applied", {
                        "kind": action.kind,
                        "session_id": action.session_id,
                        "outcome": "skipped_unknown_session",
                    })
                    continue
                assignment = state.assignments[ownership.assignment_id]
                session = state.sessions.get(action.session_id)
                if session is None:
                    state.journal.emit("negotiation_action_applied", {
                        "kind": action.kind,
                        "session_id": action.session_id,
                        "outcome": "skipped_missing_session",
                    })
                    continue
                succeeded, _ = self._session_hook(
                    state,
                    action.session_id,
                    "apply_coverage_feedback",
                    RunPhase.FOLLOWUP,
                    action.obligation_ids,
                )
                if succeeded:
                    result.append(assignment)
                    outcome = "scheduled"
                else:
                    outcome = "skipped_feedback_failed"
                state.journal.emit("negotiation_action_applied", {
                    "kind": action.kind,
                    "session_id": action.session_id,
                    "obligation_ids": action.obligation_ids,
                    "outcome": outcome,
                })
                continue
            selected = tuple(
                obligation_by_id[item] for item in action.obligation_ids
                if item in obligation_by_id
            )
            plan = fallback_assignment_plan(selected, state.inputs.topology, state.inputs.config)
            if not plan.assignments:
                for obligation in selected:
                    state.unknowns.append({
                        "obligation_id": obligation.id,
                        "reason": "bounded follow-up assignment was infeasible",
                        "resolution_policy": obligation.unresolved_policy,
                    })
                    assert state.coverage is not None
                    state.coverage.mark_unresolved(obligation.id)
                state.journal.emit("negotiation_action_applied", {
                    "kind": action.kind,
                    "obligation_ids": action.obligation_ids,
                    "outcome": "recorded_infeasible",
                })
                continue
            assignment = replace(
                plan.assignments[0],
                id=f"{plan.assignments[0].id}-followup-{index}",
                title=f"{plan.assignments[0].title} follow-up {index}",
                estimated_turns=min(action.estimated_turns, plan.assignments[0].estimated_turns),
            )
            state.assignments[assignment.id] = assignment
            result.append(assignment)
            state.journal.emit("negotiation_action_applied", {
                "kind": action.kind,
                "obligation_ids": action.obligation_ids,
                "assignment_id": assignment.id,
                "outcome": "scheduled",
            })
        return tuple(result)

    def _collect_candidates(self, state: _RunState) -> tuple[CandidateFinding, ...]:
        for index, candidate in enumerate(state.inputs.candidate_findings):
            state.candidate_occurrences[f"input:{index}"] = candidate
        grouped: dict[str, list[tuple[str, CandidateFinding]]] = {}
        for occurrence_ref, candidate in sorted(state.candidate_occurrences.items()):
            grouped.setdefault(candidate.candidate_id, []).append(
                (occurrence_ref, candidate)
            )
        state.candidates.clear()
        state.collision_dispositions.clear()
        unique_candidates: list[CandidateFinding] = []
        for candidate_id in sorted(grouped):
            occurrences = grouped[candidate_id]
            if len(occurrences) == 1:
                unique_candidates.append(occurrences[0][1])
                continue
            for occurrence_ref, candidate in occurrences:
                # Candidate IDs are model-local handles.  Independent
                # specialists commonly emit simple IDs such as ``c1``; a
                # collision must not discard every occurrence before the
                # critic can compare them.  Scope only the colliding IDs so
                # ordinary model IDs remain readable and stable.
                scope_seed = (
                    f"{candidate.collector_session_id}|{occurrence_ref}"
                ).encode("utf-8", "replace")
                scope_suffix = hashlib.sha256(scope_seed).hexdigest()[:12]
                scoped_id = f"{candidate_id}@{scope_suffix}"
                scoped = replace(
                    candidate,
                    candidate_id=scoped_id,
                    contributor_candidate_ids=tuple(dict.fromkeys((
                        *candidate.contributor_candidate_ids,
                        candidate_id,
                    ))),
                )
                unique_candidates.append(scoped)
                disposition = {
                    "candidate_id": scoped_id,
                    "original_candidate_id": candidate_id,
                    "occurrence_ref": occurrence_ref,
                    "action": "scope",
                    "reason": "candidate-id-scoped",
                    "target_id": None,
                    "collector_session_id": candidate.collector_session_id,
                    "model_identity": candidate.model_identity,
                }
                state.collision_dispositions.append(disposition)
                state.journal.emit("candidate_disposition", disposition)
        change_facts, _metadata = _authoritative_change_facts(
            state.inputs.topology,
        )
        consolidated = consolidate_candidates(
            unique_candidates,
            changed_files=state.inputs.changed_files,
            change_facts=change_facts,
            obligations={item.id: item for item in state.obligations},
            evidence=state.evidence.snapshot(),
        )
        for candidate in consolidated.candidates:
            state.candidates[candidate.candidate_id] = candidate
        for disposition in consolidated.dispositions:
            projected = _json_value(disposition)
            state.collision_dispositions.append(projected)
            state.journal.emit("candidate_disposition", projected)
        return tuple(state.candidates[key] for key in sorted(state.candidates))

    @staticmethod
    def _conservative_critic(candidates: Iterable[CandidateFinding]) -> dict[str, object]:
        decisions = []
        for item in sorted(candidates, key=lambda value: value.candidate_id):
            unambiguous = all((
                item.claim.strip(), item.affected_location.strip(), item.causal_chain.strip(),
                item.user_visible_consequence.strip(), item.manual_validation.strip(),
                item.supporting_evidence_ids, item.related_obligation_ids,
            ))
            decisions.append({
                "candidate_id": item.candidate_id,
                "action": "request_verification" if unambiguous else "reject",
            })
        return {"decisions": decisions}

    def _adjudicate(self, state: _RunState, candidates: tuple[CandidateFinding, ...]) -> None:
        obligation_map = {item.id: item for item in state.obligations}
        if not candidates:
            state.review = AdjudicatedReview()
            return
        critic_result: object
        if self.critic is None:
            self._degrade(state, "critic", "deterministic conservative critic fallback")
            critic_result = self._conservative_critic(candidates)
        else:
            try:
                retained = {
                    record.id: record
                    for record in state.evidence.snapshot().records
                    if record.is_usable_for_coverage
                }
                candidate_evidence = {}
                for candidate in candidates:
                    excerpts = []
                    for evidence_id in (
                        *candidate.supporting_evidence_ids,
                        *candidate.contradicting_evidence_ids,
                    ):
                        record = retained.get(evidence_id)
                        if record is None:
                            continue
                        excerpt = record.content.encode("utf-8")[:1200].decode(
                            "utf-8", errors="replace",
                        )
                        excerpts.append({
                            "evidence_id": record.id,
                            "path": record.source_path,
                            "category": record.category,
                            "tool": record.tool,
                            "status": record.status,
                            "content_hash": record.content_hash,
                            "content_excerpt": excerpt,
                            "truncated": record.truncated,
                            "redacted": record.redacted,
                            "contradicts": record.contradicts,
                        })
                    candidate_evidence[candidate.candidate_id] = tuple(excerpts)
                critic_context = {
                    "candidates": candidates,
                    "candidate_evidence": candidate_evidence,
                    "obligations": obligation_map,
                    "changed_files": state.inputs.changed_files,
                    "pr_metadata": state.inputs.pr_metadata,
                    "policy": state.inputs.policy,
                    "change_overview": change_overview_orientation(
                        state.change_overview,
                    ),
                }
                critic_result = self._model_request(
                    state,
                    role="critic",
                    request_id="critic:1",
                    phase=RunPhase.FINALIZATION,
                    component=self.critic,
                    method="adjudicate",
                    context=critic_context,
                )
                diagnostics = _critic_response_diagnostics(critic_result)
                if diagnostics["ignored_fields"]:
                    state.journal.emit("critic_response_normalized", diagnostics)
                try:
                    critic_result = _validated_critic_result(
                        critic_result, candidates,
                    )
                except ValueError as exc:
                    if "omitted candidate decisions" not in str(exc):
                        raise
                    partial_rows, missing_ids = _partial_critic_result(
                        critic_result, candidates,
                    )
                    if not missing_ids:
                        raise
                    missing_candidates = tuple(
                        item for item in candidates
                        if item.candidate_id in set(missing_ids)
                    )
                    state.journal.emit("critic_repair_requested", {
                        "missing_candidate_ids": missing_ids,
                        "accepted_decision_count": len(partial_rows),
                    })
                    try:
                        repaired = self._model_request(
                            state,
                            role="critic",
                            request_id="critic:repair",
                            phase=RunPhase.FINALIZATION,
                            component=self.critic,
                            method="adjudicate",
                            context={
                                **critic_context,
                                "critic_repair": {
                                    "missing_candidate_ids": missing_ids,
                                    "accepted_decisions": partial_rows,
                                    "instruction": (
                                        "Return decisions only for the missing candidate IDs; "
                                        "do not repeat accepted decisions."
                                    ),
                                },
                            },
                        )
                        repaired_rows, still_missing = _partial_critic_result(
                            repaired, missing_candidates,
                        )
                        if still_missing:
                            raise ValueError(
                                "critic repair omitted candidate decisions"
                            )
                        critic_result = _validated_critic_result(
                            {"actions": (*partial_rows, *repaired_rows)},
                            candidates,
                        )
                        state.journal.emit("critic_repair_completed", {
                            "repaired_candidate_ids": tuple(
                                item["candidate_id"] for item in repaired_rows
                            ),
                        })
                    except Exception as repair_exc:
                        # Preserve valid initial decisions and apply the
                        # evidence-gated fallback only to IDs still missing.
                        self._degrade(state, "critic", _bounded_error(repair_exc))
                        fallback = self._conservative_critic(missing_candidates)
                        critic_result = {
                            "actions": (
                                *partial_rows,
                                *fallback.get("decisions", ()),
                            ),
                        }
            except Exception as exc:
                if not any(
                    str(item.get("component", "")) == "critic"
                    for item in state.degradations
                ):
                    self._degrade(state, "critic", _bounded_error(exc))
                critic_result = self._conservative_critic(candidates)
        try:
            state.review = adjudicate_candidates(
                candidates, critic_result, state.evidence,
                obligations=obligation_map, changed_files=state.inputs.changed_files,
            )
        except Exception as exc:
            self._degrade(state, "adjudication", _bounded_error(exc))
            state.review = AdjudicatedReview()
        for disposition in state.review.dispositions:
            state.journal.emit("candidate_disposition", _json_value(disposition))

    def _handoff_context(self, state: _RunState, status: str) -> ReviewHandoffContext:
        assert state.coverage is not None
        coverage = state.coverage.snapshot()
        obligation_statuses = dict(coverage.obligation_statuses)
        evidence_by_obligation = dict(coverage.evidence_by_obligation)
        reviewed_obligations = tuple(
            item for item in state.obligations
            if evidence_by_obligation.get(item.id)
            or obligation_statuses.get(item.id) is ObligationStatus.COVERED
        )
        completed_assignments = tuple(
            state.assignments[assignment_id]
            for assignment_id in sorted(state.assignment_sessions)
            if assignment_id in state.assignments
        )
        change_topics = _orientation_topics(
            state.inputs.topology.get("file_roles", ())
        )
        if not change_topics and state.inputs.changed_files:
            change_topics = (ReviewOrientationTopic.REPOSITORY_BEHAVIOR,)
        specialist_topics = _orientation_topics(
            lens
            for assignment in completed_assignments
            for lens in assignment.lenses
        )
        coverage_boundary_topics = _orientation_topics(
            signal
            for obligation in reviewed_obligations
            for signal in (
                obligation.origin,
                *obligation.required_evidence_categories,
                *(
                    (obligation.subject,)
                    if obligation.origin == "risk-rule"
                    else ()
                ),
            )
        )
        recipe_ids = tuple(sorted(
            item.id for item in state.inputs.policy.recipes
            if state.coverage.recipe_statuses().get(item.id)
            in {"covered", "partially_covered"}
        ))
        unresolved_obligations = tuple(
            item for item in state.obligations
            if item.mandatory
            and obligation_statuses.get(item.id) is not ObligationStatus.COVERED
        )
        finding_topics = _orientation_topics(
            item.category for item in state.review.accepted
        )
        risk_topics = _orientation_topics(
            state.inputs.classification.get("risk_flags", ())
        )
        unresolved_topics = _orientation_topics(
            signal
            for obligation in unresolved_obligations
            for signal in (
                obligation.origin,
                obligation.subject,
                *obligation.required_evidence_categories,
            )
        )
        relationship_topics = (
            (ReviewOrientationTopic.CROSS_COMPONENT_CONTRACTS,)
            if state.inputs.topology.get("relationships")
            else ()
        )
        review_emphasis_topics = tuple(dict.fromkeys((
            *finding_topics,
            *risk_topics,
            *relationship_topics,
            *unresolved_topics,
        )))[:3]
        prepared_finding_severities = tuple(
            note.severity for note in state.notes
            if note.severity in {"info", "minor", "major", "blocker"}
        )

        what_changed, ai_reviewed = _behavioral_handoff_candidates(
            changed_files=state.inputs.changed_files,
            topology=state.inputs.topology,
            obligations=state.obligations,
            evidence_records=state.evidence.snapshot().records,
            reviewed_obligation_ids=tuple(item.id for item in reviewed_obligations),
            allow_role_fallback=(
                status == "degraded"
                or "changed_contract_facts" not in state.inputs.topology
            ),
        )
        overview = str(state.change_overview.get("overview") or "").strip()
        if overview:
            what_changed = (
                _deterministic_handoff_change_summary(state.change_overview)
                if status == "degraded"
                else (overview,)
            )
        component_ids = tuple(sorted(
            str(item.get("id", ""))
            for item in state.inputs.topology.get("components", ())
            if isinstance(item, Mapping) and str(item.get("id", "")).strip()
        ))
        summarized_review = _deterministic_reviewed_summary(
            changed_files=state.inputs.changed_files,
            component_ids=component_ids,
            reviewed_obligations=reviewed_obligations,
        )
        if summarized_review and status == "degraded":
            ai_reviewed = summarized_review
        else:
            ai_reviewed = ai_reviewed[:3]
        human_focus = _deterministic_handoff_focus(
            state.obligations,
            state.blocking_obligation_ids,
            bool(state.degradations),
        )
        tracked_paths = tuple(dict.fromkeys((
            *state.inputs.tracked_paths,
            *state.inputs.changed_files,
        )))
        context_paths = _deterministic_context_paths(
            state.inputs.topology,
            tracked_paths,
            extra_paths=(
                *(
                    record.source_path
                    for record in state.evidence.snapshot().records
                    if record.is_usable_for_coverage and record.source_path
                ),
                *(
                    path
                    for obligation in reviewed_obligations
                    for path in (*obligation.scope, *obligation.seed_hints)
                ),
            ),
        )
        return ReviewHandoffContext(
            recommendation=state.verdict,
            status=status,
            change_topics=change_topics,
            component_ids=component_ids,
            specialist_topics=specialist_topics,
            recipe_ids=recipe_ids,
            coverage_boundary_topics=coverage_boundary_topics,
            unresolved_thread_count=len(state.notes),
            highest_thread_severity=max(
                prepared_finding_severities, default=None,
                key=lambda value: {"info": 0, "minor": 1, "major": 2, "blocker": 3}.get(value, 0),
            ),
            review_emphasis_topics=review_emphasis_topics,
            material_coverage_limited=bool(
                state.degradations or state.blocking_obligation_ids
            ),
            candidate_retention_limited=self._candidate_retention_limited(state),
            degraded_stages=tuple(
                str(item.get("component", ""))
                for item in state.degradations
                if str(item.get("component", "")).strip()
            ),
            source_access_requests=tuple(state.source_requests),
            what_changed=what_changed,
            what_changed_is_validated_overview=bool(overview),
            ai_reviewed=ai_reviewed,
            human_focus=human_focus,
            context_paths=tuple(sorted(context_paths)),
        )

    def _apply_finalizer_proposal(
        self,
        state: _RunState,
        base: ReviewHandoffContext,
        proposal: FinalizerProposal,
    ) -> ReviewHandoffContext:
        allowed_components = set(base.component_ids)
        allowed_recipes = set(base.recipe_ids)

        def topics(
            values: Iterable[ReviewOrientationTopic],
            allowed: Iterable[ReviewOrientationTopic],
        ) -> tuple[ReviewOrientationTopic, ...]:
            allowed_topics = set(allowed)
            return tuple(sorted(
                {item for item in values if item in allowed_topics},
                key=lambda item: item.value,
            ))

        def selected_summaries(
            proposed: Iterable[str],
            allowed: tuple[str, ...],
            *,
            minimum: int = 0,
        ) -> tuple[str, ...]:
            selected = tuple(
                dict.fromkeys(item for item in proposed if item in set(allowed))
            )[:5]
            return selected if len(selected) >= minimum else allowed[:5]

        selected = replace(
            base,
            change_topics=topics(proposal.change_topics, base.change_topics),
            component_ids=tuple(sorted(set(proposal.component_ids) & allowed_components)),
            specialist_topics=topics(
                proposal.specialist_topics,
                base.specialist_topics,
            ),
            recipe_ids=tuple(sorted(set(proposal.recipe_ids) & allowed_recipes)),
            coverage_boundary_topics=topics(
                proposal.coverage_boundary_topics,
                base.coverage_boundary_topics,
            ),
            review_emphasis_topics=topics(
                proposal.review_emphasis_topics,
                base.review_emphasis_topics,
            ),
            what_changed=selected_summaries(
                proposal.what_changed,
                base.what_changed,
                minimum=min(2, len(base.what_changed)),
            ),
            ai_reviewed=selected_summaries(
                proposal.ai_reviewed, base.ai_reviewed,
                minimum=min(1, len(base.ai_reviewed)),
            ),
        )
        if not state.degradations:
            return selected
        return replace(
            selected,
            change_topics=tuple(sorted(
                {*base.change_topics, *selected.change_topics},
                key=lambda item: item.value,
            )),
            component_ids=tuple(sorted({
                *base.component_ids, *selected.component_ids,
            })),
            specialist_topics=tuple(sorted(
                {*base.specialist_topics, *selected.specialist_topics},
                key=lambda item: item.value,
            )),
            recipe_ids=tuple(sorted({
                *base.recipe_ids, *selected.recipe_ids,
            })),
            coverage_boundary_topics=tuple(sorted(
                {
                    *base.coverage_boundary_topics,
                    *selected.coverage_boundary_topics,
                },
                key=lambda item: item.value,
            )),
            review_emphasis_topics=tuple(dict.fromkeys((
                *selected.review_emphasis_topics,
                *base.review_emphasis_topics,
            )))[:3],
        )

    def _apply_handoff_summary_proposal(
        self,
        state: _RunState,
        base: ReviewHandoffContext,
        proposal: HandoffSummaryProposal,
    ) -> ReviewHandoffContext:
        """Admit concise prose only when every declared authority reference exists."""
        changed_paths = {
            _normalize_repository_path(path)
            for path in state.inputs.changed_files
            if _normalize_repository_path(path)
        }
        component_ids = set(base.component_ids)
        assert state.coverage is not None
        coverage = state.coverage.snapshot()
        covered_ids = {
            obligation_id
            for obligation_id, status in coverage.obligation_statuses
            if status is ObligationStatus.COVERED
        }
        tracked_paths = {
            _normalize_repository_path(path)
            for path in (*state.inputs.tracked_paths, *state.inputs.changed_files)
            if _normalize_repository_path(path)
        }
        retained_evidence_paths = {
            _normalize_repository_path(record.source_path)
            for record in state.evidence.snapshot().records
            if record.is_usable_for_coverage and record.source_path
        }
        covered_obligation_paths = {
            _normalize_repository_path(path)
            for obligation in state.obligations
            if obligation.id in covered_ids
            for path in (*obligation.scope, *obligation.seed_hints)
            if _normalize_repository_path(path)
        }
        context_paths = _deterministic_context_paths(
            state.inputs.topology,
            tracked_paths,
            extra_paths=(
                *retained_evidence_paths,
                *covered_obligation_paths,
            ),
        )
        allowed_reference_paths = changed_paths | set(context_paths)
        declared_paths = {
            _normalize_repository_path(path)
            for path in proposal.referenced_paths
            if _normalize_repository_path(path)
        }
        if not declared_paths <= allowed_reference_paths:
            raise ValueError("handoff summary references an unchanged path")
        if not set(proposal.referenced_component_ids) <= component_ids:
            raise ValueError("handoff summary references an unknown component")
        if not set(proposal.referenced_obligation_ids) <= covered_ids:
            raise ValueError("handoff summary claims unsupported obligation coverage")

        combined = (
            proposal.ai_reviewed_summary + " " + proposal.human_focus
        )
        prose_paths = {
            _normalize_repository_path(path)
            for path in _prose_path_references(combined, tracked_paths)
        }
        if not prose_paths <= allowed_reference_paths:
            raise ValueError("handoff summary contains an undeclared path")
        # References in otherwise valid prose are orientation metadata.  If a
        # model omitted the redundant reference array, retain the path after
        # the same changed/context allowlist check instead of discarding the
        # entire handoff.
        declared_paths.update(prose_paths)
        direct_context_paths = _direct_change_references(combined, context_paths)
        if direct_context_paths:
            raise ValueError("handoff summary contains a direct change claim for an unchanged path")
        declared_components = {
            item.casefold() for item in proposal.referenced_component_ids
        }
        allowed_components = {item.casefold() for item in component_ids}
        concrete_component_refs = {
            match.group(1).casefold()
            for match in _HANDOFF_COMPONENT_REFERENCE.finditer(combined)
            if match.group(1).casefold() not in {
                "cross", "human", "repository", "runtime", "security",
                "system", "trust",
            }
        }
        if (
            not concrete_component_refs <= declared_components
            or not concrete_component_refs <= allowed_components
        ):
            raise ValueError(
                "handoff summary contains an undeclared or unknown component"
            )

        normalized = " ".join(combined.casefold().split())
        forbidden_judgments = (
            "approve", "request changes", "request_changes", "merge safe",
            "safe to merge", "no further review", "no issues", "fully covered",
            "all obligations", "complete coverage",
        )
        if any(value in normalized for value in forbidden_judgments):
            raise ValueError("handoff summary attempts to change verdict or coverage")
        if re.search(
            r"(?:^|\s)(?:blocker|major|minor|finding|defect)\b|"
            r"(?:^|[\s`])[^`\s]+:\d+(?:\b|`)",
            normalized,
        ):
            raise ValueError("detailed findings belong in review notes")

        # Do not let a candidate claim leak into the sticky handoff even when it
        # happens to use valid changed paths and components.
        detailed_claims = tuple(
            " ".join(str(value).casefold().split())
            for candidate in (
                *state.review.accepted,
                *state.review.unknowns,
                *state.review.verification_requests,
            )
            for value in (
                getattr(candidate, "claim", ""),
                getattr(candidate, "user_visible_consequence", ""),
            )
            if len(" ".join(str(value).split())) >= 16
        )
        if any(claim in normalized for claim in detailed_claims):
            raise ValueError("detailed finding claim belongs in a review note")

        overview = str(state.change_overview.get("overview") or "").strip()
        if not overview:
            raise ValueError("validated change overview is unavailable")
        return replace(
            base,
            what_changed=(overview,),
            ai_reviewed=(proposal.ai_reviewed_summary,),
            review_emphasis_topics=(),
            human_focus=(proposal.human_focus,),
        )

    @staticmethod
    def _minimal_handoff(verdict: str, degraded: bool) -> ReviewHandoff:
        recommendation = {
            "request_changes": "Request changes",
            "approve": "Approve",
        }.get(verdict, "Human review required")
        status = (
            "AI review completed with material coverage limits"
            if degraded else "AI review complete"
        )
        markdown = (
            "## AI Review Handoff\n\n"
            f"**Recommendation:** {recommendation}\n\n"
            f"**Status:** {status}\n\n"
            "These focus suggestions do not reduce responsibility to review the complete change.\n"
        )
        return ReviewHandoff(
            markdown=markdown,
            recommendation=recommendation,
            status=status,
        )

    def _finalize_products(self, state: _RunState) -> None:
        assert state.coverage is not None
        obligation_map = {item.id: item for item in state.obligations}
        statuses = state.coverage.obligation_statuses()
        unresolved = tuple(
            item for item in state.obligations
            if item.mandatory and statuses.get(item.id) is not ObligationStatus.COVERED
        )
        policy_result = apply_runtime_verdict_policy(
            model_verdict=state.inputs.model_verdict,
            unresolved=unresolved,
            allow_approve=state.effective_allow_approve,
            evidence=state.evidence,
            obligations=obligation_map,
            changed_files=state.inputs.changed_files,
            review=state.review,
            policy=state.inputs.policy.verdict_policy,
        )
        state.verdict = policy_result.verdict
        state.verdict_source = policy_result.source
        state.blocking_finding_ids = policy_result.blocking_finding_ids
        state.blocking_obligation_ids = policy_result.blocking_obligation_ids
        state.journal.emit("verdict_selected", {
            "verdict": state.verdict,
            "source": state.verdict_source,
            "blocking_finding_ids": state.blocking_finding_ids,
            "blocking_obligation_ids": state.blocking_obligation_ids,
        })
        try:
            state.retention_verification_requests = (
                self._candidate_retention_verification_requests(state)
            )
            coverage_verification_requests = _coverage_verification_requests(
                state.obligations,
                state.blocking_obligation_ids,
            )
            state.coverage_verification_requests = coverage_verification_requests
            state.notes = build_review_notes(
                state.review, state.evidence, state.effective_publishing_mode,
                obligations=obligation_map, changed_files=state.inputs.changed_files,
                verification_requests=(
                    *state.inputs.verification_requests,
                    *state.retention_verification_requests,
                ),
                source_access_requests=state.source_requests,
            )
        except Exception as exc:
            self._degrade(state, "review_notes", _bounded_error(exc))
            state.notes = ()
        status = self._review_status(state)
        context = self._handoff_context(state, status)
        # Keep the controller-owned prose as the safe fallback for degraded
        # runs.  A finalizer response can be structurally valid while still
        # collapsing into a file/method inventory; that is precisely the
        # situation the deterministic handoff is meant to cover.  The model
        # may still contribute authorized topics below, but it must not replace
        # the human-facing behavioral overview or the explicit recheck focus.
        deterministic_context = context
        if self.finalizer is not None and state.deadline.remaining(now=self.clock()) > 0:
            try:
                coverage_snapshot = state.coverage.snapshot()
                covered_obligation_ids = tuple(
                    obligation_id
                    for obligation_id, obligation_status
                    in coverage_snapshot.obligation_statuses
                    if obligation_status is ObligationStatus.COVERED
                )
                proposed = self._model_request(
                    state,
                    role="finalizer",
                    request_id="finalizer:1",
                    phase=RunPhase.FINALIZATION,
                    component=self.finalizer,
                    method="finalize",
                    context={
                        "review": state.review,
                        "coverage": state.coverage.snapshot(),
                        "verdict": state.verdict,
                        "verdict_source": state.verdict_source,
                        "unknowns": tuple(state.unknowns),
                        "policy": state.inputs.policy,
                        "pr_metadata": state.inputs.pr_metadata,
                        "handoff_summary_candidates": {
                            "what_changed": context.what_changed,
                            "ai_reviewed": context.ai_reviewed,
                        },
                        "change_overview": change_overview_orientation(
                            state.change_overview,
                        ),
                        "successful_review_facts": {
                            "covered_obligation_ids": covered_obligation_ids,
                            "evidence_paths": tuple(sorted({
                                record.source_path
                                for record in state.evidence.snapshot().records
                                if record.is_usable_for_coverage
                                and record.source_path
                            })),
                            "specialist_scopes": tuple(
                                tuple(assignment.boundary_paths)
                                for assignment in state.assignments.values()
                                if assignment.id in state.assignment_sessions
                            ),
                            "note_themes": tuple(sorted({
                                note.severity for note in state.notes
                                if note.severity
                            })),
                            "degraded_stages": context.degraded_stages,
                        },
                    },
                )
                if isinstance(proposed, Mapping) and {
                    "ai_reviewed_summary", "human_focus",
                } <= set(proposed):
                    summary = _handoff_summary_proposal(proposed)
                    context = self._apply_handoff_summary_proposal(
                        state, context, summary,
                    )
                    state.journal.emit("handoff_summary_applied", {
                        "referenced_paths": summary.referenced_paths,
                        "referenced_component_ids": (
                            summary.referenced_component_ids
                        ),
                        "referenced_obligation_ids": (
                            summary.referenced_obligation_ids
                        ),
                        "change_topics": tuple(
                            item.value for item in context.change_topics
                        ),
                        "component_ids": context.component_ids,
                        "specialist_topics": tuple(
                            item.value for item in context.specialist_topics
                        ),
                        "recipe_ids": context.recipe_ids,
                        "coverage_boundary_topics": tuple(
                            item.value for item in context.coverage_boundary_topics
                        ),
                        "review_emphasis_topics": (),
                        "what_changed": context.what_changed,
                        "what_changed_is_validated_overview": True,
                        "ai_reviewed": context.ai_reviewed,
                        "human_focus": context.human_focus,
                    })
                else:
                    # Typed in-process integrations using the pre-v2 selector
                    # remain readable; production adapters request authored prose.
                    proposal = _finalizer_proposal(proposed)
                    context = self._apply_finalizer_proposal(
                        state, context, proposal,
                    )
                    state.journal.emit("finalizer_proposal_applied", {
                        "recommendation": proposal.recommendation,
                        "change_topics": tuple(
                            item.value for item in context.change_topics
                        ),
                        "component_ids": context.component_ids,
                        "specialist_topics": tuple(
                            item.value for item in context.specialist_topics
                        ),
                        "recipe_ids": context.recipe_ids,
                        "coverage_boundary_topics": tuple(
                            item.value for item in context.coverage_boundary_topics
                        ),
                        "review_emphasis_topics": tuple(
                            item.value for item in context.review_emphasis_topics
                        ),
                        "what_changed": context.what_changed,
                        "what_changed_is_validated_overview": (
                            context.what_changed_is_validated_overview
                        ),
                        "ai_reviewed": context.ai_reviewed,
                    })
            except Exception as exc:
                state.journal.emit("recovery", {
                    "component": "handoff_summarizer",
                    "action": "deterministic-fallback",
                    "reason": _bounded_error(exc),
                })
                context = self._handoff_context(
                    state, self._review_status(state),
                )
        elif self.finalizer is None:
            state.journal.emit("recovery", {
                "component": "handoff_summarizer",
                "action": "deterministic-fallback",
                "reason": "no optional handoff summarizer configured",
            })
            context = self._handoff_context(
                state, self._review_status(state),
            )
        else:
            state.journal.emit("recovery", {
                "component": "handoff_summarizer",
                "action": "deterministic-fallback",
                "reason": "handoff summarizer skipped after absolute deadline",
            })
            context = self._handoff_context(
                state, self._review_status(state),
            )
        # Recompute after the optional finalizer: invoking it may itself have
        # degraded the run, in which case the degraded handoff must include
        # the broader behavioral fallback and its coverage warning.
        deterministic_context = self._handoff_context(
            state, self._review_status(state),
        )
        if state.degradations:
            context = replace(
                context,
                what_changed=deterministic_context.what_changed,
                what_changed_is_validated_overview=(
                    deterministic_context.what_changed_is_validated_overview
                ),
                ai_reviewed=deterministic_context.ai_reviewed,
                human_focus=deterministic_context.human_focus,
            )
            state.journal.emit("handoff_summary_guarded", {
                "reason": "degraded-run-uses-controller-behavioral-prose",
            })
        try:
            state.handoff = build_review_handoff(
                context, review=state.review, evidence=state.evidence,
                obligations=obligation_map, changed_files=state.inputs.changed_files,
            )
        except Exception as exc:
            state.journal.emit("recovery", {
                "component": "handoff_summarizer",
                "action": "deterministic-fallback",
                "reason": _bounded_error(exc),
            })
            state.handoff = self._minimal_handoff(
                state.verdict, bool(state.degradations),
            )

    def _phase_allocations(self, state: _RunState) -> list[dict[str, object]]:
        shares = state.inputs.config.phase_shares
        percentages = {
            "precheck": 0, "planning": shares.planning, "initial": shares.initial,
            "followup": shares.followup, "finalization": shares.finalization,
            "publish_ready": 0, "complete": 0,
        }
        return [
            {
                "phase": phase,
                "status": state.phase_outcomes.get(phase, "not_started"),
                "allocated_percent": percentages[phase],
                "allocated_seconds": round(
                    state.inputs.config.review_deadline_sec * percentages[phase] / 100, 6,
                ),
            }
            for phase in _PHASES
        ]

    def _artifact(self, state: _RunState, path: Path | None) -> dict[str, object]:
        assert state.coverage is not None
        coverage = state.coverage.snapshot()
        statuses = dict(coverage.obligation_statuses)
        evidence_by_obligation = dict(coverage.evidence_by_obligation)
        recipe_states = state.coverage.recipe_statuses()
        for recipe in state.inputs.policy.recipes:
            recipe_states.setdefault(recipe.id, "not_applicable")

        def projected_status(item: CoverageObligation) -> str:
            if item.id in statuses:
                return statuses[item.id].value
            if item.origin == "recipe-accounting" and item.recipe_id:
                return ObligationStatus(recipe_states[item.recipe_id]).value
            return statuses[item.id].value

        unique_sessions: dict[str, object] = {}
        for result in state.session_results.values():
            unique_sessions[result.session_id] = result
        sessions = []
        for session_id in sorted(set(state.ownership).union(unique_sessions)):
            ownership = state.ownership.get(session_id)
            result = unique_sessions.get(session_id)
            sessions.append({
                "session_id": session_id,
                "assignment_id": ownership.assignment_id if ownership else None,
                "ownership": _json_value(ownership) if ownership else None,
                "state": result.state.value if result else "failed_or_not_started",
                "checkpoint": _checkpoint_projection(result.checkpoint) if result else None,
                "budget": _budget_projection(result.budget) if result else _budget_projection(BudgetUsage()),
                "degraded": bool(result.degraded) if result else True,
                "finalization_diagnostics": (
                    _json_value(getattr(result, "finalization_diagnostics", ()))
                    if result else []
                ),
            })
        session_budgets = {
            item["session_id"]: item["budget"] for item in sessions
        }
        for session_id, usage in state.failed_session_budgets.items():
            session_budgets.setdefault(session_id, _budget_projection(usage))
        for session_id, request_ids in state.request_attempt_ids_by_session.items():
            charged_turns = len(request_ids)
            existing = session_budgets.get(session_id)
            if existing is None:
                session_budgets[session_id] = _budget_projection(BudgetUsage(
                    model_turns=charged_turns,
                ))
            elif int(existing.get("model_turns", 0)) < charged_turns:
                existing = dict(existing)
                existing["model_turns"] = charged_turns
                session_budgets[session_id] = existing
        evidence = [
            _evidence_projection(record)
            for record in state.evidence.snapshot().records
        ]
        journal_events = state.journal.snapshot()
        events = [
            {
                "sequence": event.sequence,
                "kind": event.kind,
                "payload": _json_value(event.payload),
            }
            for event in journal_events
        ]
        artifacts_events = [
            {"sequence": event["sequence"], "kind": event["kind"]}
            for event in events
        ]
        policy_digest = _digest(state.inputs.policy)
        config_digest = _digest({
            "runtime": state.inputs.config,
            "adapter": state.inputs.adapter_configuration,
        })
        run_id = _artifact_id(state.inputs)
        artifact: dict[str, object] = {
            "accepted_candidates": [_json_value(item) for item in state.review.accepted],
            "candidate_dispositions": [
                _json_value(item) for item in state.review.dispositions
            ] + list(state.collision_dispositions),
            "artifact_id": run_id,
            "artifact_write": {
                "status": "ready" if path is not None else "failed",
                **({"path": path.name} if path is not None else {
                    "error": "artifact output path rejected",
                }),
            },
            "assignment_plan": {
                "source": state.plan_source,
                "planner_repaired": state.planner_repaired,
                "ignored_transformations": list(state.planner_diagnostics),
                "unassigned_obligation_ids": list(state.plan.unassigned_obligation_ids),
                "unassigned_obligation_reasons": dict(
                    state.plan.unassigned_obligation_reasons
                ),
            },
            "change_overview": _json_value(state.change_overview),
            "assignments": [_json_value(state.assignments[key]) for key in sorted(state.assignments)],
            "base_sha": state.inputs.base_sha,
            "budgets": {
                "sessions": session_budgets,
                "request_attempts": [
                    _json_value(state.request_attempts[key])
                    for key in sorted(state.request_attempts)
                ],
                "totals": {
                    "model_turns": sum(
                        item["model_turns"] for item in session_budgets.values()
                    ),
                    "tool_calls": sum(
                        item["tool_calls"] for item in session_budgets.values()
                    ),
                    "recoveries": sum(
                        item["recoveries"] for item in session_budgets.values()
                    ),
                    "controller_model_requests": sum(
                        1 for event in state.journal.snapshot()
                        if event.kind == "model_request_started"
                    ),
                    "controller_model_completed": sum(
                        1 for event in journal_events
                        if event.kind == "model_request_completed"
                    ),
                    "controller_model_failed": sum(
                        1 for event in journal_events
                        if event.kind == "model_request_failed"
                    ),
                    "controller_model_timed_out": sum(
                        1 for event in journal_events
                        if event.kind == "model_request_timed_out"
                    ),
                    "specialist_hook_attempts": sum(
                        1 for event in journal_events
                        if event.kind == "specialist_hook_started"
                    ),
                    "specialist_hook_failures": sum(
                        1 for event in journal_events
                        if event.kind in {
                            "specialist_hook_failed", "specialist_hook_timed_out",
                        }
                    ),
                    "specialist_model_requests": sum(
                        1 for event in journal_events
                        if event.kind == "specialist_request_started"
                    ),
                    "specialist_model_completed": sum(
                        1 for event in journal_events
                        if event.kind == "specialist_request_completed"
                    ),
                    "specialist_model_failed": sum(
                        1 for event in journal_events
                        if event.kind == "specialist_request_failed"
                    ),
                    "specialist_model_timed_out": sum(
                        1 for event in journal_events
                        if event.kind == "specialist_request_timed_out"
                    ),
                    "specialist_model_cutoff": sum(
                        1 for event in journal_events
                        if event.kind
                        == "specialist_request_timed_out_at_phase_cutoff"
                    ),
                },
            },
            "coverage": {
                item.id: {
                    "status": projected_status(item),
                    "mandatory": item.mandatory,
                    "risk_tier": item.risk_tier,
                    "unresolved_policy": item.unresolved_policy,
                    "recipe_id": item.recipe_id,
                    "origin": item.origin,
                    "subject": item.subject,
                    "explanation": item.explanation,
                    "scope": list(item.scope),
                    "satisfaction_predicates": list(item.satisfaction_predicates),
                    "requires_independent_verification": item.requires_independent_verification,
                    "required_evidence_categories": list(item.required_evidence_categories),
                    "evidence_ids": list(evidence_by_obligation.get(item.id, ())),
                }
                for item in state.obligations
            },
            "configuration": {
                "runtime": _json_value(state.inputs.config),
                "adapter": _json_value(state.inputs.adapter_configuration),
            },
            "degradation": list(state.degradations),
            "evaluation_status": self._review_status(state),
            "event_references": artifacts_events,
            "events": events,
            "event_journal": {
                "count": len(events),
                "digest": _digest(events),
            },
            "evidence": evidence,
            "handoff": _json_value(state.handoff),
            "head_sha": state.inputs.head_sha,
            "notes": [
                {
                    "kind": item.kind.value,
                    "fingerprint": item.fingerprint,
                    "related_obligation_ids": list(item.related_obligation_ids),
                    "evidence_ids": list(item.evidence_ids),
                }
                for item in state.notes
            ],
            "coverage_verification_requests": _json_value(
                state.coverage_verification_requests
            ),
            "retention_verification_requests": _json_value(
                state.retention_verification_requests
            ),
            "phases": self._phase_allocations(state),
            "policy": {
                "version": state.inputs.policy.version,
                "digest": policy_digest,
                "config_digest": config_digest,
                "authorization": _json_value(
                    state.inputs.pr_metadata.get(
                        "policy_authorization", {},
                    )
                ),
            },
            "pr_number": state.inputs.pr_number,
            "publishing": {
                "ready": state.publishing_ready,
                "status": "not_published",
                "mode": state.effective_publishing_mode,
                "allow_approve": state.effective_allow_approve,
                "required_note_count": state.required_note_count,
                "built_note_count": len(state.notes),
            },
            "recipes": {
                key: {"status": value} for key, value in sorted(recipe_states.items())
            },
            "rejected_candidates": [_json_value(item) for item in state.review.rejected],
            "candidate_unknowns": [
                {
                    "candidate_id": item.candidate_id,
                    "related_obligation_ids": list(item.related_obligation_ids),
                    "supporting_evidence_ids": list(item.supporting_evidence_ids),
                    "contradicting_evidence_ids": list(item.contradicting_evidence_ids),
                }
                for item in state.review.unknowns
            ],
            "repository": state.inputs.repository,
            "schema_version": _SCHEMA_VERSION,
            "sessions": sessions,
            "source_access_requests": [_json_value(item) for item in state.source_requests],
            "timing": {
                "deadline_seconds": state.inputs.config.review_deadline_sec,
                "phase_shares": _json_value(state.inputs.config.phase_shares),
                "finalization_reserve_seconds": round(
                    state.inputs.config.review_deadline_sec
                    * state.inputs.config.phase_shares.finalization / 100,
                    6,
                ),
            },
            "unknowns": sorted(
                state.unknowns,
                key=lambda item: (str(item.get("obligation_id", "")), str(item.get("reason", ""))),
            ),
            "verdict": {
                "value": state.verdict,
                "source": state.verdict_source,
                "blocking_finding_ids": list(state.blocking_finding_ids),
                "blocking_obligation_ids": list(state.blocking_obligation_ids),
            },
        }
        projected = _artifact_value(artifact)
        if not isinstance(projected, dict):
            raise TypeError("artifact projection must be an object")
        return projected

    @staticmethod
    def _validate_artifact(artifact: Mapping[str, object]) -> None:
        required = {
            "schema_version", "repository", "pr_number", "base_sha", "head_sha",
            "policy", "phases", "assignments", "sessions", "budgets", "evidence",
            "assignment_plan",
            "coverage", "recipes", "unknowns", "source_access_requests",
            "accepted_candidates", "rejected_candidates", "handoff", "notes",
            "coverage_verification_requests", "retention_verification_requests",
            "candidate_dispositions", "candidate_unknowns",
            "verdict", "degradation", "publishing", "event_references",
            "evaluation_status", "artifact_write", "timing",
            "events", "event_journal",
        }
        missing = sorted(required - set(artifact))
        if missing:
            raise ValueError("terminal artifact missing fields: " + ", ".join(missing))
        if artifact.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("terminal artifact schema version is invalid")
        artifact_id = artifact.get("artifact_id")
        if (
            not isinstance(artifact_id, str)
            or len(artifact_id) != 32
            or any(character not in "0123456789abcdef" for character in artifact_id)
        ):
            raise ValueError("terminal artifact identity is invalid")
        coverage = artifact.get("coverage")
        recipes = artifact.get("recipes")
        terminal_obligation_statuses = {
            "covered", "partially_covered", "unresolved", "not_applicable",
            "suppressed_by_policy",
        }
        terminal_recipe_statuses = terminal_obligation_statuses
        if not isinstance(coverage, Mapping) or any(
            not isinstance(value, Mapping)
            or value.get("status") not in terminal_obligation_statuses
            for value in coverage.values()
        ):
            raise ValueError("every obligation requires a terminal status")
        if not isinstance(recipes, Mapping) or any(
            not isinstance(value, Mapping)
            or value.get("status") not in terminal_recipe_statuses
            for value in recipes.values()
        ):
            raise ValueError("every recipe requires a terminal status")
        events = artifact.get("events")
        event_journal = artifact.get("event_journal")
        event_references = artifact.get("event_references")
        if not isinstance(events, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in events
        ):
            raise ValueError("terminal artifact events must be an array of objects")
        expected_sequences = list(range(1, len(events) + 1))
        if [item.get("sequence") for item in events] != expected_sequences:
            raise ValueError("terminal artifact event sequence is not contiguous")
        if not isinstance(event_journal, Mapping) or (
            event_journal.get("count") != len(events)
            or event_journal.get("digest") != _digest(events)
        ):
            raise ValueError("terminal artifact event journal digest is inconsistent")
        if not isinstance(event_references, (list, tuple)) or list(event_references) != [
            {"sequence": item.get("sequence"), "kind": item.get("kind")}
            for item in events
        ]:
            raise ValueError("terminal artifact event references are inconsistent")
        publishing = artifact.get("publishing")
        phases = artifact.get("phases")
        if not isinstance(publishing, Mapping) or not isinstance(phases, (list, tuple)):
            raise ValueError("terminal artifact publishing state is invalid")
        phase_statuses = {
            item.get("phase"): item.get("status")
            for item in phases if isinstance(item, Mapping)
        }
        if set(phase_statuses) != set(_PHASES) or any(
            status not in {"complete", "incomplete", "degraded", "skipped"}
            for status in phase_statuses.values()
        ):
            raise ValueError("terminal artifact phases require terminal outcomes")
        event_phase_outcomes: dict[object, object] = {}
        for event in events:
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if event.get("kind") == "phase_completed":
                event_phase_outcomes[payload.get("phase")] = payload.get("status")
            elif event.get("kind") == "phase_skipped":
                event_phase_outcomes[payload.get("phase")] = "skipped"
        if event_phase_outcomes and any(
            phase_statuses.get(phase) != status
            for phase, status in event_phase_outcomes.items()
        ):
            raise ValueError("artifact and event phase outcomes are inconsistent")
        publish_phase_status = next((
            item.get("status") for item in phases
            if isinstance(item, Mapping) and item.get("phase") == "publish_ready"
        ), None)
        if publishing.get("ready"):
            if publish_phase_status != "complete":
                raise ValueError("publishing readiness requires a completed publish phase")
            if (
                publishing.get("mode") != "comment"
                and publishing.get("built_note_count")
                != publishing.get("required_note_count")
            ):
                raise ValueError("detailed publishing readiness requires every note")
        json.dumps(
            artifact,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def _finish_after_unexpected(self, state: _RunState, exc: BaseException) -> None:
        self._degrade(state, "controller", _bounded_error(exc))
        if state.phase is not None and state.phase_outcomes.get(state.phase) == "running":
            self._complete_phase(state, status="degraded")
        if state.coverage is not None:
            snapshot = state.coverage.snapshot()
            statuses = dict(snapshot.obligation_statuses)
            for obligation in state.obligations:
                if statuses.get(obligation.id) is ObligationStatus.PENDING:
                    state.coverage.mark_unresolved(obligation.id)
        while state.phase != "complete":
            next_phase = _LEGAL_TRANSITIONS.get(state.phase)
            if next_phase is None:
                break
            if next_phase == "complete":
                self._transition(state, next_phase)
                self._complete_phase(state, status="degraded")
            else:
                self._skip_phase(state, next_phase)

    @staticmethod
    def _last_resort_result(
        identity: _PrimitiveRunIdentity,
        terminal_capture: Mapping[str, object],
        exc: BaseException,
    ) -> ReviewResult:
        error = format_callback_error(exc)
        try:
            obligations = tuple(terminal_capture.get("obligations", ()))
        except BaseException:
            obligations = ()
        try:
            recipes = tuple(terminal_capture.get("recipes", ()))
        except BaseException:
            recipes = ()
        coverage = {
            str(item[0]): {
                "status": str(item[1]),
                "mandatory": bool(item[2]),
                "risk_tier": "unknown",
                "unresolved_policy": "report_unknown",
                "recipe_id": None,
                "origin": "terminal_capture",
                "subject": str(item[0]),
                "explanation": "last-resort terminal projection",
                "scope": [],
                "satisfaction_predicates": [],
                "requires_independent_verification": False,
                "required_evidence_categories": [],
                "evidence_ids": [],
            }
            for item in obligations
            if isinstance(item, tuple) and len(item) == 3
        }
        recipe_projection = {
            str(item[0]): {"status": str(item[1])}
            for item in recipes
            if isinstance(item, tuple) and len(item) == 2
        }
        phases = [
            {
                "phase": phase,
                "status": "degraded" if phase in {"precheck", "complete"} else "skipped",
                "allocated_percent": 0,
                "allocated_seconds": 0.0,
            }
            for phase in _PHASES
        ]
        empty_digest = hashlib.sha256(b"[]").hexdigest()
        artifact = {
            "schema_version": _SCHEMA_VERSION,
            "artifact_id": identity.artifact_id,
            "repository": identity.repository,
            "pr_number": identity.pr_number,
            "base_sha": identity.base_sha,
            "head_sha": identity.head_sha,
            "policy": {
                "version": identity.policy_version,
                "digest": "0" * 64,
                "config_digest": "0" * 64,
            },
            "phases": phases,
            "assignment_plan": {
                "source": "terminal_fallback",
                "planner_repaired": False,
                "unassigned_obligation_ids": list(coverage),
            },
            "assignments": [],
            "sessions": [],
            "budgets": {"sessions": {}, "totals": {
                "model_turns": 0, "tool_calls": 0, "recoveries": 0,
                "controller_model_requests": 0,
            }},
            "evidence": [],
            "coverage": coverage,
            "recipes": recipe_projection,
            "unknowns": [{
                "obligation_id": "",
                "reason": error,
                "resolution_policy": "human_review",
            }],
            "source_access_requests": [],
            "accepted_candidates": [],
            "rejected_candidates": [],
            "candidate_dispositions": [],
            "candidate_unknowns": [],
            "handoff": {
                "markdown": "## AI Review Handoff\n\n**Recommendation:** Human review required\n",
                "recommendation": "Human review required",
                "status": "",
                "change_map": [],
                "reviewed_focuses": [],
                "specialist_focuses": [],
                "recipe_focuses": [],
                "coverage_boundaries": [],
                "thread_status": None,
                "finding_theme": None,
                "review_emphasis": [],
                "coverage_warning": None,
                "access_request_count": 0,
                "access_request_url": None,
                "what_changed": [],
                "ai_reviewed": [],
                "human_focus": [],
            },
            "notes": [],
            "verdict": {
                "value": "notice",
                "source": "controller-terminal-fallback",
                "blocking_finding_ids": [],
                "blocking_obligation_ids": [],
            },
            "degradation": [{"component": "terminal_safety", "reason": error}],
            "publishing": {
                "ready": False,
                "status": "not_published",
                "mode": "comment",
                "allow_approve": False,
                "required_note_count": 0,
                "built_note_count": 0,
            },
            "event_references": [],
            "events": [],
            "event_journal": {"count": 0, "digest": empty_digest},
            "evaluation_status": "degraded",
            "artifact_write": {"status": "failed", "error": error},
            "timing": {
                "deadline_seconds": 0,
                "phase_shares": {},
                "finalization_reserve_seconds": 0,
            },
        }
        try:
            frozen = _freeze_result_value(artifact)
            if not isinstance(frozen, Mapping):
                frozen = artifact
        except BaseException:
            frozen = artifact
        return ReviewResult(
            artifact=frozen,
            handoff=ReviewHandoff(
                markdown=artifact["handoff"]["markdown"],
                recommendation="Human review required",
            ),
            verdict="notice",
            verdict_source="controller-terminal-fallback",
            publishing_ready=False,
            artifact_write_error=error,
        )

    def run(self, inputs: ReviewInputs) -> ReviewResult:
        """Never let a post-identity runtime failure escape the controller."""
        identity = _primitive_run_identity(inputs)
        terminal_capture: dict[str, object] = {"obligations": (), "recipes": ()}
        try:
            return self._run_impl(inputs, terminal_capture)
        except BaseException as exc:
            return self._last_resort_result(identity, terminal_capture, exc)

    def _run_impl(
        self, inputs: ReviewInputs, terminal_capture: dict[str, object],
    ) -> ReviewResult:
        """Run all legal phases and always return a terminal in-memory result."""
        try:
            terminal_capture["recipes"] = tuple(
                (str(recipe.id), "unresolved") for recipe in inputs.policy.recipes
            )
        except BaseException:
            terminal_capture["recipes"] = ()
        journal = EventJournal(self.event_sink)
        initialization_error: BaseException | None = None
        try:
            started_at = self.clock()
            if not isfinite(started_at):
                raise ValueError("controller clock must return a finite value")
        except BaseException as exc:
            started_at = time.monotonic()
            initialization_error = exc
        deadline = RunDeadline(started_at, inputs.config.review_deadline_sec, inputs.config.phase_shares)
        evidence = EvidenceStore()
        # Request-attempt transitions are emitted immediately at the gateway
        # boundary.  The richer specialist request events are still admitted
        # after each wave, but these bounded events make in-flight LLM work
        # visible in the job log instead of appearing only after a wave ends.
        def request_transition(attempt: RequestAttempt) -> None:
            journal.emit(
                f"llm_request_{attempt.status}",
                self._request_attempt_event_payload(attempt),
            )

        state = _RunState(
            inputs=inputs,
            journal=journal,
            deadline=deadline,
            evidence=evidence,
            request_attempt_journal=RequestAttemptJournal(
                self.clock, transition_sink=request_transition,
            ),
        )
        path: Path | None = None
        path_error: str | None = None
        artifact: dict[str, object] = {}
        write_error: str | None = None
        try:
            if initialization_error is not None:
                raise initialization_error
            if self._evidence_seed is not None:
                if (
                    self._evidence_seed.repository != inputs.repository
                    or self._evidence_seed.head_sha != inputs.head_sha
                ):
                    raise ValueError(
                        "evidence seed binding does not match repository/head"
                    )
                state.evidence = EvidenceStore.from_snapshot(
                    self._evidence_seed.snapshot,
                )
            elif self._provided_evidence_store is not None:
                state.evidence = EvidenceStore.from_snapshot(
                    self._provided_evidence_store.snapshot(),
                )
            else:
                state.evidence = self._evidence_store_factory()
                if not isinstance(state.evidence, EvidenceStore):
                    raise TypeError(
                        "evidence_store_factory must return EvidenceStore"
                    )
            journal.emit("run_started", {
                "repository": inputs.repository,
                "pr_number": inputs.pr_number,
                "base_sha": inputs.base_sha,
                "head_sha": inputs.head_sha,
            })
            self._transition(state, "precheck")
            if not inputs.repository.strip() or inputs.pr_number <= 0:
                raise ValueError("repository and positive PR number are required")
            if inputs.publishing_mode not in {"comment", "review_comment", "review_verdict"}:
                raise ValueError("invalid publishing mode")
            (
                state.effective_publishing_mode,
                state.effective_allow_approve,
            ) = self._publishing_authority(inputs)
            for warning in inputs.configuration_warnings:
                self._degrade(state, "configuration", warning)
            try:
                path = _resolve_artifact_path(
                    self.artifact_output_root, inputs.artifact_path,
                )
            except BaseException as exc:
                path_error = _bounded_error(exc)
                self._degrade(state, "artifact_output_path", path_error)
            self._complete_phase(state)
            self._transition(state, "planning")
            state.obligations = self.obligation_deriver(
                inputs.topology, inputs.classification, inputs.policy,
            )
            terminal_capture["obligations"] = tuple(
                (
                    str(item.id),
                    "unresolved" if item.mandatory else "not_applicable",
                    bool(item.mandatory),
                )
                for item in state.obligations
            )
            state.coverage = CoverageLedger(state.obligations)
            state.change_overview = self._summarize_changes(state)
            state.plan = self._plan(state)
            state.assignments = {
                item.id: item for item in state.plan.assignments
            }
            for item in state.plan.assignments:
                journal.emit("assignment_decision", {
                    "assignment_id": item.id,
                    "obligation_ids": item.obligation_ids,
                    "source": state.plan_source,
                })
            unassigned_reasons = dict(state.plan.unassigned_obligation_reasons)
            for obligation_id in state.plan.unassigned_obligation_ids:
                state.coverage.mark_unresolved(obligation_id)
                obligation = state.coverage.obligation(obligation_id)
                state.unknowns.append({
                    "obligation_id": obligation_id,
                    "reason": unassigned_reasons.get(
                        obligation_id,
                        "deterministic assignment capacity exhausted",
                    ),
                    "resolution_policy": obligation.unresolved_policy,
                })

            self._complete_phase(state)
            self._transition(state, "initial")
            initial, initial_snapshot = self._run_wave(
                state, state.plan.assignments, RunPhase.INITIAL,
            )
            reconciliation = self._reconcile(state, initial, initial_snapshot)

            self._complete_phase(state)
            self._transition(state, "followup")
            followup_lease = state.deadline.lease_for(RunPhase.FOLLOWUP)
            for session_id in sorted(state.sessions):
                self._session_hook(
                    state, session_id, "update_lease", RunPhase.FOLLOWUP,
                    followup_lease,
                )
            # Negotiation is deliberately one-action-at-a-time.  Recompute the
            # authoritative state after each bounded wave so the next proposal
            # cannot rely on stale obligations, leases, or coverage gains.
            max_rounds = max(
                1,
                state.inputs.config.max_followup_sessions
                + len(state.obligations),
            )
            for round_index in range(max_rounds):
                if not reconciliation.uncovered_obligation_ids:
                    break
                before_uncovered = set(reconciliation.uncovered_obligation_ids)
                before_covered = {
                    obligation_id
                    for obligation_id, status in reconciliation.snapshot.obligation_statuses
                    if status is ObligationStatus.COVERED
                }
                before_evidence = frozenset(state.evidence.snapshot().evidence_ids)
                journal.emit("negotiation_round", {
                    "round": round_index + 1,
                    "uncovered_count": len(before_uncovered),
                })
                actions = self._negotiate(
                    state, reconciliation, attempt=round_index + 1,
                )
                if not actions:
                    break
                followups = self._followup_assignments(state, actions)
                followup, followup_snapshot = self._run_wave(
                    state, followups, RunPhase.FOLLOWUP,
                )
                next_reconciliation = self._reconcile(
                    state, followup, followup_snapshot,
                )
                # record_unknown and infeasible proposals intentionally make no
                # coverage progress; stop rather than repeatedly asking the model
                # to emit the same action. Evidence collection itself counts as
                # progress even when it has not yet satisfied an obligation.
                reconciliation = next_reconciliation
                after_covered = {
                    obligation_id
                    for obligation_id, status in reconciliation.snapshot.obligation_statuses
                    if status is ObligationStatus.COVERED
                }
                after_evidence = frozenset(state.evidence.snapshot().evidence_ids)
                made_progress = (
                    bool(after_covered - before_covered)
                    or before_evidence != after_evidence
                )
                if not made_progress:
                    break
            for obligation_id in reconciliation.uncovered_obligation_ids:
                if not any(item.get("obligation_id") == obligation_id for item in state.unknowns):
                    obligation = state.coverage.obligation(obligation_id)
                    state.coverage.mark_unresolved(obligation_id)
                    state.unknowns.append({
                        "obligation_id": obligation_id,
                        "reason": "mandatory coverage remained unresolved after bounded follow-up",
                        "resolution_policy": obligation.unresolved_policy,
                    })

            self._complete_phase(state)
            self._transition(state, "finalization")
            for key in sorted(state.sessions):
                session = state.sessions[key]
                if state.deadline.remaining(now=self.clock()) <= 0:
                    self._degrade(
                        state, "deadline",
                        "absolute deadline reached; specialist finalization used retained checkpoint",
                    )
                    break
                updated, _ = self._session_hook(
                    state,
                    key,
                    "update_lease",
                    RunPhase.FINALIZATION,
                    state.deadline.lease_for(RunPhase.FINALIZATION),
                )
                if not updated:
                    continue
                succeeded, result = self._session_hook(
                    state, key, "finalize", RunPhase.FINALIZATION,
                )
                if succeeded and result is not None:
                    state.session_results[(session.assignment.id, result.session_id)] = result
                    self._promote_degraded_session_result(
                        state, session.assignment.id, result,
                    )
                    self._admit_specialist_request_events(state, result)
                    checkpoint = getattr(result, "checkpoint", None)
                    report = getattr(result, "report", None)
                    journal.emit("specialist_finalized", {
                        "assignment_id": session.assignment.id,
                        "session_id": result.session_id,
                        "source": (
                            report.get("source")
                            if isinstance(report, Mapping) else "unknown"
                        ),
                        "degraded": bool(getattr(result, "degraded", False)),
                        "candidate_count": len(tuple(
                            getattr(session, "candidate_findings", ())
                        )),
                        "evidence_count": len(tuple(
                            getattr(checkpoint, "evidence_ids", ())
                        )),
                        "retention_unknown": bool(
                            checkpoint is not None
                            and _CANDIDATE_RETENTION_UNKNOWN in tuple(
                                getattr(checkpoint, "unknowns", ())
                            )
                        ),
                    })
                    diagnostics = tuple(getattr(
                        result, "finalization_diagnostics", (),
                    ))
                    if diagnostics:
                        journal.emit("specialist_checkpoint_diagnostics", {
                            "assignment_id": session.assignment.id,
                            "session_id": result.session_id,
                            "diagnostics": _json_value(diagnostics),
                        })
                    journal.emit("session_transition", {
                        "session_id": result.session_id,
                        "state": result.state.value,
                    })
                    journal.emit("budget_changed", {
                        "session_id": result.session_id,
                        "usage": _budget_projection(result.budget),
                    })
                    if state.deadline.remaining(now=self.clock()) <= 0:
                        self._degrade(
                            state, "deadline",
                            "specialist finalization completed at the absolute deadline",
                        )
                        break
            # A checkpoint can be marked degraded while the worker is still
            # recoverable.  Only promote the final retained result (or a
            # session that could not be finalized) to the run-level status;
            # otherwise a transient initial flag permanently poisons an
            # otherwise successful finalization.
            for (assignment_id, _session_id), result in tuple(
                state.session_results.items()
            ):
                self._promote_degraded_session_result(state, assignment_id, result)
            candidates = self._collect_candidates(state)
            self._adjudicate(state, candidates)
            state.source_requests.extend(inputs.source_access_requests)
            state.source_requests = list({
                (
                    item.obligation_id,
                    item.host,
                    item.candidate_url,
                    item.purpose,
                    item.authority_reason,
                ): item
                for item in state.source_requests
            }.values())
            state.source_requests.sort(
                key=lambda item: (
                    item.obligation_id, item.host, item.candidate_url, item.purpose,
                ),
            )
            for request in state.source_requests:
                journal.emit("source_access_request", {
                    "fingerprint": _digest(request.as_dict())[:32],
                    "host": request.host,
                    "obligation_id": request.obligation_id,
                })
            self._finalize_products(state)

            self._complete_phase(state)
            state.required_note_count = self._required_note_count(state)
            state.publishing_ready = self._publication_is_ready(state, path)
            self._transition(state, "publish_ready")
            if state.publishing_ready:
                journal.emit("publishing_ready", {
                    "handoff": bool(state.handoff.markdown),
                    "note_ids": tuple(item.fingerprint for item in state.notes),
                    "verdict": state.verdict,
                    "verdict_source": state.verdict_source,
                })
            else:
                journal.emit("publishing_blocked", {
                    "mode": state.effective_publishing_mode,
                    "required_note_count": state.required_note_count,
                    "built_note_count": len(state.notes),
                })
            self._complete_phase(
                state,
                status="complete" if state.publishing_ready else "degraded",
            )
            self._transition(state, "complete")
            self._complete_phase(
                state,
                status=self._review_status(state),
            )
        except BaseException as exc:  # terminal artifact survives every controlled failure
            self._finish_after_unexpected(state, exc)
            if state.coverage is None:
                state.coverage = CoverageLedger(())
            if not state.handoff.markdown:
                state.handoff = self._minimal_handoff(state.verdict, True)
            state.publishing_ready = False

        # Waves close their own request-journal slices at each phase boundary.
        # This final sweep also accounts for requests launched by finalization
        # hooks, including callbacks that remain orphaned past their cutoff.
        self._admit_wave_request_attempts(
            state, state.request_attempt_journal.close_since(0),
        )

        artifact_identity = _artifact_id(inputs)
        journal.emit("artifact_reference", {
            "artifact_id": artifact_identity,
            "filename": path.name if path is not None else "specialist-review-artifact.json",
        })
        try:
            artifact = self._artifact(state, path)
            self._validate_artifact(artifact)
        except BaseException as exc:
            self._degrade(state, "artifact_projection", _bounded_error(exc))
            state.publishing_ready = False
            emergency_results = {
                result.session_id: result for result in state.session_results.values()
            }
            emergency_sessions = [
                {
                    "session_id": session_id,
                    "assignment_id": (
                        state.ownership[session_id].assignment_id
                        if session_id in state.ownership else None
                    ),
                    "ownership": _json_value(state.ownership[session_id])
                    if session_id in state.ownership else None,
                    "state": emergency_results[session_id].state.value,
                    "checkpoint": _checkpoint_projection(
                        emergency_results[session_id].checkpoint,
                    ),
                    "budget": _budget_projection(emergency_results[session_id].budget),
                    "degraded": bool(emergency_results[session_id].degraded),
                    "finalization_diagnostics": _json_value(getattr(
                        emergency_results[session_id],
                        "finalization_diagnostics",
                        (),
                    )),
                }
                for session_id in sorted(emergency_results)
            ]
            emergency_budget_map = {
                item["session_id"]: item["budget"] for item in emergency_sessions
            }
            emergency_budget_map.update({
                session_id: _budget_projection(usage)
                for session_id, usage in state.failed_session_budgets.items()
            })
            for session_id, request_ids in state.request_attempt_ids_by_session.items():
                charged_turns = len(request_ids)
                existing = emergency_budget_map.get(session_id)
                if existing is None:
                    emergency_budget_map[session_id] = _budget_projection(
                        BudgetUsage(model_turns=charged_turns),
                    )
                elif int(existing.get("model_turns", 0)) < charged_turns:
                    existing = dict(existing)
                    existing["model_turns"] = charged_turns
                    emergency_budget_map[session_id] = existing
            emergency_coverage = state.coverage.snapshot()
            emergency_statuses = dict(emergency_coverage.obligation_statuses)
            emergency_evidence = dict(emergency_coverage.evidence_by_obligation)
            artifact = {
                "schema_version": _SCHEMA_VERSION,
                "artifact_id": artifact_identity,
                "repository": inputs.repository,
                "pr_number": inputs.pr_number,
                "base_sha": inputs.base_sha,
                "head_sha": inputs.head_sha,
                "policy": {"version": inputs.policy.version, "digest": _digest(inputs.policy), "config_digest": _digest(inputs.config)},
                "phases": self._phase_allocations(state),
                "assignment_plan": {
                    "source": state.plan_source,
                    "planner_repaired": state.planner_repaired,
                    "ignored_transformations": list(state.planner_diagnostics),
                    "unassigned_obligation_ids": list(
                        state.plan.unassigned_obligation_ids,
                    ),
                    "unassigned_obligation_reasons": dict(
                        state.plan.unassigned_obligation_reasons
                    ),
                },
                "assignments": [
                    _json_value(state.assignments[key])
                    for key in sorted(state.assignments)
                ],
                "sessions": emergency_sessions,
                "budgets": {
                    "sessions": emergency_budget_map,
                    "request_attempts": [
                        _json_value(state.request_attempts[key])
                        for key in sorted(state.request_attempts)
                    ],
                    "totals": {
                        "model_turns": sum(
                            item["model_turns"]
                            for item in emergency_budget_map.values()
                        ),
                        "tool_calls": sum(
                            item["tool_calls"]
                            for item in emergency_budget_map.values()
                        ),
                        "recoveries": sum(
                            item["recoveries"]
                            for item in emergency_budget_map.values()
                        ),
                        "controller_model_requests": sum(
                            item.kind == "model_request_started"
                            for item in journal.snapshot()
                        ),
                    },
                },
                "evidence": [
                    _evidence_projection(record)
                    for record in state.evidence.snapshot().records
                ],
                "coverage": {
                    item.id: {
                        "status": emergency_statuses.get(
                            item.id, ObligationStatus.UNRESOLVED,
                        ).value,
                        "mandatory": item.mandatory,
                        "risk_tier": item.risk_tier,
                        "unresolved_policy": item.unresolved_policy,
                        "recipe_id": item.recipe_id,
                        "origin": item.origin,
                        "subject": item.subject,
                        "explanation": item.explanation,
                        "scope": list(item.scope),
                        "satisfaction_predicates": list(item.satisfaction_predicates),
                        "requires_independent_verification": item.requires_independent_verification,
                        "required_evidence_categories": list(item.required_evidence_categories),
                        "evidence_ids": list(emergency_evidence.get(item.id, ())),
                    }
                    for item in state.obligations
                },
                "recipes": {
                    item.id: {
                        "status": state.coverage.recipe_statuses().get(
                            item.id, "not_applicable",
                        ),
                    }
                    for item in inputs.policy.recipes
                },
                "unknowns": list(state.unknowns),
                "source_access_requests": [
                    _json_value(item) for item in state.source_requests
                ],
                "accepted_candidates": [
                    _json_value(item) for item in state.review.accepted
                ],
                "rejected_candidates": [
                    _json_value(item) for item in state.review.rejected
                ],
                "candidate_dispositions": [
                    _json_value(item) for item in state.review.dispositions
                ] + list(state.collision_dispositions),
                "candidate_unknowns": [
                    _json_value(item) for item in state.review.unknowns
                ],
                "handoff": _json_value(state.handoff),
                "notes": [
                    {
                        "kind": item.kind.value,
                        "fingerprint": item.fingerprint,
                        "related_obligation_ids": list(item.related_obligation_ids),
                        "evidence_ids": list(item.evidence_ids),
                    }
                    for item in state.notes
                ],
                "coverage_verification_requests": _json_value(
                    state.coverage_verification_requests
                ),
                "retention_verification_requests": _json_value(
                    state.retention_verification_requests
                ),
                "verdict": {
                    "value": state.verdict,
                    "source": state.verdict_source,
                    "blocking_finding_ids": list(state.blocking_finding_ids),
                    "blocking_obligation_ids": list(state.blocking_obligation_ids),
                },
                "degradation": list(state.degradations),
                "publishing": {"ready": state.publishing_ready, "status": "not_published"},
                "event_references": [{"sequence": item.sequence, "kind": item.kind} for item in journal.snapshot()],
                "events": [
                    {
                        "sequence": item.sequence,
                        "kind": item.kind,
                        "payload": _json_value(item.payload),
                    }
                    for item in journal.snapshot()
                ],
                "event_journal": {
                    "count": len(journal.snapshot()),
                    "digest": _digest([
                        {
                            "sequence": item.sequence,
                            "kind": item.kind,
                            "payload": _json_value(item.payload),
                        }
                        for item in journal.snapshot()
                    ]),
                },
                "evaluation_status": "degraded",
                "artifact_write": {
                    "status": "ready" if path is not None else "failed",
                    **({"path": path.name} if path is not None else {
                        "error": "artifact output path rejected",
                    }),
                },
                "timing": {"deadline_seconds": inputs.config.review_deadline_sec, "phase_shares": _json_value(inputs.config.phase_shares), "finalization_reserve_seconds": inputs.config.review_deadline_sec * inputs.config.phase_shares.finalization / 100},
            }
            projected = _artifact_value(artifact)
            if not isinstance(projected, dict):
                raise TypeError("emergency artifact projection must be an object")
            artifact = projected
            self._validate_artifact(artifact)
        if path is None:
            write_error = path_error or "artifact output path rejected"
            journal.emit("artifact_write_failed", {
                "filename": "specialist-review-artifact.json", "error": write_error,
            })
        else:
            try:
                checked_path = _resolve_artifact_path(
                    self.artifact_output_root, inputs.artifact_path,
                )
                if checked_path != path:
                    raise ValueError("artifact output path changed before write")
                if _path_identity(self.artifact_output_root) != self._artifact_root_identity:
                    raise ValueError("artifact output root identity changed before write")
                if self._uses_atomic_writer:
                    write_status = _atomic_write_json(
                        path,
                        artifact,
                        directory_fd=self._artifact_root_fd,
                        root_identity=self._artifact_root_identity,
                    )
                else:
                    write_status = self.artifact_writer(path, artifact)
                artifact = dict(artifact)
                artifact["artifact_write"] = {
                    "status": write_status
                    if write_status in {"written", "written_durability_warning"}
                    else "written",
                    "path": path.name,
                }
            except BaseException as exc:
                write_error = _bounded_error(exc)
                journal.emit("artifact_write_failed", {
                    "filename": path.name, "error": write_error,
                })
        if write_error is not None:
            state.publishing_ready = False
            artifact = dict(artifact)
            artifact["artifact_write"] = {"status": "failed", "error": write_error}
            artifact["evaluation_status"] = "degraded"
            artifact["publishing"] = {"ready": False, "status": "not_published"}
            artifact["event_references"] = [
                {"sequence": item.sequence, "kind": item.kind}
                for item in journal.snapshot()
            ]
            artifact["events"] = [
                {
                    "sequence": item.sequence,
                    "kind": item.kind,
                    "payload": _json_value(item.payload),
                }
                for item in journal.snapshot()
            ]
            artifact["event_journal"] = {
                "count": len(artifact["events"]),
                "digest": _digest(artifact["events"]),
            }
            self._validate_artifact(artifact)
        return ReviewResult(
            artifact=_freeze_result_value(artifact),  # type: ignore[arg-type]
            handoff=state.handoff,
            notes=state.notes,
            verdict=state.verdict,
            verdict_source=state.verdict_source,
            events=journal.snapshot(),
            artifact_path=path,
            artifact_write_error=write_error,
            publishing_ready=state.publishing_ready,
        )
