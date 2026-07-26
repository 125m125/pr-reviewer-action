#!/usr/bin/env python3
"""A/B evaluation harness for comparing PR review modes.

Compares review approaches on a shared PR corpus:
  - tools_off:     no tool harness, direct model call only
  - native_loop:   native tool-calling loop (the only tool mode as of 2.0)

For each PR the harness runs all enabled modes and collects:
  - findings quality  (vs known-good findings)
  - token usage       (input + output tokens per mode)
  - wall-clock time   (seconds from first to last model call)

Outputs a JSON report with per-mode metrics and a side-by-side comparison.
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from pr_reviewer.specialist_runtime.replay import (
    EXPECTED_FIELDS,
    budget_history,
    replay_fixture,
    replay_web_policy_fixture,
    validated_strings,
)
from pr_reviewer.specialist_runtime.types import ReviewNote


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class KnownFinding:
    """A single known-good finding for a PR."""
    category: str          # e.g. "security", "correctness", "style"
    severity: str          # "critical", "high", "medium", "low", "info"
    description: str
    file_path: str | None = None
    line_range: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "category": self.category,
            "severity": self.severity,
            "description": self.description,
        }
        if self.file_path:
            d["file_path"] = self.file_path
        if self.line_range:
            d["line_range"] = list(self.line_range)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> KnownFinding:
        lr = d.get("line_range")
        return cls(
            category=d["category"],
            severity=d["severity"],
            description=d["description"],
            file_path=d.get("file_path"),
            line_range=tuple(lr) if lr else None,
        )


@dataclass
class ReviewRun:
    """Results from a single review mode on a single PR."""
    mode: str              # "tools_off", "native_loop"
    pr_number: int
    repo_full_name: str
    tokens_input: int = 0
    tokens_output: int = 0
    wall_clock_sec: float = 0.0
    verdict: str | None = None          # "approve" or "request_changes"
    findings: list[dict[str, Any]] = field(default_factory=list)
    review_markdown: str = ""
    error: str | None = None
    model_used: str = ""
    # Structured trace from tool-harness.json: each is {tool, args, status}.
    # Populated for native_loop (and any harness mode that emits tool_calls);
    # the capability checker grades the agentic evidence chain against it.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "pr_number": self.pr_number,
            "repo_full_name": self.repo_full_name,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "wall_clock_sec": round(self.wall_clock_sec, 3),
            "verdict": self.verdict,
            "findings_count": len(self.findings),
            "findings": self.findings,
            "tool_calls": self.tool_calls,
            "tool_stop_reason": self.tool_stop_reason,
            "error": self.error,
            "model_used": self.model_used,
        }


@dataclass
class BenchmarkResult:
    """Aggregated results for one PR across all modes."""
    pr_number: int
    repo_full_name: str
    runs: list[ReviewRun] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "repo_full_name": self.repo_full_name,
            "runs": [r.to_dict() for r in self.runs],
        }


@dataclass
class BenchmarkCorpus:
    """The full benchmark corpus with known-good findings."""
    prs: list[dict[str, Any]] = field(default_factory=list)
    offline_specialist_replays: list[dict[str, Any]] = field(default_factory=list)
    source_path: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> BenchmarkCorpus:
        data = json.loads(path.read_text(encoding="utf-8"))
        replays = data.get("offline_specialist_replays", [])
        if not isinstance(replays, list) or any(
            not isinstance(item, dict) for item in replays
        ):
            raise ValueError("offline_specialist_replays must be an array of objects")
        return cls(
            prs=data.get("benchmark_corpus", []),
            offline_specialist_replays=replays,
            source_path=path.resolve(),
        )


# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------

def load_known_findings(pr_entry: dict[str, Any]) -> list[KnownFinding]:
    """Extract known-good findings from a corpus PR entry."""
    raw = pr_entry.get("known_findings", [])
    return [KnownFinding.from_dict(f) for f in raw]


def extract_findings_from_review(review_run: ReviewRun) -> list[dict[str, Any]]:
    """Parse findings out of a review's markdown body.

    Finds lines matching common patterns like:
      - `- [security/high] description`
      - `- [correctness/medium] ...`
      - severity-prefixed bullets
    Returns list of dicts with category, severity, description.
    """
    findings = []
    if not review_run.review_markdown:
        return findings

    # Pattern: [category/severity] or category/severity prefix
    pattern = re.compile(
        r"[-*]\s+\[?(\w+)/(\w+)\]?\s+(.+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(review_run.review_markdown):
        cat = match.group(1).lower()
        sev = match.group(2).lower()
        desc = match.group(3).strip()
        if cat and sev:
            findings.append({
                "category": cat,
                "severity": sev,
                "description": desc,
            })
    return findings


# ---------------------------------------------------------------------------
# Quality comparison
# ---------------------------------------------------------------------------

def compute_precision_recall(
    found_findings: list[dict[str, Any]],
    known_findings: list[KnownFinding],
) -> dict[str, float]:
    """Compute precision and recall against known-good findings.

    Simple matching: a finding is "correct" if its category and severity
    match any known finding AND the description has >50% word overlap.
    """
    # Always include total_found/total_known so callers don't need special casing.
    if not known_findings:
        return {
            "precision": 0.0, "recall": 0.0, "f1": 0.0,
            "matched_found": 0, "total_found": len(found_findings), "total_known": 0,
        }
    if not found_findings:
        return {
            "precision": 0.0, "recall": 0.0, "f1": 0.0,
            "matched_found": 0, "total_found": 0, "total_known": len(known_findings),
        }

    # Build a set of (category, severity) tuples from known findings
    known_keys = {(f.category.lower(), f.severity.lower()) for f in known_findings}

    # Word-overlap threshold for description matching
    def word_overlap(a: str, b: str) -> float:
        words_a = set(re.findall(r"\w+", a.lower()))
        words_b = set(re.findall(r"\w+", b.lower()))
        if not words_a or not words_b:
            return 0.0
        return len(words_a & words_b) / min(len(words_a), len(words_b))

    matched_found = 0
    matched_known = 0

    for found in found_findings:
        fk = (found["category"], found["severity"])
        if fk not in known_keys:
            continue
        # Check description overlap with any matching known finding
        for kf in known_findings:
            if (kf.category.lower(), kf.severity.lower()) == fk:
                if word_overlap(found["description"], kf.description) > 0.5:
                    matched_found += 1
                    matched_known += 1
                    break

    precision = matched_found / len(found_findings) if found_findings else 0.0
    recall = matched_known / len(known_findings) if known_findings else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0.0)

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "matched_found": matched_found,
        "total_found": len(found_findings),
        "total_known": len(known_findings),
    }


# ---------------------------------------------------------------------------
# Offline specialist-runtime acceptance evaluation
# ---------------------------------------------------------------------------

_TERMINAL_OBLIGATION_STATUSES = {
    "covered", "partially_covered", "unresolved", "not_applicable",
    "suppressed_by_policy",
}


def _note_anchor_types(notes: Sequence[ReviewNote]) -> dict[str, int]:
    counts = {"line": 0, "file": 0, "general": 0}
    for note in notes:
        if note.file and note.line is not None:
            counts["line"] += 1
        elif note.file:
            counts["file"] += 1
        else:
            counts["general"] += 1
    return counts


def _budget_usage_decreased(previous: object, current: object) -> bool:
    fields = (
        "model_turns", "tool_calls", "recoveries",
        "input_tokens", "output_tokens",
    )
    if isinstance(previous, Mapping) and isinstance(current, Mapping):
        return any(
            int(current.get(field, 0)) < int(previous.get(field, 0))
            for field in fields
        )
    return int(current) < int(previous)


def _retained_evidence(
    artifact: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    return {
        str(item["evidence_id"]): item
        for item in artifact.get("evidence", ())
        if isinstance(item, Mapping)
        and isinstance(item.get("evidence_id"), str)
        and item["evidence_id"]
    }


def _citation_is_authorized(
    citation: object,
    records: Mapping[str, Mapping[str, Any]],
) -> bool:
    if not isinstance(citation, Mapping):
        return False
    evidence_id = str(citation.get("evidence_id") or "")
    record = records.get(evidence_id)
    if record is None or str(record.get("status") or "").lower() != "ok":
        return False
    provenance = record.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    allowed_sources = {
        str(value)
        for value in (
            provenance.get("final_url"),
            provenance.get("original_url"),
            record.get("source_identity"),
            record.get("source_path"),
        )
        if value
    }
    if record.get("source_path"):
        allowed_sources.add("path:" + str(record["source_path"]))
    return (
        citation.get("category") == record.get("category")
        and citation.get("tool") == record.get("tool")
        and citation.get("content_hash") == record.get("content_hash")
        and str(citation.get("source") or "") in allowed_sources
    )


def _accepted_claim_is_authorized(
    finding: Mapping[str, Any],
    artifact: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
) -> bool:
    supporting_ids = tuple(
        str(item) for item in finding.get("supporting_evidence_ids", ())
        if str(item)
    )
    citations = tuple(finding.get("supporting_citations", ()))
    citation_ids = tuple(
        str(item.get("evidence_id") or "")
        for item in citations if isinstance(item, Mapping)
    )
    obligation_ids = tuple(
        str(item) for item in finding.get("related_obligation_ids", ())
        if str(item)
    )
    required_text = (
        "candidate_id", "claim", "affected_file", "causal_chain",
        "user_visible_consequence", "manual_validation",
    )
    return bool(
        all(str(finding.get(key) or "").strip() for key in required_text)
        and supporting_ids
        and set(supporting_ids) == set(citation_ids)
        and all(_citation_is_authorized(item, records) for item in citations)
        and obligation_ids
        and all(item in artifact.get("coverage", {}) for item in obligation_ids)
        and str(finding.get("collector_session_id") or "").strip()
    )


def _unsupported_handoff_lines(artifact: Mapping[str, Any]) -> list[str]:
    handoff = artifact.get("handoff")
    if not isinstance(handoff, Mapping):
        return ["handoff is not an object"]
    markdown = str(handoff.get("markdown") or "")
    verdict_labels = {
        "approve": "Approve",
        "request_changes": "Request changes",
        "notice": "Human review required",
    }
    status_labels = {
        "complete": "AI review complete",
        "degraded": "AI review completed with material coverage limits",
    }
    verdict = artifact.get("verdict")
    verdict_value = (
        str(verdict.get("value") or "")
        if isinstance(verdict, Mapping)
        else str(verdict or "")
    )
    fixed = {
        "## AI Review Handoff",
        "### Change map",
        "### AI focus and coverage",
        "### Human review focus",
        "These focus suggestions do not reduce responsibility to review the complete change.",
        f"**Recommendation:** {verdict_labels.get(verdict_value, '')}",
        f"**Status:** {status_labels.get(str(artifact.get('evaluation_status')), '')}",
    }
    change_map = {str(item) for item in handoff.get("change_map", ())}
    focuses = {str(item) for item in handoff.get("reviewed_focuses", ())}
    emphasis = {str(item) for item in handoff.get("review_emphasis", ())}
    thread = str(handoff.get("thread_status") or "")
    warning = str(handoff.get("coverage_warning") or "")
    theme = str(handoff.get("finding_theme") or "")
    access_count = int(handoff.get("access_request_count") or 0)
    unsupported: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or line in fixed:
            continue
        if line.startswith("- ") and line[2:] in change_map.union(emphasis):
            continue
        if any(
            line == prefix + "; ".join(sorted(values))
            for prefix, values in (
                ("- Specialist focus: ", {
                    item for item in focuses
                    if not item.startswith("Repository recipe:")
                }),
                ("- Repository recipes: ", {
                    item for item in focuses
                    if item.startswith("Repository recipe:")
                }),
                ("- Coverage boundaries: ", {
                    item for item in focuses
                    if not item.startswith("Repository recipe:")
                }),
            )
            if values
        ):
            continue
        if thread and line == f"**Thread status:** {thread}":
            continue
        if warning and line == f"**Material coverage warning:** {warning}":
            continue
        if theme and line.startswith("**Aggregate finding theme:** "):
            continue
        if access_count and line.startswith("**Source access requests:** "):
            continue
        unsupported.append(line)
    return unsupported


def _quoted_note(value: object, *, limit: int = 1000) -> str:
    single_line = " ".join(str(value or "").split())[:limit]
    return "<code>" + html.escape(single_line, quote=True) + "</code>"


def _expected_finding_note(finding: Mapping[str, Any]) -> str:
    def citation_lines(values: object) -> list[str]:
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return []
        return [
            "- ID " + _quoted_note(item.get("evidence_id"), limit=160)
            + "; category " + _quoted_note(item.get("category"), limit=100)
            + "; tool " + _quoted_note(item.get("tool"), limit=100)
            + "; source " + _quoted_note(item.get("source"))
            + "; content hash " + _quoted_note(item.get("content_hash"), limit=80)
            for item in values if isinstance(item, Mapping)
        ]

    consequences = tuple(finding.get("user_visible_consequences", ())) or (
        finding.get("user_visible_consequence", ""),
    )
    validations = tuple(finding.get("manual_validations", ())) or (
        finding.get("manual_validation", ""),
    )
    supporting = citation_lines(finding.get("supporting_citations", ()))
    contradicting = citation_lines(finding.get("contradicting_citations", ()))
    markdown = (
        f"### {str(finding.get('severity') or 'info').title()} finding\n\n"
        "**Claim:** " + _quoted_note(finding.get("claim"))
        + "\n\n**User-visible consequence:**\n"
        + "\n".join("- " + _quoted_note(item) for item in consequences)
        + "\n\n**Causal chain:** " + _quoted_note(finding.get("causal_chain"))
        + "\n\n**Supporting evidence provenance / citations:**\n"
        + "\n".join(supporting)
    )
    if contradicting:
        markdown += (
            "\n\n**Contradicting evidence provenance / citations:**\n"
            + "\n".join(contradicting)
        )
    return (
        markdown
        + "\n\n**Suggested validation:**\n"
        + "\n".join("- " + _quoted_note(item) for item in validations)
    )


def _unsupported_public_claims(
    artifact: Mapping[str, Any],
    notes: Sequence[ReviewNote],
) -> tuple[str, ...]:
    records = _retained_evidence(artifact)
    accepted = tuple(
        item for item in artifact.get("accepted_candidates", ())
        if isinstance(item, Mapping)
    )
    unsupported = _unsupported_handoff_lines(artifact)
    accepted_by_fingerprint = {
        str(item.get("root_cause_fingerprint") or ""): item
        for item in accepted
    }
    projected_notes = {
        (
            str(item.get("kind") or ""),
            str(item.get("fingerprint") or ""),
        ): item
        for item in artifact.get("notes", ())
        if isinstance(item, Mapping)
    }
    for finding in accepted:
        if not _accepted_claim_is_authorized(finding, artifact, records):
            unsupported.append(str(finding.get("claim") or finding))
    for note in notes:
        projection = projected_notes.get((note.kind.value, note.fingerprint))
        expected_projection = {
            "kind": note.kind.value,
            "fingerprint": note.fingerprint,
            "related_obligation_ids": list(note.related_obligation_ids),
            "evidence_ids": list(note.evidence_ids),
        }
        if projection != expected_projection:
            unsupported.append(note.markdown)
            continue
        finding = accepted_by_fingerprint.get(note.fingerprint)
        if note.kind.value == "finding" and (
            finding is None or note.markdown != _expected_finding_note(finding)
        ):
            unsupported.append(note.markdown)
    return tuple(dict.fromkeys(item for item in unsupported if item))


_ADVERSARIAL_PREDICATES = {
    "no_progress_resume.same_session": True,
    "no_progress_resume.budget_reset": False,
    "reconstruction.reason": "repetitive-transcript",
    "reconstruction.recoveries": 1,
    "reconstruction.checkpoint_retained": True,
    "planner_repair.repair_requests": 1,
    "planner_repair.source": "model_repaired_validated",
    "failed_critic.terminal": True,
    "failed_critic.fallback": "conservative",
    "deadline_cutoff.deadline_violation": False,
    "deadline_cutoff.finalization_reserved": True,
    "deadline_cutoff.cutoff_enforced": True,
    "deadline_cutoff.terminal": True,
    "completion_inversion.coverage_stable": True,
    "completion_inversion.evidence_stable": True,
    "completion_inversion.orders_enforced": True,
    "completion_inversion.terminal": True,
    "completion_inversion.controller_runs": 2,
    "note_anchor_race.stable": True,
}


def _failed_adversarial_predicates(
    cases: Mapping[str, Mapping[str, Any]] | None,
) -> list[str]:
    if not isinstance(cases, Mapping):
        return ["adversarial_cases.missing"]
    failed = []
    for path, expected in _ADVERSARIAL_PREDICATES.items():
        scenario, predicate = path.split(".", 1)
        value = cases.get(scenario)
        actual = value.get(predicate) if isinstance(value, Mapping) else None
        if actual != expected:
            failed.append(path)
    return failed


def evaluate_specialist_replay(
    artifact: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    notes: Sequence[ReviewNote] = (),
    observed: Mapping[str, Any] | None = None,
    adversarial_cases: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Grade a real schema-v2 artifact against strict offline acceptance gates."""
    missing_expected = sorted(EXPECTED_FIELDS - set(expected))
    if missing_expected:
        raise ValueError(
            "specialist expectations missing fields: " + ", ".join(missing_expected)
        )
    observed = dict(observed or {})
    coverage = artifact.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    mandatory_ids = validated_strings(
        expected["mandatory_obligation_ids"],
        "expected mandatory_obligation_ids",
    )
    expected_ids = validated_strings(
        expected["obligation_ids"], "expected obligation_ids",
    )
    missing = sorted(set(expected_ids) - set(coverage))
    unexpected = sorted(set(coverage) - set(expected_ids))
    missing_mandatory = sorted(set(mandatory_ids) - set(coverage))
    invalid_statuses = sorted(
        obligation_id for obligation_id in mandatory_ids
        if obligation_id in coverage and (
            not isinstance(coverage[obligation_id], Mapping)
            or coverage[obligation_id].get("status")
            not in _TERMINAL_OBLIGATION_STATUSES
        )
    )
    recipes = artifact.get("recipes")
    recipes = recipes if isinstance(recipes, Mapping) else {}
    expected_recipes = expected["recipe_statuses"]
    recipe_mismatches = sorted(
        recipe_id for recipe_id, status in expected_recipes.items()
        if not isinstance(recipes.get(recipe_id), Mapping)
        or recipes[recipe_id].get("status") != status
    )
    unsupported = _unsupported_public_claims(artifact, notes)
    failed_adversarial = _failed_adversarial_predicates(adversarial_cases)
    unsafe_fetch_attempts = int(observed.get("unsafe_fetch_attempts", 0))
    elapsed = float(observed.get("elapsed_simulated_sec", 0.0))
    history = observed.get("budget_history")
    if not isinstance(history, Mapping):
        history = budget_history(artifact)
    reset_sessions = sorted(
        str(session_id)
        for session_id, values in history.items()
        if isinstance(values, Sequence)
        and not isinstance(values, (str, bytes))
        and any(
            _budget_usage_decreased(previous, current)
            for previous, current in zip(values, values[1:])
        )
    )
    accepted = tuple(
        item for item in artifact.get("accepted_candidates", ())
        if isinstance(item, Mapping)
    )
    accepted_ids = {
        str(item.get("candidate_id", "")) for item in accepted
        if str(item.get("candidate_id", "")).strip()
    }
    expected_finding_ids = set(expected["finding_ids"])
    missing_findings = sorted(expected_finding_ids - accepted_ids)
    artifact_unknown_ids = {
        str(item.get("obligation_id") or item.get("candidate_id") or "")
        for key in ("unknowns", "candidate_unknowns")
        for item in artifact.get(key, ())
        if isinstance(item, Mapping)
        and str(item.get("obligation_id") or item.get("candidate_id") or "").strip()
    }
    unexpected_unknowns = sorted(
        artifact_unknown_ids - set(expected["acceptable_unknowns"])
    )
    retained_evidence_ids = {
        str(item.get("evidence_id", ""))
        for item in artifact.get("evidence", ())
        if isinstance(item, Mapping)
        and str(item.get("evidence_id", "")).strip()
    }
    referenced_evidence_ids = {
        str(evidence_id)
        for item in accepted
        for field in ("supporting_evidence_ids", "contradicting_evidence_ids")
        for evidence_id in item.get(field, ())
    }
    referenced_evidence_ids.update(
        evidence_id for note in notes for evidence_id in note.evidence_ids
    )
    missing_evidence_ids = sorted(
        referenced_evidence_ids - retained_evidence_ids
    )
    head_mismatch = bool(
        expected.get("head_sha")
        and artifact.get("head_sha") != expected["head_sha"]
    )
    totals = artifact.get("budgets", {}).get("totals", {})
    if not isinstance(totals, Mapping):
        totals = {}
    budget_exceeded = (
        int(totals.get("specialist_model_requests", 0))
        > int(expected["max_model_turns"])
        or int(totals.get("tool_calls", 0)) > int(expected["max_tool_calls"])
        or int(totals.get("recoveries", 0)) > int(expected["max_recoveries"])
    )
    failure_gates: list[str] = []
    if artifact.get("schema_version") != 2:
        failure_gates.append("artifact_schema")
    if missing_mandatory or invalid_statuses:
        failure_gates.append("missing_mandatory_status")
    if missing or unexpected:
        failure_gates.append("obligation_accounting")
    if recipe_mismatches:
        failure_gates.append("recipe_status")
    if unsupported:
        failure_gates.append("unsupported_public_claim")
    if unsafe_fetch_attempts:
        failure_gates.append("unsafe_fetch")
    if reset_sessions:
        failure_gates.append("budget_reset")
    if elapsed > float(expected["deadline_sec"]):
        failure_gates.append("deadline_violation")
    if budget_exceeded:
        failure_gates.append("budget_exceeded")
    if missing_findings:
        failure_gates.append("missing_expected_finding")
    if unexpected_unknowns:
        failure_gates.append("unexpected_unknown")
    if missing_evidence_ids:
        failure_gates.append("missing_evidence")
    if head_mismatch:
        failure_gates.append("head_mismatch")
    if failed_adversarial:
        failure_gates.append("adversarial_failure")
    phases = artifact.get("phases")
    phases = phases if isinstance(phases, Sequence) else ()
    return {
        "passed": not failure_gates,
        "failure_gates": failure_gates,
        "obligation_accounting": {
            "expected": len(expected_ids),
            "observed": len(coverage),
            "missing": missing,
            "unexpected": unexpected,
            "statuses": {
                key: value.get("status")
                for key, value in sorted(coverage.items())
                if isinstance(value, Mapping)
            },
        },
        "recipe_status": {
            key: value.get("status")
            for key, value in sorted(recipes.items())
            if isinstance(value, Mapping)
        },
        "unsupported_claims": list(unsupported),
        "adversarial": {
            "failed": failed_adversarial,
        },
        "candidates": {
            "accepted": len(accepted),
            "rejected": len(artifact.get("rejected_candidates", ())),
            "expected_ids": sorted(expected_finding_ids),
            "missing_expected_ids": missing_findings,
        },
        "unknowns": {
            "observed_ids": sorted(artifact_unknown_ids),
            "unexpected_ids": unexpected_unknowns,
        },
        "evidence": {
            "retained": len(retained_evidence_ids),
            "referenced": len(referenced_evidence_ids),
            "missing_ids": missing_evidence_ids,
            "head_matches": not head_mismatch,
        },
        "review_note_anchor_types": _note_anchor_types(notes),
        "sources": {
            "denials": int(observed.get("source_denials", 0)),
            "requests": len(artifact.get("source_access_requests", ()))
            + int(observed.get("source_access_requests", 0)),
            "unsafe_fetch_attempts": unsafe_fetch_attempts,
        },
        "runtime": {
            "model_turns": int(totals.get("specialist_model_requests", 0)),
            "controller_model_turns": int(
                totals.get("controller_model_requests", 0)
            ),
            "tool_calls": int(totals.get("tool_calls", 0)),
            "recoveries": int(totals.get("recoveries", 0)),
            "budget_reset_sessions": reset_sessions,
            "elapsed_simulated_sec": elapsed,
            "deadline_sec": expected["deadline_sec"],
        },
        "phase_timing": [dict(item) for item in phases if isinstance(item, Mapping)],
        "finalization_reserve_seconds": artifact.get("timing", {}).get(
            "finalization_reserve_seconds", 0,
        ),
    }


