"""Enforcement logic for review verdicts.

Applies evidence blocker and tool harness enforcement rules, overriding
the model's verdict to ``request_changes`` when configured conditions are met.
Ported from the enforcement section in ``scripts/run_review.sh``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class RuntimeVerdictPolicyResult:
    """Deterministic specialist-runtime verdict and its policy provenance."""

    verdict: str
    source: str
    blocking_finding_ids: tuple[str, ...] = ()
    blocking_obligation_ids: tuple[str, ...] = ()
    unknown_obligation_ids: tuple[str, ...] = ()


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def derive_runtime_verdict(
    *,
    model_verdict: str,
    supported_findings: Mapping[str, str],
    unresolved: Iterable[object],
    allow_approve: bool,
    policy: Mapping[str, Any] | None = None,
) -> RuntimeVerdictPolicyResult:
    """Apply evidence-backed severity and unresolved-coverage policy.

    This pure specialist-runtime helper deliberately does not replace the
    existing file-based enforcement functions or GitHub approval guardrails.
    """
    settings = dict(policy or {})
    configured = settings.get(
        "blocking_severities", settings.get("request_changes_severities")
    )
    if configured is None:
        blocking_severities = {"blocker", "major"}
    elif isinstance(configured, (list, tuple, set, frozenset)) and all(
        isinstance(item, str) for item in configured
    ):
        blocking_severities = {
            str(item).strip().lower() for item in configured if str(item).strip()
        }.intersection({"blocker", "major"})
    else:
        # A malformed repository policy must not silently disable the secure
        # default.  The validated v2 parser rejects this earlier; this guard
        # protects direct/internal callers as well.
        blocking_severities = {"blocker", "major"}

    blocking_findings = tuple(sorted(
        str(finding_id).strip()
        for finding_id, severity in supported_findings.items()
        if str(finding_id).strip()
        and str(severity).strip().lower() in blocking_severities
    ))

    unresolved_items = tuple(unresolved)
    blocking_obligations: list[str] = []
    unknown_obligations: list[str] = []
    supported_high_risk_tiers = {"critical", "high"}
    configured_high_risk_tiers = settings.get("high_risk_tiers")
    if configured_high_risk_tiers is None:
        high_risk_tiers = supported_high_risk_tiers
    elif isinstance(
        configured_high_risk_tiers, (list, tuple, set, frozenset)
    ) and all(isinstance(item, str) for item in configured_high_risk_tiers):
        normalized_high_risk_tiers = {
            item.strip().lower()
            for item in configured_high_risk_tiers
            if item.strip()
        }
        if (
            not normalized_high_risk_tiers
            or not normalized_high_risk_tiers.issubset(supported_high_risk_tiers)
        ):
            high_risk_tiers = supported_high_risk_tiers
        else:
            high_risk_tiers = normalized_high_risk_tiers
    else:
        high_risk_tiers = supported_high_risk_tiers
    blocking_policies = {
        "block", "blocking", "request_changes", "block_when_unresolved",
    }
    for obligation in unresolved_items:
        obligation_id = str(
            _field(obligation, "obligation_id", _field(obligation, "id", ""))
        ).strip()
        if not obligation_id:
            continue
        mandatory = bool(_field(obligation, "mandatory", True))
        risk_tier = str(_field(obligation, "risk_tier", "normal")).strip().lower()
        unresolved_policy = str(
            _field(obligation, "unresolved_policy", "record_unknown")
        ).strip().lower()
        explicitly_blocking = bool(_field(obligation, "block_when_unresolved", False))
        if (
            mandatory
            and risk_tier in high_risk_tiers
            and (explicitly_blocking or unresolved_policy in blocking_policies)
        ):
            blocking_obligations.append(obligation_id)
        else:
            unknown_obligations.append(obligation_id)

    if blocking_obligations:
        return RuntimeVerdictPolicyResult(
            # Missing coverage is a review-status signal, not a confirmed
            # defect.  Keep the obligation IDs and incomplete source for
            # policy/reporting consumers, but do not publish a defect-style
            # request-changes verdict unless an evidence-backed finding also
            # exists.
            verdict="request_changes" if blocking_findings else "notice",
            source="incomplete-high-risk-coverage",
            blocking_finding_ids=blocking_findings,
            blocking_obligation_ids=tuple(sorted(set(blocking_obligations))),
            unknown_obligation_ids=tuple(sorted(set(unknown_obligations))),
        )
    if blocking_findings:
        return RuntimeVerdictPolicyResult(
            verdict="request_changes",
            source="supported-findings",
            blocking_finding_ids=blocking_findings,
            unknown_obligation_ids=tuple(sorted(set(unknown_obligations))),
        )
    if not allow_approve:
        return RuntimeVerdictPolicyResult(
            verdict="request_changes",
            source="approval-disabled",
            unknown_obligation_ids=tuple(sorted(set(unknown_obligations))),
        )
    return RuntimeVerdictPolicyResult(
        verdict="approve",
        source="model" if str(model_verdict).strip().lower() == "approve" else "policy",
        unknown_obligation_ids=tuple(sorted(set(unknown_obligations))),
    )


def apply_evidence_blocker_enforcement(
    evidence_path: str = "evidence-providers.json",
    output_path: str = "ai-output.json",
) -> tuple[bool, str]:
    """Override verdict to request_changes if any evidence provider reported a blocker.

    Parameters
    ----------
    evidence_path : str
        Path to ``evidence-providers.json``.
    output_path : str
        Path to ``ai-output.json`` (modified in place).

    Returns
    -------
    tuple[bool, str]
        (applied, reason) — reason is empty when not applied.
    """
    if not Path(evidence_path).exists():
        return False, ""
    try:
        evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return False, ""

    if not evidence.get("has_blocker"):
        return False, ""

    blocker_ids = [
        p["id"]
        for p in evidence.get("providers", [])
        if p.get("provider_severity") == "blocker"
    ]
    ids_str = ", ".join(blocker_ids)

    data = json.loads(Path(output_path).read_text(encoding="utf-8", errors="replace"))
    markdown = str(data.get("review_markdown") or "")

    markdown = (
        (markdown + "\n\n## Evidence Provider Blockers\n"
         + "One or more configured evidence providers reported blocker-level findings"
         + (f" ({ids_str})" if ids_str else "")
         + ". Resolve blocker findings before approval.")
    )

    data["verdict"] = "request_changes"
    data["review_markdown"] = markdown
    reason = (
        f"Evidence provider blocker detected"
        + (f": {ids_str}" if ids_str else "")
        + ". One or more configured evidence providers reported blocker-level findings."
    )
    Path(output_path).write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    return True, reason


def _get_tool_harness_failure_reason(tool_harness_path: str = "tool-harness.json") -> str | None:
    if not Path(tool_harness_path).exists():
        return None
    try:
        harness = json.loads(Path(tool_harness_path).read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None

    if harness.get("planning_error") is not None:
        return str(harness["planning_error"])
    if harness.get("error") is not None:
        return str(harness["error"])
    executed = harness.get("executed_request_count", 0)
    if executed > 0:
        statuses = harness.get("tool_results", [])
        if not any(t.get("status") == "ok" for t in statuses if isinstance(t, dict)):
            return "all tool requests failed"
    return None


def _count_successful_requests(tool_harness_path: str = "tool-harness.json") -> int:
    if not Path(tool_harness_path).exists():
        return 0
    try:
        harness = json.loads(Path(tool_harness_path).read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return 0
    return sum(
        1
        for t in harness.get("tool_results", [])
        if isinstance(t, dict) and t.get("status") == "ok"
    )


def _harness_requested_tools(tool_harness_path: str = "tool-harness.json") -> bool:
    if not Path(tool_harness_path).exists():
        return False
    try:
        harness = json.loads(Path(tool_harness_path).read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return False
    planned = harness.get("planned_request_count", 0)
    executed = harness.get("executed_request_count", 0)
    return planned > 0 or executed > 0


def apply_tool_harness_failure_enforcement(
    tool_harness_path: str = "tool-harness.json",
    output_path: str = "ai-output.json",
) -> tuple[bool, str]:
    """Override verdict to request_changes if tool harness planning or execution failed.

    Parameters
    ----------
    tool_harness_path : str
        Path to ``tool-harness.json``.
    output_path : str
        Path to ``ai-output.json`` (modified in place).

    Returns
    -------
    tuple[bool, str]
        (applied, reason) — reason is empty when not applied.
    """
    reason = _get_tool_harness_failure_reason(tool_harness_path)
    if not reason:
        return False, ""

    data = json.loads(Path(output_path).read_text(encoding="utf-8", errors="replace"))
    markdown = str(data.get("review_markdown") or "")

    markdown = (
        markdown
        + "\n\n## Tool Harness Failure\n"
        + f"The tool harness failed during planning or execution ({reason}). "
        + "This workflow is configured fail-closed for tool harness failures; "
        + "rerun after reducing tool planning context or fixing connectivity."
    )

    data["verdict"] = "request_changes"
    data["review_markdown"] = markdown
    reason = (
        f"Tool harness failure detected ({reason}). "
        + "The tool harness failed during planning or execution; "
        + "this workflow is configured fail-closed for tool harness failures."
    )
    Path(output_path).write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    return True, reason


def apply_tool_min_successful_enforcement(
    min_required: int,
    tool_harness_path: str = "tool-harness.json",
    output_path: str = "ai-output.json",
) -> tuple[bool, str]:
    """Override verdict to request_changes if fewer than ``min_required`` tool requests succeeded.

    Parameters
    ----------
    min_required : int
        Minimum number of successful tool requests required.
    tool_harness_path : str
        Path to ``tool-harness.json``.
    output_path : str
        Path to ``ai-output.json`` (modified in place).

    Returns
    -------
    tuple[bool, str]
        (applied, reason) — reason is empty when not applied.
    """
    successful = _count_successful_requests(tool_harness_path)
    if successful >= min_required:
        return False, ""

    data = json.loads(Path(output_path).read_text(encoding="utf-8", errors="replace"))
    markdown = str(data.get("review_markdown") or "")

    markdown = (
        markdown
        + "\n\n## Tool Harness Insufficient Evidence\n"
        + f"This workflow requires at least {min_required} successful tool requests, "
        + f"but only {successful} succeeded. Rerun after adjusting tool planning settings."
    )

    data["verdict"] = "request_changes"
    data["review_markdown"] = markdown
    reason = (
        f"Tool harness gathered insufficient evidence. "
        + f"This workflow requires at least {min_required} successful tool requests, "
        + f"but only {successful} succeeded."
    )
    Path(output_path).write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    return True, reason


def normalize_enforced_review_markdown(
    output_path: str = "ai-output.json",
    reasons: list[str] | None = None,
) -> None:
    """Add 'Final Recommendation: Request changes' banner when enforcement forced request_changes.

    Parameters
    ----------
    output_path : str
        Path to ``ai-output.json``.
    reasons : list[str] | None
        Specific enforcement reason strings to include in the banner.
    """
    data = json.loads(Path(output_path).read_text(encoding="utf-8", errors="replace"))
    verdict = data.get("verdict")
    markdown = str(data.get("review_markdown") or "")

    if verdict == "request_changes":
        markdown = re.sub(
            r"(?im)^(#{1,6}\s*)?Recommendation:\s*Approve\s*$",
            r"\1Model recommendation before enforcement: Approve",
            markdown,
        )
        if not markdown.lstrip().startswith("## Final Recommendation"):
            if reasons:
                reasons_bullet = "\n".join(f"- {r}" for r in reasons)
                banner = (
                    "## Final Recommendation\n"
                    "Request changes. The following enforcement check(s) require this PR "
                    "to be treated as blocking even if the model's initial review text was approving:\n\n"
                    f"{reasons_bullet}\n\n"
                    + markdown.lstrip()
                )
            else:
                banner = (
                    "## Final Recommendation\n"
                    "Request changes. One or more configured enforcement checks require this PR "
                    "to be treated as blocking even if the model's initial review text was approving.\n\n"
                    + markdown.lstrip()
                )
            markdown = banner

        data["review_markdown"] = markdown
        Path(output_path).write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")


def apply_verdict_policy(
    policy: str = "model",
    output_path: str = "ai-output.json",
) -> str:
    """Derive the verdict from structured findings when configured.

    ``model`` (default) leaves the model's verdict untouched. With
    ``findings_severity_gated`` the verdict is computed deterministically from
    the findings array: ``request_changes`` iff any blocker-severity finding
    exists, otherwise ``approve``. When the model produced no findings the
    policy falls back to the model verdict, so weaker models degrade to
    today's behaviour. Enforcement overlays (evidence blockers, tool-harness
    failure) run after this and can still force ``request_changes``.

    Returns the verdict source applied: ``"model"`` or ``"findings"``. The
    source is also recorded in the output JSON as ``verdict_source``.
    """
    data = json.loads(Path(output_path).read_text(encoding="utf-8", errors="replace"))
    findings = data.get("findings")
    source = "model"

    if (
        policy == "findings_severity_gated"
        and isinstance(findings, list)
        and findings
    ):
        blockers = [
            f
            for f in findings
            if isinstance(f, dict) and f.get("severity") == "blocker"
        ]
        derived = "request_changes" if blockers else "approve"
        if derived != data.get("verdict"):
            note = (
                f"\n\n_Verdict derived from structured findings "
                f"(verdict_policy=findings_severity_gated): "
                f"{len(blockers)} blocker finding(s) out of {len(findings)}; "
                f"model verdict was '{data.get('verdict')}'._"
            )
            data["review_markdown"] = str(data.get("review_markdown") or "") + note
        data["verdict"] = derived
        source = "findings"

    data["verdict_source"] = source
    Path(output_path).write_text(
        json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return source


def apply_all_enforcement(
    evidence_blocker_enabled: bool = False,
    tool_failure_enabled: bool = False,
    tool_min_successful: int = 0,
    evidence_path: str = "evidence-providers.json",
    tool_harness_path: str = "tool-harness.json",
    output_path: str = "ai-output.json",
) -> int:
    """Apply all configured enforcement rules in sequence.

    Parameters
    ----------
    evidence_blocker_enabled : bool
        Whether evidence blocker enforcement is enabled.
    tool_failure_enabled : bool
        Whether tool harness failure enforcement is enabled.
    tool_min_successful : int
        Minimum successful tool requests (0 = disabled).
    evidence_path, tool_harness_path, output_path : str
        File paths as documented above.

    Returns
    -------
    int
        Number of enforcement actions applied (0, 1, or 2).
    """
    applied = 0
    reasons: list[str] = []

    if evidence_blocker_enabled:
        ok, reason = apply_evidence_blocker_enforcement(evidence_path, output_path)
        if ok:
            applied += 1
            reasons.append(reason)

    if tool_failure_enabled:
        ok, reason = apply_tool_harness_failure_enforcement(tool_harness_path, output_path)
        if ok:
            applied += 1
            reasons.append(reason)
        elif tool_min_successful > 0:
            ok, reason = apply_tool_min_successful_enforcement(
                tool_min_successful, tool_harness_path, output_path
            )
            if ok:
                applied += 1
                reasons.append(reason)

    if applied > 0:
        normalize_enforced_review_markdown(output_path, reasons if reasons else None)

    return applied
