"""Bounded source packets and validated cross-component boundary outcomes."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from .evidence import EvidenceRecord, EvidenceSnapshot
from .obligation_assessment import (
    ObligationAssessment, ObligationDisposition,
    boundary_participant_source,
    boundary_source_diagnostic as _source_diagnostic,
)
from .policy import BoundaryPolicy


_COMPLETE_DISPOSITIONS = frozenset({
    ObligationDisposition.COVERED,
    ObligationDisposition.NOT_APPLICABLE,
})
_OUTCOMES = frozenset({
    "supported", "insufficient_evidence", "potential_contradiction",
})
_RESULT_KEYS = frozenset({
    "question", "outcome", "reason", "evidence_ids", "missing_fact",
    "suggested_investigation",
})


def _participant_source(
    record: EvidenceRecord, participant: str,
    assessment: ObligationAssessment, boundary: BoundaryPolicy,
) -> bool:
    return boundary_participant_source(
        record, boundary.endpoint_paths.get(participant, ()), boundary.contract_paths,
        not_applicable=assessment.disposition is ObligationDisposition.NOT_APPLICABLE,
    )


@dataclass(frozen=True)
class BoundaryEvaluation:
    boundary_id: str
    input_fingerprint: str
    outcome: str
    reason: str
    evidence_ids: tuple[str, ...]
    missing_fact: str = ""
    suggested_investigation: str = ""


def build_boundary_context(
    boundary: BoundaryPolicy,
    assessments: Iterable[ObligationAssessment],
    evidence: EvidenceSnapshot,
    *,
    max_bytes: int,
    obligations: Mapping[str, str] | None = None,
    expected_head_sha: str | None = None,
) -> dict:
    if not isinstance(boundary, BoundaryPolicy):
        raise TypeError("boundary must be a BoundaryPolicy")
    if not isinstance(evidence, EvidenceSnapshot):
        raise TypeError("evidence must be an EvidenceSnapshot")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    owner_by_obligation = dict(obligations or {})
    participants = tuple(boundary.participants)
    relevant: list[tuple[str, ObligationAssessment]] = []
    for assessment in assessments:
        if not isinstance(assessment, ObligationAssessment):
            raise TypeError("assessments must contain ObligationAssessment values")
        participant = owner_by_obligation.get(assessment.obligation_id)
        if participant is None:
            participant = next(
                (
                    candidate for candidate in participants
                    if candidate in {assessment.obligation_id, assessment.target}
                ),
                None,
            )
        if participant in participants:
            relevant.append((str(participant), assessment))
    relevant.sort(key=lambda item: (item[0], item[1].obligation_id, item[1].target))

    records_by_id = {record.id: record for record in evidence.records}
    cited_ids = {
        evidence_id
        for _participant, assessment in relevant
        for evidence_id in assessment.evidence_ids
    }
    selected_ids = set(cited_ids)
    contract_records = tuple(
        record for record in evidence.records
        if record.source_path and any(
            fnmatch.fnmatchcase(record.source_path, pattern)
            for pattern in boundary.contract_paths
        )
    )
    if contract_records and not any(
        record.id in cited_ids for record in contract_records
    ):
        usable_fallbacks = sorted(
            (
                record for record in contract_records
                if _source_diagnostic(record, expected_head_sha) is None
            ),
            key=lambda item: item.id,
        )
        fallback = usable_fallbacks[0] if usable_fallbacks else min(
            contract_records, key=lambda item: item.id,
        )
        selected_ids.add(fallback.id)
    for record in evidence.records:
        if set(record.contradicts).intersection(selected_ids):
            selected_ids.add(record.id)
    selected = tuple(
        sorted(
            (records_by_id[item] for item in selected_ids if item in records_by_id),
            key=lambda item: item.id,
        )
    )
    source_records = {
        record.id: record for record in selected
        if _source_diagnostic(record, expected_head_sha) is None
    }
    source_revisions = {
        record.provenance.head_sha for record in source_records.values()
        if record.provenance.head_sha
    }
    mixed_revisions = len(source_revisions) > 1

    assessment_packets = [
        {
            "participant_id": participant,
            "target": assessment.target,
            "obligation_id": assessment.obligation_id,
            "disposition": assessment.disposition.value,
            "reason": assessment.reason,
            "evidence_ids": list(assessment.evidence_ids),
            "assessed_paths": list(assessment.assessed_paths),
            "omitted_paths": list(assessment.omitted_paths),
            "assessment_version": assessment.assessment_version,
            "next_actions": list(assessment.next_actions),
        }
        for participant, assessment in relevant
    ]
    participant_evidence = {
        participant: sorted({
            evidence_id
            for owner, assessment in relevant
            if owner == participant
            for evidence_id in assessment.evidence_ids
            if evidence_id in source_records
            and _participant_source(source_records[evidence_id], participant, assessment, boundary)
        })
        for participant in participants
    }
    contradicted = {
        contradicted_id
        for record in source_records.values()
        for contradicted_id in record.contradicts
    }
    missing = []
    diagnostics = []
    followups = []
    if mixed_revisions:
        diagnostics.append("source evidence contains mixed head_sha revisions")
    for participant in participants:
        participant_assessments = [
            assessment for owner, assessment in relevant if owner == participant
        ]
        usable = [
            assessment for assessment in participant_assessments
            if assessment.disposition in _COMPLETE_DISPOSITIONS
            and participant_evidence[participant]
            and not (
                assessment.disposition == ObligationDisposition.NOT_APPLICABLE
                and set(assessment.evidence_ids).intersection(contradicted)
            )
        ]
        if not usable:
            missing.append(participant)
            if not participant_evidence[participant]:
                diagnostics.append(f"missing usable source evidence for participant {participant}")
                followups.append(f"Inspect and cite usable source evidence for participant {participant}.")
            else:
                diagnostics.append(f"participant {participant} assessment remains incomplete or contradicted")
                actions = list(dict.fromkeys(
                    action for assessment in participant_assessments
                    for action in assessment.next_actions
                ))
                followups.append(
                    f"Complete participant {participant}'s assessment using its retained source evidence. "
                    + (" ".join(actions) if actions else
                       "Record the remaining assessment gap; do not reread sources merely because the assessment is incomplete.")
                )

    contract_evidence_ids = sorted(
        record.id for record in selected
        if record.id in source_records
        and record.source_path
        and any(
            fnmatch.fnmatchcase(record.source_path, pattern)
            for pattern in boundary.contract_paths
        )
    )
    if boundary.contract_paths and not contract_evidence_ids:
        diagnostics.append("missing usable source evidence for boundary contract")
        followups.append("Inspect and cite the boundary contract source.")

    invalid_evidence = sorted(
        record.id for record in selected if record.id not in source_records
    )
    for evidence_id in invalid_evidence:
        diagnostics.append(
            f"source evidence {evidence_id} is unusable: "
            f"{_source_diagnostic(records_by_id[evidence_id], expected_head_sha)}"
        )

    evidence_packets = [_record_packet(record) for record in selected]
    semantic = {
        "boundary": {
            "id": boundary.id,
            "question": boundary.objective,
            "contract_paths": list(boundary.contract_paths),
            "participants": list(participants),
            "contract_change_owner": boundary.contract_change_owner,
            "endpoint_paths": {
                key: list(value) for key, value in boundary.endpoint_paths.items()
            },
        },
        "assessments": assessment_packets,
        "evidence": [_record_semantics(record) for record in selected],
    }
    fingerprint = "boundary:" + hashlib.sha256(
        _canonical_json(semantic).encode("utf-8")
    ).hexdigest()
    context = {
        "boundary_id": boundary.id,
        "boundary": semantic["boundary"],
        "question": boundary.objective,
        "required_participants": list(participants),
        "assessments": assessment_packets,
        "evidence": evidence_packets,
        "participant_evidence_ids": participant_evidence,
        "contract_evidence_ids": contract_evidence_ids,
        "source_evidence_ids": sorted(source_records),
        "missing_participants": missing,
        "incomplete": bool(missing or mixed_revisions or (
            boundary.contract_paths and not contract_evidence_ids
        )),
        "diagnostics": diagnostics,
        "suggested_investigation": " ".join(followups),
        "input_fingerprint": fingerprint,
        "controller_fingerprint": fingerprint,
        "source_limits": {
            "max_bytes": max_bytes,
            "snapshot_max_content_bytes": evidence.max_content_bytes,
        },
    }
    packet_bytes = len(_canonical_json(context).encode("utf-8"))
    context["source_limits"]["full_packet_bytes"] = packet_bytes
    if packet_bytes > max_bytes:
        context["evidence"] = []
        context["source_evidence_ids"] = []
        context["contract_evidence_ids"] = []
        context["participant_evidence_ids"] = {
            participant: [] for participant in participants
        }
        context["missing_participants"] = list(participants)
        context["incomplete"] = True
        context["diagnostics"].append(
            f"required boundary evidence exceeds max_bytes ({packet_bytes} > {max_bytes})"
        )
        context["suggested_investigation"] = (
            "Reduce the cited boundary source packet to focused evidence for each participant "
            "and contract; the existing packet exceeds the context budget."
        )
    return context


def validate_boundary_evaluation(
    raw: object, context: Mapping,
) -> BoundaryEvaluation:
    if not isinstance(raw, Mapping):
        raise ValueError("boundary evaluation must be an object")
    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping")
    unknown = set(raw) - _RESULT_KEYS
    if unknown:
        raise ValueError(
            "boundary evaluation contains unknown keys: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    outcome = str(raw.get("outcome") or "").strip()
    if outcome not in _OUTCOMES:
        raise ValueError(
            "boundary evaluation outcome must be supported, "
            "insufficient_evidence, or potential_contradiction"
        )
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        raise ValueError("boundary evaluation reason must be non-empty")
    raw_evidence_ids = raw.get("evidence_ids", [])
    if not isinstance(raw_evidence_ids, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in raw_evidence_ids
    ):
        raise ValueError("boundary evaluation evidence_ids must be an array of strings")
    evidence_ids = tuple(item.strip() for item in raw_evidence_ids)
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("boundary evaluation evidence_ids must be unique")
    supplied_ids = {
        str(item.get("id"))
        for item in context.get("evidence", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    unknown_ids = set(evidence_ids) - supplied_ids
    if unknown_ids:
        raise ValueError(
            "boundary evaluation cites evidence outside the supplied packet: "
            + ", ".join(sorted(unknown_ids))
        )
    source_ids = set(context.get("source_evidence_ids", ()))
    if set(evidence_ids) - source_ids:
        raise ValueError("boundary evaluation citations must name source evidence")
    if outcome == "supported":
        if context.get("incomplete"):
            diagnostic = "; ".join(str(item) for item in context.get("diagnostics", ()))
            raise ValueError(
                "supported boundary evaluation requires a complete source evidence packet"
                + (f": {diagnostic}" if diagnostic else "")
            )
        if str(raw.get("question") or "").strip() != str(context.get("question") or ""):
            raise ValueError("supported boundary evaluation must name the specific question")
        if not evidence_ids:
            raise ValueError("supported boundary evaluation requires source evidence citations")
        cited = set(evidence_ids)
        participant_evidence = context.get("participant_evidence_ids", {})
        for participant in context.get("required_participants", ()):
            if not cited.intersection(participant_evidence.get(participant, ())):
                raise ValueError(
                    "supported boundary evaluation requires source evidence for "
                    f"participant {participant}"
                )
        contract_ids = set(context.get("contract_evidence_ids", ()))
        if contract_ids and not cited.intersection(contract_ids):
            raise ValueError(
                "supported boundary evaluation requires boundary contract source evidence"
            )
    return BoundaryEvaluation(
        boundary_id=str(context.get("boundary_id") or ""),
        input_fingerprint=str(context.get("input_fingerprint") or ""),
        outcome=outcome,
        reason=reason,
        evidence_ids=evidence_ids,
        missing_fact=str(raw.get("missing_fact") or "").strip(),
        suggested_investigation=str(
            raw.get("suggested_investigation") or ""
        ).strip(),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _arguments(record: EvidenceRecord) -> object:
    try:
        return json.loads(record.arguments)
    except json.JSONDecodeError:
        return record.arguments


def _provenance(record: EvidenceRecord) -> dict[str, object]:
    value = record.provenance
    return {
        "head_sha": value.head_sha,
        "policy_hash": value.policy_hash,
        "policy_rule_id": value.policy_rule_id,
        "source_classification": value.source_classification,
        "original_url": value.original_url,
        "final_url": value.final_url,
        "retrieved_at": value.retrieved_at,
        "max_age_hours": value.max_age_hours,
    }


def _record_packet(record: EvidenceRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "category": record.category,
        "tool": record.tool,
        "arguments": _arguments(record),
        "source_identity": record.source_identity,
        "source_path": record.source_path,
        "provenance": _provenance(record),
        "status": record.status,
        "content": record.content,
        "content_hash": record.content_hash,
        "mime_type": record.mime_type,
        "truncated": record.truncated,
        "redacted": record.redacted,
        "contradicts": list(record.contradicts),
    }


def _record_semantics(record: EvidenceRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "tool": record.tool,
        "arguments": _arguments(record),
        "source_identity": record.source_identity,
        "source_path": record.source_path,
        "status": record.status,
        "content_hash": record.content_hash,
        "truncated": record.truncated,
        "redacted": record.redacted,
        "head_sha": record.provenance.head_sha,
        "contradicts": list(record.contradicts),
    }
