import pytest

from pr_reviewer.conversation import Conversation
from pr_reviewer.specialist_runtime.model_gateway import (
    ModelTurnRequest,
    OpenAIModelGateway,
)
from pr_reviewer.transport import ModelRequestError


def conversation():
    value = Conversation(system="system")
    value.add_user("user")
    return value


def stop_response(content: str):
    return {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": content},
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def test_role_model_override_and_deadline_bound_timeout():
    calls = []
    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="secret", default_model="main",
        role_models={"planner": "plan-model"},
        transport=lambda *args, **kwargs: calls.append((args, kwargs)) or stop_response("{}"),
    )

    result = gateway.complete(ModelTurnRequest(
        role="planner", conversation=conversation(), max_tokens=512,
        response_schema={"type": "object"}, tools_enabled=False,
        timeout_sec=20, stream=False,
    ))

    assert calls[0][0][2]["model"] == "plan-model"
    assert calls[0][0][4] == 20
    assert result.finish_reason == "stop"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 2}


def test_gateway_rejects_non_openai_format_before_transport():
    called = False

    def transport(*_args, **_kwargs):
        nonlocal called
        called = True
        return stop_response("{}")

    with pytest.raises(ValueError, match="OpenAI-compatible"):
        OpenAIModelGateway(
            base_url="http://model/v1", api_key="", default_model="main",
            api_format="anthropic", transport=transport,
        )
    assert called is False


def test_absolute_deadline_reduces_transport_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pr_reviewer.specialist_runtime.model_gateway.time.monotonic", lambda: 100.0,
    )
    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="", default_model="main",
        transport=lambda *args, **kwargs: calls.append((args, kwargs)) or stop_response("{}"),
    )

    gateway.complete(ModelTurnRequest(
        role="planner", conversation=conversation(), max_tokens=512,
        response_schema={"type": "object"}, tools_enabled=False,
        timeout_sec=20, deadline_at=107.5, stream=False,
    ))

    assert calls[0][0][4] == 7.5


def test_streaming_request_gets_a_fresh_watchdog():
    calls = []
    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="", default_model="main",
        stream_watchdog=True,
        transport=lambda *args, **kwargs: calls.append((args, kwargs)) or stop_response("{}"),
    )

    gateway.complete(ModelTurnRequest(
        role="specialist", conversation=conversation(), max_tokens=512,
        response_schema=None, tools_enabled=True, timeout_sec=20, stream=True,
    ))

    assert calls[0][0][2]["stream"] is True
    assert calls[0][1]["stream_watchdog"].api_format == "openai"


def test_stream_failure_retries_with_strictly_decreasing_logical_deadline(monkeypatch):
    clock = iter((100.0, 101.0, 105.0, 106.0))
    monkeypatch.setattr(
        "pr_reviewer.specialist_runtime.model_gateway.time.monotonic", lambda: next(clock),
    )
    timeouts = []

    def transport(*args, **_kwargs):
        timeouts.append(args[4])
        if len(timeouts) == 1:
            return {"error": {"message": "stream failed"}}
        return stop_response("{}")

    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="", default_model="main", transport=transport,
    )
    result = gateway.complete(ModelTurnRequest(
        role="planner", conversation=conversation(), max_tokens=512,
        response_schema={"type": "object"}, tools_enabled=False, timeout_sec=20, stream=True,
    ))

    assert timeouts == [19.0, 15.0]
    assert result.request_diagnostics["duration_sec"] <= 20


def test_structured_retry_uses_the_same_decreasing_logical_deadline(monkeypatch):
    clock = iter((100.0, 101.0, 104.0, 107.0))
    monkeypatch.setattr(
        "pr_reviewer.specialist_runtime.model_gateway.time.monotonic", lambda: next(clock),
    )
    timeouts = []

    def transport(*args, **_kwargs):
        timeouts.append(args[4])
        if len(timeouts) == 1:
            raise ModelRequestError("unsupported response format", status=400)
        return stop_response("{}")

    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="", default_model="main", transport=transport,
    )
    result = gateway.complete(ModelTurnRequest(
        role="planner", conversation=conversation(), max_tokens=512,
        response_schema={"type": "object"}, tools_enabled=False, timeout_sec=20, stream=False,
    ))

    assert timeouts == [19.0, 16.0]
    assert result.request_diagnostics["duration_sec"] <= 20


def test_expired_deadline_prevents_subsequent_retry_transport_call(monkeypatch):
    clock = iter((100.0, 101.0, 120.0, 121.0))
    monkeypatch.setattr(
        "pr_reviewer.specialist_runtime.model_gateway.time.monotonic", lambda: next(clock),
    )
    calls = []

    def transport(*_args, **_kwargs):
        calls.append(True)
        return {"error": {"message": "stream failed"}}

    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="", default_model="main", transport=transport,
    )

    with pytest.raises(RuntimeError, match="deadline exceeded"):
        gateway.complete(ModelTurnRequest(
            role="planner", conversation=conversation(), max_tokens=512,
            response_schema={"type": "object"}, tools_enabled=False, timeout_sec=20, stream=True,
        ))

    assert calls == [True]
