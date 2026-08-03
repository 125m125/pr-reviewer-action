"""Continuous checkpoint/resume/recovery state for one specialist."""

from __future__ import annotations

import json
import inspect
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from pr_reviewer.conversation import Conversation
from pr_reviewer.tool_loop import decode_native_tool_arguments, native_tool_request_key

from .budget import BudgetExhausted, BudgetLedger, SessionLease
from .callbacks import CALLBACK_POOL, CallbackTimedOut, format_callback_error
from .coverage import CoverageLedger
from .evidence import EvidenceRecord, EvidenceStore
from .model_gateway import ModelGateway, ModelTurnRequest, ModelTurnResult
from .request_attempts import RequestAttemptJournal
from .types import (
    BudgetUsage,
    CandidateFinding,
    change_overview_orientation,
    CoverageObligation,
    ObligationStatus,
    RunPhase,
    SessionCheckpoint,
    SessionState,
    SpecialistAssignment,
)
from .web_evidence import (
    SearchCandidate,
    SourceAccessRequest,
    source_access_request,
)


ToolExecutor = Callable[..., dict[str, Any]]

_CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_by_obligation": {"type": "object"},
        "inspected": {"type": "array", "items": {"type": "string"}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "candidate_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "root_cause_fingerprint": {"type": "string"},
                    "claim": {"type": "string"},
                    "affected_location": {"type": "string"},
                    "causal_chain": {"type": "string"},
                    "severity": {"type": "string"},
                    "category": {"type": "string"},
                    "supporting_evidence_ids": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "contradicting_evidence_ids": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "related_obligation_ids": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "confidence_rationale": {
                        "type": "string",
                        "description": (
                            "Typed consequence support declaration. Start with "
                            "consequence_support: and one of reachable_input_path, "
                            "failing_behavioral_test, violated_invariant, affected_consumer, "
                            "or contradicting_evidence, followed by evidence_ids containing "
                            "exact retained evidence IDs and the form's required key=value details."
                        ),
                    },
                    "user_visible_consequence": {"type": "string"},
                    "manual_validation": {"type": "string"},
                },
                "required": [
                    "candidate_id", "claim", "affected_location",
                    "causal_chain", "supporting_evidence_ids",
                    "related_obligation_ids", "confidence_rationale",
                    "user_visible_consequence",
                    "manual_validation",
                ],
                "additionalProperties": False,
            },
        },
        "invariants_evaluated": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "proposed_next_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["unresolved"],
    "additionalProperties": False,
}

# Candidate lifecycle fields are additive so older providers can still emit the
# legacy ``candidate_findings`` array.  New checkpoints use compact updates and
# a separate array for genuinely new candidate objects.
_CHECKPOINT_SCHEMA["properties"]["candidate_updates"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["active", "withdrawn", "superseded"],
            },
            "reason": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "superseded_by": {"type": "string"},
        },
        "required": ["candidate_id", "status"],
        "additionalProperties": False,
    },
}
_CHECKPOINT_SCHEMA["properties"]["new_candidates"] = {
    "type": "array",
    "items": _CHECKPOINT_SCHEMA["properties"]["candidate_findings"]["items"],
}

_RECOVERY_REASONS = frozenset({
    "repetitive-transcript",
    "polluted-transcript",
    "context-pressure",
    "invalid-provider-history",
    "transport-incompatibility",
})
_CHECKPOINT_TURN_RESERVE = 2
_CANDIDATE_RETENTION_UNKNOWN = "candidate-retention-unknown"
_MAX_CHECKPOINT_CANDIDATE_IDS = 20
_MAX_CHECKPOINT_CANDIDATE_ID_CHARS = 256
_CHECKPOINT_RETENTION_INSTRUCTION = (
    " Required keys: unresolved, candidate_updates, new_candidates, and unknowns. "
    "Empty candidate_updates and new_candidates arrays are valid and mean no "
    "candidate state changed. Existing candidates remain active unless explicitly "
    "updated with status withdrawn or superseded; omission never withdraws one. "
    "Use compact candidate_updates entries such as "
    "{\"candidate_id\":\"c1\",\"status\":\"withdrawn\",\"reason\":\"...\"}. "
    "Put full candidate objects only in new_candidates. The legacy "
    "\"candidate_findings\" array is accepted for compatibility; the "
    "controller derives internal candidate handles from admitted candidate objects. "
    "Use only exact "
    "retained evidence IDs (evidence:<hash>) from successful tool results in "
    "evidence_ids and supporting_evidence_ids; repository paths are not evidence IDs."
    " JSON shape starts with {\"unresolved\":[\"OB-id\"],\"candidate_updates\":[]}."
)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _resolve_retained_evidence_id(
    value: object,
    retained: Mapping[str, EvidenceRecord],
) -> str | None:
    """Resolve an exact ID or one unambiguous model-shortened ID prefix."""
    candidate = str(value or "").strip()
    if candidate in retained:
        return candidate
    if not candidate.startswith("evidence:"):
        return None
    prefix = candidate[:-3] if candidate.endswith("...") else candidate
    if len(prefix) < len("evidence:") + 8:
        return None
    matches = tuple(item for item in retained if item.startswith(prefix))
    return matches[0] if len(matches) == 1 else None


def _rewrite_rationale_evidence_ids(
    rationale: str,
    retained: Mapping[str, EvidenceRecord],
) -> str:
    """Expand uniquely shortened evidence IDs in a typed rationale."""
    parts = []
    for part in rationale.split(";"):
        key, separator, raw_values = part.partition("=")
        if separator and key.strip().casefold() == "evidence_ids":
            resolved = tuple(dict.fromkeys(
                item
                for item in (
                    _resolve_retained_evidence_id(value, retained)
                    for value in raw_values.split(",")
                )
                if item is not None
            ))
            parts.append(f"{key.strip()}={','.join(resolved)}")
        else:
            parts.append(part)
    return ";".join(parts)


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


