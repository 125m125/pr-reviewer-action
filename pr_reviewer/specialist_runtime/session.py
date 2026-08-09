"""Continuous checkpoint/resume/recovery state for one specialist."""

from __future__ import annotations

import json
import inspect
import math
import re
import time
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from pr_reviewer.conversation import Conversation, EpochCompactionStats
from pr_reviewer.tool_loop import decode_native_tool_arguments, native_tool_request_key
from pr_reviewer.transport import ModelRequestError

from .budget import BudgetExhausted, BudgetLedger, SessionLease
from .callbacks import (
    CALLBACK_POOL,
    CallbackTimedOut,
    format_callback_error,
    mask_runtime_text,
)
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

COMPACTED_EVIDENCE_TOOL_NAME = "read_compacted_evidence"
COMPACTED_EVIDENCE_SCHEMA: dict[str, Any] = {
    "name": COMPACTED_EVIDENCE_TOOL_NAME,
    "description": (
        "Read a bounded excerpt from an evidence result that the controller "
        "explicitly marked as compacted. Only evidence IDs listed in a recent "
        "compaction marker are valid; this tool never reads arbitrary evidence "
        "or creates new evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "evidence_id": {
                "type": "string",
                "description": "Exact evidence ID from a compaction marker.",
            },
            "offset": {
                "type": "integer",
                "description": "Optional zero-based character offset (default 0).",
            },
            "limit": {
                "type": "integer",
                "description": "Optional excerpt size, capped by the controller.",
            },
        },
        "required": ["evidence_id"],
        "additionalProperties": False,
    },
}
_MAX_COMPACTED_EVIDENCE_READS = 4
_MAX_COMPACTED_EVIDENCE_READ_CHARS = 4_000
_CONTEXT_LIMIT_SIGNALS = (
    "context_length_exceeded",
    "context size",
    "maximum context",
    "prompt too long",
    "too many tokens",
)


def _is_context_limit_error(exc: BaseException) -> bool:
    """Classify only bounded provider context-pressure signals."""
    if isinstance(exc, (TimeoutError, KeyboardInterrupt)):
        return False
    if type(exc).__name__ == "CancelledError":
        return False
    if isinstance(exc, ModelRequestError):
        if exc.timeout or exc.status in {401, 403}:
            return False
        values = (
            format_callback_error(exc, limit=2_000),
            mask_runtime_text(exc.body, limit=2_000),
        )
    else:
        values = (format_callback_error(exc, limit=2_000),)
    text = "\n".join(values).casefold()
    return any(signal in text for signal in _CONTEXT_LIMIT_SIGNALS)

_CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "evidence_ids": {
            "type": "array", "maxItems": 40,
            "items": {"type": "string", "maxLength": 256},
        },
        "evidence_by_obligation": {"type": "object"},
        "inspected": {
            "type": "array", "maxItems": 40,
            "items": {"type": "string", "maxLength": 256},
        },
        "unresolved": {
            "type": "array", "maxItems": 40,
            "items": {"type": "string", "maxLength": 256},
        },
        "hypotheses": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 500},
        },
        "working_summary": {"type": "string", "maxLength": 2_000},
        "completed_steps": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 500},
        },
        "candidate_findings": {
            "type": "array", "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "maxLength": 128},
                    "root_cause_fingerprint": {"type": "string", "maxLength": 128},
                    "claim": {"type": "string", "maxLength": 300},
                    "affected_location": {"type": "string", "maxLength": 256},
                    "causal_chain": {"type": "string", "maxLength": 600},
                    "severity": {"type": "string", "maxLength": 32},
                    "category": {"type": "string", "maxLength": 64},
                    "supporting_evidence_ids": {
                        "type": "array", "maxItems": 12,
                        "items": {"type": "string", "maxLength": 256},
                    },
                    "contradicting_evidence_ids": {
                        "type": "array", "maxItems": 12,
                        "items": {"type": "string", "maxLength": 256},
                    },
                    "related_obligation_ids": {
                        "type": "array", "maxItems": 12,
                        "items": {"type": "string", "maxLength": 256},
                    },
                    "confidence_rationale": {
                        "type": "string", "maxLength": 700,
                        "description": (
                            "Typed consequence support declaration. Start with "
                            "consequence_support: and one of reachable_input_path, "
                            "failing_behavioral_test, violated_invariant, affected_consumer, "
                            "or contradicting_evidence, followed by evidence_ids containing "
                            "exact retained evidence IDs and the form's required key=value details."
                        ),
                    },
                    "user_visible_consequence": {"type": "string", "maxLength": 300},
                    "manual_validation": {"type": "string", "maxLength": 300},
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
        "invariants_evaluated": {
            "type": "array", "maxItems": 20,
            "items": {"type": "string", "maxLength": 500},
        },
        "unknowns": {
            "type": "array", "maxItems": 20,
            "items": {"type": "string", "maxLength": 500},
        },
        "proposed_next_actions": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 500},
        },
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
            "reason": {"type": "string", "maxLength": 300},
            "evidence_ids": {
                "type": "array", "maxItems": 12,
                "items": {"type": "string", "maxLength": 256},
            },
            "superseded_by": {"type": "string", "maxLength": 128},
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


class CheckpointDisposition(str, Enum):
    """Declared lifecycle action after a checkpoint validates."""

    COMPACT_RESUME = "compact_resume"
    PAUSE = "pause"
    FINALIZE = "finalize"


_CHECKPOINT_LIFECYCLE_INSTRUCTIONS = {
    CheckpointDisposition.COMPACT_RESUME: (
        "Immediate compaction after validation: yes. "
        "After validation, resume the specialist session."
    ),
    CheckpointDisposition.PAUSE: (
        "Immediate compaction after validation: no. "
        "After validation, pause for controller evaluation."
    ),
    CheckpointDisposition.FINALIZE: (
        "Immediate compaction after validation: no. "
        "After validation, finalize without resuming the specialist session."
    ),
}
_CHECKPOINT_CUMULATIVE_INSTRUCTION = (
    "This checkpoint must be cumulative and self-contained because it may "
    "become a future epoch boundary."
)
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
    "Keep checkpoints compact: emit at most 8 new candidates, with one concise "
    "sentence per claim/causal_chain/consequence/manual_validation field; keep "
    "claim under 300 characters, causal_chain and confidence_rationale under 600 "
    "characters, and consequence/manual_validation under 300 characters. "
    "Use only exact "
    "retained evidence IDs (evidence:<hash>) from successful tool results in "
    "evidence_ids and supporting_evidence_ids; repository paths are not evidence IDs."
    " JSON shape starts with {\"unresolved\":[\"OB-id\"],\"candidate_updates\":[]}."
)
_CHECKPOINT_REPAIR_INSTRUCTION = (
    "Repair the previous checkpoint as one JSON object matching the schema."
    + _CHECKPOINT_RETENTION_INSTRUCTION
)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _bounded_text(value: object, *, max_length: int) -> str:
    return str(value).strip()[:max_length] if value is not None else ""


