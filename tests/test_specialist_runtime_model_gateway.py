import pytest

from pr_reviewer.conversation import Conversation
from pr_reviewer.specialist_runtime.model_gateway import (
    ModelTurnRequest,
    OpenAIModelGateway,
)


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