def _contains_candidate_shaped_text(text: str) -> bool:
    """Recognize candidate payloads without retaining untrusted model text."""
    lowered = text.lower() if isinstance(text, str) else ""
    return (
        "candidate_findings" in lowered
        and ("candidate_id" in lowered or "claim" in lowered)
    )


@dataclass(frozen=True)
class _CandidateRetentionSignal:
    """Bounded candidate declarations observed across checkpoint attempts."""

    candidate_ids: tuple[str, ...] = ()
    unidentified_shapes: int = 0
    omitted_candidate_ids: int = 0

    @property
    def is_material(self) -> bool:
        return bool(
            self.candidate_ids
            or self.unidentified_shapes
            or self.omitted_candidate_ids
        )

    def merged(self, other: "_CandidateRetentionSignal") -> "_CandidateRetentionSignal":
        combined_ids = tuple(dict.fromkeys((
            *self.candidate_ids,
            *other.candidate_ids,
        )))
        return _CandidateRetentionSignal(
            candidate_ids=combined_ids[:_MAX_CHECKPOINT_CANDIDATE_IDS],
            unidentified_shapes=min(
                _MAX_CHECKPOINT_CANDIDATE_IDS,
                max(self.unidentified_shapes, other.unidentified_shapes),
            ),
            omitted_candidate_ids=min(
                _MAX_CHECKPOINT_CANDIDATE_IDS,
                max(
                    self.omitted_candidate_ids,
                    other.omitted_candidate_ids,
                    len(combined_ids) - _MAX_CHECKPOINT_CANDIDATE_IDS,
                ),
            ),
        )


def _candidate_retention_signal(text: str) -> _CandidateRetentionSignal:
    """Retain only bounded structured IDs/counts, never candidate prose."""
    raw = _json_object(text)
    if not isinstance(raw, Mapping):
        # Malformed structured candidate payloads remain conservative, while
        # ordinary prose containing no JSON candidate keys is non-material.
        return _CandidateRetentionSignal(
            unidentified_shapes=1 if _contains_candidate_shaped_text(text) else 0,
        )
    if not any(
        key in raw for key in (
            "candidate_finding_ids", "candidate_findings",
            "candidate_updates", "new_candidates",
        )
    ):
        return _CandidateRetentionSignal(
            unidentified_shapes=1 if _contains_candidate_shaped_text(text) else 0,
        )
    candidate_ids: list[str] = []
    unidentified_shapes = 0
    omitted_candidate_ids = 0
    raw_declared_ids = raw.get("candidate_finding_ids")
    if isinstance(raw_declared_ids, list):
        if len(raw_declared_ids) > _MAX_CHECKPOINT_CANDIDATE_IDS:
            omitted_candidate_ids = 1
        for value in raw_declared_ids[:_MAX_CHECKPOINT_CANDIDATE_IDS + 1]:
            candidate_id = str(value).strip()
            if candidate_id:
                candidate_ids.append(candidate_id)
    elif raw_declared_ids is not None and raw_declared_ids != ():
        unidentified_shapes = 1
    raw_candidates = raw.get("candidate_findings")
    if isinstance(raw_candidates, list):
        if len(raw_candidates) > _MAX_CHECKPOINT_CANDIDATE_IDS:
            omitted_candidate_ids = 1
        for value in raw_candidates[:_MAX_CHECKPOINT_CANDIDATE_IDS + 1]:
            if isinstance(value, Mapping):
                candidate_id = str(value.get("candidate_id") or "").strip()
                if candidate_id:
                    candidate_ids.append(candidate_id)
                    if not str(value.get("claim") or "").strip():
                        unidentified_shapes += 1
                else:
                    unidentified_shapes += 1
            else:
                unidentified_shapes += 1
    elif raw_candidates is not None and raw_candidates != ():
        unidentified_shapes += 1
    raw_new_candidates = raw.get("new_candidates")
    if isinstance(raw_new_candidates, list):
        if len(raw_new_candidates) > _MAX_CHECKPOINT_CANDIDATE_IDS:
            omitted_candidate_ids = 1
        for value in raw_new_candidates[:_MAX_CHECKPOINT_CANDIDATE_IDS + 1]:
            if isinstance(value, Mapping):
                candidate_id = str(value.get("candidate_id") or "").strip()
                if candidate_id:
                    candidate_ids.append(candidate_id)
                    if not str(value.get("claim") or "").strip():
                        unidentified_shapes += 1
                else:
                    unidentified_shapes += 1
            else:
                unidentified_shapes += 1
    elif raw_new_candidates is not None and raw_new_candidates != ():
        unidentified_shapes += 1
    raw_updates = raw.get("candidate_updates")
    if isinstance(raw_updates, list):
        if len(raw_updates) > _MAX_CHECKPOINT_CANDIDATE_IDS:
            omitted_candidate_ids = 1
        for value in raw_updates[:_MAX_CHECKPOINT_CANDIDATE_IDS + 1]:
            if isinstance(value, Mapping):
                candidate_id = str(value.get("candidate_id") or "").strip()
                if candidate_id:
                    candidate_ids.append(candidate_id)
                    if str(value.get("status") or "").strip().lower() not in {
                        "active", "withdrawn", "superseded",
                    }:
                        unidentified_shapes += 1
                else:
                    unidentified_shapes += 1
            else:
                unidentified_shapes += 1
    elif raw_updates is not None and raw_updates != ():
        unidentified_shapes += 1
    bounded_ids: list[str] = []
    for candidate_id in dict.fromkeys(candidate_ids):
        if len(candidate_id) > _MAX_CHECKPOINT_CANDIDATE_ID_CHARS:
            omitted_candidate_ids = 1
            continue
        bounded_ids.append(candidate_id)
    if len(bounded_ids) > _MAX_CHECKPOINT_CANDIDATE_IDS:
        omitted_candidate_ids = max(
            omitted_candidate_ids,
            len(bounded_ids) - _MAX_CHECKPOINT_CANDIDATE_IDS,
        )
    return _CandidateRetentionSignal(
        candidate_ids=tuple(bounded_ids[:_MAX_CHECKPOINT_CANDIDATE_IDS]),
        unidentified_shapes=min(
            unidentified_shapes, _MAX_CHECKPOINT_CANDIDATE_IDS,
        ),
        omitted_candidate_ids=min(
            omitted_candidate_ids, _MAX_CHECKPOINT_CANDIDATE_IDS,
        ),
    )