def _bounded_strings(
    value: object,
    *,
    max_items: int,
    max_length: int,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    items: list[str] = []
    for item in value:
        text = _bounded_text(item, max_length=max_length)
        if text and text not in items:
            items.append(text)
        if len(items) == max_items:
            break
    return tuple(items)


def _textual_tool_call_reason(value: object) -> str | None:
    """Detect provider text that looks like a tool call but was not native."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.casefold()
    if "</tool_call>" in text or "</function>" in text:
        return "textual tool-call closing markup"
    if "<tool_call" in text or "<parameter" in text:
        return "textual tool-call markup"
    if re.search(r"\[(?:read|git)_[a-z0-9_-]+\]\s*<", text):
        return "bracketed textual tool-call markup"
    return None


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


def _contains_candidate_json_syntax(text: str) -> bool:
    """Require a candidate key followed by a JSON-like value delimiter."""
    if not isinstance(text, str):
        return False
    return bool(re.search(
        r"[\"']?candidate_(?:findings|updates|new_candidates|finding_ids)"
        r"[\"']?\s*:\s*[\[{]",
        text,
        flags=re.IGNORECASE,
    ))


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
    lowered_text = text.casefold() if isinstance(text, str) else ""
    has_candidate_key = any(
        key in lowered_text for key in (
            "candidate_findings", "candidate_updates", "new_candidates",
            "candidate_finding_ids",
        )
    )
    structured_text = bool(
        isinstance(text, str)
        and (
            text.strip().startswith(("{", "[", "```"))
            or (has_candidate_key and _contains_candidate_json_syntax(text))
        )
    )
    if not isinstance(raw, Mapping):
        # Malformed structured candidate payloads remain conservative, while
        # ordinary prose containing no JSON candidate keys is non-material.
        return _CandidateRetentionSignal(
            unidentified_shapes=(
                1 if structured_text and _contains_candidate_shaped_text(text) else 0
            ),
        )
    if not any(
        key in raw for key in (
            "candidate_finding_ids", "candidate_findings",
            "candidate_updates", "new_candidates",
        )
    ):
        return _CandidateRetentionSignal(
            unidentified_shapes=(
                1 if structured_text and _contains_candidate_shaped_text(text) else 0
            ),
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
                    if (
                        str(value.get("status") or "").strip().lower() == "superseded"
                        and not str(value.get("superseded_by") or "").strip()
                    ):
                        unidentified_shapes += 1
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
    finish_reason: str = ""
    text_source: str = ""
    tool_call_count: int = 0


@dataclass(frozen=True)
class _AdmissionEstimate:
    """Bounded request-size projection used before provider transport."""

    mode: str
    input_tokens: int
    response_tokens: int
    safety_tokens: int
    admission_tokens: int
    source: str
    rendered_bytes: int
    coarse_input_tokens: int


@dataclass
class _AdmissionCalibration:
    """Conservative provider calibration for one rendered request mode."""

    last_rendered_bytes: int = 0
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    max_tokens_per_rendered_byte: float = 0.0
    max_positive_offset: int = 0


@dataclass(frozen=True)
class _CheckpointSpan:
    """Controller-owned event span for one validated model checkpoint."""

    request_start: int
    response_end: int
    disposition: CheckpointDisposition
    compacted: bool = False


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
        self.checkpoint_max_tokens = min(
            max_tokens,
            max(2_048, self.recovery_max_tokens * 2),
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
        self._tool_call_evidence_ids: dict[str, str] = {}
        self._compacted_evidence: dict[str, EvidenceRecord] = {}
        self._compacted_evidence_read_keys: set[tuple[str, int, int]] = set()
        self._compacted_evidence_reads = 0
        self._checkpoint_spans: list[_CheckpointSpan] = []
        self._tool_lease_exhausted = False
        self._recovery_turn_pending = False
        self._emergency_checkpoint_attempted = False
        self._final_result: SessionResult | None = None
        self._request_events: list[SpecialistRequestEvent] = []
        self._finalization_diagnostics: list[dict[str, object]] = []
        self._last_context_admission: dict[str, object] = {}
        self._admission_calibration = {
            "tools": _AdmissionCalibration(),
            "structured": _AdmissionCalibration(),
        }
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
        return "Active candidates (use these exact IDs for updates): " + json.dumps(
            entries, sort_keys=True,
        )

    def _checkpoint_prompt(
        self,
        reason: str,
        disposition: CheckpointDisposition | str,
    ) -> str:
        disposition = CheckpointDisposition(disposition)
        return (
            "Checkpoint requested (not a final report). Checkpoint reason: "
            + str(reason)
            + ".\n"
            + _CHECKPOINT_LIFECYCLE_INSTRUCTIONS[disposition]
            + "\n"
            + _CHECKPOINT_CUMULATIVE_INSTRUCTION
            + "\n"
            + self._active_candidate_register()
            + _CHECKPOINT_RETENTION_INSTRUCTION
        )

    @staticmethod
    def _usage_tokens(usage: Mapping[str, Any], key: str) -> int:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        if not math.isfinite(float(value)) or value <= 0:
            return 0
        return math.ceil(value)

    def _request_mode(self, tools_enabled: bool) -> str:
        return "tools" if tools_enabled else "structured"

    def _request_schema_name(self, schema: dict[str, Any] | None) -> str | None:
        return "specialist_checkpoint" if schema is _CHECKPOINT_SCHEMA else None

    def _renderable_request(
        self,
        *,
        tools_enabled: bool,
        schema: dict[str, Any] | None,
        max_tokens: int,
        conversation: Conversation | None = None,
        allow_fallbacks: bool = True,
    ) -> ModelTurnRequest:
        return ModelTurnRequest(
            role="specialist",
            conversation=self.conversation if conversation is None else conversation,
            max_tokens=max_tokens,
            response_schema=schema,
            tools_enabled=tools_enabled,
            timeout_sec=self.request_timeout_sec,
            deadline_at=self.lease.deadline_at,
            stream=self.stream,
            response_schema_name=self._request_schema_name(schema),
            reasoning_effort="none" if not tools_enabled else None,
            allow_fallbacks=allow_fallbacks,
        )

    def _estimate_admission(
        self,
        *,
        tools_enabled: bool,
        max_tokens: int,
        schema: dict[str, Any] | None = None,
        conversation: Conversation | None = None,
    ) -> _AdmissionEstimate:
        mode = self._request_mode(tools_enabled)
        rendered_conversation = (
            self.conversation if conversation is None else conversation
        )
        coarse_tokens = max(0, int(rendered_conversation.approx_tokens()))
        renderer = getattr(self.gateway, "rendered_request_bytes", None)
        rendered_bytes = 0
        if callable(renderer):
            try:
                value = renderer(self._renderable_request(
                    tools_enabled=tools_enabled,
                    schema=(schema if schema is not None else (
                        None if tools_enabled else _CHECKPOINT_SCHEMA
                    )),
                    max_tokens=max_tokens,
                    conversation=rendered_conversation,
                ))
                if not isinstance(value, bool):
                    rendered_bytes = max(0, int(value))
            except (TypeError, ValueError, OverflowError):
                rendered_bytes = 0

        calibration = self._admission_calibration[mode]
        candidates = [(coarse_tokens, "coarse-conversation", 0)]
        if rendered_bytes > 0:
            candidates.append((
                math.ceil(rendered_bytes / 3), "rendered-fallback", 1,
            ))
            if calibration.max_tokens_per_rendered_byte > 0:
                candidates.append((
                    math.ceil(
                        rendered_bytes
                        * calibration.max_tokens_per_rendered_byte
                    ),
                    "provider-calibrated",
                    2,
                ))
            if calibration.max_positive_offset > 0:
                candidates.append((
                    coarse_tokens + calibration.max_positive_offset,
                    "provider-calibrated",
                    2,
                ))
        input_tokens, source, _priority = max(
            candidates, key=lambda item: (item[0], item[2]),
        )
        response_tokens = max(0, int(max_tokens))
        admission_tokens = (
            input_tokens + response_tokens + self.wire_safety_tokens
        )
        return _AdmissionEstimate(
            mode=mode,
            input_tokens=input_tokens,
            response_tokens=response_tokens,
            safety_tokens=self.wire_safety_tokens,
            admission_tokens=admission_tokens,
            source=source,
            rendered_bytes=rendered_bytes,
            coarse_input_tokens=coarse_tokens,
        )

    def _record_admission_calibration(
        self,
        estimate: _AdmissionEstimate,
        usage: Mapping[str, Any],
    ) -> tuple[int, int]:
        calibration = self._admission_calibration[estimate.mode]
        prompt_tokens = self._usage_tokens(usage, "prompt_tokens")
        completion_tokens = self._usage_tokens(usage, "completion_tokens")
        calibration.last_rendered_bytes = estimate.rendered_bytes
        calibration.last_completion_tokens = completion_tokens
        if prompt_tokens > 0:
            calibration.last_prompt_tokens = prompt_tokens
            if estimate.rendered_bytes > 0:
                calibration.max_tokens_per_rendered_byte = max(
                    calibration.max_tokens_per_rendered_byte,
                    prompt_tokens / estimate.rendered_bytes,
                )
            calibration.max_positive_offset = max(
                calibration.max_positive_offset,
                prompt_tokens - estimate.coarse_input_tokens,
            )
        return prompt_tokens, completion_tokens

    def _checkpoint_pressure_due(self) -> bool:
        projected = Conversation(
            system=self.conversation.system,
            events=list(self.conversation.events),
            tool_schemas=list(self.conversation.tool_schemas),
        )
        projected.add_user(self._checkpoint_prompt(
            "context-pressure", CheckpointDisposition.COMPACT_RESUME,
        ))
        checkpoint = self._estimate_admission(
            tools_enabled=False,
            max_tokens=self.checkpoint_max_tokens,
            schema=_CHECKPOINT_SCHEMA,
            conversation=projected,
        )
        repair_instruction_tokens = math.ceil(
            len(_CHECKPOINT_REPAIR_INSTRUCTION.encode("utf-8")) / 3
        )
        reserved_tokens = (
            checkpoint.input_tokens
            + (self.checkpoint_max_tokens * 2)
            + repair_instruction_tokens
            + self.wire_safety_tokens
        )
        return reserved_tokens >= self.max_context_tokens

    def _request(
        self,
        *,
        tools_enabled: bool,
        schema: dict[str, Any] | None,
        purpose: str = "unknown",
        max_output_tokens: int | None = None,
        allow_compaction: bool = True,
        allow_gateway_fallbacks: bool = True,
    ) -> ModelTurnResult:
        remaining_output_tokens = self.budget.remaining_output_tokens()
        if remaining_output_tokens is not None and remaining_output_tokens <= 0:
            raise BudgetExhausted("output token limit exhausted")
        configured_max_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else (
                self.recovery_max_tokens
                if self._recovery_turn_pending
                else self.max_tokens
            )
        )
        request_max_tokens = (
            configured_max_tokens
            if remaining_output_tokens is None
            else min(configured_max_tokens, remaining_output_tokens)
        )
        admission = self._estimate_admission(
            tools_enabled=tools_enabled,
            max_tokens=request_max_tokens,
            schema=schema,
        )
        remaining_input_tokens = self.budget.remaining_input_tokens()
        if (
            remaining_input_tokens is not None
            and admission.input_tokens > remaining_input_tokens
        ):
            raise BudgetExhausted("input token limit exhausted")
        self._last_context_admission = {
            "context_tokens_before": admission.input_tokens,
            "context_tokens_after": admission.input_tokens,
            "max_context_tokens": self.max_context_tokens,
            "requested_output_tokens": request_max_tokens,
            "wire_safety_tokens": self.wire_safety_tokens,
            "rendered_request_bytes": admission.rendered_bytes,
            "admission_tokens": admission.admission_tokens,
            "admission_source": admission.source,
            "compacted_evidence_count": 0,
            "assistant_messages_compacted": 0,
        }
        if admission.admission_tokens > self.max_context_tokens:
            if allow_compaction:
                evidence_before = len(self._compacted_evidence)
                assistant_before = len(self._assistant_analysis_bodies())
                self._compact_conversation()
                admission = self._estimate_admission(
                    tools_enabled=tools_enabled,
                    max_tokens=request_max_tokens,
                    schema=schema,
                )
                self._last_context_admission.update({
                    "context_tokens_after": admission.input_tokens,
                    "rendered_request_bytes": admission.rendered_bytes,
                    "admission_tokens": admission.admission_tokens,
                    "admission_source": admission.source,
                    "compacted_evidence_count": (
                        len(self._compacted_evidence) - evidence_before
                    ),
                    "assistant_messages_compacted": max(
                        0, assistant_before - len(self._assistant_analysis_bodies()),
                    ),
                })
            if admission.admission_tokens > self.max_context_tokens:
                raise BudgetExhausted(
                    "model context limit cannot admit input and requested output"
                )
        timeout = self.lease.request_timeout(
            self.request_timeout_sec, now=self.clock(),
        )
        self.budget.reserve_model_turn()
        self._request_turn += 1
        request_id = f"{self.session_id}:model:{self._request_turn}"
        schema_name = self._request_schema_name(schema)
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
                input_tokens=admission.input_tokens,
                max_output_tokens=request_max_tokens,
                admission_tokens=admission.admission_tokens,
                admission_source=admission.source,
                purpose=purpose,
            )
        try:
            request = self._renderable_request(
                tools_enabled=tools_enabled,
                schema=schema,
                max_tokens=request_max_tokens,
                allow_fallbacks=allow_gateway_fallbacks,
            )
            request = replace(request, timeout_sec=timeout)
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
                self._request_attempt_journal.finish(
                    request_id,
                    terminal_status,
                    error=format_callback_error(exc, limit=500),
                )
            self._request_events.append(SpecialistRequestEvent(
                request_id,
                terminal_status,
                tools_enabled,
                schema_name,
                format_callback_error(exc, limit=500),
            ))
            raise
        actual_prompt_tokens = self._usage_tokens(
            result.usage, "prompt_tokens",
        )
        actual_completion_tokens = self._usage_tokens(
            result.usage, "completion_tokens",
        )
        if self._request_attempt_journal is not None:
            self._request_attempt_journal.finish(
                request_id,
                "completed",
                finish_reason=result.finish_reason,
                text_source=result.text_source,
                tool_call_count=len(result.tool_calls),
                actual_prompt_tokens=actual_prompt_tokens,
                actual_completion_tokens=actual_completion_tokens,
            )
        self._request_events.append(SpecialistRequestEvent(
            request_id, "completed", tools_enabled, schema_name,
            finish_reason=result.finish_reason,
            text_source=result.text_source,
            tool_call_count=len(result.tool_calls),
        ))
        prompt_tokens, completion_tokens = self._record_admission_calibration(
            admission, result.usage,
        )
        self.budget.record_model_usage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
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
        resuming_checkpoint = self.state is SessionState.CHECKPOINT
        if resuming_checkpoint and self._checkpoint_spans:
            continuation_admission = self._estimate_admission(
                tools_enabled=True,
                max_tokens=self.max_tokens,
            )
            continuation_pressure = (
                continuation_admission.admission_tokens > self.max_context_tokens
                or self._checkpoint_pressure_due()
            )
            if continuation_pressure:
                self._compact_validated_epoch()
                continuation_admission = self._estimate_admission(
                    tools_enabled=True,
                    max_tokens=self.max_tokens,
                )
                continuation_pressure = (
                    continuation_admission.admission_tokens
                    > self.max_context_tokens
                    or self._checkpoint_pressure_due()
                )
                if continuation_pressure:
                    reconstructed = self._reconstruct_from_valid_checkpoint()
                    continuation_admission = self._estimate_admission(
                        tools_enabled=True,
                        max_tokens=self.max_tokens,
                    )
                    continuation_pressure = (
                        not reconstructed
                        or continuation_admission.admission_tokens
                        > self.max_context_tokens
                        or self._checkpoint_pressure_due()
                    )
                if continuation_pressure:
                    self.state = SessionState.CHECKPOINT
                    return self._snapshot(degraded=True)
        self.state = SessionState.EXPLORING
        while True:
            if self.conversation.approx_tokens() > self.max_context_tokens:
                return self.request_checkpoint(
                    "context-pressure",
                    disposition=CheckpointDisposition.COMPACT_RESUME,
                )
            remaining_input_tokens = self.budget.remaining_input_tokens()
            if (
                remaining_input_tokens is not None
                and self.conversation.approx_tokens() > remaining_input_tokens
            ):
                raise BudgetExhausted("input token limit exhausted")
            remaining_output_tokens = self.budget.remaining_output_tokens()
            if remaining_output_tokens is not None and remaining_output_tokens <= 0:
                raise BudgetExhausted("output token limit exhausted")
            if self._checkpoint_pressure_due():
                return self.request_checkpoint(
                    "context-pressure",
                    disposition=CheckpointDisposition.COMPACT_RESUME,
                )
            if self.budget.remaining_model_turns() <= _CHECKPOINT_TURN_RESERVE:
                return self.request_checkpoint("checkpoint-retention-reserve")
            try:
                turn = self._request(
                    tools_enabled=True, schema=None, purpose="exploration",
                )
            except BudgetExhausted as exc:
                if "model context limit" not in str(exc):
                    raise
                return self.request_checkpoint(
                    "context-pressure",
                    disposition=CheckpointDisposition.COMPACT_RESUME,
                )
            except BaseException as exc:
                if not _is_context_limit_error(exc):
                    raise
                return self._recover_from_provider_context_limit(exc)
            self._candidate_retention_signal = (
                self._candidate_retention_signal.merged(
                    _candidate_retention_signal(turn.content)
                )
            )
            assistant_start = len(self.conversation.events)
            self.conversation.add_assistant_turn(
                reasoning=turn.reasoning,
                content=turn.content,
                calls=turn.tool_calls,
            )
            if not turn.tool_calls:
                textual_tool_reason = _textual_tool_call_reason(turn.content)
                if textual_tool_reason is not None:
                    self.budget.record_tool_rejection(textual_tool_reason)
                    self.conversation.add_user(
                        "The previous response contained textual tool-call markup, "
                        "which was not executed. Use the advertised native tool "
                        "calls instead; do not emit XML, function, or parameter "
                        "markup. Continue the investigation or return a checkpoint."
                    )
                    if self.budget.record_no_progress() >= self.max_no_progress_streak:
                        return self.request_checkpoint(
                            "malformed-textual-tool-call",
                            disposition=CheckpointDisposition.COMPACT_RESUME,
                        )
                    continue
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
                self._checkpoint_spans.append(_CheckpointSpan(
                    request_start=assistant_start,
                    response_end=len(self.conversation.events),
                    disposition=CheckpointDisposition.PAUSE,
                ))
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
                    return self.request_checkpoint(
                        "no-progress-guard",
                        disposition=CheckpointDisposition.COMPACT_RESUME,
                    )

    def _recover_from_provider_context_limit(
        self, provider_error: BaseException,
    ) -> SessionResult:
        """Attempt one no-tools checkpoint, then use only validated state."""
        emergency_error = provider_error
        if not self._emergency_checkpoint_attempted:
            self._emergency_checkpoint_attempted = True
            previous_checkpoint = self.latest_checkpoint
            previous_candidates = self.candidate_findings
            previous_candidate_statuses = dict(self._candidate_statuses)
            previous_gaps = self._current_gaps
            previous_coverage = self.coverage.snapshot()
            previous_retention_signal = self._candidate_retention_signal
            previous_event_count = len(self.conversation.events)

            def rollback_tentative_checkpoint() -> None:
                self.latest_checkpoint = previous_checkpoint
                self.candidate_findings = previous_candidates
                self._candidate_statuses = previous_candidate_statuses
                self._current_gaps = previous_gaps
                self._candidate_retention_signal = previous_retention_signal
                del self.conversation.events[previous_event_count:]
                self.coverage.replace_reconciled_state(
                    dict(previous_coverage.evidence_by_obligation),
                    (
                        obligation_id
                        for obligation_id, status
                        in previous_coverage.obligation_statuses
                        if status is ObligationStatus.UNRESOLVED
                    ),
                )

            try:
                emergency_result = self.request_checkpoint(
                    "provider-context-limit",
                    disposition=CheckpointDisposition.COMPACT_RESUME,
                    allow_gateway_fallbacks=False,
                    allow_repair=False,
                )
            except BaseException as exc:
                rollback_tentative_checkpoint()
                if not _is_context_limit_error(exc):
                    raise
                emergency_error = exc
            else:
                if not emergency_result.degraded:
                    return emergency_result
                rollback_tentative_checkpoint()
                return self._fallback_after_emergency_checkpoint(
                    emergency_error,
                    diagnostic_recorded=True,
                )

        return self._fallback_after_emergency_checkpoint(emergency_error)

    def _fallback_after_emergency_checkpoint(
        self,
        emergency_error: BaseException,
        *,
        diagnostic_recorded: bool = False,
    ) -> SessionResult:
        if self._reconstruct_from_valid_checkpoint():
            if diagnostic_recorded and self._finalization_diagnostics:
                self._finalization_diagnostics[-1]["fallback_projection"] = False
                self._finalization_diagnostics[-1]["retention_unknown"] = False
            else:
                self._record_checkpoint_diagnostic(
                    reason="provider-context-limit",
                    initial_parse="unavailable",
                    repair_attempted=False,
                    repair_parse="not_attempted",
                    fallback_projection=False,
                    retention_unknown=False,
                    initial_error=format_callback_error(emergency_error, limit=300),
                    context_admission=self._last_context_admission,
                )
            self.state = SessionState.CHECKPOINT
            return self._snapshot()

        self.latest_checkpoint = self._project_checkpoint(
            self._current_gaps,
            candidate_retention_unknown=True,
        )
        if diagnostic_recorded and self._finalization_diagnostics:
            self._finalization_diagnostics[-1]["retention_unknown"] = True
        else:
            self._record_checkpoint_diagnostic(
                reason="provider-context-limit",
                initial_parse="unavailable",
                repair_attempted=False,
                repair_parse="not_attempted",
                fallback_projection=True,
                retention_unknown=True,
                initial_error=format_callback_error(emergency_error, limit=300),
                context_admission=self._last_context_admission,
            )
        self.state = SessionState.CHECKPOINT
        return self._snapshot(degraded=True)

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
            if name == COMPACTED_EVIDENCE_TOOL_NAME:
                self.conversation.add_tool_result(
                    call_id, self._read_compacted_evidence(arguments),
                )
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
                self._tool_call_evidence_ids[call_id] = prior.id
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
            self._tool_call_evidence_ids[call_id] = record.id
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
        disposition: CheckpointDisposition | str = CheckpointDisposition.PAUSE,
        candidate_signal: _CandidateRetentionSignal | None = None,
        allow_gateway_fallbacks: bool = True,
        allow_repair: bool = True,
    ) -> SessionResult:
        """Request a structured checkpoint; never force a final report."""
        disposition = CheckpointDisposition(disposition)
        if candidate_signal is not None:
            self._candidate_retention_signal = (
                self._candidate_retention_signal.merged(candidate_signal)
            )
        checkpoint_request_start = len(self.conversation.events)
        self.conversation.add_user(self._checkpoint_prompt(reason, disposition))
        request_event_count = len(self._request_events)
        try:
            turn = self._request(
                tools_enabled=False,
                schema=_CHECKPOINT_SCHEMA,
                purpose="checkpoint",
                max_output_tokens=self.checkpoint_max_tokens,
                allow_compaction=False,
                allow_gateway_fallbacks=allow_gateway_fallbacks,
            )
        except (BudgetExhausted, TimeoutError) as exc:
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
            self._record_checkpoint_diagnostic(
                reason=reason,
                initial_parse="unavailable",
                repair_attempted=False,
                repair_parse="not_attempted",
                fallback_projection=True,
                retention_unknown=(
                    _CANDIDATE_RETENTION_UNKNOWN
                    in self.latest_checkpoint.unknowns
                ),
                initial_error=f"{type(exc).__name__}: {exc}",
                context_admission=self._last_context_admission,
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
        initial_parse = "valid" if checkpoint is not None else "invalid"
        needs_repair = checkpoint is None or _candidate_retention_lost(
            self._candidate_retention_signal,
            checkpoint,
            accounted_candidate_ids=self._accounted_candidate_ids(),
        )
        repair_attempted = False
        repair_parse = "not_attempted"
        initial_finish_reason = turn.finish_reason
        repair_finish_reason = ""
        repair_error = ""
        if needs_repair and allow_repair:
            repair_attempted = True
            self.conversation.add_user(_CHECKPOINT_REPAIR_INSTRUCTION)
            repair_event_count = len(self._request_events)
            try:
                repair = self._request(
                    tools_enabled=False,
                    schema=_CHECKPOINT_SCHEMA,
                    purpose="checkpoint-repair",
                    max_output_tokens=self.checkpoint_max_tokens,
                    allow_compaction=False,
                    allow_gateway_fallbacks=allow_gateway_fallbacks,
                )
            except (BudgetExhausted, TimeoutError) as exc:
                if (
                    len(self._request_events) > repair_event_count
                    and self._request_events[-1].status == "completed"
                ):
                    raise
                repair = None
                repair_error = f"{type(exc).__name__}: {exc}"
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
                repair_parse = "valid" if checkpoint is not None else "invalid"
                repair_finish_reason = repair.finish_reason
            else:
                repair_parse = "unavailable"
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
        # Keep one bounded diagnostic for every checkpoint request, including
        # successful first-pass checkpoints.  This makes the lifecycle log
        # distinguish “valid checkpoint accepted” from “repair/fallback”
        # instead of only explaining failures.
        self._record_checkpoint_diagnostic(
            reason=reason,
            initial_parse=initial_parse,
            repair_attempted=repair_attempted,
            repair_parse=repair_parse,
            fallback_projection=fallback_projection,
            retention_unknown=retention_unknown,
            initial_finish_reason=initial_finish_reason,
            repair_finish_reason=repair_finish_reason,
            repair_error=repair_error,
            context_admission=self._last_context_admission,
        )
        self.latest_checkpoint = checkpoint
        if not fallback_projection and not retention_unknown:
            self._checkpoint_spans.append(_CheckpointSpan(
                request_start=checkpoint_request_start,
                response_end=len(self.conversation.events),
                disposition=disposition,
            ))
            if disposition is CheckpointDisposition.COMPACT_RESUME:
                self._compact_validated_epoch()
        self.state = SessionState.CHECKPOINT
        return self._snapshot(degraded=fallback_projection or retention_unknown)

    def _checkpoint_from_text(self, text: str) -> SessionCheckpoint | None:
        raw = _json_object(text)
        if raw is None or not isinstance(raw.get("unresolved"), list):
            return None
        previous = self.latest_checkpoint
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
        candidate_statuses = dict(self._candidate_statuses)
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
            candidate_statuses[candidate.candidate_id] = "active"

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
                or candidate_id not in candidate_statuses
            ):
                # Updates are authoritative only for controller-known IDs.
                return None
            if status == "active":
                if candidate_id not in candidates:
                    return None
            elif status == "superseded":
                replacement_id = str(update.get("superseded_by") or "").strip()
                if (
                    not replacement_id
                    or replacement_id == candidate_id
                    or replacement_id not in candidates
                ):
                    return None
                candidates.pop(candidate_id, None)
            else:
                candidates.pop(candidate_id, None)
            candidate_statuses[candidate_id] = status

        # Superseded candidates must point to a replacement that remains active
        # after all updates in this checkpoint have been applied.
        for update in updates:
            if str(update.get("status") or "").strip().lower() != "superseded":
                continue
            replacement_id = str(update.get("superseded_by") or "").strip()
            if replacement_id not in candidates:
                return None

        self.candidate_findings = tuple(candidates[key] for key in sorted(candidates))
        self._candidate_statuses = candidate_statuses
        self._current_gaps = self._derive_current_gaps()
        evidence_ids = list(dict.fromkeys(evidence_ids))
        return SessionCheckpoint(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=tuple(evidence_ids),
            imported_evidence_ids=previous.imported_evidence_ids,
            working_summary=(
                _bounded_text(raw.get("working_summary"), max_length=2_000)
                if "working_summary" in raw else previous.working_summary
            ),
            completed_steps=(
                _bounded_strings(
                    raw.get("completed_steps"), max_items=12, max_length=500,
                )
                if "completed_steps" in raw else previous.completed_steps
            ),
            hypotheses=(
                _bounded_strings(
                    raw.get("hypotheses"), max_items=12, max_length=500,
                )
                if "hypotheses" in raw else previous.hypotheses
            ),
            candidate_finding_ids=tuple(
                item.candidate_id for item in self.candidate_findings
            ),
            obligation_statuses=tuple(sorted(self.coverage.obligation_statuses().items())),
            invariants_evaluated=(
                _bounded_strings(
                    raw.get("invariants_evaluated"), max_items=20, max_length=500,
                )
                if "invariants_evaluated" in raw
                else previous.invariants_evaluated
            ),
            unknowns=self._current_gaps,
            proposed_next_actions=(
                _bounded_strings(
                    raw.get("proposed_next_actions"),
                    max_items=12,
                    max_length=500,
                )
                or self._current_gaps
                if "proposed_next_actions" in raw
                else previous.proposed_next_actions
            ),
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
        previous = getattr(self, "latest_checkpoint", None)
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
            imported_evidence_ids=(
                previous.imported_evidence_ids if previous is not None else ()
            ),
            working_summary=previous.working_summary if previous is not None else "",
            completed_steps=previous.completed_steps if previous is not None else (),
            hypotheses=previous.hypotheses if previous is not None else (),
            candidate_finding_ids=tuple(
                item.candidate_id for item in self.candidate_findings
            ),
            obligation_statuses=tuple(sorted(self.coverage.obligation_statuses().items())),
            invariants_evaluated=(
                previous.invariants_evaluated if previous is not None else ()
            ),
            unknowns=self._current_gaps,
            proposed_next_actions=(
                previous.proposed_next_actions
                if previous is not None else self._current_gaps
            ),
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

    def _compaction_replacements(self, end_index: int) -> dict[str, dict[str, object]]:
        retained = {
            record.id: record
            for record in self.evidence_store.snapshot().records
            if record.is_usable_for_coverage
        }
        replacements: dict[str, dict[str, object]] = {}
        for event in self.conversation.events[:end_index]:
            if event.get("kind") != "tool_result":
                continue
            call_id = str(event.get("call_id") or "")
            evidence_id = self._tool_call_evidence_ids.get(call_id, "")
            record = retained.get(evidence_id)
            if record is None:
                continue
            replacements[call_id] = {
                "status": "compacted",
                "evidence_id": record.id,
                "source_path": record.source_path or record.source_identity,
                "original_bytes": len(record.content.encode("utf-8")),
            }
        return replacements

    def _compacted_evidence_catalogue(
        self,
        *,
        max_bytes: int | None = None,
        priority_evidence_ids: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        entries = []
        ordered_ids = tuple(dict.fromkeys((
            *(
                evidence_id
                for evidence_id in priority_evidence_ids
                if evidence_id in self._compacted_evidence
            ),
            *sorted(self._compacted_evidence),
        )))
        for evidence_id in ordered_ids[:20]:
            record = self._compacted_evidence[evidence_id]
            entry = {
                "evidence_id": evidence_id,
                "source_path": record.source_path or record.source_identity,
                "original_bytes": len(record.content.encode("utf-8")),
            }
            candidate = [*entries, entry]
            if (
                max_bytes is not None
                and len(json.dumps(candidate, sort_keys=True).encode("utf-8"))
                > max(0, max_bytes)
            ):
                break
            entries.append(entry)
        return entries

    def _bounded_reconstruction_checkpoint(self) -> dict[str, object]:
        """Project cumulative state with a fixed-size evidence ledger."""
        payload = self._cumulative_checkpoint_payload()
        active_evidence_ids = tuple(dict.fromkeys((
            *(
                evidence_id
                for candidate in self.candidate_findings
                for evidence_id in (
                    *candidate.supporting_evidence_ids,
                    *candidate.contradicting_evidence_ids,
                )
            ),
            *self.latest_checkpoint.evidence_ids,
        )))[:40]
        active_evidence_set = set(active_evidence_ids)
        checkpoint = payload.get("latest_checkpoint")
        if isinstance(checkpoint, dict):
            checkpoint["evidence_ids"] = list(active_evidence_ids)
            checkpoint["imported_evidence_ids"] = list(
                self.latest_checkpoint.imported_evidence_ids[:40]
            )
        coverage = payload.get("coverage")
        if isinstance(coverage, dict):
            by_obligation = coverage.get("evidence_by_obligation")
            if isinstance(by_obligation, dict):
                coverage["evidence_by_obligation"] = {
                    obligation_id: [
                        evidence_id
                        for evidence_id in evidence_ids
                        if evidence_id in active_evidence_set
                    ][:40]
                    for obligation_id, evidence_ids in by_obligation.items()
                }
        active_candidate_ids = {
            candidate.candidate_id for candidate in self.candidate_findings
        }
        statuses = payload.get("candidate_statuses")
        if isinstance(statuses, dict):
            payload["candidate_statuses"] = {
                candidate_id: status
                for candidate_id, status in statuses.items()
                if candidate_id in active_candidate_ids
            }

        metadata_budget = max(0, self.recovery_evidence_bytes // 2)
        bounded_metadata: list[object] = []
        for item in payload.get("evidence_metadata", ()):
            if not isinstance(item, dict):
                continue
            if active_evidence_set and item.get("id") not in active_evidence_set:
                continue
            candidate = [*bounded_metadata, item]
            if (
                len(json.dumps(candidate, sort_keys=True).encode("utf-8"))
                > metadata_budget
            ):
                break
            bounded_metadata.append(item)
        payload["evidence_metadata"] = bounded_metadata
        return payload

    def _compact_validated_epoch(self) -> EpochCompactionStats:
        """Compact only history closed by the latest validated checkpoint."""
        if not self._checkpoint_spans:
            return EpochCompactionStats()
        latest = self._checkpoint_spans[-1]
        if latest.compacted:
            return EpochCompactionStats()

        old_events = list(self.conversation.events)
        span_markers = [
            (old_events[span.request_start], old_events[span.response_end - 1])
            for span in self._checkpoint_spans
        ]
        protected_ids = {id(old_events[0])} if old_events else set()
        for span in self._checkpoint_spans:
            protected_ids.update(
                id(event)
                for event in old_events[span.request_start:span.response_end]
            )

        prune_before = (
            self._checkpoint_spans[-2].request_start
            if len(self._checkpoint_spans) >= 2
            else 0
        )
        removed_old_events = 0
        pruned_evidence_ids: set[str] = set()
        working: list[dict[str, Any]] = []
        for index, event in enumerate(old_events):
            if (
                prune_before
                and index < prune_before
                and id(event) not in protected_ids
            ):
                removed_old_events += 1
                if event.get("kind") == "tool_result":
                    evidence_id = self._tool_call_evidence_ids.get(
                        str(event.get("call_id") or ""),
                        "",
                    )
                    if evidence_id:
                        pruned_evidence_ids.add(evidence_id)
                continue
            if id(event) in protected_ids:
                working.append({"kind": "checkpoint_protected", "event": event})
            else:
                working.append(event)

        latest_start_event = old_events[latest.request_start]
        boundary = next(
            index
            for index, event in enumerate(working)
            if event.get("kind") == "checkpoint_protected"
            and event.get("event") is latest_start_event
        )
        replacements = self._compaction_replacements(latest.request_start)
        before_results = {
            str(event.get("call_id") or ""): str(event.get("content", ""))
            for event in working[:boundary]
            if event.get("kind") == "tool_result"
        }
        projected = Conversation(
            system=self.conversation.system,
            events=working,
            tool_schemas=list(self.conversation.tool_schemas),
        )
        stats = projected.compact_tool_epoch(
            boundary,
            replacements,
            keep_newest_results=2,
        )
        self.conversation.events = [
            event["event"]
            if event.get("kind") == "checkpoint_protected"
            else event
            for event in projected.events
        ]
        after_results = {
            str(event.get("call_id") or ""): str(event.get("content", ""))
            for event in self.conversation.events
            if event.get("kind") == "tool_result"
        }
        retained = {
            record.id: record for record in self.evidence_store.snapshot().records
        }
        for evidence_id in pruned_evidence_ids:
            record = retained.get(evidence_id)
            if record is not None and record.is_usable_for_coverage:
                self._compacted_evidence[evidence_id] = record
        for call_id, old_content in before_results.items():
            if after_results.get(call_id) == old_content:
                continue
            evidence_id = self._tool_call_evidence_ids.get(call_id, "")
            record = retained.get(evidence_id)
            if record is not None and record.is_usable_for_coverage:
                self._compacted_evidence[evidence_id] = record

        rebuilt_spans: list[_CheckpointSpan] = []
        for span, (start_event, end_event) in zip(
            self._checkpoint_spans, span_markers,
        ):
            start = next(
                index for index, event in enumerate(self.conversation.events)
                if event is start_event
            )
            end = next(
                index for index, event in enumerate(self.conversation.events)
                if event is end_event
            ) + 1
            rebuilt_spans.append(_CheckpointSpan(
                request_start=start,
                response_end=end,
                disposition=span.disposition,
                compacted=span.compacted,
            ))
        rebuilt_spans[-1] = replace(rebuilt_spans[-1], compacted=True)
        self._checkpoint_spans = rebuilt_spans

        continuation = {
            "cumulative_checkpoint": self._cumulative_checkpoint_payload(),
            "compacted_evidence": self._compacted_evidence_catalogue(),
            "removal_summary": {
                **asdict(stats),
                "removed_old_events": removed_old_events,
            },
            "proposed_next_actions": list(
                self.latest_checkpoint.proposed_next_actions
            ),
        }
        self.conversation.events.append({
            "kind": "user",
            "content": (
                "Validated checkpoint epoch compacted. Continue from the "
                "proposed next actions; use read_compacted_evidence only for "
                "catalogued IDs:\n"
                + json.dumps(continuation, sort_keys=True)
            ),
            "epoch_continuation": True,
        })
        return stats

    def _compact_conversation(self) -> None:
        """Compatibility entry point; never compact without a valid boundary."""
        self._compact_validated_epoch()

    def _reconstruct_from_valid_checkpoint(self) -> bool:
        """Emergency rebuild from controller-owned cumulative checkpoint state."""
        if not self._checkpoint_spans:
            return False
        previous = self.conversation
        rebuilt = Conversation(
            system=previous.system,
            tool_schemas=list(previous.tool_schemas),
        )
        rebuilt.add_user(self._assignment_prompt())
        latest = self._checkpoint_spans[-1]
        exchange_groups: list[list[dict[str, Any]]] = []
        tail = previous.events[latest.response_end:]
        index = 0
        assistant_kinds = {
            "assistant_reasoning",
            "assistant_text",
            "assistant_tool_calls",
        }
        while index < len(tail):
            event = tail[index]
            if event.get("epoch_continuation"):
                index += 1
                continue
            if event.get("kind") == "user":
                exchange_groups.append([event])
                index += 1
                continue
            if event.get("kind") not in assistant_kinds:
                index += 1
                continue
            group: list[dict[str, Any]] = []
            call_ids: list[str] = []
            while index < len(tail):
                item = tail[index]
                if item.get("kind") in assistant_kinds:
                    group.append(item)
                    if item.get("kind") == "assistant_tool_calls":
                        call_ids.extend(
                            str(call.get("id") or "")
                            for call in item.get("calls", ())
                        )
                    index += 1
                    continue
                if item.get("kind") == "assistant_turn_boundary":
                    group.append(item)
                    index += 1
                break
            result_ids: list[str] = []
            while index < len(tail) and tail[index].get("kind") == "tool_result":
                group.append(tail[index])
                result_ids.append(str(tail[index].get("call_id") or ""))
                index += 1
            if not call_ids or sorted(call_ids) == sorted(result_ids):
                exchange_groups.append(group)

        remaining_exchange_bytes = max(
            1_000,
            min(self.recovery_evidence_bytes, self.max_context_tokens * 2),
        )
        newest_groups: list[list[dict[str, Any]]] = []
        for group in reversed(exchange_groups):
            group_bytes = len(
                json.dumps(group, sort_keys=True).encode("utf-8")
            )
            if group_bytes > remaining_exchange_bytes:
                continue
            newest_groups.append(group)
            remaining_exchange_bytes -= group_bytes
        selected_event_ids = {
            id(event) for group in newest_groups for event in group
        }
        retained_records = {
            record.id: record for record in self.evidence_store.snapshot().records
        }
        newly_omitted_evidence_ids: list[str] = []
        for group in exchange_groups:
            if any(id(event) in selected_event_ids for event in group):
                continue
            for event in group:
                if event.get("kind") != "tool_result":
                    continue
                evidence_id = self._tool_call_evidence_ids.get(
                    str(event.get("call_id") or ""),
                    "",
                )
                record = retained_records.get(evidence_id)
                if record is not None and record.is_usable_for_coverage:
                    self._compacted_evidence[evidence_id] = record
                    newly_omitted_evidence_ids.append(evidence_id)
        checkpoint_request_start = len(rebuilt.events)
        rebuilt.add_user(
            "Emergency reconstruction from the latest validated cumulative "
            "checkpoint. The controller-owned snapshot follows."
        )
        snapshot = {
            "cumulative_checkpoint": self._bounded_reconstruction_checkpoint(),
            "compacted_evidence": self._compacted_evidence_catalogue(
                max_bytes=max(0, self.recovery_evidence_bytes // 2),
                priority_evidence_ids=tuple(reversed(newly_omitted_evidence_ids)),
            ),
        }
        rebuilt.add_assistant_turn(
            content=json.dumps(snapshot, sort_keys=True),
        )
        checkpoint_response_end = len(rebuilt.events)
        for group in reversed(newest_groups):
            rebuilt.events.extend(group)
        rebuilt.events.append({
            "kind": "user",
            "content": (
                "Continue the same specialist assignment from proposed_next_actions. "
                "Treat the cumulative checkpoint as continuation memory and use only "
                "the bounded compacted-evidence catalogue for retrieval."
            ),
            "epoch_continuation": True,
            "emergency_reconstruction": True,
        })
        self.conversation = rebuilt
        self._checkpoint_spans = [_CheckpointSpan(
            request_start=checkpoint_request_start,
            response_end=checkpoint_response_end,
            disposition=CheckpointDisposition.COMPACT_RESUME,
            compacted=True,
        )]
        return True

    def _conversation_evidence_bodies(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for event in self.conversation.events:
            if event.get("kind") != "tool_result":
                continue
            evidence_id = self._tool_call_evidence_ids.get(
                str(event.get("call_id") or ""),
                "",
            )
            try:
                payload = json.loads(str(event.get("content", "")))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if not evidence_id:
                if isinstance(payload, Mapping):
                    evidence_id = str(payload.get("evidence_id") or "").strip()
            if not evidence_id:
                # Conversation-level tool-result bounds can cut a one-line
                # JSON envelope before it remains parseable. The evidence ID
                # is deliberately emitted first and can still be recovered
                # without trusting the truncated body.
                match = re.search(
                    r'"evidence_id"\s*:\s*"([^"]+)"',
                    str(event.get("content", "")),
                )
                evidence_id = match.group(1).strip() if match else ""
            if evidence_id:
                result[evidence_id] = str(event.get("content", ""))
        return result

    def _assistant_analysis_bodies(self) -> tuple[str, ...]:
        return tuple(
            str(event.get("content", ""))
            for event in self.conversation.events
            if event.get("kind") in {"assistant_text", "assistant_reasoning"}
        )

    def _read_compacted_evidence(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) - {"evidence_id", "offset", "limit"}:
            return {"status": "error", "error": "unexpected retrieval arguments"}
        evidence_id = str(arguments.get("evidence_id") or "").strip()
        if not evidence_id:
            return {"status": "error", "error": "evidence_id is required"}
        record = self._compacted_evidence.get(evidence_id)
        if record is None:
            return {
                "status": "error",
                "error": "evidence ID is not marked as compacted",
            }
        raw_offset = arguments.get("offset", 0)
        raw_limit = arguments.get("limit", _MAX_COMPACTED_EVIDENCE_READ_CHARS)
        if (
            isinstance(raw_offset, bool)
            or not isinstance(raw_offset, int)
            or raw_offset < 0
            or isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or raw_limit <= 0
        ):
            return {"status": "error", "error": "offset and limit are invalid"}
        offset = raw_offset
        limit = min(raw_limit, _MAX_COMPACTED_EVIDENCE_READ_CHARS)
        key = (evidence_id, offset, limit)
        if key in self._compacted_evidence_read_keys:
            return {
                "status": "ok",
                "evidence_id": evidence_id,
                "replayed_compacted": True,
            }
        if self._compacted_evidence_reads >= _MAX_COMPACTED_EVIDENCE_READS:
            return {
                "status": "error",
                "error": "compacted evidence read budget exhausted",
            }
        self._compacted_evidence_read_keys.add(key)
        self._compacted_evidence_reads += 1
        content = record.content
        excerpt = content[offset:offset + limit]
        return {
            "status": "ok",
            "evidence_id": evidence_id,
            "tool": record.tool,
            "content": excerpt,
            "offset": offset,
            "limit": limit,
            "truncated": offset + len(excerpt) < len(content),
            "source_truncated": bool(record.truncated),
        }

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

    def _record_checkpoint_diagnostic(
        self,
        *,
        reason: str,
        initial_parse: str,
        repair_attempted: bool,
        repair_parse: str,
        fallback_projection: bool,
        retention_unknown: bool,
        initial_finish_reason: str = "",
        repair_finish_reason: str = "",
        initial_error: str = "",
        repair_error: str = "",
        context_admission: Mapping[str, object] | None = None,
    ) -> None:
        signal = self._candidate_retention_signal
        diagnostic: dict[str, object] = {
            "reason": str(reason)[:120],
            "initial_parse": initial_parse,
            "repair_attempted": bool(repair_attempted),
            "repair_parse": repair_parse,
            "initial_finish_reason": str(initial_finish_reason)[:40],
            "repair_finish_reason": str(repair_finish_reason)[:40],
            "fallback_projection": bool(fallback_projection),
            "retention_unknown": bool(retention_unknown),
            "material_candidate_signal": signal.is_material,
            "candidate_ids": signal.candidate_ids,
            "candidate_ids_seen": len(signal.candidate_ids),
            "unidentified_candidate_shapes": signal.unidentified_shapes,
            "omitted_candidate_ids": signal.omitted_candidate_ids,
            "initial_error": str(initial_error)[:300],
            "repair_error": str(repair_error)[:300],
        }
        diagnostic.update(dict(context_admission or {}))
        self._finalization_diagnostics.append(diagnostic)
        del self._finalization_diagnostics[:-8]

    def _cumulative_checkpoint_payload(self) -> dict[str, object]:
        """Materialize controller-owned state needed to reconstruct a session."""
        checkpoint = self.latest_checkpoint
        coverage = self.coverage.snapshot()
        retained_evidence = self.evidence_store.snapshot().records
        cumulative_evidence_ids = tuple(dict.fromkeys((
            *checkpoint.evidence_ids,
            *(record.id for record in retained_evidence),
        )))
        checkpoint_payload: dict[str, object] = {
            "session_id": checkpoint.session_id,
            "state": checkpoint.state.value,
            "evidence_ids": list(cumulative_evidence_ids),
            "imported_evidence_ids": list(checkpoint.imported_evidence_ids),
            "working_summary": checkpoint.working_summary,
            "completed_steps": list(checkpoint.completed_steps),
            "hypotheses": list(checkpoint.hypotheses),
            "candidate_finding_ids": list(checkpoint.candidate_finding_ids),
            "obligation_statuses": {
                obligation_id: status.value
                for obligation_id, status in checkpoint.obligation_statuses
            },
            "invariants_evaluated": list(checkpoint.invariants_evaluated),
            "unknowns": list(checkpoint.unknowns),
            "proposed_next_actions": list(checkpoint.proposed_next_actions),
        }
        evidence_metadata: list[dict[str, object]] = []
        for record in retained_evidence:
            metadata = asdict(record)
            metadata.pop("content", None)
            evidence_metadata.append(metadata)
        return {
            "latest_checkpoint": checkpoint_payload,
            "candidate_findings": [
                asdict(candidate) for candidate in self.candidate_findings
            ],
            "candidate_statuses": dict(sorted(self._candidate_statuses.items())),
            "coverage": {
                "obligation_statuses": {
                    obligation_id: status.value
                    for obligation_id, status in coverage.obligation_statuses
                },
                "recipe_statuses": dict(coverage.recipe_statuses),
                "evidence_by_obligation": {
                    obligation_id: list(evidence_ids)
                    for obligation_id, evidence_ids in coverage.evidence_by_obligation
                },
            },
            "evidence_metadata": evidence_metadata,
        }

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
            **self._cumulative_checkpoint_payload(),
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
        recovery_span_start = len(rebuilt.events)
        rebuilt.add_user(
            "Recovery reconstruction. Continue the same logical specialist session:\n"
            + json.dumps(
                recovery_payload,
                sort_keys=True,
                default=lambda value: value.value if hasattr(value, "value") else str(value),
            )
        )
        self.conversation = rebuilt
        self._checkpoint_spans = [_CheckpointSpan(
            request_start=recovery_span_start,
            response_end=len(rebuilt.events),
            disposition=CheckpointDisposition.COMPACT_RESUME,
            compacted=True,
        )]
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