def evaluate_web_policy_replay(result: Mapping[str, Any]) -> dict[str, Any]:
    """Grade measured source-policy behavior; fixture expectations are comparisons."""
    expected = result.get("expected")
    if not isinstance(expected, Mapping):
        raise ValueError("web replay result lacks expected comparisons")
    approved = list(result.get("approved_fetches", ()))
    denials = int(result.get("source_denials", 0))
    requests = int(result.get("source_access_requests", 0))
    unsafe = int(result.get("unsafe_fetch_attempts", 0))
    public_result = {
        key: value for key, value in result.items() if key != "expected"
    }
    public_text = json.dumps(public_result, sort_keys=True)
    leaked = [
        str(marker)
        for marker in expected.get("forbidden_public_text", ())
        if str(marker) and str(marker) in public_text
    ]
    mismatches = []
    if approved != list(expected.get("approved_fetches", ())):
        mismatches.append("approved_fetches")
    if denials != int(expected.get("source_denials", -1)):
        mismatches.append("source_denials")
    if requests != int(expected.get("source_access_requests", -1)):
        mismatches.append("source_access_requests")
    if unsafe != int(expected.get("unsafe_fetch_attempts", -1)):
        mismatches.append("unsafe_fetch_attempts")
    note = result.get("request_note")
    if not isinstance(note, Mapping) or note.get("kind") != "source_access_request":
        mismatches.append("source_access_request_note")
    gates = []
    if unsafe:
        gates.append("unsafe_fetch")
    if leaked:
        gates.append("unsupported_public_claim")
    if mismatches:
        gates.append("web_source_policy")
    return {
        "passed": not gates,
        "failure_gates": gates,
        "sources": {
            "approved_fetches": len(approved),
            "denials": denials,
            "requests": requests,
            "unsafe_fetch_attempts": unsafe,
        },
        "mismatches": mismatches,
        "unsupported_claims": leaked,
    }