def _candidate_retention_lost(
    signal: _CandidateRetentionSignal,
    checkpoint: SessionCheckpoint | None,
    *,
    accounted_candidate_ids: tuple[str, ...] = (),
) -> bool:
    if not signal.is_material:
        return False
    admitted = set(checkpoint.candidate_finding_ids) if checkpoint is not None else set()
    admitted.update(accounted_candidate_ids)
    return bool(
        set(signal.candidate_ids) - admitted
        or signal.unidentified_shapes
        or signal.omitted_candidate_ids
    )


def _assignment_json_value(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): _assignment_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_assignment_json_value(item) for item in value]
    return value


def specialist_assignment_prompt(
    assignment: object,
    *,
    change_overview: Mapping[str, object] | None = None,
) -> str:
    """Serialize the immutable semantic assignment for initial and recovery turns."""
    lenses = getattr(assignment, "analytical_lens", "")
    if not lenses:
        lenses = ", ".join(getattr(assignment, "lenses", ()))
    primary = tuple(getattr(assignment, "primary_obligation_ids", ()))
    all_ids = tuple(getattr(assignment, "obligation_ids", ()))
    independent = tuple(getattr(assignment, "independent_obligation_ids", ()))
    payload = {
        "assignment_id": getattr(
            assignment, "assignment_id", getattr(assignment, "id", ""),
        ),
        "title": getattr(assignment, "title", ""),
        "objective": getattr(assignment, "objective", ""),
        "obligation_ids": list(dict.fromkeys((*primary, *all_ids, *independent))),
        "independent_obligation_ids": list(independent),
        "analytical_lens": lenses,
        "seed_paths": list(getattr(assignment, "seed_paths", ())),
        "permitted_boundaries": list(getattr(
            assignment,
            "permitted_boundaries",
            getattr(assignment, "boundary_paths", ()),
        )),
        "obligation_briefs": _assignment_json_value(getattr(
            assignment, "obligation_briefs", (),
        )),
        "changed_context": _assignment_json_value(getattr(
            assignment, "changed_context", (),
        )),
        "changed_context_omitted_paths": int(getattr(
            assignment, "changed_context_omitted_paths", 0,
        )),
        "changed_context_semantics": (
            "This is bounded orientation to assigned changed paths, not proof of "
            "complete diff or file coverage."
        ),
        "exploration_contract": (
            "Inspect assigned changed diffs first with read_pr_diff, using "
            "changed_context only as bounded orientation. Then use read_file only "
            "for the minimum surrounding source needed to evaluate assigned "
            "predicates. Bounded, truncated, or omitted context does not prove "
            "that other content is absent."
        ),
        "change_overview": _assignment_json_value(
            change_overview_orientation(change_overview),
        ),
    }
    return "Immutable specialist assignment:\n" + json.dumps(
        payload, sort_keys=True,
    )


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
        if not any(
            source_path == scope_path or source_path.startswith(scope_path + "/")
            for raw_path in scoped_paths
            if (scope_path := _normalized_path(raw_path))
        ):
            return False
    category = record.category.strip().lower()
    return bool(category) and category in {
        item.strip().lower()
        for item in obligation.required_evidence_categories
        if item.strip()
    }


@dataclass(frozen=True)
class SpecialistRequestEvent:
    """One actual specialist gateway request transition."""

    request_id: str
    status: str
    tools_enabled: bool
    response_schema_name: str | None
    error: str = ""


