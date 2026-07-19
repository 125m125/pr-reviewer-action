"""OpenAI-compatible role-model gateway for the specialist runtime.

The gateway deliberately owns only request construction and provider transport.
Session state, tool execution, evidence, and review-finalization policy remain
callers' responsibilities.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from pr_reviewer.conversation import Conversation
from pr_reviewer.stream_watchdog import StreamWatchdog
from pr_reviewer.tool_loop import extract_intermediate_turn
from pr_reviewer.transport import run_chat_request

# ``transport`` adds scripts/ to sys.path before importing this module's
# dependencies, so this is the same redaction implementation used by the
# established specialist runner.
from redact import mask_secrets


Transport = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ModelTurnRequest:
    """One provider turn, independent of runtime/session lifecycle state."""

    role: str
    conversation: Conversation
    max_tokens: int
    response_schema: dict[str, Any] | None
    tools_enabled: bool
    timeout_sec: float
    stream: bool
    deadline_at: float | None = None
    temperature: float | None = None
    response_schema_name: str | None = None
    reasoning_effort: str | None = None
    tokens_param: str = "max_tokens"
    cache_prefix: bool = False
    keep_full_history_on_verdict: bool = True


@dataclass(frozen=True)
class ModelTurnResult:
    """A normalized provider response plus bounded request diagnostics."""

    response: dict[str, Any]
    tool_calls: tuple[dict[str, Any], ...]
    text: str
    text_source: str
    finish_reason: str
    usage: dict[str, Any]
    request_diagnostics: dict[str, Any]
    stream_watchdog_triggered: bool = False
    stream_watchdog_reason: str = ""


class ModelGateway(Protocol):
    """Provider boundary consumed by specialist runtime sessions."""

    def complete(self, request: ModelTurnRequest) -> ModelTurnResult:
        """Complete one model turn."""


@dataclass
class OpenAIModelGateway:
    """OpenAI chat-completions gateway with legacy retry hardening."""

    base_url: str
    api_key: str
    default_model: str
    role_models: Mapping[str, str] = field(default_factory=dict)
    api_format: str = "openai"
    response_format: str = "json_schema"
    stream_watchdog: bool = True
    transport: Transport | None = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.api_format = self.api_format.strip().lower()
        if self.api_format != "openai":
            raise ValueError("specialist runtime requires an OpenAI-compatible api_format")
        if not self.default_model.strip():
            raise ValueError("default_model must not be empty")
        if self.response_format not in {"off", "json_object", "json_schema"}:
            raise ValueError("response_format must be off, json_object, or json_schema")
        self.role_models = {
            str(role): str(model).strip()
            for role, model in self.role_models.items()
            if str(model).strip()
        }
        if self.transport is None:
            self.transport = run_chat_request

    def model_for_role(self, role: str) -> str:
        """Return the configured override, otherwise the deterministic default."""
        return self.role_models.get(role, self.default_model)

    def complete(self, request: ModelTurnRequest) -> ModelTurnResult:
        """Render and issue exactly one logical model turn.

        Structured-output and streaming retries may cause multiple physical
        requests, but each uses the same absolute request deadline.
        """
        if request.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if request.timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        payload = request.conversation.to_request_payload(
            "openai",
            self.model_for_role(request.role),
            stream=request.stream,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            verdict_turn=not request.tools_enabled,
            keep_full_history_on_verdict=request.keep_full_history_on_verdict,
            response_format=self.response_format if not request.tools_enabled else None,
            response_schema=request.response_schema,
            response_schema_name=(request.response_schema_name or f"specialist_{request.role}"),
            reasoning_effort=request.reasoning_effort,
            tokens_param=request.tokens_param,
            cache_prefix=request.cache_prefix,
        )
        response, diagnostics = self.complete_payload(
            payload,
            request.role,
            timeout_sec=request.timeout_sec,
            deadline_at=request.deadline_at,
            stream_watchdog=(StreamWatchdog("openai") if request.stream and self.stream_watchdog else None),
        )
        calls, text, text_source, finish_reason = extract_intermediate_turn(response, "openai")
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        return ModelTurnResult(
            response=response,
            tool_calls=tuple(calls),
            text=text,
            text_source=text_source,
            finish_reason=finish_reason,
            usage=usage,
            request_diagnostics=diagnostics,
            stream_watchdog_triggered=bool(response.get("stream_watchdog_triggered")),
            stream_watchdog_reason=str(response.get("stream_watchdog_reason") or ""),
        )

    def complete_payload(
        self,
        payload: dict[str, Any],
        role: str,
        *,
        timeout_sec: float,
        deadline_at: float | None = None,
        compact_fallback_payload: dict[str, Any] | None = None,
        stream_watchdog: StreamWatchdog | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Send an already-rendered OpenAI payload with hardened fallbacks.

        This adapter keeps the legacy sequential runner on the same transport
        behavior while new runtime callers use :meth:`complete` directly.
        """
        started = time.monotonic()
        structured_fallback = False
        original_error = ""

        def request_timeout() -> float:
            if timeout_sec <= 0 or not math.isfinite(timeout_sec):
                raise ValueError("timeout_sec must be a positive finite number")
            if deadline_at is None:
                return timeout_sec
            if not math.isfinite(deadline_at):
                raise ValueError("deadline_at must be finite")
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("model request deadline exceeded before transport")
            return min(timeout_sec, remaining)

        def post(candidate: dict[str, Any], *, watchdog: StreamWatchdog | None = None) -> dict[str, Any]:
            assert self.transport is not None
            kwargs: dict[str, Any] = {}
            if watchdog is not None and candidate.get("stream"):
                kwargs["stream_watchdog"] = watchdog
            return self.transport(
                self.base_url, "openai", candidate, self.api_key, request_timeout(), **kwargs,
            )

        def unstructured_retry() -> dict[str, Any]:
            nonlocal structured_fallback
            structured_fallback = True
            candidate = compact_fallback_payload or payload
            candidate = {
                key: value for key, value in candidate.items()
                if key not in {"response_format", "stream_options"}
            }
            candidate["stream"] = False
            try:
                return post(candidate)
            except Exception as final_exc:
                final_error = mask_secrets(str(final_exc))[:1000]
                raise RuntimeError(
                    f"structured output request failed: {original_error}; "
                    f"unstructured fallback failed: {final_error}"
                ) from final_exc

        try:
            response = post(payload, watchdog=stream_watchdog)
            usable = not (payload.get("stream") and response.get("error"))
            if not usable:
                original_error = mask_secrets(json.dumps(response.get("error")))[:1000]
        except Exception as exc:
            usable = False
            original_error = mask_secrets(str(exc))[:1000]
            provider_rejected = bool(getattr(exc, "provider_rejected", False))
            if not payload.get("stream") or provider_rejected:
                if "response_format" not in payload:
                    raise
                response = unstructured_retry()
                usable = True

        if not usable:
            fallback = {key: value for key, value in payload.items() if key != "stream_options"}
            fallback["stream"] = False
            try:
                response = post(fallback)
            except Exception as exc:
                if "response_format" not in payload:
                    raise
                original_error = original_error or mask_secrets(str(exc))[:1000]
                response = unstructured_retry()

        usage = response.get("usage") if isinstance(response, dict) else {}
        diagnostics = {
            "role": role,
            "model": payload.get("model"),
            "duration_sec": round(time.monotonic() - started, 3),
            "usage": usage if isinstance(usage, dict) else {},
            "response_format": self.response_format if "response_format" in payload else "off",
            "structured_output_fallback": structured_fallback,
            "structured_output_error": original_error if structured_fallback else "",
        }
        return response, diagnostics
