"""Controller-owned obligation conclusions and bounded attempt history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from .callbacks import mask_runtime_text
from .evidence import EvidenceRecord, EvidenceSnapshot
from .types import CoverageObligation


class ObligationDisposition(str, Enum):
    PENDING = "pending"
    COVERED = "covered"
    NOT_APPLICABLE = "not_applicable"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ObligationAttempt:
    target: str
    disposition: ObligationDisposition
    reason: str
    evidence_ids: tuple[str, ...]
    next_actions: tuple[str, ...]
    action_fingerprint: str
    accepted: bool
    validation_reason: str
    evidence_before_count: int = 0
    evidence_after_count: int = 0

    @property
    def evidence_delta(self) -> int:
        return max(0, self.evidence_after_count - self.evidence_before_count)


@dataclass(frozen=True)
class ObligationAssessment:
    target: str
    obligation_id: str
    disposition: ObligationDisposition = ObligationDisposition.PENDING
    reason: str = ""
    evidence_ids: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    attempts: tuple[ObligationAttempt, ...] = ()


@dataclass(frozen=True)
class AssessmentProposalResult:
    accepted: bool
    target: str
    disposition: ObligationDisposition | None
    reason: str


def _bounded(value: object, limit: int) -> str:
    return " ".join(mask_runtime_text(value, limit=limit).split())[:limit]


def _actions(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        item for value in values
        if (item := _bounded(value, 300))
    ))[:8]


def _fingerprint(actions: tuple[str, ...]) -> str:
    payload = json.dumps(
        [item.casefold() for item in actions], separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


class ObligationAssessmentLedger:
    """Validate model proposals while retaining controller authority."""

    def __init__(
        self,
        *,
        session_id: str,
        obligations: Iterable[CoverageObligation],
        obligation_ids: Iterable[str],
    ) -> None:
        if not str(session_id).strip():
            raise ValueError("session_id must be non-empty")
        by_id = {item.id: item for item in obligations}
        owned = tuple(dict.fromkeys(str(item) for item in obligation_ids))
        # Keep session construction total for compatibility callers that only
        # need the semantic prompt. Production controllers still reconcile
        # against the authoritative CoverageLedger before accepting closure.
        for obligation_id in set(owned) - set(by_id):
            by_id[obligation_id] = CoverageObligation(
                obligation_id=obligation_id,
                origin="assignment",
                subject=obligation_id,
                explanation="Inspect the assigned obligation.",
            )
        self.session_id = str(session_id)
        self._obligations = {item: by_id[item] for item in owned}
        self._handle_to_id = {
            f"O{index}": obligation_id
            for index, obligation_id in enumerate(owned, start=1)
        }
        self._id_to_handle = {
            obligation_id: target
            for target, obligation_id in self._handle_to_id.items()
        }
        self._assessments = {
            target: ObligationAssessment(target, obligation_id)
            for target, obligation_id in self._handle_to_id.items()
        }

    def handles(self) -> tuple[str, ...]:
        return tuple(self._handle_to_id)

    def obligation_id(self, target: str) -> str | None:
        canonical = self.canonical_target(target)
        return self._handle_to_id.get(canonical) if canonical else None

    def canonical_target(self, target: object) -> str | None:
        """Resolve a short handle or an exact obligation ID owned by this session."""
        value = str(target or "").strip()
        if value in self._handle_to_id:
            return value
        return self._id_to_handle.get(value)

    def assessment(self, target: str) -> ObligationAssessment:
        canonical = self.canonical_target(target)
        try:
            return self._assessments[str(canonical)]
        except KeyError as exc:
            raise KeyError(f"unknown obligation target: {target}") from exc

    def assessments(self) -> tuple[ObligationAssessment, ...]:
        return tuple(self._assessments[target] for target in self._handle_to_id)

    def restore(self, values: Iterable[ObligationAssessment]) -> None:
        restored = {item.target: item for item in values}
        if set(restored) != set(self._handle_to_id) or any(
            restored[target].obligation_id != obligation_id
            for target, obligation_id in self._handle_to_id.items()
        ):
            raise ValueError("assessment snapshot differs from controller targets")
        self._assessments = restored

    def open_targets(self) -> tuple[str, ...]:
        return tuple(
            target for target, assessment in self._assessments.items()
            if assessment.disposition in {
                ObligationDisposition.PENDING,
                ObligationDisposition.UNRESOLVED,
            }
        )

    def consume_next_actions(self, obligation_ids: Iterable[str]) -> None:
        selected = set(str(item) for item in obligation_ids)
        for target, assessment in tuple(self._assessments.items()):
            if assessment.obligation_id not in selected or not assessment.next_actions:
                continue
            self._assessments[target] = ObligationAssessment(
                **{**assessment.__dict__, "next_actions": ()}
            )

    def explain(self, target: str) -> dict[str, object]:
        assessment = self.assessment(target)
        target = assessment.target
        obligation = self._obligations[assessment.obligation_id]
        return {
            "target": target,
            "objective": obligation.explanation,
            "risk_tier": obligation.risk_tier,
            "required_evidence": list(obligation.required_evidence_categories),
            "scope": list(obligation.scope),
            "seed_hints": list(obligation.seed_hints),
            "disposition": assessment.disposition.value,
            "last_conclusion": assessment.reason,
            "attempt_count": len(assessment.attempts),
            "next_actions": list(assessment.next_actions),
            "permitted_dispositions": [item.value for item in ObligationDisposition if item is not ObligationDisposition.PENDING],
        }

    def propose(
        self,
        *,
        target: str,
        disposition: str,
        reason: object,
        evidence_ids: Iterable[object],
        next_actions: Iterable[object],
        evidence: EvidenceSnapshot,
        eligible: Callable[[EvidenceRecord, CoverageObligation], bool],
    ) -> AssessmentProposalResult:
        target = self.canonical_target(target) or str(target).strip()
        assessment = self._assessments.get(target)
        if assessment is None:
            return AssessmentProposalResult(False, target, None, "unknown or unowned target")
        try:
            proposed = ObligationDisposition(str(disposition).strip().lower())
        except ValueError:
            return AssessmentProposalResult(False, target, None, "invalid disposition")
        if proposed is ObligationDisposition.PENDING:
            return AssessmentProposalResult(False, target, proposed, "pending cannot be proposed")
        conclusion = _bounded(reason, 600)
        retained_ids = tuple(dict.fromkeys(
            str(item).strip() for item in evidence_ids if str(item).strip()
        ))[:20]
        actions = _actions(next_actions)
        fingerprint = _fingerprint(actions)
        records = {record.id: record for record in evidence.records}
        obligation = self._obligations[assessment.obligation_id]
        error = ""
        if not conclusion:
            error = "a concise reason is required"
        elif any(item not in records for item in retained_ids):
            error = "proposal references unknown retained evidence"
        elif proposed is ObligationDisposition.COVERED and not retained_ids:
            error = "covered requires retained evidence"
        elif proposed is ObligationDisposition.COVERED and not all(
            eligible(records[item], obligation) for item in retained_ids
        ):
            error = "covered requires eligible retained evidence"
        elif proposed is ObligationDisposition.NOT_APPLICABLE and not any(
            records[item].is_usable_for_coverage
            and records[item].source_path in obligation.scope
            for item in retained_ids
        ):
            error = "not_applicable requires changed-state evidence"
        elif proposed is ObligationDisposition.UNRESOLVED and not actions:
            error = "unresolved requires a concrete next action"
        elif proposed is ObligationDisposition.UNRESOLVED and any(
            item.accepted and item.action_fingerprint == fingerprint
            for item in assessment.attempts
        ):
            error = "unresolved requires a novel next action"
        elif proposed is ObligationDisposition.UNRESOLVED and sum(
            item.accepted
            and item.disposition is ObligationDisposition.UNRESOLVED
            for item in assessment.attempts
        ) >= (2 if obligation.risk_tier in {"high", "critical"} else 1):
            error = "unresolved follow-up attempt limit reached"
        attempt = ObligationAttempt(
            target=target, disposition=proposed, reason=conclusion,
            evidence_ids=retained_ids, next_actions=actions,
            action_fingerprint=fingerprint, accepted=not error,
            validation_reason=error or "accepted",
            evidence_before_count=len(assessment.evidence_ids),
            evidence_after_count=len(retained_ids),
        )
        attempts = (*assessment.attempts, attempt)[-12:]
        if error:
            self._assessments[target] = ObligationAssessment(
                **{**assessment.__dict__, "attempts": attempts}
            )
            return AssessmentProposalResult(False, target, proposed, error)
        self._assessments[target] = ObligationAssessment(
            target=target, obligation_id=assessment.obligation_id,
            disposition=proposed, reason=conclusion,
            evidence_ids=retained_ids, next_actions=actions, attempts=attempts,
        )
        return AssessmentProposalResult(True, target, proposed, "accepted")
