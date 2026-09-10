"""Assign known CI failures once, after the planner has settled its assignments."""

from collections import defaultdict
from dataclasses import replace
from fnmatch import fnmatchcase
from hashlib import sha256
import json

from .assignments import AssignmentPlan, _obligation_briefs
from .types import CoverageObligation


def assign_test_failure_triage(plan, results, components):
    def matches(path, patterns):
        return bool(path) and any(
            path == pattern or fnmatchcase(path, pattern)
            or fnmatchcase(path, pattern.replace("**/", ""))
            for pattern in patterns
        )

    component_paths = {str(c["id"]): tuple(c.get("paths", ())) for c in components}
    groups = defaultdict(list)
    for row in results:
        if row.get("status") not in {"failed", "errored"}:
            continue
        path = str(row.get("file") or "").replace("\\", "/")
        owners = sorted(k for k, patterns in component_paths.items() if matches(path, patterns))
        key = ("component", owners[0]) if owners else ("report", str(row.get("report") or "unknown"))
        groups[key].append(row)
    if not groups:
        return plan, ()
    ordered = sorted(groups.items())
    # Bound scheduling metadata, not indexed tests. Overflow still gets an owner.
    if len(ordered) > 8:
        ordered = ordered[:7] + [(("overflow", "remaining reports"), [
            row for _key, rows in ordered[7:] for row in rows
        ])]
    assignments = list(plan.assignments)
    assigned_counts = defaultdict(int)
    obligations = []
    unassigned = list(plan.unassigned_obligation_ids)
    reasons = list(plan.unassigned_obligation_reasons)
    for key, rows in ordered:
        files = {str(row.get("file") or "").replace("\\", "/") for row in rows} - {""}
        patterns = component_paths.get(key[1], ()) if key[0] == "component" else ()

        def rank(item):
            paths = (*item.seed_paths, *item.boundary_paths)
            direct = any(matches(path, paths) for path in files)
            component = any(matches(path, patterns) for path in paths)
            test_owner = any("test" in category.lower() for category in item.expected_evidence)
            return (not direct, not component, not test_owner,
                    assigned_counts[item.id], len(item.obligation_ids), item.id)

        target = min(assignments, key=rank) if assignments else None
        identifier = "obligation:ci-test-triage:" + sha256(json.dumps(key).encode()).hexdigest()[:16]
        reports = sorted({str(row.get("report") or "") for row in rows})
        query = {"name_regex": ".*", "status": "failed"}
        if len(reports) == 1 and reports[0] and len(reports[0]) <= 120:
            query["report"] = reports[0]
        examples = ", ".join(str(row.get("name") or "")[:50] for row in rows[:2])
        explanation = (
            f"Triage {len(rows)} failed/errored CI cases. Query read_test_results "
            f"{json.dumps(query)}; also query status=errored and paginate with offset. "
            "Determine PR-related, unrelated, environmental/flaky, or unresolved; "
            "failures alone are not findings. Examples: " + examples
        )[:500]
        obligation = CoverageObligation(
            identifier, "ci-test-triage", f"CI failures: {key[1]}"[:160],
            required_evidence_categories=("test-result",),
            satisfaction_predicates=("recorded_evidence",),
            explanation=explanation,
        )
        obligations.append(obligation)
        if target is None:
            unassigned.append(identifier)
            reasons.append((identifier, "No existing specialist available for CI failure triage"))
            continue
        assigned_counts[target.id] += 1
        index = assignments.index(target)
        assignments[index] = replace(
            target, obligation_ids=(*target.obligation_ids, identifier),
            primary_obligation_ids=(*(target.primary_obligation_ids or target.obligation_ids), identifier),
            obligation_briefs=(*target.obligation_briefs, *_obligation_briefs((obligation,))),
        )
    return AssignmentPlan(tuple(assignments), tuple(unassigned), tuple(reasons)), tuple(obligations)
