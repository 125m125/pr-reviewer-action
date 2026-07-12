"""Native tool-calling loop driver (#203, umbrella #197 §1 item 3/7).

Drives an agentic exchange against a tool-capable model: send the corpus +
tool schemas, execute the tool calls the model returns, append the results,
and repeat until the model stops calling tools or a budget runs out.

Each turn can be streamed (``stream=True``): the injected ``post_fn`` is
responsible for reassembling the SSE deltas (via
``pr_reviewer.sse_reassembler``) back into the non-streaming response shape
this module parses, so the driver itself stays format-agnostic. Streaming
restores the long-request timeout protection (Cloudflare's 100s edge timer
etc.) that a blocking POST through a proxy loses on long thinking-model
turns; the non-streamed request stays available as the per-turn fallback
the transport falls back to when a stream can't be reassembled (#204).

The module is deliberately I/O-free: the HTTP POST and the tool execution
are injected callables, so the whole loop is unit-testable against scripted
conversations without a model server. ``scripts/run_tool_harness.py`` owns
the real wiring (curl transport + the existing read-only executor with its
allowlists, caps, and timeouts — none of which change here).

Reliability posture (issue #203 comment, Gemma-4-26B-A4B at Tau2 ≈68%):
the loop budgets for repair instead of assuming competence. Malformed
arguments come back as error tool-results the model can react to, duplicate
calls are answered from a dedup note without burning budget, every call id
the model issues gets *some* result before the next request (the
``Conversation.open_tool_call_ids`` contract), and hard caps bound rounds,
total calls, and wall clock. A model that never calls tools at all is
reported as ``degraded`` so the caller can fall back to a corpus-only
review (the plan_execute planner fallback was removed in #304).
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .conversation import Conversation

# Stop reasons (LoopOutcome.stop_reason)
STOP_MODEL_DONE = "model-stopped"
STOP_NO_TOOL_CALLS = "no-tool-calls"
STOP_MAX_ROUNDS = "max-rounds"
STOP_BUDGET = "tool-call-budget-exhausted"
STOP_WALL_CLOCK = "wall-clock-exceeded"
STOP_REQUEST_ERROR = "request-error"
STOP_TRUNCATED = "truncated-turn"
STOP_CONTEXT_BUDGET = "context-budget-exhausted"
STOP_UNEXECUTED_TEXTUAL_TOOL_INTENT = "unexecuted-textual-tool-intent"
STOP_STAGNATION = "no-progress-stagnation"
STOP_REPETITIVE_TEXT = "repetitive-assistant-text"

_TEXTUAL_TOOL_REPAIR_NOTE = (
    "Your previous response contained textual tool-call markup, but the API "
    "returned no native tool_calls, so nothing was executed. If that evidence "
    "is still needed, issue the calls now through the provided native tool API. "
    "Do not write or repeat textual tool-call markup. If it is no longer needed, "
    "state that explicitly and finish the investigation."
)

# Synthetic result bodies. These are model-facing: they must explain the
# refusal in one sentence so a self-correcting model has something to act on.
_STAGNATION_NOTE = (
    "Exploration was stopped because recent rounds requested no new evidence. "
    "Do not request more tools; synthesize the report from the existing evidence."
)
_BUDGET_NOTE = (
    "Tool-call budget exhausted: this call was not executed. "
    "Finish the analysis with the evidence you already have."
)


@dataclass
class LoopBudgets:
    """Hard stop conditions. The driver owns these; Conversation's token
    helpers are advisory (see pr_reviewer/conversation.py module docs)."""

    max_tool_calls: int = 4  # total executed calls across rounds (TOOL_MAX_REQUESTS)
    max_rounds: int = 3  # model round-trips (TOOL_MAX_ROUNDS)
    wall_clock_sec: float = 120.0  # whole-loop ceiling (TOOL_LOOP_WALL_CLOCK_SEC)
    # When the conversation outgrows this, the oldest tool results are
    # compacted before the next request (newest results stay intact) — by a
    # model-generated digest when a summarizer is wired, else blunt truncation.
    max_conversation_tokens: int = 24000
    truncated_result_bytes: int = 2000
    # Results kept verbatim when summarizing the rest (the model is actively
    # reasoning over the newest evidence).
    summarize_keep_newest: int = 2
    model_context_tokens: int = 0
    max_textual_tool_repairs: int = 1
    max_total_textual_tool_repairs: int = 3
    max_consecutive_no_progress_rounds: int = 2
    max_repeated_call_sets: int = 3
    max_truncation_continuations: int = 1


def adaptive_loop_budgets(
    max_rounds: int,
    max_tool_calls: int,
    wall_clock_sec: float,
    *,
    review_route: str = "primary",
    risk_flag_count: int = 0,
) -> "LoopBudgets":
    """Apply the documented legacy mapping of two planning turns per round.

    Positive configured call and time limits are used exactly, with no hidden
    cap or route-dependent reduction.

    The budget is the SAME on every route — the route selects the MODEL, never
    the tool budget. An earlier version shallow-capped the primary route (then
    misnamed "fast") on low-risk PRs to "save budget on a trivial diff", but the
    loop already self-limits (it stops as soon as the model stops calling
    tools), so the cap never saved cost on trivial PRs — it only starved the
    PRs that genuinely need a multi-hop chain (e.g. reading a deployed version,
    then verifying it against a host platform's compatibility matrix). The
    primary model is fully capable; don't ration its evidence-gathering.
    ``review_route``/``risk_flag_count`` are retained for signature stability
    and possible future heuristics.
    """
    if max_rounds <= 0 or max_tool_calls <= 0 or wall_clock_sec <= 0:
        raise ValueError("native-loop budgets must be positive")
    rounds = max_rounds * 2
    return LoopBudgets(
        max_tool_calls=max_tool_calls,
        max_rounds=rounds,
        wall_clock_sec=float(wall_clock_sec),
    )


@dataclass
class ExecutedCall:
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]  # executor shape: {"tool", "status", "result"}


@dataclass
class LoopOutcome:
    executed: list[ExecutedCall] = field(default_factory=list)
    rounds: int = 0
    tool_calls_issued: int = 0  # everything the model asked for, incl. refused
    stop_reason: str = STOP_NO_TOOL_CALLS
    final_text: str = ""
    # True when the model never issued a single tool call: the caller degrades
    # to a corpus-only review (the plan_execute planner fallback was removed in #304).
    degraded: bool = False
    error: str = ""
    final_text_source: str = "none"
    finish_reasons: list[str] = field(default_factory=list)
    text_sources: list[str] = field(default_factory=list)
    planning_turns_attempted: int = 0
    calls_executed: int = 0
    calls_rejected: int = 0
    calls_duplicated: int = 0
    calls_malformed: int = 0
    truncation_retries: int = 0
    compaction_runs: int = 0
    compaction_tokens_removed: int = 0
    textual_tool_intent_detected: bool = False
    textual_tool_intent_markers: list[str] = field(default_factory=list)
    textual_tool_repair_attempts: int = 0
    consecutive_textual_tool_repair_attempts: int = 0
    textual_tool_repaired: bool = False
    textual_tool_unexecuted: bool = False
    duplicate_only_rounds: int = 0
    no_progress_rounds: int = 0
    max_consecutive_no_progress: int = 0
    repeated_call_set_max: int = 0
    stagnation_stop_reason: str = ""
    preserved_truncated_bytes: int = 0
    preserved_truncated_tokens: int = 0
    continuation_attempts: int = 0
    terminal_synthesis_recovered: bool = False
    repetitive_text_detected: bool = False


def detect_textual_tool_intent(text: str) -> list[str]:
    """Return stable marker identifiers, never parsed tools or arguments.

    Detection is deliberately limited to paired structured markup. Prose about
    a "tool call" is not an execution protocol and must not trigger repair.
    One identifier is returned per complete block so callers can report a
    bounded count without retaining or logging the untrusted text.
    """
    if not isinstance(text, str) or not text:
        return []
    blocks = re.findall(r"<tool_call\b[^>]*>.*?</tool_call\s*>", text, re.I | re.S)
    return ["qwen_xml_tool_call"] * len(blocks)


def effective_intermediate_text(
    message: dict[str, Any], api_format: str
) -> tuple[str, str]:
    """Return safe internal assistant text and its source for a loop turn.

    OpenAI reasoning is used only when ordinary content is blank. Callers must
    not use this helper for final verdict parsing or publish fallback text.
    """
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content, "content"
    if api_format == "openai":
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning, "reasoning_fallback"
    return "", "none"


def extract_intermediate_turn(
    response: dict[str, Any], api_format: str
) -> tuple[list[dict[str, Any]], str, str, str]:
    """Return calls, effective text, text source, and provider finish reason."""
    calls, text = extract_tool_calls(response, api_format)
    finish_reason = ""
    source = "content" if text.strip() else "none"
    if api_format == "openai":
        choices = response.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        if not isinstance(message, dict):
            message = {}
        text, source = effective_intermediate_text(message, api_format)
        finish_reason = str(choice.get("finish_reason") or "")
    else:
        finish_reason = str(response.get("stop_reason") or "")
    return calls, text, source, finish_reason


def extract_tool_calls(
    response: dict[str, Any], api_format: str
) -> tuple[list[dict[str, Any]], str]:
    """Pull (tool_calls, text) out of a non-streaming chat response.

    Returned calls are in the flat ``{"id", "name", "arguments"}`` shape that
    ``Conversation.add_assistant_tool_calls`` accepts, with ``arguments``
    kept as an opaque JSON string per the #233 contract. Anthropic
    ``tool_use`` inputs are serialised once at this boundary.
    """
    calls: list[dict[str, Any]] = []
    text_parts: list[str] = []

    if api_format == "anthropic":
        content = response.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    call_id = block.get("id")
                    name = block.get("name")
                    if not isinstance(call_id, str) or not isinstance(name, str):
                        continue
                    raw_input = block.get("input")
                    try:
                        arguments = json.dumps(
                            raw_input if raw_input is not None else {},
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    except (TypeError, ValueError):
                        arguments = str(raw_input)
                    calls.append({"id": call_id, "name": name, "arguments": arguments})
        return calls, "".join(text_parts)

    # OpenAI format
    choices = response.get("choices")
    message: dict[str, Any] = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        maybe = choices[0].get("message")
        if isinstance(maybe, dict):
            message = maybe
    if isinstance(message.get("content"), str):
        text_parts.append(message["content"])
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            call_id = raw.get("id")
            name = fn.get("name") or raw.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            args = fn.get("arguments")
            if args is None:
                args = raw.get("arguments")
            if not isinstance(args, str):
                try:
                    args = json.dumps(
                        args if args is not None else {},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                except (TypeError, ValueError):
                    args = str(args)
            calls.append({"id": call_id, "name": name, "arguments": args})
    return calls, "".join(text_parts)


def _request_key(name: str, args: dict[str, Any]) -> str:
    # Mirrors scripts/run_tool_harness.py request_key so dedup behaves the
    # same in both harness modes.
    return f"{name}:{json.dumps(args, sort_keys=True, separators=(',', ':'))}"


def _normalise_assistant_text(text: str) -> str:
    paragraphs = [re.sub(r"\s+", " ", item).strip().lower()
                  for item in re.split(r"\n\s*\n", text or "")]
    return "\n\n".join(item for item in paragraphs if item)


def repetitive_assistant_text(text: str, previous: str = "") -> bool:
    """Detect only obvious repetition; this intentionally does no semantic judging."""
    current = _normalise_assistant_text(text)
    if not current:
        return False
    if previous and current == _normalise_assistant_text(previous) and len(current) >= 80:
        return True
    if len(current) < 600:
        return False
    paragraphs = [item for item in current.split("\n\n") if len(item) >= 80]
    if len(paragraphs) < 3:
        return False
    counts: dict[str, int] = {}
    for item in paragraphs:
        counts[item] = counts.get(item, 0) + 1
    repeated = max((len(item) * count for item, count in counts.items() if count >= 3), default=0)
    return repeated >= int(len(current) * 0.6)


def drive_tool_loop(
    conversation: Conversation,
    post_fn: Callable[[dict[str, Any]], dict[str, Any]],
    execute_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    *,
    api_format: str,
    model: str,
    budgets: LoopBudgets | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    stream: bool = False,
    tokens_param: str = "max_tokens",
    reasoning_effort: str | None = None,
    cache_prefix: bool = False,
    summarize_fn: Callable[[str], str] | None = None,
    time_fn: Callable[[], float] = time.monotonic,
) -> LoopOutcome:
    """Run the agentic loop until the model stops or a budget hits.

    ``post_fn`` takes a wire-ready request payload and returns the parsed
    response JSON (raising on transport failure). When ``stream`` is set the
    payload carries ``stream: true`` and ``post_fn`` owns SSE reassembly,
    handing back the same non-streaming response shape. ``execute_fn`` takes
    ``(tool_name, args)`` and returns the executor result dict
    ``{"tool", "status", "result"}`` — in production this is
    ``run_tool_harness.execute_tool_request`` with allowlists/caps bound in.

    The conversation is mutated in place; on return it carries the full
    transcript (every issued call answered) and can be re-emitted for the
    verdict turn by the caller.
    """
    budgets = budgets or LoopBudgets()
    outcome = LoopOutcome()
    started = time_fn()
    calls_executed = 0
    seen_keys: set[str] = set()
    successful_results: dict[str, dict[str, Any]] = {}
    consecutive_no_progress = 0
    duplicate_call_sets: dict[tuple[str, ...], int] = {}
    previous_assistant_text = ""
    textual_repair_pending = False
    consecutive_textual_tool_repairs = 0

    while outcome.rounds < budgets.max_rounds:
        if time_fn() - started > budgets.wall_clock_sec:
            outcome.stop_reason = STOP_WALL_CLOCK
            break

        # Keep the next request within the advisory context budget by
        # compacting the oldest tool results (newest stay intact). When a
        # summarizer is wired, fold them into a model-generated digest that
        # preserves salient facts; otherwise (or if it frees nothing / fails)
        # fall back to blunt truncation, which is the guaranteed backstop.
        if conversation.approx_tokens() > budgets.max_conversation_tokens:
            before_tokens = conversation.approx_tokens()
            summarized = 0
            if summarize_fn is not None:
                try:
                    summarized = conversation.summarize_oldest_tool_results(
                        summarize_fn, keep_newest=budgets.summarize_keep_newest
                    )
                except Exception:  # noqa: BLE001 — summarization is best-effort
                    summarized = 0
            if (
                not summarized
                or conversation.approx_tokens() > budgets.max_conversation_tokens
            ):
                conversation.truncate_oldest_tool_results(
                    budgets.truncated_result_bytes
                )
            if conversation.approx_tokens() > budgets.max_conversation_tokens:
                conversation.truncate_oldest_assistant_text(
                    1000, keep_newest=budgets.summarize_keep_newest
                )
            if conversation.approx_tokens() > budgets.max_conversation_tokens:
                conversation.collapse_oldest_completed_history(
                    max(1000, min(8000, budgets.max_conversation_tokens * 2)),
                    keep_newest_results=budgets.summarize_keep_newest,
                )
            if conversation.approx_tokens() > budgets.max_conversation_tokens:
                conversation.collapse_oldest_completed_history(
                    max(256, min(2000, budgets.max_conversation_tokens)),
                    keep_newest_results=0,
                )
            if conversation.approx_tokens() > budgets.max_conversation_tokens:
                outcome.stop_reason = STOP_CONTEXT_BUDGET
                outcome.error = (
                    "Conversation could not be compacted below the configured "
                    "planning-context allowance."
                )
                break
            removed = max(0, before_tokens - conversation.approx_tokens())
            if removed:
                outcome.compaction_runs += 1
                outcome.compaction_tokens_removed += removed

        remaining_calls = max(0, budgets.max_tool_calls - calls_executed)
        remaining_turns = max(0, budgets.max_rounds - outcome.rounds)
        budget_note = (
            f"Exploration budget before this turn: {remaining_calls} tool calls "
            f"and {remaining_turns} planning turns remain. Prioritize unresolved "
            "correctness risks. Do not repeat completed checks. Stop requesting "
            "tools when the evidence is sufficient so you can synthesize it."
        )
        if consecutive_no_progress:
            allowance = max(0, budgets.max_consecutive_no_progress_rounds - consecutive_no_progress)
            budget_note += (
                f" No-progress allowance remaining: {allowance} round(s); another "
                "duplicate cycle may end exploration."
            )
        if (
            textual_repair_pending
            and consecutive_textual_tool_repairs < budgets.max_textual_tool_repairs
            and outcome.textual_tool_repair_attempts < budgets.max_total_textual_tool_repairs
        ):
            budget_note = _TEXTUAL_TOOL_REPAIR_NOTE + "\n\n" + budget_note
            outcome.textual_tool_repair_attempts += 1
            consecutive_textual_tool_repairs += 1
            outcome.consecutive_textual_tool_repair_attempts = consecutive_textual_tool_repairs

        payload = conversation.to_request_payload(
            api_format,
            model,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tokens_param=tokens_param,
            cache_prefix=cache_prefix,
            ephemeral_user_note=budget_note,
        )
        outcome.planning_turns_attempted += 1
        try:
            response = post_fn(payload)
        except Exception as exc:  # noqa: BLE001 — transport errors end the loop
            outcome.stop_reason = STOP_REQUEST_ERROR
            outcome.error = str(exc)
            break

        outcome.rounds += 1
        calls, text, text_source, finish_reason = extract_intermediate_turn(
            response, api_format
        )
        outcome.finish_reasons.append(finish_reason or "unknown")
        outcome.text_sources.append(text_source)
        repetitive = repetitive_assistant_text(text, previous_assistant_text)
        if text:
            previous_assistant_text = text
        if repetitive:
            outcome.repetitive_text_detected = True

        # Real API tool calls always win. Textual pseudo-calls are never parsed
        # or executed; they only trigger one bounded request to reissue them via
        # the native schema.
        if not calls:
            markers = detect_textual_tool_intent(text)
            if markers:
                outcome.textual_tool_intent_detected = True
                outcome.textual_tool_intent_markers.extend(markers)
                if text:
                    conversation.add_assistant_text(text)
                if (
                    not textual_repair_pending
                    and consecutive_textual_tool_repairs < budgets.max_textual_tool_repairs
                    and outcome.textual_tool_repair_attempts
                    < budgets.max_total_textual_tool_repairs
                    and outcome.rounds < budgets.max_rounds
                ):
                    textual_repair_pending = True
                    continue
                outcome.textual_tool_unexecuted = True
                outcome.stop_reason = STOP_UNEXECUTED_TEXTUAL_TOOL_INTENT
                outcome.error = (
                    "Exploration ended with structured textual tool intent that "
                    "was not reissued as native tool calls."
                )
                break
            if repetitive:
                if text:
                    conversation.add_assistant_text(text)
                conversation.add_system_note(_STAGNATION_NOTE)
                outcome.final_text = text
                outcome.final_text_source = text_source
                outcome.stop_reason = STOP_REPETITIVE_TEXT
                outcome.stagnation_stop_reason = "repetitive-text"
                break
            if finish_reason == "length":
                if text:
                    conversation.add_assistant_text(text)
                    outcome.final_text = text
                    outcome.final_text_source = text_source
                    outcome.preserved_truncated_bytes = len(text.encode("utf-8"))
                    outcome.preserved_truncated_tokens = max(1, len(text) // 4)
                if (
                    outcome.continuation_attempts < budgets.max_truncation_continuations
                    and outcome.rounds < budgets.max_rounds
                ):
                    outcome.continuation_attempts += 1
                    conversation.add_user(
                        "Your previous turn was cut off by the completion-token limit. "
                        "Continue the same investigation from that reasoning. You may "
                        "still use the available tools when evidence is missing; otherwise "
                        "finish with the requested structured answer. Do not restart or "
                        "repeat completed analysis."
                    )
                    continue
                outcome.stop_reason = STOP_TRUNCATED
                outcome.error = "Model response was truncated before a usable answer or tool call."
                break
            textual_repair_pending = False
            outcome.final_text = text
            outcome.final_text_source = text_source
            outcome.stop_reason = (
                STOP_MODEL_DONE if outcome.tool_calls_issued else STOP_NO_TOOL_CALLS
            )
            break

        if text:
            # Interleaved reasoning text rides along inside the same
            # assistant turn on the wire; Conversation stores it as a
            # separate event, which both renderers merge correctly.
            conversation.add_assistant_text(text)
        conversation.add_assistant_tool_calls(calls)
        outcome.tool_calls_issued += len(calls)

        # Decide each call's disposition SEQUENTIALLY — dedup (seen_keys) and
        # the budget counter are stateful and must stay deterministic and
        # call-ordered. Only the to-run executions are then fanned out
        # concurrently: the executor is read-only and a round's calls are
        # independent (the model emitted them together). Results are applied in
        # the original call order to preserve the open-call contract.
        plan: list[tuple[str, str, Any]] = []  # (call_id, kind, data) in order
        to_execute: dict[int, tuple[str, dict[str, Any]]] = {}
        round_keys: list[str] = []
        for idx, call in enumerate(calls):
            call_id = call["id"]
            # Arguments arrive as an opaque JSON string (#233 contract); parse
            # here, and on failure answer with a repairable error instead of
            # crashing the loop — weak models misquote JSON.
            try:
                args = json.loads(call["arguments"]) if call["arguments"] else {}
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                outcome.calls_malformed += 1
                outcome.calls_rejected += 1
                plan.append(
                    (call_id, "error",
                     {"error": f"Invalid tool arguments (not a JSON object): {exc}"})
                )
                continue

            key = _request_key(call["name"], args)
            round_keys.append(key)
            if key in seen_keys:
                outcome.calls_duplicated += 1
                outcome.calls_rejected += 1
                if key in successful_results:
                    replay = dict(successful_results[key])
                    replay["replayed_duplicate"] = True
                    replay["canonical_request_key"] = key
                    plan.append((call_id, "dup", replay))
                else:
                    plan.append((call_id, "dup", {
                        "error": "Duplicate non-successful request; retrying it cannot provide new evidence.",
                        "replayed_duplicate": True,
                        "canonical_request_key": key,
                    }))
                continue

            if calls_executed >= budgets.max_tool_calls:
                outcome.calls_rejected += 1
                plan.append((call_id, "budget", {"error": _BUDGET_NOTE}))
                continue

            seen_keys.add(key)
            calls_executed += 1
            outcome.calls_executed = calls_executed
            to_execute[idx] = (call["name"], args)
            plan.append((call_id, "exec", idx))

        # Fan out the executions (read-only, independent within a round).
        results_by_idx: dict[int, dict[str, Any]] = {}
        if len(to_execute) == 1:
            (only_idx, (name, args)), = to_execute.items()
            results_by_idx[only_idx] = execute_fn(name, args)
        elif to_execute:
            with ThreadPoolExecutor(max_workers=min(len(to_execute), 8)) as pool:
                futures = {
                    pool.submit(execute_fn, name, args): i
                    for i, (name, args) in to_execute.items()
                }
                for fut in futures:
                    results_by_idx[futures[fut]] = fut.result()

        # Apply results in call order (synthetic refusals inline).
        successful_this_round = 0
        for call_id, kind, data in plan:
            if kind != "exec":
                conversation.add_tool_result(call_id, data, is_error=kind != "dup")
                continue
            name, args = to_execute[data]
            result = results_by_idx[data]
            outcome.executed.append(
                ExecutedCall(tool=name, args=args, result=result)
            )
            conversation.add_tool_result(
                call_id,
                result.get("result", {}),
                is_error=result.get("status") != "ok",
            )
            if result.get("status") == "ok":
                key = _request_key(name, args)
                successful_results[key] = {
                    "tool": result.get("tool", name),
                    "status": "ok",
                    "result": result.get("result", {}),
                    "provenance": "original_bounded_tool_result",
                }
                successful_this_round += 1

        duplicate_only = bool(plan) and all(kind == "dup" for _, kind, _ in plan)
        if duplicate_only:
            outcome.duplicate_only_rounds += 1
            signature = tuple(sorted(round_keys))
            duplicate_call_sets[signature] = duplicate_call_sets.get(signature, 0) + 1
            outcome.repeated_call_set_max = max(outcome.repeated_call_set_max,
                                                duplicate_call_sets[signature])
        if successful_this_round:
            consecutive_no_progress = 0
            if textual_repair_pending:
                textual_repair_pending = False
                consecutive_textual_tool_repairs = 0
                outcome.consecutive_textual_tool_repair_attempts = 0
                outcome.textual_tool_repaired = True
        elif calls and textual_repair_pending:
            # The model returned to the native protocol, but the executor did
            # not produce usable evidence. Do not grant a fresh repair budget.
            textual_repair_pending = False
        else:
            consecutive_no_progress += 1
            outcome.no_progress_rounds += 1
            outcome.max_consecutive_no_progress = max(
                outcome.max_consecutive_no_progress, consecutive_no_progress
            )

        repeated_set_stop = duplicate_only and duplicate_call_sets.get(
            tuple(sorted(round_keys)), 0
        ) >= budgets.max_repeated_call_sets
        if (consecutive_no_progress >= budgets.max_consecutive_no_progress_rounds
                or repeated_set_stop or repetitive):
            conversation.add_system_note(_STAGNATION_NOTE)
            outcome.stop_reason = STOP_REPETITIVE_TEXT if repetitive else STOP_STAGNATION
            outcome.stagnation_stop_reason = (
                "repetitive-text" if repetitive else
                "repeated-call-set" if repeated_set_stop else "consecutive-no-progress"
            )
            break

        if calls_executed >= budgets.max_tool_calls:
            outcome.stop_reason = STOP_BUDGET
            break
    else:
        outcome.stop_reason = STOP_MAX_ROUNDS

    # A repair request that ended on a hard stop (transport, time, context, or
    # truncation) did not execute the previously expressed intent. A clean
    # no-tool response clears ``textual_repair_pending`` above because the model
    # may explicitly decide the evidence is no longer needed.
    if textual_repair_pending:
        outcome.textual_tool_unexecuted = True

    outcome.degraded = (
        outcome.tool_calls_issued == 0
        and not outcome.final_text
        and outcome.stop_reason in (STOP_NO_TOOL_CALLS, STOP_REQUEST_ERROR)
    )
    return outcome
