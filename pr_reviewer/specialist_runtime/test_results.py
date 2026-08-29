"""Load bounded CI test results and expose them as review evidence."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree

from .evidence import EvidenceProvenance, EvidenceStore


_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_STATUSES = frozenset({"passed", "failed", "skipped", "errored", "xfailed", "unknown"})
_MAX_CASES = 2_000
_MAX_JUNIT_XML_BYTES = 10 * 1024 * 1024
_MAX_JUNIT_ARCHIVE_BYTES = 50 * 1024 * 1024


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


def _junit_report(source: str, content: bytes) -> dict[str, object] | None:
    if (
        not content
        or len(content) > _MAX_JUNIT_XML_BYTES
        or b"<!DOCTYPE" in content.upper()
        or b"<!ENTITY" in content.upper()
    ):
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    tests: list[dict[str, object]] = []
    for node in root.iter("testcase"):
        name = _text(node.get("name"), 1_000)
        classname = _text(node.get("classname"), 1_000)
        if not name:
            continue
        failure = node.find("failure")
        error = node.find("error")
        skipped = node.find("skipped")
        detail = failure if failure is not None else (
            error if error is not None else skipped
        )
        status = (
            "failed" if failure is not None else
            "errored" if error is not None else
            "skipped" if skipped is not None else
            "passed"
        )
        test: dict[str, object] = {
            "name": f"{classname}::{name}" if classname else name,
            "status": status,
            "file": _path(node.get("file")),
            "message": _text(
                ((detail.get("message") if detail is not None else "") or
                 (detail.text if detail is not None else ""))
            ),
        }
        try:
            line = int(node.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        if line > 0:
            test["line"] = line
        tests.append(test)
    return {"name": source, "tests": tests} if tests else None


def build_junit_manifest(
    paths: Iterable[str | Path], *, repository: str, head_sha: str,
) -> dict[str, object]:
    """Normalize bounded JUnit XML files or ZIP artifacts for one immutable head."""
    if not repository.strip() or not _SHA_RE.fullmatch(head_sha.strip()):
        raise ValueError("test result binding requires repository and immutable head SHA")
    reports: list[dict[str, object]] = []
    total_archive_bytes = 0
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        total_archive_bytes += path.stat().st_size
        if total_archive_bytes > _MAX_JUNIT_ARCHIVE_BYTES:
            break
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for member in sorted(archive.infolist(), key=lambda item: item.filename):
                    if (
                        member.is_dir()
                        or not member.filename.casefold().endswith(".xml")
                        or member.file_size > _MAX_JUNIT_XML_BYTES
                    ):
                        continue
                    report = _junit_report(
                        f"{path.name}:{_path(member.filename)}",
                        archive.read(member),
                    )
                    if report is not None:
                        reports.append(report)
        elif path.suffix.casefold() == ".xml":
            report = _junit_report(path.name, path.read_bytes())
            if report is not None:
                reports.append(report)

    counts = {key: 0 for key in ("passed", "failed", "skipped", "errored")}
    total = 0
    ranked_cases: list[tuple[int, int, int]] = []
    for report_index, report in enumerate(reports):
        for test_index, test in enumerate(report["tests"]):
            status = str(test.get("status") or "unknown")
            ranked_cases.append((
                0 if status in {"failed", "errored"} else 1,
                report_index,
                test_index,
            ))
    selected_cases = {
        (report_index, test_index)
        for _priority, report_index, test_index
        in sorted(ranked_cases)[:_MAX_CASES]
    }
    bounded_reports: list[dict[str, object]] = []
    for report_index, report in enumerate(reports):
        tests = list(report["tests"])
        total += len(tests)
        for test in tests:
            status = str(test.get("status") or "unknown")
            if status in counts:
                counts[status] += 1
        selected = [
            test for test_index, test in enumerate(tests)
            if (report_index, test_index) in selected_cases
        ]
        report_counts = {
            key: sum(test.get("status") == key for test in tests)
            for key in counts
        }
        bounded_reports.append({
            **report,
            "tests": selected,
            "statistics": {
                "total": len(tests),
                "retained": len(selected),
                **report_counts,
            },
        })
    return {
        "repository": repository.strip(),
        "head_sha": head_sha.strip(),
        "reports": bounded_reports,
        "statistics": {
            "source_reports": len(reports),
            "total": total,
            "retained": len(selected_cases),
            **counts,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize bounded JUnit artifacts")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--unavailable-reason", default="")
    parser.add_argument("artifacts", nargs="*")
    args = parser.parse_args(argv)
    manifest = build_junit_manifest(
        args.artifacts, repository=args.repository, head_sha=args.head_sha,
    )
    if not manifest["statistics"]["retained"]:
        manifest["availability_reason"] = (
            args.unavailable_reason or "no parseable same-head JUnit test cases found"
        )
    Path(args.output).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    return 0


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


__all__ = ["build_junit_manifest", "load_test_results", "seed_test_results"]


if __name__ == "__main__":
    raise SystemExit(main())
