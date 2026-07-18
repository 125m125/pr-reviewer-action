"""Immutable domain values for the specialist review runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class RunPhase(str, Enum):
    PLANNING = "planning"
    INITIAL = "initial"
    FOLLOWUP = "followup"
    FINALIZATION = "finalization"


class SessionState(str, Enum):
    CREATED = "created"
    EXPLORING = "exploring"
    CHECKPOINT = "checkpoint"
    COVERAGE_EVALUATION = "coverage_evaluation"
    RECOVERY = "recovery"
    RECORDED_UNKNOWN = "recorded_unknown"
    FINALIZING = "finalizing"
    COMPLETE = "complete"


class ObligationStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    COVERED = "covered"
    PARTIALLY_COVERED = "partially_covered"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"
    SUPPRESSED_BY_POLICY = "suppressed_by_policy"


class RecipeStatus(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    ASSIGNED = "assigned"
    COVERED = "covered"
    PARTIALLY_COVERED = "partially_covered"
    UNRESOLVED = "unresolved"
    SUPPRESSED_BY_POLICY = "suppressed_by_policy"


class ReviewNoteKind(str, Enum):
    FINDING = "finding"
    VERIFICATION_REQUEST = "verification_request"
    SOURCE_ACCESS_REQUEST = "source_access_request"


@dataclass(frozen=True)
class CoverageObligation:
    obligation_id: str
    origin: str
    subject: str
    required_evidence_categories: tuple[str, ...] = ()
    satisfaction_predicates: tuple[str, ...] = ()
    risk_tier: str = "normal"
    unresolved_policy: str = "record_unknown"
    requires_independent_verification: bool = False
    scope: tuple[str, ...] = ()
    seed_hints: tuple[str, ...] = ()
    explanation: str = ""
    recipe_id: str | None = None
    recipe_execution: str | None = None
    mandatory: bool = True

    @property
    def id(self) -> str:
        """Compatibility alias for the stable obligation identifier."""
        return self.obligation_id

    @property
    def required_evidence(self) -> tuple[str, ...]:
        """Compatibility alias for the required evidence categories."""
        return self.required_evidence_categories


@dataclass(frozen=True)
class SpecialistAssignment:
    assignment_id: str
    objective: str
    primary_obligation_ids: tuple[str, ...] = ()
    independent_obligation_ids: tuple[str, ...] = ()
    analytical_lens: str = ""
    seed_paths: tuple[str, ...] = ()
    permitted_boundaries: tuple[str, ...] = ()
    expected_evidence_categories: tuple[str, ...] = ()
    estimated_effort: int = 0
    priority: int = 0
    overlap_justification: str = ""


@dataclass(frozen=True)
class SessionCheckpoint:
    session_id: str
    state: SessionState
    evidence_ids: tuple[str, ...] = ()
    imported_evidence_ids: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    candidate_finding_ids: tuple[str, ...] = ()
    obligation_statuses: tuple[tuple[str, ObligationStatus], ...] = ()
    invariants_evaluated: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    proposed_next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateFinding:
    candidate_id: str
    root_cause_fingerprint: str
    claim: str
    affected_location: str = ""
    causal_chain: str = ""
    severity: str = "info"
    category: str = ""
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    related_obligation_ids: tuple[str, ...] = ()
    collector_session_id: str = ""
    model_identity: str = ""
    confidence_rationale: str = ""


@dataclass(frozen=True)
class ReviewHandoff:
    markdown: str = ""
    recommendation: str = ""
    change_map: tuple[str, ...] = ()
    reviewed_focuses: tuple[str, ...] = ()
    thread_status: str | None = None
    finding_theme: str | None = None
    review_emphasis: tuple[str, ...] = ()
    coverage_warning: str | None = None
    access_request_count: int = 0
    access_request_url: str | None = None


@dataclass(frozen=True)
class ReviewNote:
    kind: ReviewNoteKind
    fingerprint: str
    markdown: str
    related_obligation_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    file: str | None = None
    line: int | None = None
    severity: str | None = None


@dataclass(frozen=True)
class BudgetLimits:
    model_turns: int
    tool_calls: int
    recoveries: int
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class BudgetUsage:
    model_turns: int = 0
    tool_calls: int = 0
    recoveries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_rejections: int = 0
    no_progress_streak: int = 0
    tool_rejection_reasons: tuple[str, ...] = ()
    no_progress_reset_reasons: tuple[str, ...] = ()
    recovery_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhaseShares:
    planning: int = 10
    initial: int = 60
    followup: int = 20
    finalization: int = 10

    def __post_init__(self) -> None:
        values = (self.planning, self.initial, self.followup, self.finalization)
        if any(not isinstance(value, int) for value in values):
            raise ValueError("phase shares must be whole percentages")
        if any(value < 0 for value in values):
            raise ValueError("phase shares cannot be negative")
        if sum(values) != 100:
            raise ValueError("phase shares must total 100")
        if self.finalization <= 0:
            raise ValueError("finalization share must be positive")