def run_offline_specialist_replays(
    corpus: BenchmarkCorpus,
) -> list[dict[str, Any]]:
    """Execute repository-recorded specialist fixtures without external I/O."""
    if not corpus.offline_specialist_replays:
        return []
    if corpus.source_path is None:
        raise ValueError("offline specialist corpus requires a source path")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in corpus.offline_specialist_replays:
        if set(entry) != {"id", "kind", "fixture"}:
            raise ValueError(
                "offline specialist entry must contain exactly id, kind, and fixture"
            )
        replay_id = str(entry["id"]).strip()
        replay_kind = str(entry["kind"]).strip()
        fixture_value = str(entry["fixture"]).strip()
        if not replay_id or replay_kind not in {"runtime", "web_policy"} or not fixture_value:
            raise ValueError("offline specialist id, kind, and fixture are invalid")
        if replay_id in seen:
            raise ValueError(f"duplicate offline specialist replay id: {replay_id}")
        seen.add(replay_id)
        fixture_path = (corpus.source_path.parent / fixture_value).resolve()
        if replay_kind == "web_policy":
            web = replay_web_policy_fixture(fixture_path)
            metrics = evaluate_web_policy_replay(web)
            results.append({
                "id": replay_id,
                "kind": replay_kind,
                "fixture": fixture_value,
                "passed": metrics["passed"],
                "failure_gates": metrics["failure_gates"],
                "metrics": metrics,
            })
            continue
        replay = replay_fixture(fixture_path)
        metrics = evaluate_specialist_replay(
            replay.artifact,
            replay.expected,
            notes=replay.notes,
            observed=replay.observed,
            adversarial_cases=replay.failures,
        )
        results.append({
            "id": replay_id,
            "kind": replay_kind,
            "fixture": fixture_value,
            "passed": metrics["passed"],
            "failure_gates": metrics["failure_gates"],
            "metrics": metrics,
            "artifact": {
                "schema_version": replay.artifact["schema_version"],
                "artifact_id": replay.artifact["artifact_id"],
                "repository": replay.artifact["repository"],
                "head_sha": replay.artifact["head_sha"],
                "evaluation_status": replay.artifact["evaluation_status"],
            },
            "adversarial_cases": replay.failures,
        })
    return results


