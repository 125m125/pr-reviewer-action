"""Evidence-authoritative adjudication, verdict policy, and review products."""

from __future__ import annotations

from dataclasses import (
    dataclass,
    fields as dataclass_fields,
    is_dataclass,
    replace,
)
from enum import Enum
import hashlib
import html
import json
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from pr_reviewer.enforcement import RuntimeVerdictPolicyResult, derive_runtime_verdict

from .coverage import evidence_satisfies_obligation
from .evidence import EvidenceRecord, EvidenceSnapshot, EvidenceStore
from .types import CandidateFinding, CoverageObligation, ReviewHandoff, ReviewNote, ReviewNoteKind
from .web_evidence import (
    RepositoryAccessRequest,
    SourceAccessRequest,
    repository_access_request,
)


_CRITIC_ACTIONS = frozenset({
    "keep", "reject", "merge", "request_verification", "downgrade_unknown",
})
_SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
_SEVERITY_ALIASES = {
    "critical": "blocker",
    "high": "major",
    "medium": "minor",
    "moderate": "minor",
    "low": "minor",
    "informational": "info",
}
_NOTE_VALUE_LIMIT = 1000


@dataclass(frozen=True)
class CandidateDisposition:
    candidate_id: str
    action: str
    reason: str
    target_id: str | None = None


@dataclass(frozen=True)
class CandidateConsolidation:
    candidates: tuple[CandidateFinding, ...] = ()
    dispositions: tuple[CandidateDisposition, ...] = ()


@dataclass(frozen=True)
class EvidenceCitation:
    evidence_id: str
    category: str
    tool: str
    source: str
    content_hash: str


@dataclass(frozen=True)
class AcceptedFinding:
    candidate_id: str
    root_cause_fingerprint: str
    deduplication_key: str
    claim: str
    user_visible_consequence: str
    affected_file: str
    line: int | None
    causal_chain: str
    severity: str
    category: str
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    related_obligation_ids: tuple[str, ...]
    supporting_citations: tuple[EvidenceCitation, ...]
    contradicting_citations: tuple[EvidenceCitation, ...]
    manual_validation: str
    confidence_rationale: str
    collector_session_id: str = ""
    model_identity: str = ""
    contributor_candidate_ids: tuple[str, ...] = ()
    user_visible_consequences: tuple[str, ...] = ()
    manual_validations: tuple[str, ...] = ()

    @property
    def affected_location(self) -> str:
        return self.affected_file + (f":{self.line}" if self.line is not None else "")

    @property
    def citations(self) -> tuple[EvidenceCitation, ...]:
        return self.supporting_citations + self.contradicting_citations


@dataclass(frozen=True)
class CandidateVerificationRequest:
    candidate: CandidateFinding
    reason: str


class ReviewOrientationTopic(str, Enum):
    IMPLEMENTATION = "implementation"
    DOCUMENTATION = "documentation"
    REPOSITORY_BEHAVIOR = "repository_behavior"
    DATABASE = "database"
    AUTHORIZATION = "authorization"
    CACHING = "caching"
    CONCURRENCY = "concurrency"
    API_CONTRACTS = "api_contracts"
    FAILURE_RECOVERY = "failure_recovery"
    CROSS_COMPONENT_CONTRACTS = "cross_component_contracts"
    TEST_COVERAGE = "test_coverage"
    GENERATED_ARTIFACTS = "generated_artifacts"
    DEPLOYMENT = "deployment"
    SOURCE_POLICY = "source_policy"
    SECURITY = "security"


_TOPIC_LABELS = {
    ReviewOrientationTopic.IMPLEMENTATION: "Runtime implementation behavior",
    ReviewOrientationTopic.DOCUMENTATION: "Documentation and operator guidance",
    ReviewOrientationTopic.REPOSITORY_BEHAVIOR: "Repository behavior and integration",
    ReviewOrientationTopic.DATABASE: "Database and persistence",
    ReviewOrientationTopic.AUTHORIZATION: "Authorization boundaries",
    ReviewOrientationTopic.CACHING: "Caching and invalidation",
    ReviewOrientationTopic.CONCURRENCY: "Concurrency and ordering",
    ReviewOrientationTopic.API_CONTRACTS: "API and schema contracts",
    ReviewOrientationTopic.FAILURE_RECOVERY: "Failure recovery",
    ReviewOrientationTopic.CROSS_COMPONENT_CONTRACTS: "Cross-component contracts",
    ReviewOrientationTopic.TEST_COVERAGE: "Test coverage",
    ReviewOrientationTopic.GENERATED_ARTIFACTS: "Generated artifacts",
    ReviewOrientationTopic.DEPLOYMENT: "Deployment and runtime configuration",
    ReviewOrientationTopic.SOURCE_POLICY: "External source policy",
    ReviewOrientationTopic.SECURITY: "Security-sensitive behavior",
}


def review_orientation_label(value: object) -> str | None:
    """Return the production sparse-handoff label for a typed topic value."""
    try:
        topic = value if isinstance(value, ReviewOrientationTopic) else ReviewOrientationTopic(
            _unicode(value).strip().casefold()
        )
    except ValueError:
        return None
    return _TOPIC_LABELS[topic]


@dataclass(frozen=True)
class ReviewHandoffContext:
    recommendation: str = ""
    status: str = ""
    change_topics: tuple[ReviewOrientationTopic, ...] = ()
    component_ids: tuple[str, ...] = ()
    specialist_topics: tuple[ReviewOrientationTopic, ...] = ()
    recipe_ids: tuple[str, ...] = ()
    coverage_boundary_topics: tuple[ReviewOrientationTopic, ...] = ()
    # Compatibility names for a pre-publication aggregate. These values describe
    # prepared detail notes only; they never represent GitHub resolution state.
    unresolved_thread_count: int = 0
    highest_thread_severity: str | None = None
    review_emphasis_topics: tuple[ReviewOrientationTopic, ...] = ()
    material_coverage_limited: bool = False
    candidate_retention_limited: bool = False
    degraded_stages: tuple[str, ...] = ()
    diagnostics_url: str | None = None
    source_access_requests: tuple[
        SourceAccessRequest | RepositoryAccessRequest, ...
    ] = ()
    access_request_url: str | None = None
    what_changed: tuple[str, ...] = ()
    what_changed_is_validated_overview: bool = False
    ai_reviewed: tuple[str, ...] = ()
    ai_reviewed_is_validated_summary: bool = False
    human_focus: tuple[str, ...] = ()
    # Controller-supplied unchanged paths that are valid context for prose
    # such as an affected consumer or retained evidence source.  They are not
    # direct change locations and must never authorize findings.
    context_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdjudicatedReview:
    accepted: tuple[AcceptedFinding, ...] = ()
    rejected: tuple[CandidateDisposition, ...] = ()
    verification_requests: tuple[CandidateVerificationRequest, ...] = ()
    unknowns: tuple[CandidateFinding, ...] = ()
    dispositions: tuple[CandidateDisposition, ...] = ()


def _unicode(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))


def _identity_text(value: object) -> str:
    text = _unicode(value).casefold()
    semantic = "".join(
        character
        if character.isspace()
        or unicodedata.category(character)[0] in {"L", "N", "P", "S"}
        else " "
        for character in text
    )
    return " ".join(semantic.split())


def _detail_identity(value: object) -> str:
    text = _identity_text(value)
    start = 0
    end = len(text)
    while start < end and unicodedata.category(text[start])[0] in {"P", "S"}:
        start += 1
    while end > start and unicodedata.category(text[end - 1])[0] in {"P", "S"}:
        end -= 1
    return text[start:end].strip()


def _stable_detail_values(values: Iterable[object]) -> tuple[str, ...]:
    by_identity: dict[str, str] = {}
    for value in values:
        normalized = _unicode(value).strip()
        identity = _detail_identity(normalized)
        if not identity:
            continue
        current = by_identity.get(identity)
        if current is None or normalized < current:
            by_identity[identity] = normalized
    return tuple(by_identity[key] for key in sorted(by_identity))