@dataclass(frozen=True)
class SessionResult:
    """Detached projection of current or completed specialist state."""

    session_id: str
    state: SessionState
    checkpoint: SessionCheckpoint
    budget: BudgetUsage
    report: Mapping[str, Any] | None = None
    degraded: bool = False
    request_events: tuple[SpecialistRequestEvent, ...] = ()
    finalization_diagnostics: tuple[Mapping[str, object], ...] = ()


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
        recovery_max_tokens: int | None = None,
        recovery_evidence_bytes: int = 8_000,
        clock: Callable[[], float] = time.monotonic,
        wire_safety_tokens: int = 256,
        change_overview: Mapping[str, object] | None = None,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        if (
            request_timeout_sec <= 0
            or max_tokens <= 0
            or (recovery_max_tokens is not None and recovery_max_tokens <= 0)
        ):
            raise ValueError("request timeout and max tokens must be positive")
        self.session_id = session_id
        self.assignment = assignment
        self.change_overview = json.loads(json.dumps(
            _assignment_json_value(change_overview or {}),
            sort_keys=True,
        ))
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
        self.recovery_max_tokens = min(
            max_tokens,
            max_tokens if recovery_max_tokens is None else recovery_max_tokens,
        )
        self.recovery_evidence_bytes = recovery_evidence_bytes
        self.clock = clock
        self.wire_safety_tokens = max(0, int(wire_safety_tokens))
        self.state = SessionState.CREATED
        self._current_gaps = self._assigned_obligation_ids()
        self.candidate_findings: tuple[CandidateFinding, ...] = ()
        # Lifecycle state includes withdrawn/superseded IDs so a legitimate
        # update is accounted for even though only active findings are exposed
        # through ``candidate_findings`` and checkpoints.
        self._candidate_statuses: dict[str, str] = {}
        self._candidate_retention_signal = _CandidateRetentionSignal()
        self.latest_checkpoint = self._project_checkpoint(())
        self.source_access_requests: tuple[SourceAccessRequest, ...] = ()
        self._successful_requests: dict[str, EvidenceRecord] = {}
        self._successful_collections: dict[str, str] = {}
        self._tool_lease_exhausted = False
        self._recovery_turn_pending = False
        self._final_result: SessionResult | None = None
        self._request_events: list[SpecialistRequestEvent] = []
        self._finalization_diagnostics: list[dict[str, object]] = []
        self._request_attempt_journal: RequestAttemptJournal | None = None
        self._request_assignment_id = str(getattr(
            assignment, "assignment_id", getattr(assignment, "id", ""),
        ))
        self._request_turn = 0
        if not self.conversation.events:
            self.conversation.add_user(self._assignment_prompt())
        self._advertise_obligation_associations()

    def _advertise_obligation_associations(self) -> None:
        """Add controller-owned association metadata only to specialist schemas."""
        schemas = json.loads(json.dumps(self.conversation.tool_schemas))
        for schema in schemas:
            parameters = schema.get("parameters")
            if not isinstance(parameters, dict):
                continue
            properties = parameters.get("properties")
            if not isinstance(properties, dict):
                continue
            properties["obligation_ids"] = {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional assigned obligation IDs this collection is meant "
                    "to support. Evidence categories are derived by the runtime."
                ),
            }
        self.conversation.tool_schemas = schemas

    def _assignment_prompt(self) -> str:
        return specialist_assignment_prompt(
            self.assignment,
            change_overview=self.change_overview,
        )

    @property
    def request_events(self) -> tuple[SpecialistRequestEvent, ...]:
        return tuple(self._request_events)

    def bind_request_attempt_journal(
        self, journal: RequestAttemptJournal, assignment_id: str,
    ) -> None:
        if self._request_attempt_journal not in {None, journal}:
            raise RuntimeError("specialist request journal cannot be rebound")
        self._request_attempt_journal = journal
        self._request_assignment_id = str(assignment_id)

    def _assigned_obligation_ids(self) -> tuple[str, ...]:
        primary = tuple(getattr(self.assignment, "primary_obligation_ids", ()))
        all_ids = tuple(getattr(self.assignment, "obligation_ids", ()))
        independent = tuple(getattr(self.assignment, "independent_obligation_ids", ()))
        return tuple(dict.fromkeys((*primary, *all_ids, *independent)))

    def _accounted_candidate_ids(self) -> tuple[str, ...]:
        return tuple(self._candidate_statuses)

    def _active_candidate_register(self) -> str:
        if not self.candidate_findings:
            return "Active candidates: []"
        entries = []
        for candidate in self.candidate_findings[:_MAX_CHECKPOINT_CANDIDATE_IDS]:
            claim = " ".join(candidate.claim.split())[:180]
            entries.append({
                "candidate_id": candidate.candidate_id,
                "claim": claim,
                "affected_location": candidate.affected_location,
            })
        return "Active candidates (use these short IDs for updates): " + json.dumps(
            entries, sort_keys=True,
        )

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
        configured_max_tokens = (
            self.recovery_max_tokens
            if self._recovery_turn_pending
            else self.max_tokens
        )
        request_max_tokens = (
            configured_max_tokens
            if remaining_output_tokens is None
            else min(configured_max_tokens, remaining_output_tokens)
        )
        if (
            estimated_input_tokens
            + request_max_tokens
            + self.wire_safety_tokens
            > self.max_context_tokens
        ):
            self._compact_conversation()
            estimated_input_tokens = self.conversation.approx_tokens()
            if (
                estimated_input_tokens
                + request_max_tokens
                + self.wire_safety_tokens
                > self.max_context_tokens
            ):
                raise BudgetExhausted(
                    "model context limit cannot admit input and requested output"
                )
        timeout = self.lease.request_timeout(
            self.request_timeout_sec, now=self.clock(),
        )
        exploration_budget_note = None
        if tools_enabled:
            exploration_budget_note = (
                "Exploration budget before this turn: "
                f"{self.budget.remaining_model_turns()} model turns and "
                f"{self.budget.remaining_tool_calls()} tool calls remain. "
                "Prioritize unresolved correctness risks. Do not repeat completed checks."
            )
        self.budget.reserve_model_turn()
        self._request_turn += 1
        request_id = f"{self.session_id}:model:{self._request_turn}"
        schema_name = (
            "specialist_checkpoint" if schema is _CHECKPOINT_SCHEMA else None
        )
        self._request_events.append(SpecialistRequestEvent(
            request_id, "started", tools_enabled, schema_name,
        ))
        if self._request_attempt_journal is not None:
            self._request_attempt_journal.start(
                request_id=request_id,
                session_id=self.session_id,
                assignment_id=self._request_assignment_id,
                phase=self.lease.phase.value,
                turn=self._request_turn,
                input_tokens=estimated_input_tokens,
                max_output_tokens=request_max_tokens,
            )
        try:
            request = ModelTurnRequest(
                role="specialist", conversation=self.conversation,
                max_tokens=request_max_tokens, response_schema=schema,
                tools_enabled=tools_enabled, timeout_sec=timeout,
                deadline_at=self.lease.deadline_at, stream=self.stream,
                response_schema_name=schema_name,
                ephemeral_user_note=exploration_budget_note,
            )
            result = CALLBACK_POOL.run(
                lambda: self.gateway.complete(request),
                timeout_sec=timeout,
                name="specialist-gateway",
            )
        except BaseException as exc:
            terminal_status = (
                "timed_out" if isinstance(exc, CallbackTimedOut) else "failed"
            )
            if self._request_attempt_journal is not None:
                self._request_attempt_journal.finish(request_id, terminal_status)
            self._request_events.append(SpecialistRequestEvent(
                request_id,
                terminal_status,
                tools_enabled,
                schema_name,
                format_callback_error(exc, limit=500),
            ))
            raise
        if self._request_attempt_journal is not None:
            self._request_attempt_journal.finish(request_id, "completed")
        self._request_events.append(SpecialistRequestEvent(
            request_id, "completed", tools_enabled, schema_name,
        ))
        self.budget.record_model_usage(
            input_tokens=int(result.usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(result.usage.get("completion_tokens", 0) or 0),
        )
        self._recovery_turn_pending = False
        return result

    def explore(self) -> SessionResult:
        """Explore until the specialist emits or is forced to a checkpoint."""
        if self._final_result is not None:
            return self._final_result
        self.lease.request_timeout(
            self.request_timeout_sec, now=self.clock(),
        )
        self.state = SessionState.EXPLORING
        while True:
            if self.conversation.approx_tokens() > self.max_context_tokens:
                self._compact_conversation()
                if self.conversation.approx_tokens() > self.max_context_tokens:
                    return self.request_checkpoint("context-pressure")
            remaining_input_tokens = self.budget.remaining_input_tokens()
            if (
                remaining_input_tokens is not None
                and self.conversation.approx_tokens() > remaining_input_tokens
            ):
                raise BudgetExhausted("input token limit exhausted")
            remaining_output_tokens = self.budget.remaining_output_tokens()
            if remaining_output_tokens is not None and remaining_output_tokens <= 0:
                raise BudgetExhausted("output token limit exhausted")
            if self.budget.remaining_model_turns() <= _CHECKPOINT_TURN_RESERVE:
                return self.request_checkpoint("checkpoint-retention-reserve")
            try:
                turn = self._request(tools_enabled=True, schema=None)
            except BudgetExhausted as exc:
                if "model context limit" not in str(exc):
                    raise
                return self.request_checkpoint("context-pressure")
            self._candidate_retention_signal = (
                self._candidate_retention_signal.merged(
                    _candidate_retention_signal(turn.content)
                )
            )
            self.conversation.add_assistant_turn(
                reasoning=turn.reasoning,
                content=turn.content,
                calls=turn.tool_calls,
            )
            if not turn.tool_calls:
                checkpoint = self._checkpoint_from_text(turn.content)
                if (
                    checkpoint is None
                    or _candidate_retention_lost(
                        self._candidate_retention_signal,
                        checkpoint,
                        accounted_candidate_ids=self._accounted_candidate_ids(),
                    )
                ):
                    return self.request_checkpoint(
                        "model-stopped-without-valid-checkpoint",
                    )
                self.latest_checkpoint = checkpoint
                self.state = SessionState.CHECKPOINT
                return self._snapshot()
            progressed = self._execute_calls(turn.tool_calls)
            if self._tool_lease_exhausted:
                return self.request_checkpoint("tool-lease-expired")
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
            if "evidence_category" in arguments or "obligation_id" in arguments:
                self.budget.record_tool_rejection(
                    "model-supplied evidence authority is forbidden"
                )
                self.conversation.add_tool_result(
                    call_id,
                    {"error": "evidence categories are runtime-derived"},
                    is_error=True,
                )
                continue
            raw_obligation_ids = arguments.pop("obligation_ids", ())
            if raw_obligation_ids in (None, ()):
                requested_obligation_ids: tuple[str, ...] = ()
            elif (
                isinstance(raw_obligation_ids, list)
                and all(
                    isinstance(item, str) and item.strip()
                    for item in raw_obligation_ids
                )
            ):
                requested_obligation_ids = tuple(dict.fromkeys(
                    item.strip() for item in raw_obligation_ids
                ))
            else:
                self.budget.record_tool_rejection("invalid obligation_ids")
                self.conversation.add_tool_result(
                    call_id, {"error": "obligation_ids must be an array of strings"},
                    is_error=True,
                )
                continue
            if set(requested_obligation_ids) - set(
                self._assigned_obligation_ids()
            ):
                self.budget.record_tool_rejection("unassigned obligation_ids")
                self.conversation.add_tool_result(
                    call_id, {"error": "obligation_ids contain an unassigned id"},
                    is_error=True,
                )
                continue
            key = native_tool_request_key(name, arguments)
            prior = self._successful_requests.get(key)
            if prior is not None:
                collection_id = self._successful_collections.get(key)
                if collection_id:
                    self._associate_collection(
                        collection_id, prior, requested_obligation_ids,
                    )
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
                timeout = self.lease.request_timeout(
                    self.request_timeout_sec, now=self.clock(),
                )
            except TimeoutError:
                self.budget.record_tool_rejection("session lease expired")
                self.conversation.add_tool_result(
                    call_id, {"error": "session lease expired"}, is_error=True,
                )
                self._tool_lease_exhausted = True
                break
            try:
                signature = inspect.signature(self.execute_tool)
                supports_deadline = all(
                    parameter in signature.parameters
                    or any(
                        item.kind is inspect.Parameter.VAR_KEYWORD
                        for item in signature.parameters.values()
                    )
                    for parameter in ("timeout_sec", "deadline_at")
                )
                if supports_deadline:
                    result = self.execute_tool(
                        name,
                        arguments,
                        timeout_sec=timeout,
                        deadline_at=self.lease.deadline_at,
                    )
                else:
                    result = self.execute_tool(name, arguments)
            except Exception as exc:  # noqa: BLE001 - executor failures become tool results
                result = {"tool": name, "status": "error", "error": str(exc)}
            if not isinstance(result, dict):
                result = {"tool": name, "status": "error", "error": "invalid executor result"}
            is_error = str(result.get("status", "")).lower() not in {"ok", "success", "completed"}
            self._record_source_access_requests(
                name, result, requested_obligation_ids,
            )
            record, collection = self.evidence_store.add_tool_result_with_collection(
                session_id=self.session_id, tool=name, arguments=arguments, result=result,
            )
            self._associate_collection(
                collection.id, record, requested_obligation_ids,
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
                self._successful_collections[key] = collection.id
                progressed = True
            if self.clock() >= self.lease.deadline_at:
                self._tool_lease_exhausted = True
                break
        return progressed

    def _record_source_access_requests(
        self,
        tool_name: str,
        result: Mapping[str, Any],
        requested_obligation_ids: tuple[str, ...],
    ) -> None:
        if tool_name != "web_search" or str(
            result.get("status", "")
        ).lower() not in {"ok", "success", "completed"}:
            return
        payload = result.get("result")
        if not isinstance(payload, Mapping):
            return
        unapproved = payload.get("unapproved")
        if not isinstance(unapproved, list):
            return
        obligation_ids = requested_obligation_ids or self._current_gaps
        retained = {
            (
                item.obligation_id,
                item.host,
                item.candidate_url,
                item.purpose,
            ): item
            for item in self.source_access_requests
        }
        for raw in unapproved:
            if not isinstance(raw, Mapping):
                continue
            candidate = SearchCandidate(
                title=None,
                snippet=None,
                url=str(raw.get("url") or ""),
                host=str(raw.get("host") or ""),
                path=str(raw.get("path") or ""),
                denial_reason=str(raw.get("denial_reason") or ""),
            )
            for obligation_id in obligation_ids:
                try:
                    request = source_access_request(
                        candidate,
                        obligation_id,
                        "Retrieve the discovered source to verify the assigned "
                        "review obligation.",
                        candidate.denial_reason or "source policy did not approve it",
                    )
                except ValueError:
                    continue
                retained[(
                    request.obligation_id,
                    request.host,
                    request.candidate_url,
                    request.purpose,
                )] = request
        self.source_access_requests = tuple(
            retained[key] for key in sorted(retained)
        )

    def _associate_collection(
        self,
        collection_id: str,
        record: EvidenceRecord,
        requested_obligation_ids: tuple[str, ...],
    ) -> None:
        if not record.is_usable_for_coverage:
            return
        candidates = requested_obligation_ids
        if not candidates:
            candidates = tuple(
                obligation_id for obligation_id in self._assigned_obligation_ids()
                if self.coverage.obligation(obligation_id).scope
            )
        for obligation_id in candidates:
            obligation = self.coverage.obligation(obligation_id)
            if not obligation.required_evidence_categories:
                continue
            scoped = tuple(dict.fromkeys(
                (*obligation.scope, *obligation.seed_hints)
            ))
            if scoped:
                source_path = _normalized_path(record.source_path or "")
                if not source_path or not any(
                    source_path == path or source_path.startswith(path + "/")
                    for raw_path in scoped
                    if (path := _normalized_path(raw_path))
                ):
                    continue
            elif not requested_obligation_ids:
                continue
            self.evidence_store.associate_collection(
                collection_id,
                obligation_id=obligation_id,
                categories=obligation.required_evidence_categories,
            )

    def _record_matches_obligation(
        self, record: EvidenceRecord, obligation: CoverageObligation,
    ) -> bool:
        snapshot = self.evidence_store.snapshot()
        if snapshot.associations_for(record.id, obligation.id):
            return any(
                _evidence_matches_obligation(
                    EvidenceRecord(
                        **{
                            **record.__dict__,
                            "category": category,
                        }
                    ),
                    obligation,
                )
                for _collection, association in snapshot.associations_for(
                    record.id, obligation.id,
                )
                for category in association.categories
            )
        return _evidence_matches_obligation(record, obligation)

    def request_checkpoint(
        self,
        reason: str = "controller-request",
        *,
        candidate_signal: _CandidateRetentionSignal | None = None,
    ) -> SessionResult:
        """Request a structured checkpoint; never force a final report."""
        if candidate_signal is not None:
            self._candidate_retention_signal = (
                self._candidate_retention_signal.merged(candidate_signal)
            )
        self.conversation.add_user(
            "Checkpoint requested (not a final report). Reason: "
            + str(reason)
            + "\n"
            + self._active_candidate_register()
            + _CHECKPOINT_RETENTION_INSTRUCTION
        )
        request_event_count = len(self._request_events)
        try:
            turn = self._request(tools_enabled=False, schema=_CHECKPOINT_SCHEMA)
        except (BudgetExhausted, TimeoutError):
            if (
                len(self._request_events) > request_event_count
                and self._request_events[-1].status == "completed"
            ):
                raise
            checkpoint = self._project_checkpoint(self._current_gaps)
            self.latest_checkpoint = (
                self._checkpoint_with_retention_unknown(checkpoint)
                if _candidate_retention_lost(
                    self._candidate_retention_signal,
                    checkpoint,
                    accounted_candidate_ids=self._accounted_candidate_ids(),
                )
                else checkpoint
            )
            self.state = SessionState.CHECKPOINT
            return self._snapshot(degraded=True)
        self._candidate_retention_signal = self._candidate_retention_signal.merged(
            _candidate_retention_signal(turn.content)
        )
        self.conversation.add_assistant_turn(
            reasoning=turn.reasoning,
            content=turn.content,
            calls=turn.tool_calls,
        )
        checkpoint = self._checkpoint_from_text(turn.content)
        needs_repair = checkpoint is None or _candidate_retention_lost(
            self._candidate_retention_signal,
            checkpoint,
            accounted_candidate_ids=self._accounted_candidate_ids(),
        )
        if needs_repair:
            self.conversation.add_user(
                "Repair the previous checkpoint as one JSON object matching the schema."
                + _CHECKPOINT_RETENTION_INSTRUCTION
            )
            repair_event_count = len(self._request_events)
            try:
                repair = self._request(tools_enabled=False, schema=_CHECKPOINT_SCHEMA)
            except (BudgetExhausted, TimeoutError):
                if (
                    len(self._request_events) > repair_event_count
                    and self._request_events[-1].status == "completed"
                ):
                    raise
                repair = None
            if repair is not None:
                self._candidate_retention_signal = (
                    self._candidate_retention_signal.merged(
                        _candidate_retention_signal(repair.content)
                    )
                )
                self.conversation.add_assistant_turn(
                    reasoning=repair.reasoning,
                    content=repair.content,
                    calls=repair.tool_calls,
                )
                checkpoint = self._checkpoint_from_text(repair.content)
        retention_unknown = _candidate_retention_lost(
            self._candidate_retention_signal,
            checkpoint,
            accounted_candidate_ids=self._accounted_candidate_ids(),
        )
        fallback_projection = checkpoint is None
        if fallback_projection:
            checkpoint = self._project_checkpoint(
                self._current_gaps,
                candidate_retention_unknown=retention_unknown,
            )
        elif retention_unknown:
            checkpoint = self._checkpoint_with_retention_unknown(checkpoint)
        self.latest_checkpoint = checkpoint
        self.state = SessionState.CHECKPOINT
        return self._snapshot(degraded=fallback_projection or retention_unknown)

    def _checkpoint_from_text(self, text: str) -> SessionCheckpoint | None:
        raw = _json_object(text)
        if raw is None or not isinstance(raw.get("unresolved"), list):
            return None
        retained = {record.id: record for record in self.evidence_store.snapshot().records}
        evidence_ids = list(dict.fromkeys(
            item for item in (
                _resolve_retained_evidence_id(value, retained)
                for value in _strings(raw.get("evidence_ids"))
            )
            if item is not None
        ))
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
                for raw_evidence_id in _strings(ids):
                    evidence_id = _resolve_retained_evidence_id(
                        raw_evidence_id, retained,
                    )
                    record = retained.get(evidence_id) if evidence_id else None
                    obligation = self.coverage.obligation(obligation_id)
                    if (
                        evidence_id is not None
                        and record is not None
                        and self._record_matches_obligation(record, obligation)
                    ):
                        self.coverage.attach_evidence(obligation_id, evidence_id)
                        evidence_ids.append(evidence_id)
        # The compact `inspected` checkpoint form associates retained inspected
        # evidence with covered assignment obligations; arbitrary IDs never do.
        for obligation_id in assigned:
            obligation = self.coverage.obligation(obligation_id)
            for evidence_id in evidence_ids:
                record = retained[evidence_id]
                if self._record_matches_obligation(record, obligation):
                    self.coverage.attach_evidence(obligation_id, evidence_id)
        for obligation_id in assigned.intersection(unresolved):
            self.coverage.mark_unresolved(obligation_id)
        declared_candidate_ids = set(_strings(raw.get("candidate_finding_ids")))
        candidates: dict[str, CandidateFinding] = {
            item.candidate_id: item for item in self.candidate_findings
        }
        # Legacy checkpoints may repeat full candidate objects. Treat them as
        # additions while preserving all previously active candidates.
        raw_candidates = raw.get("candidate_findings")
        new_candidates = raw.get("new_candidates")
        candidate_payloads: list[object] = []
        if isinstance(raw_candidates, list):
            candidate_payloads.extend(raw_candidates)
        if isinstance(new_candidates, list):
            candidate_payloads.extend(new_candidates)
        elif new_candidates is not None:
            return None
        for value in candidate_payloads:
            candidate = self._candidate_from_checkpoint(
                value,
                retained=retained,
                assigned=assigned,
            )
            if candidate is None:
                return None
            if declared_candidate_ids and candidate.candidate_id not in declared_candidate_ids:
                continue
            existing = candidates.get(candidate.candidate_id)
            if existing is not None and existing != candidate:
                # A repeated ID must not silently rewrite the retained finding.
                continue
            candidates[candidate.candidate_id] = candidate
            self._candidate_statuses[candidate.candidate_id] = "active"

        updates = raw.get("candidate_updates", [])
        if updates is None:
            updates = []
        if not isinstance(updates, list):
            return None
        for update in updates:
            if not isinstance(update, Mapping):
                return None
            candidate_id = str(update.get("candidate_id") or "").strip()
            status = str(update.get("status") or "").strip().lower()
            if (
                not candidate_id
                or status not in {"active", "withdrawn", "superseded"}
                or candidate_id not in self._candidate_statuses
            ):
                # Updates are authoritative only for controller-known IDs.
                return None
            if status == "active":
                if candidate_id not in candidates:
                    return None
            else:
                candidates.pop(candidate_id, None)
            self._candidate_statuses[candidate_id] = status

        self.candidate_findings = tuple(candidates[key] for key in sorted(candidates))
        self._current_gaps = self._derive_current_gaps()
        evidence_ids = list(dict.fromkeys(evidence_ids))
        return SessionCheckpoint(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=tuple(evidence_ids),
            hypotheses=_strings(raw.get("hypotheses")),
            candidate_finding_ids=tuple(
                item.candidate_id for item in self.candidate_findings
            ),
            obligation_statuses=tuple(sorted(self.coverage.obligation_statuses().items())),
            invariants_evaluated=_strings(raw.get("invariants_evaluated")),
            unknowns=self._current_gaps,
            proposed_next_actions=self._current_gaps,
        )

    def _candidate_from_checkpoint(
        self,
        value: object,
        *,
        retained: Mapping[str, EvidenceRecord],
        assigned: set[str],
    ) -> CandidateFinding | None:
        if not isinstance(value, Mapping):
            return None
        allowed = {
            "candidate_id", "root_cause_fingerprint", "claim",
            "affected_location", "causal_chain", "severity", "category",
            "supporting_evidence_ids", "contradicting_evidence_ids",
            "related_obligation_ids", "confidence_rationale",
            "user_visible_consequence", "manual_validation",
        }
        if set(value) - allowed:
            return None
        candidate_id = str(value.get("candidate_id") or "").strip()
        claim = str(value.get("claim") or "").strip()
        affected_location = str(value.get("affected_location") or "").strip()
        causal_chain = str(value.get("causal_chain") or "").strip()
        consequence = str(value.get("user_visible_consequence") or "").strip()
        validation = str(value.get("manual_validation") or "").strip()
        confidence_rationale = str(
            value.get("confidence_rationale") or ""
        ).strip()
        if not all((
            candidate_id, claim, affected_location, causal_chain,
            confidence_rationale, consequence, validation,
        )):
            return None
        raw_supporting = _strings(value.get("supporting_evidence_ids"))
        raw_contradicting = _strings(value.get("contradicting_evidence_ids"))
        supporting = tuple(dict.fromkeys(
            item for item in (
                _resolve_retained_evidence_id(value, retained)
                for value in raw_supporting
            )
            if item is not None
        ))
        contradicting = tuple(dict.fromkeys(
            item for item in (
                _resolve_retained_evidence_id(value, retained)
                for value in raw_contradicting
            )
            if item is not None
        ))
        obligations = _strings(value.get("related_obligation_ids"))
        if (
            not supporting
            or not obligations
            or any(item not in assigned for item in obligations)
        ):
            return None
        model_identities = {
            retained[item].model_identity
            for item in supporting
            if retained[item].model_identity
        }
        return CandidateFinding(
            candidate_id=candidate_id,
            root_cause_fingerprint=str(
                value.get("root_cause_fingerprint") or ""
            ).strip(),
            claim=claim,
            affected_location=affected_location,
            causal_chain=causal_chain,
            severity=str(value.get("severity") or "info").strip(),
            category=str(value.get("category") or "").strip(),
            supporting_evidence_ids=supporting,
            contradicting_evidence_ids=contradicting,
            related_obligation_ids=obligations,
            collector_session_id=self.session_id,
            model_identity=(
                next(iter(model_identities)) if len(model_identities) == 1 else ""
            ),
            confidence_rationale=_rewrite_rationale_evidence_ids(
                confidence_rationale, retained,
            ),
            user_visible_consequence=consequence,
            manual_validation=validation,
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

    def _checkpoint_with_retention_unknown(
        self, checkpoint: SessionCheckpoint,
    ) -> SessionCheckpoint:
        unknowns = tuple(dict.fromkeys((
            *checkpoint.unknowns,
            _CANDIDATE_RETENTION_UNKNOWN,
        )))
        return SessionCheckpoint(
            **{
                **checkpoint.__dict__,
                "unknowns": unknowns,
                "proposed_next_actions": tuple(dict.fromkeys((
                    *checkpoint.proposed_next_actions,
                    _CANDIDATE_RETENTION_UNKNOWN,
                ))),
            }
        )

    def _project_checkpoint(
        self,
        gaps: tuple[str, ...],
        *,
        candidate_retention_unknown: bool = False,
    ) -> SessionCheckpoint:
        for obligation_id in gaps:
            try:
                self.coverage.mark_unresolved(obligation_id)
            except KeyError:
                continue
        self._current_gaps = self._derive_current_gaps()
        checkpoint = SessionCheckpoint(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=tuple(
                record.id for record in self.evidence_store.snapshot().records
                if record.is_usable_for_coverage
            ),
            candidate_finding_ids=tuple(
                item.candidate_id for item in self.candidate_findings
            ),
            obligation_statuses=tuple(sorted(self.coverage.obligation_statuses().items())),
            unknowns=self._current_gaps,
            proposed_next_actions=self._current_gaps,
        )
        return (
            self._checkpoint_with_retention_unknown(checkpoint)
            if candidate_retention_unknown
            else checkpoint
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
            request_events=tuple(self._request_events),
            finalization_diagnostics=tuple(
                dict(item) for item in self._finalization_diagnostics
            ),
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
        self.lease.request_timeout(
            self.request_timeout_sec, now=self.clock(),
        )
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
            "source_access_requests": [
                item.as_dict() for item in self.source_access_requests
            ],
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
        self._recovery_turn_pending = True
        self.state = SessionState.EXPLORING
        return self._snapshot()

    def finalize(self) -> SessionResult:
        """Close the session from its latest authoritative checkpoint."""
        if self._final_result is not None:
            return self._final_result
        self.lease.request_timeout(
            self.request_timeout_sec, now=self.clock(),
        )
        self.state = SessionState.FINALIZING
        retention_unknown = _CANDIDATE_RETENTION_UNKNOWN in self.latest_checkpoint.unknowns
        report = self._checkpoint_finalization_report()
        self.state = SessionState.COMPLETE
        self._final_result = self._snapshot(
            report=report,
            degraded=retention_unknown,
        )
        return self._final_result

    def _cache_checkpoint_fallback(self) -> SessionResult:
        self.state = SessionState.COMPLETE
        self._final_result = self._snapshot(
            report=self._checkpoint_fallback_report(), degraded=True,
        )
        return self._final_result

    def _checkpoint_finalization_report(self) -> dict[str, Any]:
        checkpoint = self.latest_checkpoint
        covered = [
            obligation_id for obligation_id, status in checkpoint.obligation_statuses
            if status.value == "covered"
        ]
        return {
            "summary": "Specialist session closed from its latest valid checkpoint.",
            "recommendation": "controller-review-required",
            "candidate_finding_ids": list(checkpoint.candidate_finding_ids),
            "evidence_ids": list(checkpoint.evidence_ids),
            "covered_obligation_ids": covered,
            "unknowns": list(checkpoint.unknowns),
            "source": "checkpoint-finalization",
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