# ---------------------------------------------------------------------------
# Capability checks (the agentic-evidence-chain criterion, #203/#207)
# ---------------------------------------------------------------------------
#
# Findings precision/recall can't express the home-ops#7462 acceptance bar —
# "did the reviewer chain tools to consult the platform's compatibility matrix
# and cite it?" That is a *capability* assertion on the evidence-gathering, not
# a findings-quality score. A scenario declares it as `expected_evidence` and
# the harness grades each run pass/fail; the bar is met as a RATE over many
# runs (a single green run proves nothing at the fast tier's reliability).
#
# Check kinds (capability passes iff ALL checks pass):
#   tool_call      — some executed tool_call matches `tool` and, for each key in
#                    `args_contains`, that call's arg holds ALL listed substrings
#   review_mentions — the published review markdown contains ANY of `any_of`
# Both are substring/case-insensitive: the grader names concrete evidence (it is
# not the reviewer), but stays loose on phrasing.


def _arg_value(call: dict[str, Any], key: str) -> str:
    args = call.get("args")
    if not isinstance(args, dict):
        return ""
    val = args.get(key)
    return val if isinstance(val, str) else ""


def evaluate_capability(
    run: ReviewRun, expected_evidence: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Grade a run against a scenario's expected_evidence.

    Returns None when the scenario declares no capability checks (so callers
    can skip capability aggregation for ordinary findings-only PRs). Otherwise
    returns {description, checks: [{id, type, passed, ...}], passed: bool}.
    A run that errored fails every check (no evidence was produced).
    """
    if not expected_evidence:
        return None
    checks = expected_evidence.get("checks", [])
    if not checks:
        return None

    results: list[dict[str, Any]] = []
    review_lc = (run.review_markdown or "").lower()

    for check in checks:
        ctype = check.get("type")
        cid = check.get("id", ctype or "check")
        passed = False

        if run.error:
            passed = False
        elif ctype == "tool_call":
            want_tool = check.get("tool")
            # ``tool`` may be a single name or a list of acceptable names — the
            # latter lets one check credit either path to the same evidence
            # (e.g. web_search OR web_fetch reaching a support matrix).
            want_tools = (
                want_tool if isinstance(want_tool, list)
                else [want_tool] if want_tool else []
            )
            args_contains = check.get("args_contains", {})
            # ``args_any_contains``: pass when ANY of the call's string arg
            # values contains ANY listed substring — tool-agnostic, so it
            # matches a matrix URL in web_fetch or a matrix query in web_search.
            any_needles = [
                str(n).lower() for n in check.get("args_any_contains", [])
            ]
            for call in run.tool_calls:
                if want_tools and call.get("tool") not in want_tools:
                    continue
                if call.get("status") not in (None, "ok"):
                    # A failed tool call isn't usable evidence.
                    continue
                ok = True
                for key, needles in args_contains.items():
                    hay = _arg_value(call, key).lower()
                    needle_list = needles if isinstance(needles, list) else [needles]
                    if not all(str(n).lower() in hay for n in needle_list):
                        ok = False
                        break
                if ok and any_needles:
                    arg_vals = (call.get("args") or {}).values()
                    haystack = " ".join(
                        v.lower() for v in arg_vals if isinstance(v, str)
                    )
                    ok = any(n in haystack for n in any_needles)
                if ok:
                    passed = True
                    break
        elif ctype == "review_mentions":
            any_of = check.get("any_of", [])
            passed = any(str(s).lower() in review_lc for s in any_of)

        results.append({"id": cid, "type": ctype, "passed": passed})

    return {
        "description": expected_evidence.get("description", ""),
        "checks": results,
        "passed": all(c["passed"] for c in results),
    }


def populate_tool_trace(run: ReviewRun, repo_path: Path) -> None:
    """Read tool-harness.json (left in the run cwd) into the ReviewRun.

    The native_loop harness emits a `tool_calls` array ({tool, args, status});
    older planner modes emit only `tool_results` (tool + status, no args), so
    fall back to that. Either way the capability checker gets the trace it can
    grade; absence of the file is silently fine (tools_off mode).
    """
    harness_file = repo_path / "tool-harness.json"
    if not harness_file.exists():
        return
    try:
        data = json.loads(harness_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    run.tool_stop_reason = data.get("stop_reason")
    if isinstance(data.get("tool_calls"), list):
        run.tool_calls = data["tool_calls"]
    elif isinstance(data.get("tool_results"), list):
        run.tool_calls = [
            {"tool": r.get("tool"), "args": {}, "status": r.get("status")}
            for r in data["tool_results"]
            if isinstance(r, dict)
        ]


# ---------------------------------------------------------------------------
# Review execution (stub — to be wired with actual review scripts)
# ---------------------------------------------------------------------------

def run_review_for_pr(
    pr_entry: dict[str, Any],
    mode: str,
    work_dir: Path,
    model_config: dict[str, str],
) -> ReviewRun:
    """Execute one review mode for a single PR.

    This is the integration point with the actual review pipeline.
    Currently produces stub results; real implementation will call
    run_review.sh with appropriate TOOL_MODE settings.

    Args:
        pr_entry: Corpus entry for one PR (with url, number, repo_full_name).
        mode: One of "tools_off", "native_loop".
        work_dir: Working directory for this run's artifacts.
        model_config: Model configuration (base_url, model, api_key, etc.).

    Returns:
        ReviewRun with collected metrics.
    """
    pr_number = pr_entry["number"]
    repo_full_name = pr_entry["repo_full_name"]

    run = ReviewRun(
        mode=mode,
        pr_number=pr_number,
        repo_full_name=repo_full_name,
    )

    try:
        start = time.monotonic()

        # Determine tool_mode argument for run_review.sh
        if mode == "tools_off":
            tool_mode_arg = ""
        elif mode == "native_loop":
            tool_mode_arg = "native_loop"
        else:
            raise ValueError(f"Unknown mode: {mode}")

        # Build the review corpus and run the review
        repo_path = work_dir / repo_full_name.replace("/", "-")
        if not repo_path.exists():
            # Clone or checkout the repo
            subprocess.run(
                ["git", "clone", f"https://github.com/{repo_full_name}.git", str(repo_path)],
                check=False,  # may fail for private repos
                capture_output=True,
            )

        if not repo_path.exists():
            run.error = f"Repo {repo_full_name} not available locally"
            return run

        # Extract PR number from URL
        import urllib.parse
        parsed = urllib.parse.urlparse(pr_entry["url"])
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2:
            pr_num = int(path_parts[-1])
        else:
            pr_num = pr_number

        # Set environment for the review run
        env = os.environ.copy()
        env["GITHUB_TOKEN"] = model_config.get("github_token", "")
        env["AI_BASE_URL"] = model_config.get("base_url", "")
        env["AI_MODEL"] = model_config.get("model", "")
        env["AI_API_KEY"] = model_config.get("api_key", "")
        if tool_mode_arg:
            env["TOOL_MODE"] = tool_mode_arg

        # Run the review via run_review.sh (resolved relative to this script,
        # so the harness is not pinned to one machine's checkout path).
        review_script = Path(__file__).resolve().parent / "run_review.sh"
        if review_script.exists():
            result = subprocess.run(
                [str(review_script)],
                cwd=str(repo_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min per PR per mode
            )
            run.wall_clock_sec = time.monotonic() - start

            # Parse outputs
            if result.returncode == 0:
                # Check for verdict output file
                verdict_file = repo_path / "verdict.json"
                if verdict_file.exists():
                    vdata = json.loads(verdict_file.read_text())
                    run.verdict = vdata.get("verdict")
                    run.review_markdown = vdata.get("review_markdown", "")
                    run.tokens_input = int(vdata.get("tokens_input", 0))
                    run.tokens_output = int(vdata.get("tokens_output", 0))
                    run.model_used = vdata.get("model_used", "")
                else:
                    # Parse from stdout if available
                    run.review_markdown = result.stdout[:2000] if result.stdout else ""

                populate_tool_trace(run, repo_path)
            else:
                run.error = f"Review failed (exit {result.returncode}): {result.stderr[:500]}"
        else:
            run.error = f"run_review.sh not found at {review_script}"

    except subprocess.TimeoutExpired:
        run.wall_clock_sec = time.monotonic() - start
        run.error = "Review timed out after 300s"
    except Exception as exc:
        run.wall_clock_sec = time.monotonic() - start
        run.error = f"Review error: {exc}"

    return run


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    results: list[BenchmarkResult],
    corpus: BenchmarkCorpus,
) -> dict[str, Any]:
    """Generate the full benchmark report."""
    modes = {"tools_off", "native_loop"}
    active_modes = set()

    # Per-mode aggregation
    mode_metrics: dict[str, dict[str, Any]] = {}
    for m in modes:
        mode_metrics[m] = {
            "runs": 0,
            "successful_runs": 0,
            "total_tokens_input": 0,
            "total_tokens_output": 0,
            "total_wall_clock_sec": 0.0,
            "findings_count": 0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "errors": 0,
            # Capability checks (agentic-evidence-chain criterion). Counted only
            # for scenarios that declare expected_evidence; pass_rate is the
            # headline number for the home-ops#7462-style regression.
            "capability_runs": 0,
            "capability_passes": 0,
        }

    report_results = []

    for bm in results:
        entry: dict[str, Any] = {
            "pr_number": bm.pr_number,
            "repo_full_name": bm.repo_full_name,
        }

        # Get known findings for this PR
        pr_entry = next(
            (p for p in corpus.prs if p["number"] == bm.pr_number),
            None,
        )
        known_findings = load_known_findings(pr_entry) if pr_entry else []
        expected_evidence = pr_entry.get("expected_evidence") if pr_entry else None

        mode_runs: dict[str, ReviewRun] = {}
        # Per-mode capability tallies for THIS PR (a PR may run N times/mode).
        pr_capability: dict[str, dict[str, int]] = {}
        for run in bm.runs:
            active_modes.add(run.mode)
            mm = mode_metrics[run.mode]
            mm["runs"] += 1
            if not run.error:
                mm["successful_runs"] += 1
                mm["total_tokens_input"] += run.tokens_input
                mm["total_tokens_output"] += run.tokens_output
                mm["total_wall_clock_sec"] += run.wall_clock_sec
                mm["findings_count"] += len(run.findings)
            else:
                mm["errors"] += 1

            cap = evaluate_capability(run, expected_evidence)
            if cap is not None:
                mm["capability_runs"] += 1
                tally = pr_capability.setdefault(run.mode, {"runs": 0, "passes": 0})
                tally["runs"] += 1
                if cap["passed"]:
                    mm["capability_passes"] += 1
                    tally["passes"] += 1

            # Keep the last run's full detail for the per-PR entry; repeated
            # runs of the same mode are summarised by the capability tally.
            mode_runs[run.mode] = run
            entry[run.mode] = run.to_dict()
            if cap is not None:
                entry[run.mode]["capability"] = cap

        if pr_capability:
            entry["capability_pass_rate"] = {
                mode: round(t["passes"] / t["runs"], 4) if t["runs"] else 0.0
                for mode, t in pr_capability.items()
            }

        # Quality comparison for each mode
        for mode in active_modes:
            if mode in mode_runs and not mode_runs[mode].error:
                found = extract_findings_from_review(mode_runs[mode])
                quality = compute_precision_recall(found, known_findings)
                mm = mode_metrics[mode]
                # Weighted average for precision/recall
                if quality["total_found"] > 0 and quality["total_known"] > 0:
                    mm["precision"] = (
                        (mm["precision"] * (mm["runs"] - 1) + quality["precision"])
                        / mm["runs"]
                    )
                    mm["recall"] = (
                        (mm["recall"] * (mm["runs"] - 1) + quality["recall"])
                        / mm["runs"]
                    )
                    mm["f1"] = (
                        (mm["f1"] * (mm["runs"] - 1) + quality["f1"])
                        / mm["runs"]
                    )

        report_results.append(entry)

    # Compute averages for each mode
    for m, mm in mode_metrics.items():
        if mm["successful_runs"] > 0:
            n = mm["successful_runs"]
            mm["avg_tokens_input"] = round(mm["total_tokens_input"] / n, 1)
            mm["avg_tokens_output"] = round(mm["total_tokens_output"] / n, 1)
            mm["avg_wall_clock_sec"] = round(mm["total_wall_clock_sec"] / n, 3)
        else:
            mm["avg_tokens_input"] = 0
            mm["avg_tokens_output"] = 0
            mm["avg_wall_clock_sec"] = 0
        # Headline agentic-capability number: fraction of capability-scored runs
        # that closed the expected evidence chain. None when no scenario in the
        # corpus declared expected_evidence for this mode.
        mm["capability_pass_rate"] = (
            round(mm["capability_passes"] / mm["capability_runs"], 4)
            if mm["capability_runs"] > 0
            else None
        )

    report = {
        "metadata": {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "harness_version": "0.1.0",
            "modes_tested": sorted(active_modes),
            "total_prs": len(results),
            "corpus_source": None,  # set by caller
        },
        "mode_summary": {m: mode_metrics[m] for m in sorted(mode_metrics)},
        "per_pr_results": report_results,
    }

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A/B evaluation harness for PR review modes",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="Path to benchmark corpus JSON file",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["tools_off", "native_loop"],
        help="Review modes to run (default: tools_off native_loop)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("AI_MODEL", ""),
        help="Model name for review runs",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=os.getenv("AI_BASE_URL", ""),
        help="AI API base URL",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.getenv("AI_API_KEY", ""),
        help="AI API key",
    )
    parser.add_argument(
        "--github-token",
        type=str,
        default=os.getenv("GITHUB_TOKEN", ""),
        help="GitHub token for PR data access",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output report path (default: stdout)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned runs without executing",
    )
    parser.add_argument(
        "--max-prs",
        type=int,
        default=None,
        help="Limit to first N PRs from corpus",
    )
    parser.add_argument(
        "--runs-per-mode",
        type=int,
        default=1,
        help=(
            "Repeat each mode N times per PR and report capability pass RATE. "
            "Use >=10 for the agentic-evidence-chain criterion — a single run "
            "is noise at the fast tier's reliability (Tau2 ~68%%)."
        ),
    )
    parser.add_argument(
        "--offline-specialist-only",
        action="store_true",
        help=(
            "Run only recorded specialist-runtime fixtures declared by the corpus; "
            "no repository clone, network, or model endpoint is used"
        ),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Load corpus
    if not args.corpus.exists():
        print(f"Error: corpus file not found: {args.corpus}", file=sys.stderr)
        return 1

    corpus = BenchmarkCorpus.from_file(args.corpus)
    if not corpus.prs and not corpus.offline_specialist_replays:
        print("Error: corpus is empty", file=sys.stderr)
        return 1
    if args.offline_specialist_only and not corpus.offline_specialist_replays:
        print(
            "Error: corpus has no offline specialist replays",
            file=sys.stderr,
        )
        return 1

    try:
        offline_replays = run_offline_specialist_replays(corpus)
    except (OSError, ValueError) as exc:
        print(f"Error: offline specialist replay failed: {exc}", file=sys.stderr)
        return 2
    offline_passed = all(item["passed"] for item in offline_replays)

    # Limit PRs if requested
    prs = corpus.prs[:args.max_prs] if args.max_prs else corpus.prs

    model_config = {
        "model": args.model,
        "base_url": args.base_url,
        "api_key": args.api_key,
        "github_token": args.github_token,
    }

    print(f"Loaded {len(corpus.prs)} PRs from corpus, running {len(prs)}...", file=sys.stderr)
    print(f"Modes: {args.modes}", file=sys.stderr)
    print(f"Model: {args.model or '(not set)'}", file=sys.stderr)
    if offline_replays:
        print(
            f"Offline specialist replays: {len(offline_replays)} "
            f"({'PASS' if offline_passed else 'FAIL'})",
            file=sys.stderr,
        )

    runs_per_mode = max(1, args.runs_per_mode)

    if args.dry_run:
        for replay in corpus.offline_specialist_replays:
            print(f"  Would replay offline: {replay['id']} [{replay['fixture']}]")
        if args.offline_specialist_only:
            return 0
        for pr in prs:
            for mode in args.modes:
                suffix = f" x{runs_per_mode}" if runs_per_mode > 1 else ""
                print(f"  Would run: {pr['repo_full_name']}#{pr['number']} [{mode}]{suffix}")
        return 0

    if args.offline_specialist_only:
        report = {
            "metadata": {
                "generated_at": datetime.datetime.now(
                    datetime.timezone.utc,
                ).isoformat(),
                "harness_version": "0.2.0",
                "corpus_source": str(args.corpus),
                "network_used": False,
            },
            "offline_specialist_replays": offline_replays,
        }
        output_text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output_text, encoding="utf-8")
            print(f"\nReport written to {args.output}", file=sys.stderr)
        else:
            print(output_text)
        return 0 if offline_passed else 2

    # Execute reviews
    results: list[BenchmarkResult] = []
    with tempfile.TemporaryDirectory(prefix="eval-harness-") as tmpdir:
        work_dir = Path(tmpdir)

        for i, pr in enumerate(prs, 1):
            print(f"[{i}/{len(prs)}] {pr['repo_full_name']}#{pr['number']}", file=sys.stderr)

            bm = BenchmarkResult(
                pr_number=pr["number"],
                repo_full_name=pr["repo_full_name"],
            )

            for mode in args.modes:
                for rep in range(runs_per_mode):
                    run = run_review_for_pr(pr, mode, work_dir, model_config)
                    bm.runs.append(run)
                    label = f"{mode}" if runs_per_mode == 1 else f"{mode} {rep + 1}/{runs_per_mode}"
                    if run.error:
                        print(f"    [{label}] ERROR: {run.error}", file=sys.stderr)
                    else:
                        findings = extract_findings_from_review(run)
                        print(
                            f"    [{label}] verdict={run.verdict} "
                            f"findings={len(findings)} "
                            f"tools={len(run.tool_calls)} "
                            f"tokens_in={run.tokens_input} "
                            f"tokens_out={run.tokens_output} "
                            f"wall={run.wall_clock_sec:.1f}s",
                            file=sys.stderr,
                        )

            results.append(bm)

    # Generate report
    report = generate_report(results, corpus)
    report["metadata"]["corpus_source"] = str(args.corpus)
    report["offline_specialist_replays"] = offline_replays

    output_text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output_text, encoding="utf-8")
        print(f"\nReport written to {args.output}", file=sys.stderr)
    else:
        print(output_text)

    return 0 if offline_passed else 2


if __name__ == "__main__":
    sys.exit(main())
