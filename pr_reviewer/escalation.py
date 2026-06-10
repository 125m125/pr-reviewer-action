"""Escalation decision for fast→smart review routing (#160).

After the fast model produced a review, decide deterministically whether the
smart model should re-review. Every trigger is boring and testable on
purpose: verdict value, required-check keyword validation, an explicit
Unknowns/Needs-Verification section (or a suspiciously short review), and
blocker-level evidence/tool signals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pr_reviewer.completeness import validate_review

# Reviews shorter than this are treated as low-confidence regardless of
# content — a fast model that produced two sentences did not review anything.
LOW_CONFIDENCE_MIN_CHARS = 200

# Header of the section the default prompt asks for "when evidence is
# incomplete" — its presence with real content is the model saying it is
# unsure.
_UNKNOWNS_HEADER_RE = re.compile(
    r"(?im)^#{1,6}\s*unknowns?\b[^\n]*$"
)

_EMPTY_SECTION_RE = re.compile(r"(?i)^\(?(none|n/?a|nothing)\)?[.!]?$")


def _load(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def is_low_confidence(review_markdown: str) -> bool:
    text = (review_markdown or "").strip()
    if len(text) < LOW_CONFIDENCE_MIN_CHARS:
        return True
    match = _UNKNOWNS_HEADER_RE.search(text)
    if match:
        rest = text[match.end():].strip()
        section = re.split(r"(?m)^#{1,6}\s", rest, maxsplit=1)[0].strip()
        if section and not _EMPTY_SECTION_RE.match(section) and len(section) > 40:
            return True
    return False


def _has_blocker_signals(evidence: dict, harness: dict) -> bool:
    if evidence.get("has_blocker"):
        return True
    if harness.get("planning_error") is not None or harness.get("error") is not None:
        return True
    executed = harness.get("executed_request_count", 0)
    results = [t for t in harness.get("tool_results", []) if isinstance(t, dict)]
    if executed and results and not any(t.get("status") == "ok" for t in results):
        return True
    return False


def should_escalate(
    on_incomplete: bool = True,
    on_request_changes: bool = True,
    on_low_confidence: bool = True,
    on_blockers: bool = True,
    output_path: str = "ai-output.json",
    classification_path: str = "classification.json",
    evidence_path: str = "evidence-providers.json",
    tool_harness_path: str = "tool-harness.json",
) -> tuple[bool, list[str]]:
    """Return (escalate, reasons) for the fast review in *output_path*.

    Must run on the raw fast output — before verdict_policy / completeness
    validation mutate it — so the triggers see what the model actually said.
    """
    data = _load(output_path)
    review = str(data.get("review_markdown") or "")
    reasons: list[str] = []

    if on_request_changes and data.get("verdict") == "request_changes":
        reasons.append("fast_request_changes")

    if on_incomplete:
        classification = _load(classification_path)
        must_check = [
            str(item) for item in (classification.get("must_check") or []) if item
        ]
        if must_check and not validate_review(must_check, review)["validated"]:
            reasons.append("incomplete_required_checks")

    if on_low_confidence and is_low_confidence(review):
        reasons.append("fast_low_confidence")

    if on_blockers and _has_blocker_signals(
        _load(evidence_path), _load(tool_harness_path)
    ):
        reasons.append("tool_or_evidence_blockers")

    return bool(reasons), reasons
