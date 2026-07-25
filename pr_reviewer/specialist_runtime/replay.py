"""Offline recorded-turn replay for the specialist review runtime.

This module is an adapter, not a second runtime.  It validates compact fixture
data, replaces only provider/network I/O, and drives the public
``ReviewController``/``SpecialistSession``/secure-web boundaries.  Acceptance
metrics and CLI exit policy remain in ``scripts.eval_harness``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence

from pr_reviewer.conversation import Conversation

from .adjudication import AdjudicatedReview, build_review_notes
from .budget import BudgetLedger, SessionLease
from .controller import FinalizerProposal, ReviewController, ReviewInputs
from .coverage import CoverageLedger, derive_obligations
from .evidence import EvidenceStore, canonical_evidence_key
from .model_gateway import ModelTurnResult
from .policy import RuntimeConfig, SourceRule, load_review_policy
from .replay_adversarial import run_failure_injections
from .session import SpecialistSession
from .types import (
    BudgetLimits,
    CandidateFinding,
    CoverageObligation,
    PhaseShares,
    ReviewNote,
)
from .web_evidence import (
    HttpRequest,
    HttpResponse,
    SearchCandidate,
    SecureFetcher,
    SourceDenied,
    SourcePolicy,
    discover,
    source_access_request,
)


_FIXTURE_FIELDS = {
    "schema_version", "id", "provider_scenario", "policy_file",
    "policy_input_version", "repository", "pr_number", "base_sha", "head_sha",
    "classification", "runtime", "topology", "representative_changes",
    "finding", "expected",
}
EXPECTED_FIELDS = {
    "deadline_sec", "obligation_ids", "mandatory_obligation_ids",
    "recipe_statuses", "finding_ids", "acceptable_unknowns",
    "max_model_turns", "max_tool_calls", "max_recoveries",
    "forbidden_public_text",
}
_FAILURE_SCENARIOS = {
    "no_progress_resume", "reconstruction", "planner_repair",
    "failed_critic", "deadline_cutoff", "completion_inversion",
    "note_anchor_race",
}


@dataclass(frozen=True)
class SpecialistReplayResult:
    fixture: Mapping[str, Any]
    artifact: Mapping[str, Any]
    expected: Mapping[str, Any]
    notes: tuple[ReviewNote, ...]
    observed: Mapping[str, Any]
    failures: Mapping[str, Mapping[str, Any]]
    unsupported_published_claims: tuple[str, ...]
    elapsed_simulated_sec: float


class _ReplayClock:
    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self._elapsed = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.started_at + self._elapsed

    @property
    def elapsed(self) -> float:
        with self._lock:
            return self._elapsed

    def advance(self, seconds: float = 1.0) -> None:
        with self._lock:
            self._elapsed += seconds


def _json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid or missing {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def validated_strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be an array of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must not contain duplicates")
    return value


def _load_fixture(
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    fixture = _json_file(root / "fixture.json", "replay fixture")
    missing = sorted(_FIXTURE_FIELDS - set(fixture))
    if missing:
        raise ValueError("fixture missing required fields: " + ", ".join(missing))
    if fixture["schema_version"] != 1:
        raise ValueError("replay fixture schema_version must be 1")
    expected = fixture.get("expected")
    if not isinstance(expected, dict):
        raise ValueError("fixture expected must be an object")
    missing = sorted(EXPECTED_FIELDS - set(expected))
    if missing:
        raise ValueError(
            "fixture expected missing required fields: " + ", ".join(missing)
        )
    obligation_ids = validated_strings(
        expected["obligation_ids"], "expected obligation_ids",
    )
    mandatory_ids = validated_strings(
        expected["mandatory_obligation_ids"],
        "expected mandatory_obligation_ids",
    )
    if not obligation_ids or not mandatory_ids:
        raise ValueError("expected obligation identifiers must not be empty")
    if not set(mandatory_ids).issubset(obligation_ids):
        raise ValueError("mandatory_obligation_ids must be a subset of obligation_ids")
    for name in (
        "deadline_sec", "max_model_turns", "max_tool_calls", "max_recoveries",
    ):
        if (
            isinstance(expected[name], bool)
            or not isinstance(expected[name], int)
            or expected[name] <= 0
        ):
            raise ValueError(f"expected {name} must be a positive integer")
    if not isinstance(expected["recipe_statuses"], dict):
        raise ValueError("expected recipe_statuses must be an object")
    for name in ("finding_ids", "acceptable_unknowns", "forbidden_public_text"):
        validated_strings(expected[name], f"expected {name}")

    relative_policy = Path(str(fixture["policy_file"]))
    if relative_policy.is_absolute() or ".." in relative_policy.parts:
        raise ValueError("fixture policy_file must stay within the fixture directory")
    policy_path = (root / relative_policy).resolve()
    try:
        policy_path.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "fixture policy_file must stay within the fixture directory"
        ) from exc
    policy_raw = _json_file(policy_path, "replay policy")
    if policy_raw.get("version") != fixture["policy_input_version"]:
        raise ValueError("fixture policy_input_version does not match the policy file")

    changes = fixture.get("representative_changes")
    if not isinstance(changes, list) or not changes or any(
        not isinstance(item, dict)
        or set(item) != {"path", "language", "role"}
        for item in changes
    ):
        raise ValueError(
            "representative_changes must contain path/language/role objects"
        )
    changed_files = fixture.get("topology", {}).get("changed_files", [])
    if {item["path"] for item in changes} != set(changed_files):
        raise ValueError(
            "representative_changes must exactly describe topology changed_files"
        )
    for item in changes:
        path = (root / item["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("representative change escapes fixture directory") from exc
        if not path.is_file():
            raise ValueError(f"representative change is missing: {item['path']}")

    turns = _json_file(root.parent / "provider-turns.json", "recorded provider turns")
    if turns.get("schema_version") != 1:
        raise ValueError("recorded provider turns schema_version must be 1")
    if turns.get("api_format") != "openai-chat-completions":
        raise ValueError(
            "recorded provider turns api_format must be openai-chat-completions"
        )
    if not isinstance(turns.get("scenarios"), dict):
        raise ValueError("recorded provider turns have an invalid schema")
    scenario = turns["scenarios"].get(fixture["provider_scenario"])
    if not isinstance(scenario, dict):
        raise ValueError("fixture provider_scenario is not recorded")
    for role in ("planner", "specialist", "critic", "finalizer"):
        if not isinstance(scenario.get(role), list) or not scenario[role]:
            raise ValueError(f"recorded scenario missing {role} turns")
    missing_failures = sorted(_FAILURE_SCENARIOS - set(turns["scenarios"]))
    if missing_failures:
        raise ValueError(
            "recorded provider turns missing failure scenarios: "
            + ", ".join(missing_failures)
        )
    for name in sorted(_FAILURE_SCENARIOS):
        failure = turns["scenarios"][name]
        if not isinstance(failure, dict):
            raise ValueError(f"recorded failure scenario {name} must be an object")
    return fixture, expected, turns


def _runtime(raw: Mapping[str, Any]) -> RuntimeConfig:
    required = {
        "deadline_sec", "request_timeout_sec", "concurrency", "max_sessions",
        "max_followup_sessions", "model_turns", "tool_calls", "recoveries",
        "phase_shares",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError("runtime fixture missing fields: " + ", ".join(missing))
    shares = raw["phase_shares"]
    if not isinstance(shares, dict) or set(shares) != {
        "planning", "initial", "followup", "finalization",
    }:
        raise ValueError("runtime phase_shares are invalid")
    return RuntimeConfig(
        review_deadline_sec=int(raw["deadline_sec"]),
        model_request_timeout_sec=int(raw["request_timeout_sec"]),
        phase_shares=PhaseShares(**shares),
        concurrency=int(raw["concurrency"]),
        max_sessions=int(raw["max_sessions"]),
        max_followup_sessions=int(raw["max_followup_sessions"]),
        session_limits=BudgetLimits(
            model_turns=int(raw["model_turns"]),
            tool_calls=int(raw["tool_calls"]),
            recoveries=int(raw["recoveries"]),
        ),
    )


def _tool_spec(
    obligation: CoverageObligation,
) -> tuple[str, dict[str, str]]:
    path = next(iter(obligation.seed_hints or obligation.scope), "")
    category = next(iter(obligation.required_evidence_categories), "")
    if not path or not category:
        raise ValueError(
            f"replay obligation lacks deterministic evidence: {obligation.id}"
        )
    return "read_file", {
        "path": path,
        "evidence_category": category,
        "obligation_id": obligation.id,
    }


def _turn(
    *,
    text: str = "",
    tool_calls: Sequence[Mapping[str, Any]] = (),
    finish_reason: str = "stop",
) -> ModelTurnResult:
    return ModelTurnResult(
        response={},
        tool_calls=tuple(dict(item) for item in tool_calls),
        text=text,
        text_source="content" if text else "none",
        finish_reason=finish_reason,
        usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def _checkpoint_text(inspected: list[str]) -> str:
    return json.dumps({
        "inspected": inspected,
        "unresolved": [],
        "hypotheses": [],
        "candidate_finding_ids": [],
        "invariants_evaluated": [],
        "unknowns": [],
        "proposed_next_actions": [],
    }, sort_keys=True)


class _SpecialistGateway:
    def __init__(
        self,
        assignment: object,
        obligations: Sequence[CoverageObligation],
        turns: Sequence[Mapping[str, Any]],
        clock: _ReplayClock,
    ) -> None:
        assigned = set(getattr(assignment, "obligation_ids", ()))
        self.obligations = tuple(item for item in obligations if item.id in assigned)
        self.turns = tuple(turns)
        self.clock = clock
        self.index = 0

    def complete(self, request: object) -> ModelTurnResult:
        del request
        if self.index >= len(self.turns):
            raise AssertionError("recorded specialist provider turns were exhausted")
        turn = self.turns[self.index]
        self.index += 1
        self.clock.advance()
        kind = turn.get("kind")
        if kind == "cover_assignment":
            calls = []
            for index, obligation in enumerate(self.obligations, start=1):
                name, arguments = _tool_spec(obligation)
                calls.append({
                    "id": f"call-{index}",
                    "name": name,
                    "arguments": json.dumps(arguments, sort_keys=True),
                })
            return _turn(tool_calls=calls, finish_reason="tool_calls")
        if kind == "checkpoint":
            inspected = sorted({
                _tool_spec(item)[1]["path"] for item in self.obligations
            })
            return _turn(text=_checkpoint_text(inspected))
        if kind == "final":
            return _turn(text=json.dumps({
                "summary": str(turn.get("summary") or "Replay complete."),
                "recommendation": str(turn.get("recommendation") or "approve"),
                "candidate_finding_ids": [],
                "evidence_ids": [],
                "unknowns": [],
            }, sort_keys=True))
        raise AssertionError(f"unsupported recorded specialist turn: {kind}")


class _Planner:
    def __init__(
        self, turns: Sequence[Mapping[str, Any]], clock: _ReplayClock,
    ) -> None:
        self.turns = tuple(turns)
        self.clock = clock
        self.calls: list[str] = []

    def plan(self, request: object) -> Mapping[str, Any]:
        del request
        self.calls.append("plan")
        self.clock.advance()
        turn = self.turns[0]
        if turn.get("kind") != "json" or not isinstance(turn.get("value"), dict):
            raise ValueError("first recorded planner turn must be a JSON value")
        return turn["value"]

    def repair(self, request: object) -> Mapping[str, Any]:
        self.calls.append("repair")
        self.clock.advance()
        if len(self.turns) != 2:
            raise ValueError("recorded planner must contain exactly one repair turn")
        turn = self.turns[1]
        if turn.get("kind") != "assignment_from_obligations":
            raise ValueError("planner repair turn must create a bounded assignment")
        obligations = tuple(
            item for item in getattr(request, "context")["obligations"]
            if item.mandatory and item.required_evidence_categories
        )
        ranks = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        priority = min(
            (item.risk_tier for item in obligations),
            key=lambda value: ranks.get(value, 2),
            default="normal",
        )
        paths = sorted({
            path for item in obligations for path in (*item.scope, *item.seed_hints)
        })
        return {"assignments": [{
            "id": turn["id"],
            "title": turn["title"],
            "objective": turn["objective"],
            "obligation_ids": [item.id for item in obligations],
            "lenses": list(turn["lenses"]),
            "seed_paths": paths,
            "boundary_paths": [],
            "expected_evidence": sorted({
                category
                for item in obligations
                for category in item.required_evidence_categories
            }),
            "estimated_turns": 3,
            "priority": priority,
            "overlap_justification": "",
        }]}


class _Critic:
    def __init__(
        self, turns: Sequence[Mapping[str, Any]], clock: _ReplayClock,
    ) -> None:
        self.turn = turns[0]
        self.clock = clock

    def adjudicate(self, request: object) -> Mapping[str, Any]:
        self.clock.advance()
        if self.turn.get("kind") == "raise":
            raise RuntimeError(
                str(self.turn.get("message") or "recorded critic failure")
            )
        return {"decisions": [
            {"candidate_id": item.candidate_id, "action": "keep"}
            for item in getattr(request, "context")["candidates"]
        ]}


class _Finalizer:
    def __init__(
        self,
        turns: Sequence[Mapping[str, Any]],
        clock: _ReplayClock,
        fixture: Mapping[str, Any],
    ) -> None:
        self.turn = turns[0]
        self.clock = clock
        self.fixture = fixture

    def finalize(self, request: object) -> FinalizerProposal:
        del request
        self.clock.advance()
        if self.turn.get("kind") != "sparse_handoff":
            raise ValueError("recorded finalizer turn must be sparse_handoff")
        return FinalizerProposal(
            component_ids=tuple(
                str(item["id"])
                for item in self.fixture["topology"]["components"]
                if isinstance(item, dict) and item.get("id")
            ),
            recipe_ids=tuple(sorted(
                self.fixture["expected"]["recipe_statuses"],
            )),
        )


def _executor(
    root: Path,
    evidence: EvidenceStore,
    session_id: str,
    clock: _ReplayClock,
):
    def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        clock.advance(0.1)
        path = (root / Path(str(arguments.get("path", "")))).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return {"tool": name, "status": "error", "error": "path escaped fixture"}
        if name != "read_file" or not path.is_file():
            return {"tool": name, "status": "error", "error": "fixture file missing"}
        result = {
            "tool": name,
            "status": "ok",
            "result": {"content": path.read_text(encoding="utf-8")},
        }
        evidence.add_tool_result(
            session_id=session_id,
            tool=name,
            arguments=arguments,
            result=result,
            category=str(arguments.get("evidence_category") or "tool-result"),
            model_identity="recorded-specialist",
        )
        return result

    return execute


def _candidate(
    fixture: Mapping[str, Any],
    root: Path,
    obligations: Sequence[CoverageObligation],
) -> CandidateFinding:
    raw = fixture["finding"]
    obligation = next((
        item for item in obligations
        if item.subject == raw["obligation_subject"]
        and raw["evidence_category"] in item.required_evidence_categories
    ), None)
    if obligation is None:
        raise ValueError("fixture finding does not resolve to an obligation")
    _, arguments = _tool_spec(obligation)
    result = {
        "tool": "read_file",
        "status": "ok",
        "result": {
            "content": (root / arguments["path"]).read_text(encoding="utf-8"),
        },
    }
    evidence_id = canonical_evidence_key("read_file", arguments, result)
    return CandidateFinding(
        candidate_id=raw["id"],
        root_cause_fingerprint="root:" + raw["id"],
        claim=raw["claim"],
        affected_location=raw["affected_location"],
        causal_chain=raw["causal_chain"],
        severity=raw["severity"],
        category=raw["category"],
        supporting_evidence_ids=(evidence_id,),
        related_obligation_ids=(obligation.id,),
        collector_session_id="fixture-input",
        model_identity="recorded-specialist",
        confidence_rationale="The retained producer/consumer evidence is direct.",
        user_visible_consequence=raw["user_visible_consequence"],
        manual_validation=raw["manual_validation"],
    )


def budget_history(
    artifact: Mapping[str, Any],
) -> dict[str, list[dict[str, int]]]:
    history: dict[str, list[dict[str, int]]] = {}
    for event in artifact.get("events", []):
        if not isinstance(event, Mapping) or event.get("kind") != "budget_changed":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        usage, session_id = payload.get("usage"), payload.get("session_id")
        if isinstance(usage, Mapping) and isinstance(session_id, str):
            history.setdefault(session_id, []).append({
                key: int(usage.get(key, 0))
                for key in (
                    "model_turns", "tool_calls", "recoveries",
                    "input_tokens", "output_tokens",
                )
            })
    return history


def replay_fixture(fixture_dir: Path | str) -> SpecialistReplayResult:
    """Replay recorded turns through the real controller/session/artifact path."""
    root = Path(fixture_dir).resolve()
    fixture, expected_file, turns = _load_fixture(root)
    policy = load_review_policy(root / fixture["policy_file"])
    obligations = derive_obligations(
        fixture["topology"], fixture["classification"], policy,
    )
    if [item.id for item in obligations] != expected_file["obligation_ids"]:
        raise ValueError(
            "fixture expected obligation_ids differ from deterministic derivation"
        )
    if [
        item.id for item in obligations if item.mandatory
    ] != expected_file["mandatory_obligation_ids"]:
        raise ValueError(
            "fixture expected mandatory_obligation_ids differ from derivation"
        )
    scenario = turns["scenarios"][fixture["provider_scenario"]]
    clock = _ReplayClock()
    planner = _Planner(scenario["planner"], clock)
    runtime = _runtime(fixture["runtime"])
    candidate = _candidate(fixture, root, obligations)

    def session_factory(
        assignment: object,
        lease: SessionLease,
        snapshot: object,
        evidence: EvidenceStore,
        coverage: CoverageLedger,
        session_obligations: Sequence[CoverageObligation],
        session_id: str,
    ) -> SpecialistSession:
        del snapshot
        return SpecialistSession(
            session_id=session_id,
            assignment=assignment,
            conversation=Conversation(system="Recorded specialist replay."),
            gateway=_SpecialistGateway(
                assignment, session_obligations, scenario["specialist"], clock,
            ),
            execute_tool=_executor(root, evidence, session_id, clock),
            evidence_store=evidence,
            coverage=coverage,
            budget=BudgetLedger(runtime.session_limits),
            lease=lease,
            request_timeout_sec=runtime.model_request_timeout_sec,
            max_tokens=1024,
            max_context_tokens=24_000,
        )

    with tempfile.TemporaryDirectory(prefix="specialist-replay-") as temp_dir:
        controller = ReviewController(
            planner=planner,
            session_factory=session_factory,
            critic=_Critic(scenario["critic"], clock),
            finalizer=_Finalizer(scenario["finalizer"], clock, fixture),
            clock=clock,
            artifact_output_root=Path(temp_dir),
        )
        result = controller.run(ReviewInputs(
            repository=fixture["repository"],
            pr_number=int(fixture["pr_number"]),
            base_sha=fixture["base_sha"],
            head_sha=fixture["head_sha"],
            topology=fixture["topology"],
            classification=fixture["classification"],
            policy=policy,
            config=runtime,
            changed_files=tuple(fixture["topology"]["changed_files"]),
            artifact_path="specialist-review-artifact.json",
            allow_approve=True,
            publishing_mode="review_comment",
            model_verdict="approve",
            candidate_findings=(candidate,),
            pr_metadata={"title": "Recorded multilingual PR replay"},
            adapter_configuration={"provider": "recorded-offline"},
        ))
    artifact = json.loads(json.dumps(result.artifact, ensure_ascii=False))
    handoff = str(artifact.get("handoff", {}).get("markdown", ""))
    unsupported = tuple(
        marker for marker in expected_file["forbidden_public_text"]
        if marker.casefold() in handoff.casefold()
    )
    expected = {**expected_file, "head_sha": fixture["head_sha"]}
    observed = {
        "unsupported_public_claims": unsupported,
        "unsafe_fetch_attempts": 0,
        "source_denials": 0,
        "source_access_requests": 0,
        "elapsed_simulated_sec": round(clock.elapsed, 6),
        "budget_history": budget_history(artifact),
    }
    return SpecialistReplayResult(
        fixture=fixture,
        artifact=artifact,
        expected=expected,
        notes=result.notes,
        observed=observed,
        failures=run_failure_injections(
            artifact, planner.calls, turns["scenarios"],
        ),
        unsupported_published_claims=unsupported,
        elapsed_simulated_sec=clock.elapsed,
    )


def replay_web_policy_fixture(fixture_dir: Path | str) -> dict[str, Any]:
    """Replay discovery/fetch policy with deterministic, credential-free I/O."""
    root = Path(fixture_dir).resolve()
    fixture = _json_file(root / "fixture.json", "web replay fixture")
    required = {
        "schema_version", "id", "approved_source", "unapproved_source",
        "redirect_escape", "source_rule", "source_access",
    }
    missing = sorted(required - set(fixture))
    if missing:
        raise ValueError("web fixture missing required fields: " + ", ".join(missing))
    if fixture["schema_version"] != 1:
        raise ValueError("web replay fixture schema_version must be 1")
    rule = fixture["source_rule"]
    policy = SourcePolicy((
        SourceRule(
            host=rule["host"],
            path_prefixes=(rule["path_prefix"],),
            classification="official",
        ),
    ))

    class Provider:
        def search(self, query: str, *, limit: int):
            del query
            return tuple(
                SearchCandidate(**{
                    key: fixture[name][key]
                    for key in ("title", "url", "snippet")
                })
                for name in ("approved_source", "unapproved_source")
            )[:limit]

    discovery = discover(
        "runtime compatibility",
        Provider(),
        policy,
        search_scan_limit=5,
        tool_max_search_results=5,
    )

    class Transport:
        def __init__(self) -> None:
            self.requests: list[str] = []

        def request(self, request: HttpRequest) -> HttpResponse:
            self.requests.append(request.url)
            if request.url == fixture["redirect_escape"]["url"]:
                return HttpResponse(
                    302,
                    {
                        "location": fixture["redirect_escape"]["location"],
                        "content-type": "text/plain",
                    },
                    fixture["redirect_escape"]["body"].encode("utf-8"),
                )
            return HttpResponse(
                200,
                {"content-type": "text/plain"},
                fixture["approved_source"]["body"].encode("utf-8"),
            )

    transport = Transport()
    fetcher = SecureFetcher(
        policy,
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
        timeout=5,
        max_bytes=4096,
        clock=lambda: 1_700_000_000.0,
    )
    fetched = fetcher.fetch(fixture["approved_source"]["url"])
    redirect_denied = False
    try:
        fetcher.fetch(fixture["redirect_escape"]["url"])
    except SourceDenied:
        redirect_denied = True
    request = source_access_request(
        discovery.unapproved[0],
        fixture["source_access"]["obligation_id"],
        fixture["source_access"]["purpose"],
        fixture["source_access"]["authority_reason"],
    )
    request_obligation = CoverageObligation(
        obligation_id=fixture["source_access"]["obligation_id"],
        origin="replay-source-policy",
        subject=request.host,
        required_evidence_categories=("external-source",),
    )
    request_note = build_review_notes(
        AdjudicatedReview(),
        EvidenceStore(),
        obligations={request_obligation.id: request_obligation},
        changed_files=(),
        source_access_requests=(request,),
    )[0]
    return {
        "approved_fetches": [fetched.provenance.final_url],
        "approved_evidence": fetched.as_dict(),
        "unapproved": discovery.as_dict()["unapproved"],
        "source_denials": len(discovery.unapproved) + int(redirect_denied),
        "unsafe_fetch_attempts": sum(
            1 for url in transport.requests if "evil.example.net" in url
        ),
        "source_access_requests": 1,
        "request_note": {
            "kind": request_note.kind.value,
            "fingerprint": request_note.fingerprint,
            "markdown": request_note.markdown,
            "related_obligation_ids": list(
                request_note.related_obligation_ids,
            ),
            "file": request_note.file,
            "line": request_note.line,
        },
    }
