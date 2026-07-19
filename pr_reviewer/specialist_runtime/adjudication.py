"""Evidence-authoritative candidate adjudication and runtime verdict policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from pr_reviewer.enforcement import RuntimeVerdictPolicyResult, derive_runtime_verdict

from .evidence import EvidenceSnapshot, EvidenceStore
from .types import CandidateFinding, ReviewHandoff, ReviewNote, ReviewNoteKind


_CRITIC_ACTIONS = frozenset({
    "keep", "reject", "merge", "request_verification", "downgrade_unknown",
})
_SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}


@dataclass(frozen=True)
class CandidateDisposition:
    candidate_id: str
    action: str
    reason: str
    target_id: str | None = None


@dataclass(frozen=True)
class AdjudicatedReview:
    accepted: tuple[CandidateFinding, ...] = ()
    rejected: tuple[CandidateDisposition, ...] = ()
    verification_requests: tuple[CandidateFinding, ...] = ()
    unknowns: tuple[CandidateFinding, ...] = ()
    dispositions: tuple[CandidateDisposition, ...] = ()


def _normalized_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _normalized_location(value: object) -> tuple[str, int | None]:
    raw = str(value or "").strip().replace("\\", "/")
    match = re.fullmatch(r"(.+?)(?::(\d+))?", raw)
    path_text = match.group(1) if match else raw
    line = int(match.group(2)) if match and match.group(2) else None
    while path_text.startswith("./"):
        path_text = path_text[2:]
    path = str(PurePosixPath(path_text)) if path_text else ""
    if (
        path in {"", "."}
        or path.startswith("/")
        or re.match(r"^[a-zA-Z]:/", path)
        or ".." in PurePosixPath(path).parts
    ):
        return "", line
    return path, line


def _normalized_candidate(candidate: CandidateFinding) -> CandidateFinding:
    path, line = _normalized_location(candidate.affected_location)
    location = path + (f":{line}" if path and line is not None else "")
    identity = "\x1f".join((
        path.casefold(),
        _normalized_text(candidate.category),
        _normalized_text(candidate.claim),
    ))
    fingerprint = "finding:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return replace(
        candidate,
        root_cause_fingerprint=fingerprint,
        affected_location=location,
        category=_normalized_text(candidate.category).replace(" ", "-"),
        severity=(
            severity if (severity := str(candidate.severity).strip().lower()) in _SEVERITY_RANK
            else "info"
        ),
        supporting_evidence_ids=tuple(sorted(set(candidate.supporting_evidence_ids))),
        contradicting_evidence_ids=tuple(sorted(set(candidate.contradicting_evidence_ids))),
        related_obligation_ids=tuple(sorted(set(candidate.related_obligation_ids))),
    )


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
    if not isinstance(critic_result, Iterable) or isinstance(critic_result, (str, bytes)):
        return ()
    return tuple(item for item in critic_result if isinstance(item, Mapping))


def _evidence_snapshot(evidence: EvidenceStore | EvidenceSnapshot) -> EvidenceSnapshot:
    if isinstance(evidence, EvidenceStore):
        return evidence.snapshot()
    if isinstance(evidence, EvidenceSnapshot):
        return evidence
    raise TypeError("evidence must be an EvidenceStore or EvidenceSnapshot")


def _authority_reason(
    candidate: CandidateFinding,
    records: Mapping[str, object],
    known_obligation_ids: set[str] | None,
) -> str | None:
    if not candidate.supporting_evidence_ids:
        return "missing-retained-evidence"
    referenced = candidate.supporting_evidence_ids + candidate.contradicting_evidence_ids
    if any(evidence_id not in records for evidence_id in referenced):
        return "missing-retained-evidence"
    if any(
        not records[evidence_id].is_usable_for_coverage
        for evidence_id in referenced
    ):
        return "unusable-retained-evidence"
    if not candidate.related_obligation_ids:
        return "missing-related-obligation"
    if known_obligation_ids is not None and any(
        obligation_id not in known_obligation_ids
        for obligation_id in candidate.related_obligation_ids
    ):
        return "unknown-related-obligation"
    return None


def adjudicate_candidates(
    candidates: Iterable[CandidateFinding],
    critic_result: object,
    evidence: EvidenceStore | EvidenceSnapshot,
    *,
    obligations: Iterable[object] | None = None,
    changed_files: Iterable[str] | None = None,
) -> AdjudicatedReview:
    """Apply critic dispositions without granting the critic content authority."""
    snapshot = _evidence_snapshot(evidence)
    records = {record.id: record for record in snapshot.records}
    known_obligation_ids = None
    if obligations is not None:
        known_obligation_ids = {
            str(getattr(item, "obligation_id", getattr(item, "id", ""))).strip()
            for item in obligations
        }
        known_obligation_ids.discard("")
    normalized_changed = None
    if changed_files is not None:
        normalized_changed = {
            path for item in changed_files if (path := _normalized_location(item)[0])
        }

    candidate_by_id: dict[str, CandidateFinding] = {}
    duplicate_ids: set[str] = set()
    invalid_location_ids: set[str] = set()
    for item in candidates:
        if not isinstance(item, CandidateFinding):
            raise TypeError("candidates must contain CandidateFinding values")
        if item.candidate_id in candidate_by_id:
            duplicate_ids.add(item.candidate_id)
            continue
        if str(item.affected_location).strip() and not _normalized_location(item.affected_location)[0]:
            invalid_location_ids.add(item.candidate_id)
        candidate_by_id[item.candidate_id] = _normalized_candidate(item)

    decisions: dict[str, Mapping[str, Any]] = {}
    for row in _decision_rows(critic_result):
        candidate_id = str(row.get("candidate_id", row.get("id", ""))).strip()
        if candidate_id in candidate_by_id and candidate_id not in decisions:
            decisions[candidate_id] = row

    accepted: list[CandidateFinding] = []
    rejected: list[CandidateDisposition] = []
    verification: list[CandidateFinding] = []
    unknowns: list[CandidateFinding] = []
    dispositions: list[CandidateDisposition] = []
    merge_sources: dict[str, list[CandidateFinding]] = {}

    for candidate_id in sorted(candidate_by_id):
        candidate = candidate_by_id[candidate_id]
        decision = decisions.get(candidate_id)
        action = str(decision.get("action", "")).strip().lower() if decision else ""
        target_id = str(
            decision.get("target_id", decision.get("merge_into", ""))
        ).strip() if decision else ""
        if candidate_id in duplicate_ids:
            disposition = CandidateDisposition(candidate_id, "reject", "duplicate-candidate-id")
            rejected.append(disposition)
            dispositions.append(disposition)
            continue
        if action not in _CRITIC_ACTIONS:
            reason = "invalid-critic-action" if action else "missing-critic-action"
            disposition = CandidateDisposition(candidate_id, "reject", reason)
            rejected.append(disposition)
            dispositions.append(disposition)
            continue
        if action == "reject":
            disposition = CandidateDisposition(
                candidate_id, action, str(decision.get("reason") or "critic-rejected")
            )
            rejected.append(disposition)
            dispositions.append(disposition)
            continue
        if action == "request_verification":
            disposition = CandidateDisposition(candidate_id, action, "critic-requested-verification")
            verification.append(candidate)
            dispositions.append(disposition)
            continue
        if action == "downgrade_unknown":
            disposition = CandidateDisposition(candidate_id, action, "critic-downgraded-to-unknown")
            unknowns.append(candidate)
            dispositions.append(disposition)
            continue
        if action == "merge":
            target = candidate_by_id.get(target_id)
            if target is None:
                disposition = CandidateDisposition(
                    candidate_id, "reject", "invalid-merge-target", target_id or None
                )
                rejected.append(disposition)
                dispositions.append(disposition)
                continue
            if target.root_cause_fingerprint != candidate.root_cause_fingerprint:
                disposition = CandidateDisposition(
                    candidate_id, "reject", "merge-root-cause-mismatch", target_id
                )
                rejected.append(disposition)
                dispositions.append(disposition)
                continue
            path, _ = _normalized_location(candidate.affected_location)
            reason = _authority_reason(candidate, records, known_obligation_ids)
            if (
                normalized_changed is not None
                and (
                    candidate_id in invalid_location_ids
                    or (path and path not in normalized_changed)
                )
            ):
                reason = "not-a-changed-causal-file"
            if reason:
                disposition = CandidateDisposition(candidate_id, "reject", reason, target_id)
                rejected.append(disposition)
                dispositions.append(disposition)
                continue
            merge_sources.setdefault(target_id, []).append(candidate)
            continue

        path, _ = _normalized_location(candidate.affected_location)
        reason = _authority_reason(candidate, records, known_obligation_ids)
        if (
            normalized_changed is not None
            and (
                candidate_id in invalid_location_ids
                or (path and path not in normalized_changed)
            )
        ):
            reason = "not-a-changed-causal-file"
        if reason:
            disposition = CandidateDisposition(candidate_id, "reject", reason)
            rejected.append(disposition)
            dispositions.append(disposition)
            continue
        accepted.append(candidate)
        dispositions.append(CandidateDisposition(candidate_id, action, "accepted"))

    accepted_by_id = {candidate.candidate_id: candidate for candidate in accepted}
    for target_id in sorted(merge_sources):
        target = accepted_by_id.get(target_id)
        sources = sorted(merge_sources[target_id], key=lambda item: item.candidate_id)
        if target is None:
            for source in sources:
                disposition = CandidateDisposition(
                    source.candidate_id, "reject", "merge-target-not-accepted", target_id
                )
                rejected.append(disposition)
                dispositions.append(disposition)
            continue
        group = (target, *sources)
        merged_target = replace(
            target,
            severity=max(group, key=lambda item: _SEVERITY_RANK[item.severity]).severity,
            supporting_evidence_ids=tuple(sorted({
                evidence_id for item in group for evidence_id in item.supporting_evidence_ids
            })),
            contradicting_evidence_ids=tuple(sorted({
                evidence_id for item in group for evidence_id in item.contradicting_evidence_ids
            })),
            related_obligation_ids=tuple(sorted({
                obligation_id for item in group for obligation_id in item.related_obligation_ids
            })),
        )
        accepted_by_id[target_id] = merged_target
        for source in sources:
            dispositions.append(CandidateDisposition(source.candidate_id, "merge", "merged", target_id))

    accepted = [accepted_by_id[item.candidate_id] for item in accepted]

    # Exact semantic duplicates retained by the critic collapse deterministically.
    by_fingerprint: dict[str, list[CandidateFinding]] = {}
    for candidate in accepted:
        by_fingerprint.setdefault(candidate.root_cause_fingerprint, []).append(candidate)
    deduplicated: list[CandidateFinding] = []
    for fingerprint in sorted(by_fingerprint):
        group = sorted(by_fingerprint[fingerprint], key=lambda item: item.candidate_id)
        representative = group[0]
        if len(group) > 1:
            representative = replace(
                representative,
                severity=max(group, key=lambda item: _SEVERITY_RANK[item.severity]).severity,
                supporting_evidence_ids=tuple(sorted({
                    evidence_id for item in group for evidence_id in item.supporting_evidence_ids
                })),
                contradicting_evidence_ids=tuple(sorted({
                    evidence_id for item in group for evidence_id in item.contradicting_evidence_ids
                })),
                related_obligation_ids=tuple(sorted({
                    obligation_id for item in group for obligation_id in item.related_obligation_ids
                })),
            )
        deduplicated.append(representative)

    return AdjudicatedReview(
        accepted=tuple(deduplicated),
        rejected=tuple(rejected),
        verification_requests=tuple(sorted(verification, key=lambda item: item.candidate_id)),
        unknowns=tuple(sorted(unknowns, key=lambda item: item.candidate_id)),
        dispositions=tuple(dispositions),
    )


def apply_runtime_verdict_policy(
    *,
    model_verdict: str,
    accepted: Iterable[object],
    unresolved: Iterable[object],
    allow_approve: bool,
    policy: Mapping[str, Any] | None = None,
) -> RuntimeVerdictPolicyResult:
    """Apply the shared pure runtime policy to adjudicated, supported inputs."""
    return derive_runtime_verdict(
        model_verdict=model_verdict,
        accepted=accepted,
        unresolved=unresolved,
        allow_approve=allow_approve,
        policy=policy,
    )


def _state_value(state: object, name: str, default: Any = None) -> Any:
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


def _clean_strings(value: object, *, limit: int | None = None) -> tuple[str, ...]:
    if value is None or isinstance(value, (str, bytes)):
        items = (value,) if isinstance(value, str) else ()
    elif isinstance(value, Iterable):
        items = tuple(value)
    else:
        items = ()
    cleaned = tuple(dict.fromkeys(
        str(item).strip() for item in items if str(item).strip()
    ))
    return cleaned if limit is None else cleaned[:limit]


def _single_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _aggregate_theme(findings: Iterable[object]) -> str | None:
    categories = tuple(
        _normalized_text(_state_value(item, "category", "")).replace(" ", "-")
        for item in findings
    )
    material = tuple(category for category in categories if category)
    generic_categories = {"bug", "correctness", "finding", "general", "info", "other"}
    if (
        len(material) < 2
        or len(set(material)) != 1
        or material[0] in generic_categories
    ):
        return None
    return material[0]


def build_review_handoff(state: object) -> ReviewHandoff:
    """Build a sparse orientation aid without finding or evidence detail."""
    recommendation = str(_state_value(state, "recommendation", "")).strip()
    status = str(_state_value(state, "status", "")).strip()
    change_map = _clean_strings(_state_value(state, "change_map", ()))
    specialist_focuses = _clean_strings(
        _state_value(state, "specialist_focuses", _state_value(state, "reviewed_focuses", ()))
    )
    recipes = _clean_strings(_state_value(state, "recipes", ()))
    boundaries = _clean_strings(_state_value(state, "coverage_boundaries", ()))
    reviewed_focuses = tuple(dict.fromkeys((*specialist_focuses, *recipes, *boundaries)))
    thread_status = _single_line(_state_value(state, "thread_status", "")) or None
    finding_theme = _aggregate_theme(
        _state_value(state, "findings", _state_value(state, "accepted", ()))
    )
    review_emphasis = _clean_strings(_state_value(state, "review_emphasis", ()), limit=3)
    coverage_warning = _single_line(_state_value(state, "coverage_warning", "")) or None
    source_requests = _state_value(
        state, "source_access_requests", _state_value(state, "source_requests", ())
    )
    explicit_count = _state_value(state, "access_request_count", None)
    if explicit_count is None:
        access_request_count = len(tuple(source_requests or ()))
    else:
        access_request_count = max(0, int(explicit_count))
    access_request_url = str(_state_value(state, "access_request_url", "")).strip() or None

    lines = ["## AI Review Handoff"]
    if recommendation:
        lines.extend(("", f"**Recommendation:** {recommendation}"))
    if status:
        lines.extend(("", f"**Status:** {status}"))
    if change_map:
        lines.extend(("", "### Change map", "", *[f"- {item}" for item in change_map]))
    if reviewed_focuses:
        lines.extend(("", "### AI focus and coverage", ""))
        if specialist_focuses:
            lines.append("- Specialist focus: " + "; ".join(specialist_focuses))
        if recipes:
            lines.append("- Repository recipes: " + "; ".join(recipes))
        if boundaries:
            lines.append("- Coverage boundaries: " + "; ".join(boundaries))
    if thread_status:
        lines.extend(("", f"**Thread status:** {thread_status}"))
    if finding_theme:
        lines.extend(("", f"**Aggregate finding theme:** {finding_theme}"))
    if review_emphasis:
        lines.extend(("", "### Human review focus", "", *[
            f"- {item}" for item in review_emphasis
        ]))
    lines.extend((
        "",
        "These focus suggestions do not reduce responsibility to review the complete change.",
    ))
    if coverage_warning:
        lines.extend(("", f"**Material coverage warning:** {coverage_warning}"))
    if access_request_count:
        access_text = f"{access_request_count} open"
        if access_request_url:
            access_text = f"[{access_text}]({access_request_url})"
        lines.extend(("", f"**Source access requests:** {access_text}"))

    return ReviewHandoff(
        markdown="\n".join(lines).strip() + "\n",
        recommendation=recommendation,
        change_map=change_map,
        reviewed_focuses=reviewed_focuses,
        thread_status=thread_status,
        finding_theme=finding_theme,
        review_emphasis=review_emphasis,
        coverage_warning=coverage_warning,
        access_request_count=access_request_count,
        access_request_url=access_request_url,
    )


def _stable_request_fingerprint(
    kind: ReviewNoteKind,
    text: str,
    obligation_ids: tuple[str, ...],
    file: str | None,
) -> str:
    identity = "\x1f".join((kind.value, _normalized_text(text), "|".join(obligation_ids), file or ""))
    return f"{kind.value}:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _request_note(
    request: object,
    *,
    kind: ReviewNoteKind,
    retained_records: Mapping[str, object],
) -> ReviewNote | None:
    text = str(
        _state_value(request, "question", _state_value(request, "purpose", _state_value(request, "reason", "")))
    ).strip()
    obligation_ids = tuple(sorted(_clean_strings(
        _state_value(request, "related_obligation_ids", _state_value(request, "obligation_ids", ()))
    )))
    if not text or not obligation_ids:
        return None
    requested_evidence = _clean_strings(_state_value(request, "evidence_ids", ()))
    evidence_ids = tuple(sorted(
        item for item in requested_evidence
        if item in retained_records and retained_records[item].is_usable_for_coverage
    ))
    raw_file = _state_value(request, "file", "")
    file, parsed_line = _normalized_location(raw_file)
    line_value = _state_value(request, "line", parsed_line)
    line = int(line_value) if line_value not in (None, "") else None
    label = "Verification request" if kind is ReviewNoteKind.VERIFICATION_REQUEST else "Source access request"
    markdown = f"### {label}\n\n{text}"
    if evidence_ids:
        markdown += "\n\nEvidence already checked: " + ", ".join(f"`{item}`" for item in evidence_ids)
    return ReviewNote(
        kind=kind,
        fingerprint=_stable_request_fingerprint(kind, text, obligation_ids, file or None),
        markdown=markdown,
        related_obligation_ids=obligation_ids,
        evidence_ids=evidence_ids,
        file=file or None,
        line=line,
    )


def build_review_notes(
    review: AdjudicatedReview,
    evidence: EvidenceStore | EvidenceSnapshot,
    publishing_mode: str = "review_comment",
    *,
    verification_requests: Iterable[object] = (),
    source_access_requests: Iterable[object] = (),
) -> tuple[ReviewNote, ...]:
    """Build typed detailed notes exclusively from adjudicated public inputs."""
    if publishing_mode == "comment":
        return ()
    if publishing_mode not in {"review_comment", "review_verdict"}:
        raise ValueError("publishing_mode must be comment, review_comment, or review_verdict")
    if not isinstance(review, AdjudicatedReview):
        raise TypeError("review must be an AdjudicatedReview")
    snapshot = _evidence_snapshot(evidence)
    records = {record.id: record for record in snapshot.records}
    notes: list[ReviewNote] = []

    for candidate in sorted(review.accepted, key=lambda item: item.root_cause_fingerprint):
        support = tuple(
            records[evidence_id] for evidence_id in candidate.supporting_evidence_ids
            if evidence_id in records and records[evidence_id].is_usable_for_coverage
        )
        contradictions = tuple(
            records[evidence_id] for evidence_id in candidate.contradicting_evidence_ids
            if evidence_id in records and records[evidence_id].is_usable_for_coverage
        )
        if (
            not support
            or len(support) != len(candidate.supporting_evidence_ids)
            or len(contradictions) != len(candidate.contradicting_evidence_ids)
            or not candidate.related_obligation_ids
        ):
            continue
        file, line = _normalized_location(candidate.affected_location)
        evidence_ids = tuple(record.id for record in (*support, *contradictions))
        if not file:
            text = (
                "Please verify whether this potential issue is present before treating it as a defect: "
                + candidate.claim
            )
            notes.append(ReviewNote(
                kind=ReviewNoteKind.VERIFICATION_REQUEST,
                fingerprint=_stable_request_fingerprint(
                    ReviewNoteKind.VERIFICATION_REQUEST,
                    candidate.root_cause_fingerprint,
                    candidate.related_obligation_ids,
                    None,
                ),
                markdown=f"### Verification request\n\n{text}",
                related_obligation_ids=candidate.related_obligation_ids,
                evidence_ids=evidence_ids,
                severity=None,
            ))
            continue
        markdown = [
            f"### {candidate.severity.title()} finding",
            "",
            candidate.claim,
        ]
        if candidate.causal_chain:
            markdown.extend(("", f"**Causal chain:** {candidate.causal_chain}"))
        markdown.extend(("", "**Supporting evidence:**"))
        markdown.extend(f"- `{record.id}`: {record.content}" for record in support)
        if contradictions:
            markdown.extend(("", "**Contradicting evidence:**"))
            markdown.extend(f"- `{record.id}`: {record.content}" for record in contradictions)
        notes.append(ReviewNote(
            kind=ReviewNoteKind.FINDING,
            fingerprint=candidate.root_cause_fingerprint,
            markdown="\n".join(markdown),
            related_obligation_ids=candidate.related_obligation_ids,
            evidence_ids=evidence_ids,
            file=file,
            line=line,
            severity=candidate.severity,
        ))

    implicit_verification = tuple(
        {
            "question": "Please verify this unresolved candidate before treating it as a defect: " + candidate.claim,
            "related_obligation_ids": candidate.related_obligation_ids,
            "evidence_ids": candidate.supporting_evidence_ids + candidate.contradicting_evidence_ids,
            "file": candidate.affected_location,
        }
        for candidate in review.verification_requests
    )
    for request in (*implicit_verification, *tuple(verification_requests)):
        note = _request_note(
            request, kind=ReviewNoteKind.VERIFICATION_REQUEST, retained_records=records
        )
        if note is not None:
            notes.append(note)
    for request in source_access_requests:
        note = _request_note(
            request, kind=ReviewNoteKind.SOURCE_ACCESS_REQUEST, retained_records=records
        )
        if note is not None:
            notes.append(note)

    return tuple(sorted(notes, key=lambda item: (item.kind.value, item.fingerprint)))
