"""Continuous checkpoint/resume/recovery state for one specialist."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from pr_reviewer.conversation import Conversation
from pr_reviewer.tool_loop import decode_native_tool_arguments, native_tool_request_key

from .budget import BudgetExhausted, BudgetLedger, SessionLease
from .coverage import CoverageLedger
from .evidence import EvidenceRecord, EvidenceStore
from .model_gateway import ModelGateway, ModelTurnRequest, ModelTurnResult
from .types import (
    BudgetUsage,
    CoverageObligation,
    ObligationStatus,
    RunPhase,
    SessionCheckpoint,
    SessionState,
    SpecialistAssignment,
)


ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]

_CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_by_obligation": {"type": "object"},
        "inspected": {"type": "array", "items": {"type": "string"}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "candidate_finding_ids": {"type": "array", "items": {"type": "string"}},
        "invariants_evaluated": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "proposed_next_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["unresolved"],
    "additionalProperties": False,
}

_FINAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "recommendation": {"type": "string"},
        "candidate_finding_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "recommendation"],
    "additionalProperties": False,
}

_RECOVERY_REASONS = frozenset({
    "repetitive-transcript",
    "polluted-transcript",
    "context-pressure",
    "invalid-provider-history",
    "transport-incompatibility",
})


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _json_object(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str) or not text.strip():
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else candidate
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        decoder = json.JSONDecoder()
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None
    return value if isinstance(value, dict) else None


def _normalized_path(value: object) -> str:
    path = str(value).strip().replace("\\", "/")
    if not path:
        return ""
    normalized = str(PurePosixPath(path))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _evidence_matches_obligation(
    record: EvidenceRecord,
    obligation: CoverageObligation,
) -> bool:
    """Apply deterministic path/category authority to one evidence mapping."""
    if not record.is_usable_for_coverage:
        return False
    scoped_paths = tuple(dict.fromkeys((*obligation.scope, *obligation.seed_hints)))
    if scoped_paths:
        source_path = _normalized_path(record.source_path or "")
        if not source_path:
            return False
        return any(
            source_path == scope_path or source_path.startswith(scope_path + "/")
            for raw_path in scoped_paths
            if (scope_path := _normalized_path(raw_path))
        )
    category = record.category.strip().lower()
    return bool(category) and category in {
        item.strip().lower()
        for item in obligation.required_evidence_categories
        if item.strip()
    }


@dataclass(frozen=True)
class SessionResult:
    """Detached projection of current or completed specialist state."""

    session_id: str
    state: SessionState
    checkpoint: SessionCheckpoint
    budget: BudgetUsage
    report: Mapping[str, Any] | None = None
    degraded: bool = False


class SpecialistSession:
    """Own exactly one conversation and lifetime ledger across follow-ups."""

    def __init__(
        self,
        *,
        session_id: str,
        assignment: SpecialistAssignment,
        conversation: Conversation,
        gateway: ModelGateway,
        execute_tool: ToolExecutor,
        evidence_store: EvidenceStore,
        coverage: CoverageLedger,
        budget: BudgetLedger,
        lease: SessionLease,
        request_timeout_sec: float,
        max_tokens: int,
        stream: bool = False,
        max_no_progress_streak: int = 2,
        max_context_tokens: int = 24_000,
        recovery_evidence_bytes: int = 8_000,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        if request_timeout_sec <= 0 or max_tokens <= 0:
            raise ValueError("request timeout and max tokens must be positive")
        self.session_id = session_id
        self.assignment = assignment
        self.conversation = conversation
        self.gateway = gateway
        self.execute_tool = execute_tool
        self.evidence_store = evidence_store
        self.coverage = coverage
        self.budget = budget
        self.lease = lease
        self.request_timeout_sec = float(request_timeout_sec)
        self.max_tokens = max_tokens
        self.stream = stream
        self.max_no_progress_streak = max_no_progress_streak
        self.max_context_tokens = max_context_tokens
        self.recovery_evidence_bytes = recovery_evidence_bytes
        self.state = SessionState.CREATED
        self._current_gaps = self._assigned_obligation_ids()
        self.latest_checkpoint = self._project_checkpoint(())
        self._successful_requests: dict[str, EvidenceRecord] = {}
        self._final_result: SessionResult | None = None
        if not self.conversation.events:
            self.conversation.add_user(self._assignment_prompt())

    def _assignment_prompt(self) -> str:
        lenses = getattr(self.assignment, "analytical_lens", "")
        if not lenses:
            lenses = ", ".join(getattr(self.assignment, "lenses", ()))
        payload = {
            "assignment_id": self.assignment.assignment_id,
            "objective": self.assignment.objective,
            "obligation_ids": list(self._assigned_obligation_ids()),
            "independent_obligation_ids": list(
                getattr(self.assignment, "independent_obligation_ids", ())
            ),
            "analytical_lens": lenses,
            "seed_paths": list(self.assignment.seed_paths),
            "permitted_boundaries": list(getattr(
                self.assignment,
                "permitted_boundaries",
                getattr(self.assignment, "boundary_paths", ()),
            )),
        }
        return "Immutable specialist assignment:\n" + json.dumps(payload, sort_keys=True)

    def _assigned_obligation_ids(self) -> tuple[str, ...]:
        primary = tuple(getattr(self.assignment, "primary_obligation_ids", ()))
        all_ids = tuple(getattr(self.assignment, "obligation_ids", ()))
        independent = tuple(getattr(self.assignment, "independent_obligation_ids", ()))
        return tuple(dict.fromkeys((*primary, *all_ids, *independent)))

    def _request(self, *, tools_enabled: bool, schema: dict[str, Any] | None) -> ModelTurnResult:
        estimated_input_tokens = self.conversation.approx_tokens()
        remaining_input_tokens = self.budget.remaining_input_tokens()
        if (
            remaining_input_tokens is not None
            and estimated_input_tokens > remaining_input_tokens
        ):
            raise BudgetExhausted("input token limit exhausted")
        remaining_output_tokens = self.budget.remaining_output_tokens()
        if remaining_output_tokens is not None and remaining_output_tokens <= 0:
            raise BudgetExhausted("output token limit exhausted")
        request_max_tokens = (
            self.max_tokens
            if remaining_output_tokens is None
            else min(self.max_tokens, remaining_output_tokens)
        )
        timeout = self.lease.request_timeout(self.request_timeout_sec)
        self.budget.reserve_model_turn()
        result = self.gateway.complete(ModelTurnRequest(
            role="specialist", conversation=self.conversation, max_tokens=request_max_tokens,
            response_schema=schema, tools_enabled=tools_enabled, timeout_sec=timeout,
            deadline_at=self.lease.deadline_at, stream=self.stream,
            response_schema_name=("specialist_checkpoint" if schema is _CHECKPOINT_SCHEMA
                                  else "specialist_final" if schema is _FINAL_SCHEMA else None),
        ))
        self.budget.record_model_usage(
            input_tokens=int(result.usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(result.usage.get("completion_tokens", 0) or 0),
        )
        return result

    def explore(self) -> SessionResult:
        """Explore until the specialist emits or is forced to a checkpoint."""
        if self._final_result is not None:
            return self._final_result
        self.lease.request_timeout(self.request_timeout_sec)
        self.state = SessionState.EXPLORING
        while True:
            if self.conversation.approx_tokens() > self.max_context_tokens:
                self._compact_conversation()
                if self.conversation.approx_tokens() > self.max_context_tokens:
                    return self.request_checkpoint("context-pressure")
            turn = self._request(tools_enabled=True, schema=None)
            if turn.text:
                self.conversation.add_assistant_text(turn.text)
            if not turn.tool_calls:
                checkpoint = self._checkpoint_from_text(turn.text)
                if checkpoint is None:
                    return self.request_checkpoint("model-stopped-without-valid-checkpoint")
                self.latest_checkpoint = checkpoint
                self.state = SessionState.CHECKPOINT
                return self._snapshot()
            self.conversation.add_assistant_tool_calls(turn.tool_calls)
            progressed = self._execute_calls(turn.tool_calls)
            if progressed:
                self.budget.reset_no_progress_streak("new retained evidence")
            else:
                streak = self.budget.record_no_progress()
                if streak >= self.max_no_progress_streak:
                    return self.request_checkpoint("no-progress-guard")

    def _execute_calls(self, calls: tuple[dict[str, Any], ...]) -> bool:
        progressed = False
        for call in calls:
            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "")
            try:
                arguments = decode_native_tool_arguments(call.get("arguments"))
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                self.budget.record_tool_rejection("invalid tool arguments")
                self.conversation.add_tool_result(call_id, {"error": str(exc)}, is_error=True)
                continue
            key = native_tool_request_key(name, arguments)
            prior = self._successful_requests.get(key)
            if prior is not None:
                self.budget.record_tool_rejection("duplicate tool request")
                self.conversation.add_tool_result(
                    call_id,
                    {"evidence_id": prior.id, "replayed_duplicate": True},
                )
                continue
            try:
                self.budget.reserve_tool_calls(1)
            except BudgetExhausted:
                self.budget.record_tool_rejection("tool call budget exhausted")
                self.conversation.add_tool_result(
                    call_id, {"error": "tool call budget exhausted"}, is_error=True,
                )
                continue
            try:
                result = self.execute_tool(name, arguments)
            except Exception as exc:  # noqa: BLE001 - executor failures become tool results
                result = {"tool": name, "status": "error", "error": str(exc)}
            if not isinstance(result, dict):
                result = {"tool": name, "status": "error", "error": "invalid executor result"}
            is_error = str(result.get("status", "")).lower() not in {"ok", "success", "completed"}
            record = self.evidence_store.add_tool_result(
                session_id=self.session_id, tool=name, arguments=arguments, result=result,
            )
            self.conversation.add_tool_result(
                call_id,
                {
                    "evidence_id": record.id,
                    "status": record.status,
                    "content": record.content,
                },
                is_error=is_error,
            )
            if record.is_usable_for_coverage:
                self._successful_requests[key] = record
                progressed = True
        return progressed

    def request_checkpoint(self, reason: str = "controller-request") -> SessionResult:
        """Request a structured checkpoint; never force a final report."""
        self.conversation.add_user(
            "Checkpoint requested (not a final report). Reason: " + str(reason)
        )
        try:
            turn = self._request(tools_enabled=False, schema=_CHECKPOINT_SCHEMA)
        except (BudgetExhausted, TimeoutError):
            self.latest_checkpoint = self._project_checkpoint(self._current_gaps)
            self.state = SessionState.CHECKPOINT
            return self._snapshot(degraded=True)
        if turn.text:
            self.conversation.add_assistant_text(turn.text)
        checkpoint = self._checkpoint_from_text(turn.text)
        if checkpoint is None:
            self.conversation.add_user(
                "Repair the previous checkpoint as one JSON object matching the schema."
            )
            try:
                repair = self._request(tools_enabled=False, schema=_CHECKPOINT_SCHEMA)
            except (BudgetExhausted, TimeoutError):
                repair = None
            if repair is not None:
                if repair.text:
                    self.conversation.add_assistant_text(repair.text)
                checkpoint = self._checkpoint_from_text(repair.text)
        self.latest_checkpoint = checkpoint or self._project_checkpoint(self._current_gaps)
        self.state = SessionState.CHECKPOINT
        return self._snapshot(degraded=checkpoint is None)

    def _checkpoint_from_text(self, text: str) -> SessionCheckpoint | None:
        raw = _json_object(text)
        if raw is None or not isinstance(raw.get("unresolved"), list):
            return None
        retained = {record.id: record for record in self.evidence_store.snapshot().records}
        evidence_ids = [item for item in _strings(raw.get("evidence_ids")) if item in retained]
        inspected = {_normalized_path(item) for item in _strings(raw.get("inspected"))}
        for record in retained.values():
            if (
                record.is_usable_for_coverage
                and _normalized_path(record.source_path or "") in inspected
            ):
                evidence_ids.append(record.id)
        evidence_ids = list(dict.fromkeys(evidence_ids))
        unresolved = _strings(raw.get("unresolved"))
        assigned = set(self._assigned_obligation_ids())
        declared = raw.get("evidence_by_obligation")
        if isinstance(declared, Mapping):
            for obligation_id, ids in declared.items():
                if obligation_id not in assigned:
                    continue
                for evidence_id in _strings(ids):
                    record = retained.get(evidence_id)
                    obligation = self.coverage.obligation(obligation_id)
                    if record is not None and _evidence_matches_obligation(record, obligation):
                        self.coverage.attach_evidence(obligation_id, evidence_id)
                        evidence_ids.append(evidence_id)
        # The compact `inspected` checkpoint form associates retained inspected
        # evidence with covered assignment obligations; arbitrary IDs never do.
        for obligation_id in assigned:
            obligation = self.coverage.obligation(obligation_id)
            for evidence_id in evidence_ids:
                record = retained[evidence_id]
                if _evidence_matches_obligation(record, obligation):
                    self.coverage.attach_evidence(obligation_id, evidence_id)
        for obligation_id in assigned.intersection(unresolved):
            self.coverage.mark_unresolved(obligation_id)
        self._current_gaps = self._derive_current_gaps()
        evidence_ids = list(dict.fromkeys(evidence_ids))
        return SessionCheckpoint(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=tuple(evidence_ids),
            hypotheses=_strings(raw.get("hypotheses")),
            candidate_finding_ids=_strings(raw.get("candidate_finding_ids")),
            obligation_statuses=tuple(sorted(self.coverage.obligation_statuses().items())),
            invariants_evaluated=_strings(raw.get("invariants_evaluated")),
            unknowns=self._current_gaps,
            proposed_next_actions=self._current_gaps,
        )

    def _derive_current_gaps(self) -> tuple[str, ...]:
        statuses = self.coverage.obligation_statuses()
        gaps: list[str] = []
        for obligation_id in self._assigned_obligation_ids():
            try:
                obligation = self.coverage.obligation(obligation_id)
            except KeyError:
                continue
            if (
                obligation.mandatory
                and statuses.get(obligation_id) is not ObligationStatus.COVERED
            ):
                gaps.append(obligation_id)
        return tuple(gaps)

    def _project_checkpoint(self, gaps: tuple[str, ...]) -> SessionCheckpoint:
        for obligation_id in gaps:
            try:
                self.coverage.mark_unresolved(obligation_id)
            except KeyError:
                continue
        self._current_gaps = self._derive_current_gaps()
        return SessionCheckpoint(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=tuple(
                record.id for record in self.evidence_store.snapshot().records
                if record.is_usable_for_coverage
            ),
            obligation_statuses=tuple(sorted(self.coverage.obligation_statuses().items())),
            unknowns=self._current_gaps,
            proposed_next_actions=self._current_gaps,
        )

    def apply_coverage_feedback(self, gaps: list[str] | tuple[str, ...]) -> None:
        """Append targeted controller gaps without replacing lifetime state."""
        if self._final_result is not None:
            return
        normalized = _strings(gaps)
        self.state = SessionState.COVERAGE_EVALUATION
        if normalized:
            self.conversation.add_user(
                "Coverage feedback; continue the same investigation for these gaps: "
                + json.dumps(normalized)
            )
            self.budget.reset_no_progress_streak("material controller feedback")
            for obligation_id in normalized:
                if obligation_id in self._assigned_obligation_ids():
                    self.coverage.mark_unresolved(obligation_id)
        self._current_gaps = self._derive_current_gaps()

    def update_lease(self, lease: SessionLease) -> None:
        """Advance the same durable session to a controller-issued later lease."""
        if not isinstance(lease, SessionLease):
            raise TypeError("lease must be a SessionLease")
        if self._final_result is not None:
            raise RuntimeError("a finalized session cannot receive a new lease")
        phase_rank = {
            RunPhase.PLANNING: 0,
            RunPhase.INITIAL: 1,
            RunPhase.FOLLOWUP: 2,
            RunPhase.FINALIZATION: 3,
        }
        if phase_rank[lease.phase] < phase_rank[self.lease.phase]:
            raise ValueError("session lease phase cannot move backward")
        self.lease = lease

    def _compact_conversation(self) -> None:
        self.conversation.truncate_oldest_tool_results(2_000)
        self.conversation.truncate_oldest_assistant_text(1_000, keep_newest=2)
        if self.conversation.approx_tokens() > self.max_context_tokens:
            self.conversation.collapse_oldest_completed_history(
                max(1_000, self.max_context_tokens * 2), keep_newest_results=2,
            )

    def _snapshot(
        self,
        *,
        report: Mapping[str, Any] | None = None,
        degraded: bool = False,
    ) -> SessionResult:
        return SessionResult(
            session_id=self.session_id, state=self.state,
            checkpoint=self.latest_checkpoint, budget=self.budget.snapshot(),
            report=dict(report) if report is not None else None, degraded=degraded,
        )

    def conversation_contains_evidence_ids(self, evidence_ids: tuple[str, ...]) -> bool:
        transcript = json.dumps(self.conversation.events, sort_keys=True)
        return all(evidence_id in transcript for evidence_id in evidence_ids)

    def recover(self, reason: str) -> SessionResult:
        """Reconstruct a clean transcript for one of the recorded reasons."""
        normalized = "-".join(str(reason).strip().lower().split())
        if normalized not in _RECOVERY_REASONS:
            raise ValueError(f"not a recorded recovery reason: {reason}")
        if self._final_result is not None:
            return self._final_result
        self.lease.request_timeout(self.request_timeout_sec)
        self.budget.record_recovery(normalized)
        self.state = SessionState.RECOVERY

        # Bound the abandoned transcript using the established compaction
        # helpers before retaining it for diagnostics/replacement.
        self._compact_conversation()
        previous = self.conversation
        rebuilt = Conversation(
            system=previous.system,
            tool_schemas=list(previous.tool_schemas),
        )
        rebuilt.add_user(self._assignment_prompt())
        evidence = []
        remaining_bytes = self.recovery_evidence_bytes
        for record in self.evidence_store.snapshot().records:
            if remaining_bytes <= 0:
                break
            encoded = record.content.encode("utf-8")
            clipped = encoded[:remaining_bytes].decode("utf-8", errors="replace")
            remaining_bytes -= len(clipped.encode("utf-8"))
            evidence.append({
                "evidence_id": record.id,
                "status": record.status,
                "source": record.source_identity,
                "content": clipped,
            })
        usage = self.budget.snapshot()
        recovery_payload = {
            "recovery_reason": normalized,
            "latest_checkpoint": asdict(self.latest_checkpoint),
            "evidence": evidence,
            "current_gaps": list(self._current_gaps),
            "deduplication_request_keys": sorted(self._successful_requests),
            "remaining_lifetime_budget": {
                "model_turns": self.budget.remaining_model_turns(),
                "tool_calls": self.budget.remaining_tool_calls(),
                "recoveries_used": usage.recoveries,
            },
        }
        rebuilt.add_user(
            "Recovery reconstruction. Continue the same logical specialist session:\n"
            + json.dumps(
                recovery_payload,
                sort_keys=True,
                default=lambda value: value.value if hasattr(value, "value") else str(value),
            )
        )
        self.conversation = rebuilt
        self.state = SessionState.EXPLORING
        return self._snapshot()

    def finalize(self) -> SessionResult:
        """Finalize once with tools disabled and one bounded schema repair."""
        if self._final_result is not None:
            return self._final_result
        try:
            self.lease.request_timeout(self.request_timeout_sec)
            self.state = SessionState.FINALIZING
            self.conversation.add_user(
                "Finalize this specialist assessment once from the latest checkpoint and "
                "retained evidence. Return only the requested JSON; tools are disabled."
            )
            turn = self._request(tools_enabled=False, schema=_FINAL_SCHEMA)
            if turn.text:
                self.conversation.add_assistant_text(turn.text)
            report = self._final_report_from_text(turn.text)
            if report is None:
                self.conversation.add_user(
                    "Schema repair: return exactly one final JSON object with non-empty "
                    "summary and recommendation fields. Tools remain disabled."
                )
                repair = self._request(tools_enabled=False, schema=_FINAL_SCHEMA)
                if repair.text:
                    self.conversation.add_assistant_text(repair.text)
                report = self._final_report_from_text(repair.text)
        except Exception:  # noqa: BLE001 - provider/admission failure degrades once
            return self._cache_checkpoint_fallback()
        if report is None:
            return self._cache_checkpoint_fallback()
        self.state = SessionState.COMPLETE
        self._final_result = self._snapshot(report=report, degraded=False)
        return self._final_result

    def _cache_checkpoint_fallback(self) -> SessionResult:
        self.state = SessionState.COMPLETE
        self._final_result = self._snapshot(
            report=self._checkpoint_fallback_report(), degraded=True,
        )
        return self._final_result

    def _final_report_from_text(self, text: str) -> dict[str, Any] | None:
        raw = _json_object(text)
        if raw is None:
            return None
        summary = raw.get("summary")
        recommendation = raw.get("recommendation")
        if not isinstance(summary, str) or not summary.strip():
            return None
        if not isinstance(recommendation, str) or not recommendation.strip():
            return None
        retained = {
            record.id for record in self.evidence_store.snapshot().records
            if record.is_usable_for_coverage
        }
        checkpoint_findings = set(self.latest_checkpoint.candidate_finding_ids)
        return {
            "summary": summary.strip(),
            "recommendation": recommendation.strip(),
            "candidate_finding_ids": [
                item for item in _strings(raw.get("candidate_finding_ids"))
                if item in checkpoint_findings
            ],
            "evidence_ids": [
                item for item in _strings(raw.get("evidence_ids")) if item in retained
            ],
            "unknowns": list(_strings(raw.get("unknowns"))),
            "source": "model-finalization",
        }

    def _checkpoint_fallback_report(self) -> dict[str, Any]:
        checkpoint = self.latest_checkpoint
        covered = [
            obligation_id for obligation_id, status in checkpoint.obligation_statuses
            if status.value == "covered"
        ]
        return {
            "summary": "Specialist finalization degraded to the latest valid checkpoint.",
            "recommendation": "controller-review-required",
            "candidate_finding_ids": list(checkpoint.candidate_finding_ids),
            "evidence_ids": list(checkpoint.evidence_ids),
            "covered_obligation_ids": covered,
            "unknowns": list(checkpoint.unknowns),
            "source": "checkpoint-fallback",
        }
