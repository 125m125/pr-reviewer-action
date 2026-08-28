"""Load bounded CI test results and expose them as review evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .evidence import EvidenceProvenance, EvidenceStore


_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_STATUSES = frozenset({"passed", "failed", "skipped", "errored", "xfailed", "unknown"})
_MAX_CASES = 2_000


def _path(value: object) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw:
        return ""
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:/", raw) or ".." in candidate.parts:
        return ""
    normalized = str(candidate)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized in {".", ".."}:
        return ""
    return normalized


def _text(value: object, limit: int = 4_000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _case(raw: Mapping[str, object], *, report: str, workflow: str, job: str) -> dict[str, object] | None:
    name = _text(raw.get("name") or raw.get("test_name"), 1_000)
    if not name:
        return None
    status = _text(raw.get("status") or raw.get("outcome"), 40).lower() or "unknown"
    if status not in _STATUSES:
        status = "unknown"
    result: dict[str, object] = {
        "name": name,
        "status": status,
        "file": _path(raw.get("file") or raw.get("path")),
        "message": _text(raw.get("message") or raw.get("failure") or raw.get("error")),
        "report": report,
        "workflow": workflow,
        "job": job,
    }
    line = raw.get("line")
    try:
        line_int = int(line) if line is not None else 0
    except (TypeError, ValueError):
        line_int = 0
    if line_int > 0:
        result["line"] = line_int
    return result


def load_test_results(
    path: str | Path,
    *,
    repository: str,
    head_sha: str,
) -> tuple[dict[str, object], ...]:
    """Load the normalized manifest emitted by a CI validation workflow.

    The manifest is intentionally data-only; this function never executes a
    report command or downloads an artifact.  It accepts either a top-level
    ``tests`` array or named ``reports`` containing test arrays.
    """
    if not repository.strip() or not _SHA_RE.fullmatch(head_sha.strip()):
        raise ValueError("test result binding requires repository and immutable head SHA")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("test results manifest must be an object")
    bound_repo = str(value.get("repository") or "").strip()
    bound_head = str(value.get("head_sha") or "").strip()
    if bound_repo and bound_repo != repository:
        raise ValueError("test results repository does not match the review")
    if bound_head and bound_head != head_sha:
        raise ValueError("test results head SHA does not match the review")

    raw_reports = value.get("reports")
    reports: list[Mapping[str, object]] = []
    if isinstance(raw_reports, list):
        reports.extend(item for item in raw_reports if isinstance(item, Mapping))
    elif isinstance(value.get("tests"), list):
        reports.append({"name": "ci", "tests": value["tests"]})
    results: list[dict[str, object]] = []
    for report in reports:
        raw_tests = report.get("tests")
        if not isinstance(raw_tests, list):
            continue
        report_name = _text(report.get("name") or report.get("id"), 200) or "ci"
        workflow = _text(report.get("workflow"), 200)
        job = _text(report.get("job"), 200)
        for raw in raw_tests:
            if not isinstance(raw, Mapping):
                continue
            parsed = _case(raw, report=report_name, workflow=workflow, job=job)
            if parsed is not None:
                results.append(parsed)
            if len(results) >= _MAX_CASES:
                return tuple(results)
    return tuple(results)


def seed_test_results(
    store: EvidenceStore,
    results: Iterable[Mapping[str, object]],
    *,
    repository: str,
    head_sha: str,
) -> tuple[str, ...]:
    """Seed normalized cases as immutable, typed ``test-result`` evidence."""
    if not isinstance(store, EvidenceStore):
        raise TypeError("store must be an EvidenceStore")
    seeded: list[str] = []
    for index, result in enumerate(tuple(results)[:_MAX_CASES], start=1):
        if not isinstance(result, Mapping):
            continue
        payload = dict(result)
        source = str(payload.get("file") or "").strip() or None
        record = store.add_tool_result(
            session_id="ci:test-results",
            tool="ci_test_results",
            arguments={
                "repository": repository,
                "head_sha": head_sha,
                "index": index,
                "name": payload.get("name", ""),
                **({"file": source} if source else {}),
            },
            result={"status": "ok", "test": payload},
            category="test-result",
            source=source,
            mime_type="application/json",
            provenance=EvidenceProvenance(
                head_sha=head_sha,
                source_classification="ci-test-result",
            ),
        )
        seeded.append(record.id)
    return tuple(seeded)


__all__ = ["load_test_results", "seed_test_results"]