def _path(value: object) -> tuple[str, int | None, str]:
    raw = _unicode(value).strip().replace("\\", "/")
    if not raw:
        return "", None, "missing"
    match = re.fullmatch(r"(.+?)(?::(\d+))?", raw)
    path_text = match.group(1) if match else raw
    raw_line = int(match.group(2)) if match and match.group(2) else None
    line = raw_line if raw_line is not None and raw_line > 0 else None
    while path_text.startswith("./"):
        path_text = path_text[2:]
    normalized = str(PurePosixPath(path_text)) if path_text else ""
    if (
        normalized in {"", "."}
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        return "", line, "invalid"
    return normalized, line, "ok"


def _exact_changed_location(
    value: object,
    changed_files: tuple[str, ...],
) -> tuple[str, int | None, str]:
    raw = _unicode(value).strip()
    if not raw:
        return "", None, "missing"
    for path in changed_files:
        if raw == path:
            return path, None, "ok"
    for path in sorted(changed_files, key=len, reverse=True):
        prefix = path + ":"
        if not raw.startswith(prefix):
            continue
        line_text = raw[len(prefix):]
        if re.fullmatch(r"[1-9]\d*", line_text):
            return path, int(line_text), "ok"
        # Preserve a defensible changed-file anchor when a specialist reports
        # a range; GitHub cannot attach a single review line to that range.
        if re.fullmatch(r"[1-9]\d*-[1-9]\d*", line_text):
            return path, None, "ok"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", line_text):
            return path, None, "ok"
        break
    return "", None, "invalid"


def _stable_strings(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return ()
    return tuple(sorted({_unicode(item).strip() for item in values if _unicode(item).strip()}))


def normalize_candidate_severity(value: object) -> str:
    severity = _unicode(value).strip().lower()
    severity = _SEVERITY_ALIASES.get(severity, severity)
    return severity if severity in _SEVERITY_RANK else "info"


def _normalized_candidate(candidate: CandidateFinding) -> CandidateFinding:
    affected_file, _, _ = _path(candidate.affected_location)
    claim_identity = _identity_text(candidate.claim)
    category = _identity_text(candidate.category).replace(" ", "-")
    identity = "\x1f".join((affected_file, category, claim_identity))
    fingerprint = (
        candidate.controller_root_fingerprint
        or "finding:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    )
    return replace(
        candidate,
        root_cause_fingerprint=fingerprint,
        affected_location=_unicode(candidate.affected_location).strip(),
        severity=normalize_candidate_severity(candidate.severity),
        category=category,
        supporting_evidence_ids=_stable_strings(candidate.supporting_evidence_ids),
        contradicting_evidence_ids=_stable_strings(candidate.contradicting_evidence_ids),
        related_obligation_ids=_stable_strings(candidate.related_obligation_ids),
        contributor_candidate_ids=_stable_strings(
            candidate.contributor_candidate_ids or (candidate.candidate_id,)
        ),
    )


_CHANGE_ANCHOR_FIELDS = (
    "symbols",
    "action_inputs",
    "workflow_steps",
    "workflow_keys",
    "headings",
)


def _controller_root_anchor(
    candidate: CandidateFinding,
    *,
    path: str,
    change_facts: Mapping[str, object],
    obligations: Mapping[str, CoverageObligation],
) -> str | None:
    anchors: dict[str, str] = {}
    raw_fact = change_facts.get(path, {})
    fact = raw_fact if isinstance(raw_fact, Mapping) else {}
    for field_name in _CHANGE_ANCHOR_FIELDS:
        raw_values = fact.get(field_name, ())
        if (
            isinstance(raw_values, (str, bytes))
            or not isinstance(raw_values, Iterable)
        ):
            continue
        for value in raw_values:
            identity = _identity_text(value)
            if len(identity) < 4:
                continue
            anchors[f"{field_name}:{identity}"] = identity

    for obligation_id in candidate.related_obligation_ids:
        obligation = obligations.get(obligation_id)
        if obligation is None:
            continue
        scoped_paths = {
            _path(value)[0]
            for value in (*obligation.scope, *obligation.seed_hints)
            if _path(value)[2] == "ok"
        }
        if scoped_paths and path not in scoped_paths:
            continue
        for value in (obligation.subject, *obligation.satisfaction_predicates):
            identity = _identity_text(value)
            if len(identity) < 4:
                continue
            anchors[f"contract:{identity}"] = identity

    if not anchors:
        return None

    def contains_anchor(text: str, anchor: str) -> bool:
        return re.search(
            rf"(?<![\w]){re.escape(anchor)}(?![\w])",
            text,
            flags=re.UNICODE,
        ) is not None

    for detail in (
        candidate.claim,
        candidate.causal_chain,
        candidate.user_visible_consequence,
        candidate.manual_validation,
    ):
        text = _identity_text(detail)
        matched = {
            key: value for key, value in anchors.items()
            if contains_anchor(text, value)
        }
        if not matched:
            continue
        maximal = {
            key: value for key, value in matched.items()
            if not any(
                value != other and value in other
                for other in matched.values()
            )
        }
        if len(maximal) == 1:
            return next(iter(maximal))
        return None
    return None


def _controller_root_identity(
    candidate: CandidateFinding,
    *,
    changed_files: set[str],
    change_facts: Mapping[str, object],
    obligations: Mapping[str, CoverageObligation],
) -> tuple[str, str, str] | None:
    path, _, location_state = _path(candidate.affected_location)
    category = _identity_text(candidate.category).replace(" ", "-")
    if location_state != "ok" or path not in changed_files or not category:
        return None
    anchor = _controller_root_anchor(
        candidate,
        path=path,
        change_facts=change_facts,
        obligations=obligations,
    )
    if anchor is None:
        return None
    return path, anchor, category


def _root_fingerprint(identity: tuple[str, str, str]) -> str:
    return "root:" + hashlib.sha256(
        "\x1f".join(identity).encode("utf-8")
    ).hexdigest()


def _consolidated_candidate(
    values: Iterable[CandidateFinding],
    *,
    identity: tuple[str, str, str],
    obligations: Mapping[str, CoverageObligation],
    valid_evidence_ids: set[str] | None,
    supported_evidence_by_candidate: Mapping[str, frozenset[str]],
) -> CandidateFinding:
    candidates = tuple(sorted(values, key=lambda item: item.candidate_id))
    path = identity[0]

    def valid_support(candidate: CandidateFinding) -> tuple[str, ...]:
        if valid_evidence_ids is None:
            return _stable_strings(candidate.supporting_evidence_ids)
        return tuple(
            evidence_id
            for evidence_id in _stable_strings(candidate.supporting_evidence_ids)
            if evidence_id in valid_evidence_ids
        )

    evidence_owner_counts: dict[str, int] = {}
    for candidate in candidates:
        for evidence_id in supported_evidence_by_candidate.get(
            candidate.candidate_id, frozenset(),
        ):
            evidence_owner_counts[evidence_id] = (
                evidence_owner_counts.get(evidence_id, 0) + 1
            )
    donor_candidate_ids = {
        candidate.candidate_id
        for candidate in candidates
        if any(
            evidence_owner_counts.get(evidence_id) == 1
            for evidence_id in supported_evidence_by_candidate.get(
                candidate.candidate_id, frozenset(),
            )
        )
    }

    def content_rank(candidate: CandidateFinding) -> tuple[object, ...]:
        known_obligations = set(candidate.related_obligation_ids) & obligations.keys()
        return (
            -(candidate.candidate_id in donor_candidate_ids),
            -bool(known_obligations),
            -sum(bool(_identity_text(value)) for value in (
                candidate.claim,
                candidate.causal_chain,
                candidate.user_visible_consequence,
                candidate.manual_validation,
            )),
            candidate.candidate_id,
        )

    representative = min(candidates, key=content_rank)
    location_candidates = tuple(
        candidate for candidate in candidates
        if candidate.candidate_id in donor_candidate_ids
    )
    if location_candidates:
        located: list[tuple[int, int, str, CandidateFinding]] = []
        for candidate in location_candidates:
            candidate_path, line, location_state = _path(
                candidate.affected_location
            )
            if location_state == "ok" and candidate_path == path:
                located.append((
                    0 if line is not None else 1,
                    line or 0,
                    candidate.candidate_id,
                    candidate,
                ))
        best_location = min(located)[3] if located else representative
        _, best_line, _ = _path(best_location.affected_location)
    else:
        locations = {
            (candidate_path, line)
            for candidate in candidates
            for candidate_path, line, location_state in (
                _path(candidate.affected_location),
            )
            if location_state == "ok" and candidate_path == path
        }
        best_line = (
            next(iter(locations))[1]
            if len(locations) == 1
            else None
        )
    affected_location = path + (
        f":{best_line}" if best_line is not None else ""
    )

    supporting = tuple(sorted({
        evidence_id
        for candidate in candidates
        for evidence_id in candidate.supporting_evidence_ids
        if valid_evidence_ids is None or evidence_id in valid_evidence_ids
    }))
    contradicting = tuple(sorted({
        evidence_id
        for candidate in candidates
        for evidence_id in candidate.contradicting_evidence_ids
        if valid_evidence_ids is None or evidence_id in valid_evidence_ids
    }))
    if not supporting:
        supporting = tuple(sorted({
            evidence_id
            for candidate in candidates
            for evidence_id in candidate.supporting_evidence_ids
        }))
    if not contradicting and valid_evidence_ids is None:
        contradicting = tuple(sorted({
            evidence_id
            for candidate in candidates
            for evidence_id in candidate.contradicting_evidence_ids
        }))
    related_obligations = tuple(sorted({
        obligation_id
        for candidate in candidates
        for obligation_id in candidate.related_obligation_ids
        if obligation_id in obligations
    }))
    if not related_obligations:
        related_obligations = tuple(sorted({
            obligation_id
            for candidate in candidates
            for obligation_id in candidate.related_obligation_ids
        }))

    severity_candidates = tuple(
        candidate for candidate in candidates
        if candidate.candidate_id in donor_candidate_ids
    )
    severity_selector = max if severity_candidates else min
    severity = severity_selector(
        severity_candidates or candidates,
        key=lambda item: (
            _SEVERITY_RANK[normalize_candidate_severity(item.severity)],
            item.candidate_id,
        ),
    ).severity
    contributors = tuple(sorted({
        contributor
        for candidate in candidates
        for contributor in (
            candidate.contributor_candidate_ids
            or (candidate.candidate_id,)
        )
    }))
    return replace(
        representative,
        root_cause_fingerprint=_root_fingerprint(identity),
        controller_root_fingerprint=_root_fingerprint(identity),
        affected_location=affected_location,
        severity=normalize_candidate_severity(severity),
        category=identity[2],
        supporting_evidence_ids=supporting,
        contradicting_evidence_ids=contradicting,
        related_obligation_ids=related_obligations,
        contributor_candidate_ids=contributors,
    )


def consolidate_candidates(
    candidates: Iterable[CandidateFinding],
    *,
    changed_files: Iterable[str],
    change_facts: Mapping[str, object],
    obligations: Mapping[str, CoverageObligation],
    valid_evidence_ids: Iterable[str] | None = None,
    evidence: EvidenceStore | EvidenceSnapshot | None = None,
) -> CandidateConsolidation:
    """Conservatively collapse candidates sharing controller-derived roots."""
    obligation_map, changed = _controller_state(obligations, changed_files)
    if not isinstance(change_facts, Mapping):
        raise TypeError("change_facts must be a controller-owned mapping")
    if evidence is not None and valid_evidence_ids is not None:
        raise ValueError("provide evidence or valid_evidence_ids, not both")
    evidence_snapshot = _snapshot(evidence) if evidence is not None else None
    records = (
        {record.id: record for record in evidence_snapshot.records}
        if evidence_snapshot is not None
        else {}
    )
    valid_ids = (
        {
            record.id for record in records.values()
            if record.is_usable_for_coverage
        }
        if evidence_snapshot is not None
        else (
            set(_stable_strings(valid_evidence_ids))
            if valid_evidence_ids is not None
            else None
        )
    )
    candidate_values = tuple(candidates)

    def root_supporting_evidence(
        candidate: CandidateFinding,
    ) -> frozenset[str]:
        if evidence_snapshot is None:
            return frozenset(
                set(candidate.supporting_evidence_ids)
                & (
                    valid_ids
                    if valid_ids is not None
                    else set(candidate.supporting_evidence_ids)
                )
            )
        related = tuple(
            obligation_map[obligation_id]
            for obligation_id in candidate.related_obligation_ids
            if obligation_id in obligation_map
        )
        satisfying_ids: set[str] = set()
        for evidence_id in candidate.supporting_evidence_ids:
            record = records.get(evidence_id)
            if record is None or not record.is_usable_for_coverage:
                continue
            for obligation in related:
                associations = evidence_snapshot.associations_for(
                    record.id, obligation.id,
                )
                if associations:
                    if any(
                        evidence_satisfies_obligation(
                            replace(record, category=category),
                            obligation,
                        )
                        for _collection, association in associations
                        for category in association.categories
                    ):
                        satisfying_ids.add(evidence_id)
                elif evidence_satisfies_obligation(record, obligation):
                    satisfying_ids.add(evidence_id)
        return frozenset(satisfying_ids)

    supported_evidence_by_candidate = {
        candidate.candidate_id: root_supporting_evidence(candidate)
        for candidate in candidate_values
    }
    grouped: dict[tuple[str, ...], list[CandidateFinding]] = {}
    for index, candidate in enumerate(candidate_values):
        if not isinstance(candidate, CandidateFinding):
            raise TypeError("candidates must contain CandidateFinding values")
        identity = _controller_root_identity(
            candidate,
            changed_files=set(changed),
            change_facts=change_facts,
            obligations=obligation_map,
        )
        key = (
            ("root", *identity)
            if identity is not None
            else ("candidate", str(index), candidate.candidate_id)
        )
        grouped.setdefault(key, []).append(candidate)

    consolidated: list[CandidateFinding] = []
    dispositions: list[CandidateDisposition] = []
    for key in sorted(grouped):
        values = grouped[key]
        if key[0] != "root" or len(values) == 1:
            candidate = values[0]
            controller_fingerprint = (
                _root_fingerprint((key[1], key[2], key[3]))
                if key[0] == "root"
                else ""
            )
            consolidated.append(replace(
                candidate,
                root_cause_fingerprint=(
                    controller_fingerprint
                    or candidate.root_cause_fingerprint
                ),
                controller_root_fingerprint=controller_fingerprint,
                contributor_candidate_ids=(
                    candidate.contributor_candidate_ids
                    or (candidate.candidate_id,)
                ),
            ))
            continue
        identity = (key[1], key[2], key[3])
        merged = _consolidated_candidate(
            values,
            identity=identity,
            obligations=obligation_map,
            valid_evidence_ids=valid_ids,
            supported_evidence_by_candidate=supported_evidence_by_candidate,
        )
        consolidated.append(merged)
        for candidate in sorted(values, key=lambda item: item.candidate_id):
            if candidate.candidate_id == merged.candidate_id:
                continue
            dispositions.append(CandidateDisposition(
                candidate_id=candidate.candidate_id,
                action="merge",
                reason="controller-root-identity",
                target_id=merged.candidate_id,
            ))
    return CandidateConsolidation(
        candidates=tuple(sorted(
            consolidated, key=lambda item: item.candidate_id,
        )),
        dispositions=tuple(sorted(
            dispositions, key=lambda item: item.candidate_id,
        )),
    )


def _deduplication_key(candidate: CandidateFinding) -> str:
    affected_file, _, _ = _path(candidate.affected_location)
    identity = "\x1f".join((
        affected_file,
        _identity_text(candidate.category),
        _identity_text(candidate.claim),
        _identity_text(candidate.causal_chain),
    ))
    return "dedup:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _merge_compatible(source: CandidateFinding, target: CandidateFinding) -> bool:
    """Allow critic deduplication without making it an authorization bypass."""
    source_file, _, source_state = _path(source.affected_location)
    target_file, _, target_state = _path(target.affected_location)
    if source_state != "ok" or target_state != "ok" or source_file != target_file:
        return False
    return bool(
        source.root_cause_fingerprint
        and source.root_cause_fingerprint == target.root_cause_fingerprint
    ) or bool(
        set(source.supporting_evidence_ids) & set(target.supporting_evidence_ids)
    )


def _snapshot(evidence: EvidenceStore | EvidenceSnapshot) -> EvidenceSnapshot:
    if isinstance(evidence, EvidenceStore):
        return evidence.snapshot()
    if isinstance(evidence, EvidenceSnapshot):
        return evidence
    raise TypeError("evidence must be an EvidenceStore or EvidenceSnapshot")


def _controller_state(
    obligations: Mapping[str, CoverageObligation],
    changed_files: Iterable[str],
) -> tuple[dict[str, CoverageObligation], tuple[str, ...]]:
    if not isinstance(obligations, Mapping):
        raise TypeError("obligations must be a controller-owned mapping")
    normalized_obligations: dict[str, CoverageObligation] = {}
    for raw_id, obligation in obligations.items():
        if not isinstance(obligation, CoverageObligation):
            raise TypeError("obligations must contain CoverageObligation values")
        obligation_id = _unicode(raw_id).strip()
        if not obligation_id or obligation_id != obligation.id:
            raise ValueError("obligation mapping keys must match obligation IDs")
        normalized_obligations[obligation_id] = obligation
    if isinstance(changed_files, (str, bytes)) or not isinstance(changed_files, Iterable):
        raise TypeError("changed_files must be a controller-owned iterable")
    normalized_changed = []
    for item in changed_files:
        changed_file, _, state = _path(item)
        if state != "ok":
            raise ValueError("changed_files must contain repository-relative paths")
        normalized_changed.append(changed_file)
    return normalized_obligations, tuple(sorted(set(normalized_changed)))


def _citation(record: EvidenceRecord) -> EvidenceCitation:
    provenance = record.provenance
    source = (
        provenance.final_url
        or provenance.original_url
        or record.source_identity
        or record.source_path
        or "retained-tool-result"
    )
    return EvidenceCitation(
        evidence_id=record.id,
        category=record.category,
        tool=record.tool,
        source=_unicode(source),
        content_hash=record.content_hash,
    )


def _consequence_support_reason(
    candidate: CandidateFinding,
    *,
    supporting: tuple[EvidenceRecord, ...],
    contradictions: tuple[EvidenceRecord, ...],
    related: tuple[CoverageObligation, ...],
    affected_file: str,
) -> str:
    """Require typed evidence for the consequence, not merely the changed mechanism."""
    prefix = "consequence_support:"
    rationale = _unicode(candidate.confidence_rationale).strip()
    if not rationale.casefold().startswith(prefix):
        return "consequence-not-supported"
    declaration = rationale[len(prefix):]
    head, *raw_fields = declaration.split(";")
    kind = head.strip().casefold()
    details: dict[str, str] = {}
    for raw_field in raw_fields:
        key, separator, value = raw_field.partition("=")
        if not separator or not key.strip() or not value.strip():
            continue
        details[key.strip().casefold()] = value.strip()
    cited_ids = {
        item.strip()
        for item in details.get("evidence_ids", "").split(",")
        if item.strip()
    }
    supporting_by_id = {record.id: record for record in supporting}
    contradiction_by_id = {record.id: record for record in contradictions}
    if not cited_ids or not cited_ids <= (supporting_by_id.keys() | contradiction_by_id.keys()):
        return "consequence-not-supported"
    cited_support = tuple(
        supporting_by_id[item] for item in sorted(cited_ids & supporting_by_id.keys())
    )
    cited_contradictions = tuple(
        contradiction_by_id[item] for item in sorted(cited_ids & contradiction_by_id.keys())
    )

    if kind == "reachable_input_path":
        causal_identity = _detail_identity(candidate.causal_chain)
        consequence_identity = _detail_identity(candidate.user_visible_consequence)
        input_identity = _detail_identity(details.get("input", ""))
        condition_identity = _detail_identity(details.get("condition", ""))
        outcome_identity = _detail_identity(details.get("outcome", ""))
        if (
            input_identity
            and input_identity in causal_identity
            and condition_identity
            and condition_identity in causal_identity
            and outcome_identity
            and outcome_identity in consequence_identity
            and any(record.source_path == affected_file for record in cited_support)
        ):
            return ""
    elif kind == "failing_behavioral_test":
        if all(details.get(key) for key in ("test", "observed")) and any(
            record.category.casefold() in {"test", "tests", "test-result", "behavioral-test"}
            or record.tool.casefold() in {"pytest", "run_tests", "test"}
            for record in cited_support
        ):
            return ""
    elif kind == "violated_invariant":
        obligation_id = details.get("obligation_id", "")
        obligation = next((item for item in related if item.id == obligation_id), None)
        contract = details.get("contract", "").strip()
        contract_kind, separator, contract_value = contract.partition(":")
        if contract.casefold() == "subject":
            contract_kind, contract_value = "subject", ""
            separator = ":"
        authoritative_contracts = set()
        if obligation is not None:
            authoritative_contracts.add(("subject", ""))
            authoritative_contracts.update(
                ("predicate_index", str(index))
                for index, _item in enumerate(obligation.satisfaction_predicates)
            )
        if all((
            cited_support,
            obligation is not None,
            separator,
            details.get("violation"),
            (contract_kind.casefold(), contract_value.strip())
            in authoritative_contracts,
        )):
            return ""
    elif kind == "affected_consumer":
        source_paths = {record.source_path for record in cited_support if record.source_path}
        if (
            all(details.get(key) for key in ("producer", "consumer", "outcome"))
            and details["producer"] in source_paths
            and details["consumer"] in source_paths
        ):
            return ""
    elif kind == "contradicting_evidence":
        linked = any(
            set(record.contradicts) & supporting_by_id.keys()
            for record in cited_contradictions
        ) or any(
            set(record.contradicts) & contradiction_by_id.keys()
            for record in cited_support
        )
        if details.get("conflict") and cited_contradictions and linked:
            return ""
    return "consequence-not-supported"


def _candidate_from_accepted(value: AcceptedFinding | CandidateFinding) -> CandidateFinding | None:
    if isinstance(value, CandidateFinding):
        return value
    if not isinstance(value, AcceptedFinding):
        return None
    return CandidateFinding(
        candidate_id=value.candidate_id,
        root_cause_fingerprint=value.root_cause_fingerprint,
        claim=value.claim,
        affected_location=value.affected_location,
        causal_chain=value.causal_chain,
        severity=value.severity,
        category=value.category,
        supporting_evidence_ids=value.supporting_evidence_ids,
        contradicting_evidence_ids=value.contradicting_evidence_ids,
        related_obligation_ids=value.related_obligation_ids,
        collector_session_id=value.collector_session_id,
        model_identity=value.model_identity,
        confidence_rationale=value.confidence_rationale,
        user_visible_consequence=value.user_visible_consequence,
        manual_validation=value.manual_validation,
        contributor_candidate_ids=value.contributor_candidate_ids,
        controller_root_fingerprint=value.root_cause_fingerprint,
    )


def _authorize(
    value: AcceptedFinding | CandidateFinding,
    *,
    records: Mapping[str, EvidenceRecord],
    obligations: Mapping[str, CoverageObligation],
    changed_files: tuple[str, ...],
) -> tuple[AcceptedFinding | None, str]:
    raw_candidate = _candidate_from_accepted(value)
    if raw_candidate is None:
        return None, "unsupported-accepted-value"
    candidate = _normalized_candidate(raw_candidate)
    affected_file, line, location_state = _exact_changed_location(
        raw_candidate.affected_location,
        changed_files,
    )
    if location_state != "ok" or not affected_file:
        return None, "missing-changed-causal-file"
    if not _identity_text(candidate.claim) or not _identity_text(candidate.causal_chain):
        return None, "missing-required-finding-detail"
    raw_consequences = (
        value.user_visible_consequences
        if isinstance(value, AcceptedFinding) and value.user_visible_consequences
        else (candidate.user_visible_consequence,)
    )
    raw_validations = (
        value.manual_validations
        if isinstance(value, AcceptedFinding) and value.manual_validations
        else (candidate.manual_validation,)
    )
    consequences = _stable_detail_values(raw_consequences)
    validations = _stable_detail_values(raw_validations)
    if not consequences or not validations:
        return None, "missing-required-finding-detail"
    claim_identity = _detail_identity(candidate.claim)
    consequence_identities = {_detail_identity(item) for item in consequences}
    if claim_identity in consequence_identities or any(
        _detail_identity(item) in {claim_identity, *consequence_identities}
        for item in validations
    ):
        return None, "non-distinct-required-finding-detail"
    if not candidate.related_obligation_ids:
        return None, "missing-related-obligation"
    if any(obligation_id not in obligations for obligation_id in candidate.related_obligation_ids):
        return None, "unknown-related-obligation"
    supporting_records = [records.get(item) for item in candidate.supporting_evidence_ids]
    if not supporting_records or any(record is None for record in supporting_records):
        return None, "missing-retained-evidence"
    usable_support = [record for record in supporting_records if record and record.is_usable_for_coverage]
    if len(usable_support) != len(supporting_records):
        return None, "unusable-retained-evidence"
    related = tuple(obligations[item] for item in candidate.related_obligation_ids)
    supporting = tuple(sorted(usable_support, key=lambda item: item.id))
    contradiction_records = [records.get(item) for item in candidate.contradicting_evidence_ids]
    if any(record is None for record in contradiction_records):
        return None, "missing-retained-evidence"
    if any(record and not record.is_usable_for_coverage for record in contradiction_records):
        return None, "unusable-retained-evidence"
    contradictions = tuple(sorted(
        (record for record in contradiction_records if record), key=lambda item: item.id
    ))
    consequence_support_reason = _consequence_support_reason(
        candidate,
        supporting=supporting,
        contradictions=contradictions,
        related=related,
        affected_file=affected_file,
    )
    cited_ids = {
        item.strip()
        for part in _unicode(candidate.confidence_rationale).split(";")
        if part.strip().casefold().startswith("evidence_ids=")
        for item in part.split("=", 1)[1].split(",")
        if item.strip()
    }
    cited_records = tuple(
        record for record in (*supporting, *contradictions)
        if record.id in cited_ids
    )
    if any(not record.content.strip() or record.truncated for record in cited_records):
        consequence_support_reason = "consequence-not-supported"
    if consequence_support_reason:
        return None, consequence_support_reason
    supporting_citations = tuple(_citation(record) for record in supporting)
    contradicting_citations = tuple(_citation(record) for record in contradictions)
    return AcceptedFinding(
        candidate_id=candidate.candidate_id,
        root_cause_fingerprint=candidate.root_cause_fingerprint,
        deduplication_key=_deduplication_key(candidate),
        claim=candidate.claim,
        user_visible_consequence=consequences[0],
        affected_file=affected_file,
        line=line,
        causal_chain=candidate.causal_chain,
        severity=candidate.severity,
        category=candidate.category,
        supporting_evidence_ids=tuple(record.id for record in supporting),
        contradicting_evidence_ids=tuple(record.id for record in contradictions),
        related_obligation_ids=candidate.related_obligation_ids,
        supporting_citations=supporting_citations,
        contradicting_citations=contradicting_citations,
        manual_validation=validations[0],
        confidence_rationale=candidate.confidence_rationale,
        user_visible_consequences=consequences,
        manual_validations=validations,
        collector_session_id=candidate.collector_session_id,
        model_identity=candidate.model_identity,
        contributor_candidate_ids=(
            candidate.contributor_candidate_ids or (candidate.candidate_id,)
        ),
    ), ""


def candidate_authorization_reason(
    candidate: CandidateFinding,
    evidence: EvidenceStore | EvidenceSnapshot,
    *,
    obligations: Mapping[str, CoverageObligation],
    changed_files: Iterable[str],
) -> str:
    """Run the final authority gate early and return its rejection reason."""
    obligation_map, changed = _controller_state(obligations, changed_files)
    records = {record.id: record for record in _snapshot(evidence).records}
    _accepted, reason = _authorize(
        candidate,
        records=records,
        obligations=obligation_map,
        changed_files=changed,
    )
    return reason


def _decision_rows(critic_result: object) -> tuple[Mapping[str, Any], ...]:
    if isinstance(critic_result, Mapping):
        nested = critic_result.get("actions", critic_result.get("decisions"))
        if nested is not None:
            critic_result = nested
        elif "candidate_id" in critic_result:
            critic_result = (critic_result,)
        else:
            critic_result = tuple(
                {"candidate_id": key, "action": value}
                for key, value in critic_result.items()
            )
    if isinstance(critic_result, (str, bytes)) or not isinstance(critic_result, Iterable):
        return ()
    return tuple(item for item in critic_result if isinstance(item, Mapping))


def _contradicts_stable_github_line_semantics(candidate: CandidateFinding) -> bool:
    """Recognize the narrow claim that GitHub review lines may be zero-based."""
    fields = (
        _unicode(candidate.claim).casefold(),
        _unicode(candidate.causal_chain).casefold(),
        _unicode(candidate.manual_validation).casefold(),
    )
    for text in fields:
        if "github" not in text:
            continue
        coordinate_assertion = (
            re.search(
                r"\b(?:zero[- ](?:based|indexed))\s+"
                r"(?:(?:github|diff|review|comment)\s+){0,3}"
                r"(?:lines?|locations?|coordinates?)\b",
                text,
            )
            is not None
            or re.search(
                r"\b(?:lines?|locations?|coordinates?)\b.{0,40}"
                r"\b(?:may|might|could)\b.{0,30}"
                r"\b(?:be|use)\b.{0,15}\bzero[- ](?:based|indexed)\b",
                text,
            )
            is not None
            or re.search(
                r"\b(?:accepts?|allows?|supports?|uses?)\b.{0,30}"
                r"\bline\s+(?:0|zero)\b",
                text,
            )
            is not None
            or re.search(
                r"\bline\s+(?:0|zero)\b.{0,30}"
                r"\b(?:valid|accepted|allowed|supported)\b",
                text,
            )
            is not None
        )
        if coordinate_assertion and any(
            term in text for term in ("diff", "review", "comment", "api")
        ):
            return True
    return False


def _record_verification_or_platform_rejection(
    candidate: CandidateFinding,
    reason: str,
    *,
    verification: dict[str, CandidateVerificationRequest],
    rejected: dict[str, CandidateDisposition],
    dispositions: dict[str, CandidateDisposition],
    target_id: str | None = None,
) -> None:
    candidate_id = candidate.candidate_id
    if _contradicts_stable_github_line_semantics(candidate):
        disposition = CandidateDisposition(
            candidate_id,
            "reject",
            "deterministic-platform-contradiction",
        )
        rejected[candidate_id] = disposition
        dispositions[candidate_id] = disposition
        return
    verification[candidate_id] = CandidateVerificationRequest(candidate, reason)
    dispositions[candidate_id] = CandidateDisposition(
        candidate_id,
        "request_verification",
        reason,
        target_id,
    )


def _merge_findings(group: Iterable[AcceptedFinding], representative_id: str) -> AcceptedFinding:
    values = tuple(sorted(group, key=lambda item: item.candidate_id))
    representative = next(item for item in values if item.candidate_id == representative_id)
    supporting_citations = {
        citation.evidence_id: citation
        for item in values for citation in item.supporting_citations
    }
    contradicting_citations = {
        citation.evidence_id: citation
        for item in values for citation in item.contradicting_citations
    }
    consequences = _stable_detail_values(
        detail
        for item in values
        for detail in (
            item.user_visible_consequences
            or (item.user_visible_consequence,)
        )
    )
    validations = _stable_detail_values(
        detail
        for item in values
        for detail in (item.manual_validations or (item.manual_validation,))
    )
    return replace(
        representative,
        severity=max(values, key=lambda item: _SEVERITY_RANK[item.severity]).severity,
        supporting_evidence_ids=tuple(sorted({
            evidence_id for item in values for evidence_id in item.supporting_evidence_ids
        })),
        contradicting_evidence_ids=tuple(sorted({
            evidence_id for item in values for evidence_id in item.contradicting_evidence_ids
        })),
        related_obligation_ids=tuple(sorted({
            obligation_id for item in values for obligation_id in item.related_obligation_ids
        })),
        supporting_citations=tuple(
            supporting_citations[key] for key in sorted(supporting_citations)
        ),
        contradicting_citations=tuple(
            contradicting_citations[key] for key in sorted(contradicting_citations)
        ),
        user_visible_consequence=consequences[0],
        manual_validation=validations[0],
        confidence_rationale=representative.confidence_rationale,
        user_visible_consequences=consequences,
        manual_validations=validations,
        contributor_candidate_ids=tuple(sorted({
            contributor for item in values for contributor in item.contributor_candidate_ids
        })),
    )


def adjudicate_candidates(
    candidates: Iterable[CandidateFinding],
    critic_result: object,
    evidence: EvidenceStore | EvidenceSnapshot,
    *,
    obligations: Mapping[str, CoverageObligation],
    changed_files: Iterable[str],
) -> AdjudicatedReview:
    """Adjudicate candidates against immutable controller-owned authority."""
    obligation_map, changed = _controller_state(obligations, changed_files)
    evidence_snapshot = _snapshot(evidence)
    records = {record.id: record for record in evidence_snapshot.records}
    candidate_by_id: dict[str, CandidateFinding] = {}
    duplicate_ids: set[str] = set()
    for value in candidates:
        if not isinstance(value, CandidateFinding):
            raise TypeError("candidates must contain CandidateFinding values")
        candidate_id = _unicode(value.candidate_id).strip()
        if not candidate_id:
            raise ValueError("candidate IDs must be non-empty")
        if candidate_id in candidate_by_id:
            duplicate_ids.add(candidate_id)
            continue
        candidate_by_id[candidate_id] = _normalized_candidate(value)

    decisions: dict[str, Mapping[str, Any]] = {}
    for row in _decision_rows(critic_result):
        candidate_id = _unicode(row.get("candidate_id", row.get("id", ""))).strip()
        if candidate_id in candidate_by_id and candidate_id not in decisions:
            decisions[candidate_id] = row

    accepted: dict[str, AcceptedFinding] = {}
    rejected: dict[str, CandidateDisposition] = {}
    verification: dict[str, CandidateVerificationRequest] = {}
    unknowns: dict[str, CandidateFinding] = {}
    dispositions: dict[str, CandidateDisposition] = {}
    merge_sources: dict[str, list[AcceptedFinding]] = {}

    for candidate_id in sorted(candidate_by_id):
        candidate = candidate_by_id[candidate_id]
        decision = decisions.get(candidate_id)
        action = _unicode(decision.get("action", "")).strip().lower() if decision else ""
        target_id = _unicode(
            decision.get("target_id", decision.get("merge_into", ""))
        ).strip() if decision else ""
        if candidate_id in duplicate_ids:
            disposition = CandidateDisposition(candidate_id, "reject", "duplicate-candidate-id")
            rejected[candidate_id] = disposition
            dispositions[candidate_id] = disposition
            continue
        if action not in _CRITIC_ACTIONS:
            reason = "invalid-critic-action" if action else "missing-critic-action"
            disposition = CandidateDisposition(candidate_id, "reject", reason)
            rejected[candidate_id] = disposition
            dispositions[candidate_id] = disposition
            continue
        if action == "reject":
            disposition = CandidateDisposition(candidate_id, action, "critic-rejected")
            rejected[candidate_id] = disposition
            dispositions[candidate_id] = disposition
            continue
        if action == "downgrade_unknown":
            unknowns[candidate_id] = candidate
            dispositions[candidate_id] = CandidateDisposition(
                candidate_id, action, "critic-downgraded-to-unknown"
            )
            continue
        if action == "request_verification":
            _record_verification_or_platform_rejection(
                candidate,
                "critic-requested-verification",
                verification=verification,
                rejected=rejected,
                dispositions=dispositions,
            )
            continue
        if candidate.severity == "info":
            disposition = CandidateDisposition(
                candidate_id, "reject", "non-actionable-info",
            )
            rejected[candidate_id] = disposition
            dispositions[candidate_id] = disposition
            continue
        authorized, reason = _authorize(
            candidate,
            records=records,
            obligations=obligation_map,
            changed_files=changed,
        )
        if authorized is None:
            _record_verification_or_platform_rejection(
                candidate,
                reason,
                verification=verification,
                rejected=rejected,
                dispositions=dispositions,
            )
            continue
        if action == "merge":
            target = candidate_by_id.get(target_id)
            if target is None or not _merge_compatible(candidate, target):
                disposition = CandidateDisposition(
                    candidate_id, "reject", "invalid-merge-target", target_id or None
                )
                rejected[candidate_id] = disposition
                dispositions[candidate_id] = disposition
                continue
            merge_sources.setdefault(target_id, []).append(authorized)
            continue
        accepted[candidate_id] = authorized
        dispositions[candidate_id] = CandidateDisposition(candidate_id, "keep", "accepted")

    for target_id in sorted(merge_sources):
        sources = merge_sources[target_id]
        target = accepted.get(target_id)
        if target is None:
            for source in sources:
                _record_verification_or_platform_rejection(
                    candidate_by_id[source.candidate_id],
                    "merge-target-not-authoritative",
                    verification=verification,
                    rejected=rejected,
                    dispositions=dispositions,
                    target_id=target_id,
                )
            continue
        accepted[target_id] = _merge_findings((target, *sources), target_id)
        for source in sources:
            dispositions[source.candidate_id] = CandidateDisposition(
                source.candidate_id, "merge", "merged", target_id
            )

    by_fingerprint: dict[str, list[AcceptedFinding]] = {}
    for finding in accepted.values():
        by_fingerprint.setdefault(finding.deduplication_key, []).append(finding)
    deduplicated: dict[str, AcceptedFinding] = {}
    for fingerprint in sorted(by_fingerprint):
        group = tuple(sorted(by_fingerprint[fingerprint], key=lambda item: item.candidate_id))
        representative_id = group[0].candidate_id
        deduplicated[representative_id] = _merge_findings(group, representative_id)
        dispositions[representative_id] = CandidateDisposition(
            representative_id, "keep", "accepted"
        )
        for contributor in group[1:]:
            dispositions[contributor.candidate_id] = CandidateDisposition(
                contributor.candidate_id, "merge", "semantic-duplicate", representative_id
            )

    return AdjudicatedReview(
        accepted=tuple(deduplicated[key] for key in sorted(deduplicated)),
        rejected=tuple(rejected[key] for key in sorted(rejected)),
        verification_requests=tuple(verification[key] for key in sorted(verification)),
        unknowns=tuple(unknowns[key] for key in sorted(unknowns)),
        dispositions=tuple(dispositions[key] for key in sorted(dispositions)),
    )


def _revalidated_findings(
    values: Iterable[object],
    *,
    evidence: EvidenceStore | EvidenceSnapshot,
    obligations: Mapping[str, CoverageObligation],
    changed_files: Iterable[str],
) -> tuple[tuple[AcceptedFinding, ...], tuple[CandidateVerificationRequest, ...]]:
    obligation_map, changed = _controller_state(obligations, changed_files)
    evidence_snapshot = _snapshot(evidence)
    records = {record.id: record for record in evidence_snapshot.records}
    accepted: list[AcceptedFinding] = []
    verification: list[CandidateVerificationRequest] = []
    for value in values:
        candidate = _candidate_from_accepted(value) if isinstance(value, (AcceptedFinding, CandidateFinding)) else None
        if candidate is None:
            continue
        authorized, reason = _authorize(
            value,
            records=records,
            obligations=obligation_map,
            changed_files=changed,
        )
        if authorized is None:
            verification.append(CandidateVerificationRequest(_normalized_candidate(candidate), reason))
        else:
            accepted.append(authorized)
    accepted.sort(key=lambda item: (item.root_cause_fingerprint, item.candidate_id))
    verification.sort(key=lambda item: item.candidate.candidate_id)
    return tuple(accepted), tuple(verification)


def apply_runtime_verdict_policy(
    *,
    model_verdict: str,
    unresolved: Iterable[object],
    allow_approve: bool,
    evidence: EvidenceStore | EvidenceSnapshot,
    obligations: Mapping[str, CoverageObligation],
    changed_files: Iterable[str],
    review: AdjudicatedReview | None = None,
    accepted: Iterable[object] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> RuntimeVerdictPolicyResult:
    """Revalidate authority before any accepted finding can affect verdict."""
    if review is not None and accepted is not None:
        raise ValueError("provide review or accepted, not both")
    values = review.accepted if isinstance(review, AdjudicatedReview) else tuple(accepted or ())
    authoritative, _ = _revalidated_findings(
        values, evidence=evidence, obligations=obligations, changed_files=changed_files
    )
    obligation_map, _ = _controller_state(obligations, changed_files)
    unresolved_controller_values: list[CoverageObligation] = []
    for item in unresolved:
        if isinstance(item, str):
            obligation_id = item
        elif isinstance(item, Mapping):
            obligation_id = _unicode(item.get("obligation_id", item.get("id", ""))).strip()
        else:
            obligation_id = _unicode(getattr(item, "obligation_id", getattr(item, "id", ""))).strip()
        if obligation_id in obligation_map:
            unresolved_controller_values.append(obligation_map[obligation_id])
    return derive_runtime_verdict(
        model_verdict=model_verdict,
        supported_findings={item.candidate_id: item.severity for item in authoritative},
        unresolved=tuple(unresolved_controller_values),
        allow_approve=allow_approve,
        policy=policy,
    )


def _exact_detail(value: object) -> str:
    return " ".join(_unicode(value).casefold().split())


def _detail_scalars(value: object, *, _seen: set[int] | None = None) -> tuple[str, ...]:
    seen = _seen if _seen is not None else set()
    if value is None:
        return ()
    if isinstance(value, Enum):
        return _detail_scalars(value.value, _seen=seen)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        values = [value]
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = None
        if decoded is not None and not isinstance(decoded, str):
            values.extend(_detail_scalars(decoded, _seen=seen))
        return tuple(values)
    if isinstance(value, bool):
        return ("true" if value else "false",)
    if isinstance(value, (int, float)):
        return (str(value),)

    identity = id(value)
    if identity in seen:
        return ()
    seen.add(identity)
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(
            detail
            for field in sorted(dataclass_fields(value), key=lambda item: item.name)
            for detail in _detail_scalars(getattr(value, field.name), _seen=seen)
        )
    if isinstance(value, Mapping):
        return tuple(
            detail
            for key, item in sorted(value.items(), key=lambda pair: _unicode(pair[0]))
            for part in (key, item)
            for detail in _detail_scalars(part, _seen=seen)
        )
    if isinstance(value, (set, frozenset)):
        values: Iterable[object] = sorted(value, key=_unicode)
    elif isinstance(value, Iterable):
        values = value
    else:
        return ()
    return tuple(
        detail
        for item in values
        for detail in _detail_scalars(item, _seen=seen)
    )


def _topic_values(
    values: object,
    *,
    forbidden: frozenset[str],
    limit: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return ()
    labels = {
        _TOPIC_LABELS[item]
        for item in values
        if isinstance(item, ReviewOrientationTopic)
        and _exact_detail(_TOPIC_LABELS[item]) not in forbidden
    }
    return tuple(sorted(labels))[:limit]


def _structured_ids(
    values: object,
    *,
    forbidden: frozenset[str],
    limit: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return ()
    result = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = _unicode(value).strip().casefold()
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9._/\-]{0,159}", normalized)
            or _exact_detail(normalized) in forbidden
        ):
            continue
        result.add(normalized)
    return tuple(sorted(result))[:limit]


def _category_topic(value: str) -> ReviewOrientationTopic | None:
    normalized = _unicode(value).strip().casefold().replace("-", "_")
    try:
        return ReviewOrientationTopic(normalized)
    except ValueError:
        return None


def _aggregate_theme_categories(
    categories: Iterable[str],
    *,
    forbidden: frozenset[str],
) -> tuple[str | None, str | None]:
    values = tuple(categories)
    topics = tuple(_category_topic(item) for item in values)
    material = {item for item in topics if item is not None}
    if len(values) < 2 or len(material) != 1 or any(item is None for item in topics):
        return None, None
    topic = next(iter(material))
    label = _TOPIC_LABELS[topic]
    if _exact_detail(label) in forbidden:
        return None, None
    return topic.value, label


def render_review_handoff(handoff: ReviewHandoff) -> str:
    """Render the complete sparse handoff from its structured projection."""
    if not isinstance(handoff, ReviewHandoff):
        raise TypeError("handoff must be a ReviewHandoff")
    reviewed = tuple(sorted(set((
        *handoff.specialist_focuses,
        *handoff.recipe_focuses,
        *handoff.coverage_boundaries,
    ))))
    if tuple(handoff.reviewed_focuses) != reviewed:
        raise ValueError("reviewed_focuses must equal the structured focus projection")
    theme_label = review_orientation_label(handoff.finding_theme)
    if handoff.finding_theme and theme_label is None:
        raise ValueError("finding_theme must be a review orientation topic")

    lines = ["## AI Review Handoff"]
    if handoff.recommendation:
        lines.extend(("", f"**Recommendation:** {handoff.recommendation}"))
    if handoff.status:
        lines.extend(("", f"**Status:** {handoff.status}"))
    if handoff.what_changed:
        lines.extend(("", "### What changed", "", " ".join(handoff.what_changed)))
    if handoff.ai_reviewed:
        lines.extend((
            "", "### What the AI reviewed", "", " ".join(handoff.ai_reviewed),
        ))
    if handoff.thread_status:
        lines.extend(("", f"**Prepared detail notes:** {handoff.thread_status}"))
    if theme_label:
        lines.extend(("", f"**Aggregate finding theme:** {theme_label}"))
    if handoff.human_focus:
        lines.extend(("", "### Human focus", "", " ".join(handoff.human_focus)))
    lines.extend((
        "",
        "These focus suggestions do not reduce responsibility to review the complete change.",
    ))
    if handoff.coverage_warning:
        lines.extend((
            "",
            f"**Material coverage warning:** {handoff.coverage_warning}",
        ))
    if handoff.access_request_count:
        access_text = f"{handoff.access_request_count} open"
        if handoff.access_request_url:
            access_text = f"[{access_text}]({handoff.access_request_url})"
        lines.extend(("", f"**Source access requests:** {access_text}"))
    return "\n".join(lines).strip() + "\n"


def project_review_handoff(
    context: ReviewHandoffContext,
    *,
    finding_categories: Iterable[str],
    forbidden_detail_roots: Iterable[object],
    obligations: Mapping[str, CoverageObligation],
    changed_files: Iterable[str] = (),
) -> ReviewHandoff:
    """Project and render a sparse handoff from authoritative structured state."""
    if not isinstance(context, ReviewHandoffContext):
        raise TypeError("context must be a ReviewHandoffContext")
    obligation_map = dict(obligations)
    recommendation_map = {
        "approve": "Approve",
        "request_changes": "Request changes",
        "notice": "Human review required",
        "human_review_required": "Human review required",
        "no_blocking_findings": "No blocking findings identified",
    }
    status_map = {
        "complete": "AI review complete",
        "degraded": "AI review completed with material coverage limits",
        "incomplete": "AI review incomplete",
    }
    recommendation = recommendation_map.get(_unicode(context.recommendation).strip().lower(), "")
    status = status_map.get(_unicode(context.status).strip().lower(), "")
    detail_values = _detail_scalars(tuple(forbidden_detail_roots))
    forbidden_values = {
        value for item in detail_values if (value := _exact_detail(item))
    }
    for item in detail_values:
        canonical = _canonical_request_url(item)
        if canonical is not None:
            forbidden_values.add(_exact_detail(canonical[1]))
    forbidden = frozenset(forbidden_values)

    def renderable(*values: object) -> bool:
        normalized = tuple(_exact_detail(value) for value in values)
        return bool(normalized) and all(
            value and value not in forbidden for value in normalized
        )

    if not renderable(recommendation, f"Recommendation: {recommendation}"):
        recommendation = ""
    if not renderable(status, f"Status: {status}"):
        status = ""
    change_topics = _topic_values(
        context.change_topics, forbidden=forbidden, limit=6
    )
    component_ids = _structured_ids(
        context.component_ids, forbidden=forbidden, limit=6
    )
    components = tuple(
        rendered
        for item in component_ids
        if renderable(rendered := f"Component: {item}")
    )
    change_map = tuple(sorted({*change_topics, *components}))
    specialist_focuses = _topic_values(
        context.specialist_topics, forbidden=forbidden, limit=6
    )
    recipe_ids = _structured_ids(
        context.recipe_ids, forbidden=forbidden, limit=6
    )
    recipes = tuple(
        rendered
        for item in recipe_ids
        if renderable(rendered := f"Repository recipe: {item}")
    )
    boundaries = _topic_values(
        context.coverage_boundary_topics, forbidden=forbidden, limit=6
    )
    specialist_line = "Specialist focus: " + "; ".join(specialist_focuses)
    if specialist_focuses and not renderable(specialist_line):
        specialist_focuses = ()
    recipe_line = "Repository recipes: " + "; ".join(recipes)
    if recipes and not renderable(recipe_line):
        recipes = ()
    boundary_line = "Coverage boundaries: " + "; ".join(boundaries)
    if boundaries and not renderable(boundary_line):
        boundaries = ()
    reviewed_focuses = tuple(sorted(set((*specialist_focuses, *recipes, *boundaries))))
    review_emphasis = _topic_values(
        context.review_emphasis_topics, forbidden=forbidden, limit=3
    )
    changed_paths = tuple(sorted(
        {
            _unicode(path).strip()
            for path in changed_files
            if _unicode(path).strip()
        }
        | {
            path
            for value in obligations.values()
            for path in (*value.scope, *value.seed_hints)
            if path
        }
    ))
    reviewed_paths = tuple(sorted({
        *changed_paths,
        *(
            _unicode(path).strip()
            for path in context.context_paths
            if _unicode(path).strip()
        ),
    }))

    def behavioral_summaries(
        values: Iterable[object], *, limit: int, require_path: bool,
        max_chars: int = 160, allowed_paths: Iterable[str] | None = None,
    ) -> tuple[str, ...]:
        selected: list[str] = []
        path_allowlist = tuple(allowed_paths or changed_paths)
        for value in values:
            text = " ".join(_unicode(value).split())
            if not text or len(text) > max_chars or not renderable(text):
                continue
            if require_path and not any(
                f"`{path}`" in text for path in path_allowlist
            ):
                continue
            if text not in selected:
                selected.append(text)
            if len(selected) == limit:
                break
        return tuple(selected)

    what_changed = behavioral_summaries(
        context.what_changed,
        limit=5,
        require_path=not context.what_changed_is_validated_overview,
        # The controller's immutable-facts fallback is already validated and
        # intentionally describes several components.  It is a handoff
        # overview, not an untrusted model claim, so allow it to be a little
        # longer than an individual behavioral detail.
        max_chars=600 if context.what_changed_is_validated_overview else 160,
    )
    ai_reviewed = behavioral_summaries(
        context.ai_reviewed,
        limit=3,
        require_path=not context.ai_reviewed_is_validated_summary,
        max_chars=600 if context.ai_reviewed_is_validated_summary else 160,
        allowed_paths=reviewed_paths,
    )
    human_focus = tuple(
        text
        for value in context.human_focus[:2]
        if (
            (text := " ".join(_unicode(value).split()))
            and len(text) <= 600
            and renderable(text)
        )
    )
    prepared_note_count = (
        context.unresolved_thread_count
        if isinstance(context.unresolved_thread_count, int)
        and not isinstance(context.unresolved_thread_count, bool)
        and context.unresolved_thread_count > 0
        else 0
    )
    raw_prepared_severity = _unicode(context.highest_thread_severity).strip().lower()
    prepared_severity = (
        raw_prepared_severity
        if raw_prepared_severity in _SEVERITY_RANK
        else None
    )
    thread_status = None
    if prepared_note_count:
        note_label = (
            "detail review note"
            if prepared_note_count == 1
            else "detail review notes"
        )
        candidate_thread_status = (
            f"{prepared_note_count} {note_label} prepared for publication"
        )
        if prepared_severity:
            candidate_thread_status += (
                f"; highest proposed finding severity: {prepared_severity}"
            )
        candidate_thread_status += "."
        if renderable(
            candidate_thread_status,
            f"Prepared detail notes: {candidate_thread_status}",
        ):
            thread_status = candidate_thread_status
    theme, theme_label = _aggregate_theme_categories(
        finding_categories,
        forbidden=forbidden,
    )
    if theme_label and not renderable(
        theme_label, f"Aggregate finding theme: {theme_label}"
    ):
        theme, theme_label = None, None
    diagnostics = _canonical_request_url(context.diagnostics_url or "")
    diagnostics_url = diagnostics[1] if diagnostics else None
    if diagnostics_url and not renderable(
        diagnostics_url, f"Diagnostics: {diagnostics_url}"
    ):
        diagnostics_url = None
    coverage_warning = None
    if context.material_coverage_limited:
        candidate_warning = "Material evidence or session coverage is incomplete."
        if context.candidate_retention_limited:
            candidate_warning += (
                " Candidate finding retention was incomplete; published finding "
                "counts may be incomplete and must not be read as clean coverage."
            )
        degraded_stages = _structured_ids(
            tuple(
                "specialist"
                if isinstance(value, str)
                and value.strip().casefold().startswith(
                    ("specialist:", "specialist_hook:")
                )
                else value
                for value in context.degraded_stages
            ),
            forbidden=forbidden,
            limit=6,
        )
        if degraded_stages:
            candidate_warning += (
                " Affected stages: " + ", ".join(degraded_stages) + "."
            )
        if diagnostics_url:
            candidate_warning += f" Diagnostics: {diagnostics_url}"
        if renderable(
            candidate_warning,
            f"Material coverage warning: {candidate_warning}",
        ):
            coverage_warning = candidate_warning
    valid_source_notes = build_source_access_request_notes(
        context.source_access_requests,
        obligations=obligation_map,
    )
    access_count = len(valid_source_notes)
    access = _canonical_request_url(context.access_request_url or "")
    access_url = access[1] if access else None
    if access_url and not renderable(access_url):
        access_url = None

    if access_count:
        access_text = f"{access_count} open"
        if access_url:
            access_text = f"[{access_text}]({access_url})"
        if not renderable(
            f"{access_count} open",
            access_text,
            f"Source access requests: {access_text}",
        ):
            access_count = 0
            access_url = None
    projection = ReviewHandoff(
        recommendation=recommendation,
        status=status,
        change_map=change_map,
        reviewed_focuses=reviewed_focuses,
        specialist_focuses=specialist_focuses,
        recipe_focuses=recipes,
        coverage_boundaries=boundaries,
        thread_status=thread_status,
        finding_theme=theme,
        review_emphasis=review_emphasis,
        coverage_warning=coverage_warning,
        access_request_count=access_count,
        access_request_url=access_url,
        what_changed=what_changed,
        ai_reviewed=ai_reviewed,
        human_focus=human_focus or review_emphasis,
    )
    return replace(projection, markdown=render_review_handoff(projection))


def build_review_handoff(
    context: ReviewHandoffContext,
    *,
    review: AdjudicatedReview | None,
    evidence: EvidenceStore | EvidenceSnapshot,
    obligations: Mapping[str, CoverageObligation],
    changed_files: Iterable[str],
) -> ReviewHandoff:
    """Build orientation exclusively from typed controller context."""
    if not isinstance(context, ReviewHandoffContext):
        raise TypeError("context must be a ReviewHandoffContext")
    obligation_map, _ = _controller_state(obligations, changed_files)
    authoritative, _ = _revalidated_findings(
        review.accepted if isinstance(review, AdjudicatedReview) else (),
        evidence=evidence,
        obligations=obligation_map,
        changed_files=changed_files,
    )
    snapshot = _snapshot(evidence)
    detail_roots: list[object] = []
    if isinstance(review, AdjudicatedReview):
        detail_roots.extend((
            review.accepted,
            review.unknowns,
            review.verification_requests,
        ))
    detail_roots.extend((
        context.source_access_requests,
        snapshot.records,
    ))
    return project_review_handoff(
        context,
        finding_categories=(item.category for item in authoritative),
        forbidden_detail_roots=detail_roots,
        obligations=obligation_map,
        changed_files=changed_files,
    )


def _quoted(value: object, *, limit: int = _NOTE_VALUE_LIMIT) -> str:
    single_line = " ".join(_unicode(value).split())[:limit]
    return "<code>" + html.escape(single_line, quote=True) + "</code>"


def _valid_line(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _request_fingerprint(
    kind: ReviewNoteKind,
    text: str,
    obligation_ids: tuple[str, ...],
    file: str | None,
    extra_identity: tuple[str, ...] = (),
) -> str:
    identity = "\x1f".join((
        kind.value, _identity_text(text), "|".join(obligation_ids), file or "", *extra_identity,
    ))
    return f"{kind.value}:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _verification_note(
    request: CandidateVerificationRequest,
    *,
    records: Mapping[str, EvidenceRecord],
    obligations: Mapping[str, CoverageObligation],
    changed_files: tuple[str, ...],
) -> ReviewNote | None:
    candidate = _normalized_candidate(request.candidate)
    if _contradicts_stable_github_line_semantics(candidate):
        return None
    if not _identity_text(candidate.claim):
        return None
    obligation_ids = tuple(
        item for item in candidate.related_obligation_ids if item in obligations
    )
    if not obligation_ids:
        return None
    file, line, state = _exact_changed_location(
        candidate.affected_location,
        changed_files,
    )
    if state != "ok":
        file, line = "", None
    evidence_ids = tuple(sorted(
        item for item in candidate.supporting_evidence_ids + candidate.contradicting_evidence_ids
        if item in records and records[item].is_usable_for_coverage
    ))
    markdown = (
        "### Verification request\n\n"
        "**Question:** Verify whether this potential issue is present before treating it as a defect: "
        + _quoted(candidate.claim)
        + "\n\n**Why human input is needed:** " + _quoted(request.reason)
    )
    if evidence_ids:
        markdown += "\n\n**Retained evidence already checked:** " + ", ".join(
            _quoted(item, limit=160) for item in evidence_ids
        )
    return ReviewNote(
        kind=ReviewNoteKind.VERIFICATION_REQUEST,
        fingerprint=_request_fingerprint(
            ReviewNoteKind.VERIFICATION_REQUEST,
            candidate.claim,
            obligation_ids,
            file or None,
            (request.reason,),
        ),
        markdown=markdown,
        related_obligation_ids=obligation_ids,
        evidence_ids=evidence_ids,
        file=file or None,
        line=line,
    )


def _mapping_verification(
    value: object,
    *,
    obligations: Mapping[str, CoverageObligation],
    changed_files: tuple[str, ...],
    records: Mapping[str, EvidenceRecord],
) -> ReviewNote | None:
    if not isinstance(value, Mapping):
        return None
    allowed = {
        "question", "reason", "related_obligation_ids", "obligation_ids",
        "evidence_ids", "file", "line",
    }
    if set(value) - allowed:
        return None
    question = _unicode(value.get("question", "")).strip()
    if not question:
        return None
    obligation_ids = _stable_strings(
        value.get("related_obligation_ids", value.get("obligation_ids", ()))
    )
    if not obligation_ids or any(item not in obligations for item in obligation_ids):
        return None
    file, parsed_line, state = _exact_changed_location(
        value.get("file", ""),
        changed_files,
    )
    if state != "ok":
        file, parsed_line = "", None
    line = _valid_line(value.get("line"))
    if "line" not in value:
        line = parsed_line
    evidence_ids = tuple(sorted(
        item for item in _stable_strings(value.get("evidence_ids", ()))
        if item in records and records[item].is_usable_for_coverage
    ))
    reason = _unicode(value.get("reason", "")).strip() or "Human confirmation is required to resolve this coverage question."
    markdown = (
        "### Verification request\n\n**Question:** " + _quoted(question)
        + "\n\n**Why human input is needed:** " + _quoted(reason)
    )
    if evidence_ids:
        markdown += "\n\n**Retained evidence already checked:** " + ", ".join(
            _quoted(item, limit=160) for item in evidence_ids
        )
    return ReviewNote(
        kind=ReviewNoteKind.VERIFICATION_REQUEST,
        fingerprint=_request_fingerprint(
            ReviewNoteKind.VERIFICATION_REQUEST, question, obligation_ids, file or None, (reason,)
        ),
        markdown=markdown,
        related_obligation_ids=obligation_ids,
        evidence_ids=evidence_ids,
        file=file or None,
        line=line,
    )


def _source_request(
    value: object,
) -> SourceAccessRequest | RepositoryAccessRequest | None:
    if isinstance(value, RepositoryAccessRequest):
        value = value.as_dict()
    if isinstance(value, SourceAccessRequest):
        return value
    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind", "source_access_request")
    if kind == "repository_access_request":
        allowed = {
            "kind", "repository", "endpoint", "revision", "obligation_id",
            "purpose", "model_purpose", "authority_reason",
        }
        if set(value) - allowed:
            return None
        fields = {
            key: _unicode(value.get(key, "")).strip()
            for key in (
                "repository", "endpoint", "revision", "obligation_id",
                "purpose", "model_purpose", "authority_reason",
            )
        }
        if not all(fields[key] for key in (
            "repository", "endpoint", "obligation_id", "purpose",
        )) or any(len(fields[key]) > limit for key, limit in {
            "repository": 200, "endpoint": 1000, "revision": 40,
            "obligation_id": 160, "purpose": 1000,
            "model_purpose": 300, "authority_reason": 500,
        }.items()):
            return None
        try:
            validated = repository_access_request(
                fields["endpoint"], fields["obligation_id"],
                "Validate the retained repository request.",
                fields["model_purpose"], fields["authority_reason"],
            )
        except ValueError:
            return None
        if (
            validated.repository != fields["repository"]
            or (validated.revision or "") != fields["revision"]
        ):
            return None
        expected_prefix = (
            "Verify existence, provenance, metadata, and bounded changed-file "
            f"information for the exact pinned repository revision {fields['revision']} "
            f"in {fields['repository']} for this assignment:"
            if fields["revision"] else
            f"Retrieve bounded read-only GitHub API metadata from {fields['repository']} "
            "for this assignment:"
        )
        if not fields["purpose"].startswith(expected_prefix):
            return None
        return RepositoryAccessRequest(
            repository=fields["repository"], endpoint=validated.endpoint,
            revision=validated.revision, obligation_id=fields["obligation_id"],
            purpose=fields["purpose"], model_purpose=fields["model_purpose"],
            authority_reason=fields["authority_reason"],
        )
    allowed = {
        "kind", "host", "candidate_url", "obligation_id", "purpose",
        "authority_reason", "model_purpose",
    }
    if set(value) - allowed or kind != "source_access_request":
        return None
    fields = {
        key: _unicode(value.get(key, "")).strip()
        for key in (
            "host", "candidate_url", "obligation_id", "purpose",
            "authority_reason", "model_purpose",
        )
    }
    if not all(fields[key] for key in ("host", "candidate_url", "obligation_id", "purpose")):
        return None
    return SourceAccessRequest(**fields)


def _canonical_request_url(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(_unicode(value).strip())
        host_value = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not host_value
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.rsplit("@", 1)[-1].endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        return None
    host = host_value.casefold()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    url = urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))
    return host, url


def _source_note(
    value: object,
    *,
    obligations: Mapping[str, CoverageObligation],
) -> ReviewNote | None:
    request = _source_request(value)
    if request is None or request.obligation_id not in obligations:
        return None
    if isinstance(request, RepositoryAccessRequest):
        reason = request.authority_reason or (
            "The repository is not in the current-branch GitHub API allowlist."
        )
        markdown = (
            "### Repository access request\n\n"
            "**Repository:** " + _quoted(request.repository, limit=200)
            + "\n\n**GitHub API endpoint:** " + _quoted(request.endpoint)
            + (
                "\n\n**Exact revision:** " + _quoted(request.revision, limit=80)
                if request.revision else ""
            )
            + "\n\n**Purpose:** " + _quoted(request.purpose)
            + (
                "\n\n**Specialist-provided context:** "
                + _quoted(request.model_purpose, limit=300)
                if request.model_purpose else ""
            )
            + "\n\n**Why human input is needed:** " + _quoted(reason)
            + "\n\nNo repository content was retrieved; access remains pending "
            "current-branch human authorization."
        )
        return ReviewNote(
            kind=ReviewNoteKind.SOURCE_ACCESS_REQUEST,
            fingerprint=_request_fingerprint(
                ReviewNoteKind.SOURCE_ACCESS_REQUEST,
                request.purpose,
                (request.obligation_id,),
                None,
                (request.repository, request.endpoint),
            ),
            markdown=markdown,
            related_obligation_ids=(request.obligation_id,),
            evidence_ids=(),
        )
    canonical = _canonical_request_url(request.candidate_url)
    if canonical is None:
        return None
    url_host, url = canonical
    host = _unicode(request.host).strip().casefold()
    if host != url_host:
        return None
    reason = request.authority_reason or "Repository source policy requires human authorization before retrieval."
    markdown = (
        "### Source access request\n\n"
        "**Host:** " + _quoted(host, limit=253)
        + "\n\n**Candidate URL:** " + _quoted(url)
        + "\n\n**Purpose:** " + _quoted(request.purpose)
        + (
            "\n\n**Specialist-provided context:** "
            + _quoted(request.model_purpose, limit=300)
            if request.model_purpose else ""
        )
        + "\n\n**Why human input is needed:** " + _quoted(reason)
        + "\n\nDiscovery metadata is not review evidence; retrieval remains pending approval."
    )
    return ReviewNote(
        kind=ReviewNoteKind.SOURCE_ACCESS_REQUEST,
        fingerprint=_request_fingerprint(
            ReviewNoteKind.SOURCE_ACCESS_REQUEST,
            request.purpose,
            (request.obligation_id,),
            None,
            (host, url),
        ),
        markdown=markdown,
        related_obligation_ids=(request.obligation_id,),
        evidence_ids=(),
    )


def build_source_access_request_notes(
    values: Iterable[object],
    *,
    obligations: Mapping[str, CoverageObligation],
) -> tuple[ReviewNote, ...]:
    """Project valid source requests using the production authorization rules."""
    notes: dict[str, ReviewNote] = {}
    repositories: dict[tuple[str, str, str], list[RepositoryAccessRequest]] = {}
    for value in values:
        request = _source_request(value)
        if request is None or request.obligation_id not in obligations:
            continue
        if isinstance(request, RepositoryAccessRequest):
            key = (request.repository, request.revision or "", request.endpoint)
            repositories.setdefault(key, []).append(request)
            continue
        note = _source_note(request, obligations=obligations)
        if note is not None:
            notes[note.fingerprint] = note

    for (repository, revision, endpoint), requests in repositories.items():
        obligation_ids = tuple(dict.fromkeys(
            request.obligation_id for request in requests
        ))
        purposes = tuple(dict.fromkeys(request.purpose for request in requests))[:8]
        contexts = tuple(dict.fromkeys(
            request.model_purpose for request in requests if request.model_purpose
        ))[:8]
        reasons = tuple(dict.fromkeys(
            request.authority_reason for request in requests
            if request.authority_reason
        ))
        reason = reasons[0] if reasons else (
            "The repository is not in the current-branch GitHub API allowlist."
        )
        markdown = (
            "### Repository access request\n\n"
            "**Repository:** " + _quoted(repository, limit=200)
            + "\n\n**GitHub API endpoint:** " + _quoted(endpoint)
            + (
                "\n\n**Exact revision:** " + _quoted(revision, limit=80)
                if revision else ""
            )
            + "\n\n**Purposes:**\n"
            + "\n".join("- " + _quoted(item) for item in purposes)
            + (
                "\n\n**Specialist-provided context:**\n"
                + "\n".join("- " + _quoted(item, limit=300) for item in contexts)
                if contexts else ""
            )
            + "\n\n**Why human input is needed:** " + _quoted(reason)
            + "\n\nNo repository content was retrieved; access remains pending "
            "current-branch human authorization."
        )
        note = ReviewNote(
            kind=ReviewNoteKind.SOURCE_ACCESS_REQUEST,
            fingerprint=_request_fingerprint(
                ReviewNoteKind.SOURCE_ACCESS_REQUEST,
                repository + ":" + endpoint,
                obligation_ids,
                None,
                (repository, revision, endpoint),
            ),
            markdown=markdown,
            related_obligation_ids=obligation_ids,
            evidence_ids=(),
        )
        notes[note.fingerprint] = note
    return tuple(notes[key] for key in sorted(notes))


def _finding_note(finding: AcceptedFinding) -> ReviewNote:
    def citation_lines(citations: tuple[EvidenceCitation, ...]) -> list[str]:
        lines = []
        for citation in citations:
            lines.append(
                "- ID " + _quoted(citation.evidence_id, limit=160)
                + "; category " + _quoted(citation.category, limit=100)
                + "; tool " + _quoted(citation.tool, limit=100)
                + "; source " + _quoted(citation.source)
                + "; content hash " + _quoted(citation.content_hash, limit=80)
            )
        return lines
    supporting_lines = citation_lines(finding.supporting_citations)
    contradicting_lines = citation_lines(finding.contradicting_citations)
    consequence_lines = [
        "- " + _quoted(item)
        for item in (
            finding.user_visible_consequences
            or (finding.user_visible_consequence,)
        )
    ]
    validation_lines = [
        "- " + _quoted(item)
        for item in (finding.manual_validations or (finding.manual_validation,))
    ]
    markdown = (
        f"### {finding.severity.title()} finding\n\n"
        "**Claim:** " + _quoted(finding.claim)
        + "\n\n**User-visible consequence:**\n" + "\n".join(consequence_lines)
        + "\n\n**Causal chain:** " + _quoted(finding.causal_chain)
        + "\n\n**Supporting evidence provenance / citations:**\n"
        + "\n".join(supporting_lines)
        + (
            "\n\n**Contradicting evidence provenance / citations:**\n"
            + "\n".join(contradicting_lines)
            if contradicting_lines else ""
        )
        + "\n\n**Suggested validation:**\n" + "\n".join(validation_lines)
    )
    return ReviewNote(
        kind=ReviewNoteKind.FINDING,
        fingerprint=finding.root_cause_fingerprint,
        markdown=markdown,
        related_obligation_ids=finding.related_obligation_ids,
        evidence_ids=tuple(citation.evidence_id for citation in finding.citations),
        file=finding.affected_file,
        line=finding.line,
        severity=finding.severity,
    )


def build_review_notes(
    review: AdjudicatedReview,
    evidence: EvidenceStore | EvidenceSnapshot,
    publishing_mode: str = "review_comment",
    *,
    obligations: Mapping[str, CoverageObligation],
    changed_files: Iterable[str],
    verification_requests: Iterable[object] = (),
    source_access_requests: Iterable[object] = (),
) -> tuple[ReviewNote, ...]:
    """Build typed notes only after defensive controller-state revalidation."""
    if publishing_mode == "comment":
        return ()
    if publishing_mode not in {"review_comment", "review_verdict"}:
        raise ValueError("publishing_mode must be comment, review_comment, or review_verdict")
    if not isinstance(review, AdjudicatedReview):
        raise TypeError("review must be an AdjudicatedReview")
    obligation_map, changed = _controller_state(obligations, changed_files)
    snapshot = _snapshot(evidence)
    records = {record.id: record for record in snapshot.records}
    authoritative, defensive_verification = _revalidated_findings(
        review.accepted, evidence=snapshot, obligations=obligation_map, changed_files=changed
    )
    notes: list[ReviewNote] = [_finding_note(item) for item in authoritative]
    candidate_requests: list[CandidateVerificationRequest] = []
    for item in review.verification_requests:
        if isinstance(item, CandidateVerificationRequest):
            candidate_requests.append(item)
        elif isinstance(item, CandidateFinding):
            candidate_requests.append(CandidateVerificationRequest(item, "Human verification is required."))
    candidate_requests.extend(defensive_verification)
    for request in sorted(candidate_requests, key=lambda item: item.candidate.candidate_id):
        note = _verification_note(
            request, records=records, obligations=obligation_map, changed_files=changed
        )
        if note is not None:
            notes.append(note)
    for value in verification_requests:
        note = _mapping_verification(
            value, obligations=obligation_map, changed_files=changed, records=records
        )
        if note is not None:
            notes.append(note)
    notes.extend(build_source_access_request_notes(
        source_access_requests,
        obligations=obligation_map,
    ))
    unique = {(note.kind.value, note.fingerprint): note for note in notes}
    return tuple(unique[key] for key in sorted(unique))
