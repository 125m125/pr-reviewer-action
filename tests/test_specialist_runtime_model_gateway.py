import json

import pytest

from pr_reviewer.conversation import Conversation
from pr_reviewer.specialist_runtime.model_gateway import (
    ModelTurnRequest,
    OpenAIModelGateway,
)
from pr_reviewer.transport import ModelRequestError


def conversation(user: str = "user"):
    value = Conversation(system="system")
    value.add_user(user)
    return value


def stop_response(content: str):
    return {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": content},
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def turn_request(value, *, tools_enabled, response_schema=None):
    return ModelTurnRequest(
        role="specialist",
        conversation=value,
        max_tokens=512,
        response_schema=response_schema,
        tools_enabled=tools_enabled,
        timeout_sec=20,
        stream=False,
    )


def test_rendered_request_bytes_matches_compact_wire_payload_for_tools():
    value = conversation("inspect the change")
    value.tool_schemas = [{
        "name": "read_file",
        "description": "Read a repository file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }]
    calls = []
    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="wire-secret", default_model="main",
        transport=lambda *args, **kwargs: calls.append((args, kwargs)) or stop_response("{}"),
    )
    request = turn_request(value, tools_enabled=True)

    payload = gateway.render_request(request)

    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert "response_format" not in payload
    assert "wire-secret" not in json.dumps(payload)
    assert gateway.rendered_request_bytes(request) == len(json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8"))
    gateway.complete(request)
    assert calls[0][0][2] == payload


def test_rendered_request_bytes_uses_structured_schema_without_tools():
    value = conversation("checkpoint now")
    value.tool_schemas = [{
        "name": "read_file",
        "description": "Read a repository file",
        "parameters": {"type": "object", "properties": {}},
    }]
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="", default_model="main",
        response_format="json_schema",
    )
    request = turn_request(
        value, tools_enabled=False, response_schema=schema,
    )

    payload = gateway.render_request(request)

    assert "tools" not in payload
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"] == schema
    assert gateway.rendered_request_bytes(request) == len(json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8"))


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
    assert 0 < calls[0][0][4] <= 20
    assert result.finish_reason == "stop"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 2}


def test_gateway_forwards_ephemeral_user_note_only_to_wire_payload():
    calls = []
    value = conversation("persistent request")
    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="secret", default_model="main",
        transport=lambda *args, **kwargs: calls.append((args, kwargs)) or stop_response("{}"),
    )

    gateway.complete(ModelTurnRequest(
        role="specialist", conversation=value, max_tokens=512,
        response_schema=None, tools_enabled=True, timeout_sec=20, stream=False,
        ephemeral_user_note="transient budget status",
    ))

    assert calls[0][0][2]["messages"][-1] == {
        "role": "user", "content": "transient budget status",
    }
    assert "transient budget status" not in json.dumps(value.events)


def test_override_payload_and_diagnostics_use_per_request_response_format():
    payloads = []

    def transport(*args, **_kwargs):
        payloads.append(args[2])
        return stop_response("{}")

    gateway = OpenAIModelGateway(
        base_url="http://model/v1",
        api_key="",
        default_model="main",
        response_format="json_schema",
        transport=transport,
    )

    result = gateway.complete(ModelTurnRequest(
        role="planner",
        conversation=conversation("REQUEST-SENTINEL"),
        max_tokens=512,
        response_schema={
            "type": "object",
            "properties": {"value": {"const": "SCHEMA-SENTINEL"}},
        },
        tools_enabled=False,
        timeout_sec=20,
        stream=False,
        response_format_override="json_object",
    ))

    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert result.request_diagnostics["response_format_configured"] == "json_schema"
    assert result.request_diagnostics["response_format_requested"] == "json_object"
    assert result.request_diagnostics["response_format_effective"] == "json_object"
    assert result.request_diagnostics["response_format"] == "json_object"
    serialized = json.dumps(result.request_diagnostics)
    assert "REQUEST-SENTINEL" not in serialized
    assert "SCHEMA-SENTINEL" not in serialized


def test_tool_enabled_payload_omits_format_but_diagnostics_keep_requested_format():
    payloads = []

    def transport(*args, **_kwargs):
        payloads.append(args[2])
        return stop_response("{}")

    gateway = OpenAIModelGateway(
        base_url="http://model/v1",
        api_key="",
        default_model="main",
        response_format="json_schema",
        transport=transport,
    )

    result = gateway.complete(ModelTurnRequest(
        role="specialist",
        conversation=conversation("REQUEST-SENTINEL"),
        max_tokens=512,
        response_schema=None,
        tools_enabled=True,
        timeout_sec=20,
        stream=False,
    ))

    assert "response_format" not in payloads[0]
    assert result.request_diagnostics["response_format_configured"] == "json_schema"
    assert result.request_diagnostics["response_format_requested"] == "json_schema"
    assert result.request_diagnostics["response_format_effective"] == "off"
    assert result.request_diagnostics["response_format"] == "off"
    assert "REQUEST-SENTINEL" not in json.dumps(result.request_diagnostics)


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
    payloads = []

    def transport(*args, **_kwargs):
        timeouts.append(args[4])
        payloads.append(args[2])
        if len(timeouts) == 1:
            raise ModelRequestError("unsupported response format", status=400)
        return stop_response("{}")

    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="", default_model="main", transport=transport,
    )
    result = gateway.complete(ModelTurnRequest(
        role="planner", conversation=conversation("REQUEST-SENTINEL"), max_tokens=512,
        response_schema={
            "type": "object",
            "properties": {"value": {"const": "SCHEMA-SENTINEL"}},
        },
        tools_enabled=False, timeout_sec=20, stream=False,
    ))

    assert timeouts == [19.0, 16.0]
    assert payloads[0]["response_format"]["type"] == "json_schema"
    assert "response_format" not in payloads[1]
    assert result.request_diagnostics["response_format_configured"] == "json_schema"
    assert result.request_diagnostics["response_format_requested"] == "json_schema"
    assert result.request_diagnostics["response_format_effective"] == "off"
    assert result.request_diagnostics["response_format"] == "off"
    assert result.request_diagnostics["structured_output_fallback"] is True
    assert "unsupported response format" in result.request_diagnostics[
        "structured_output_error"
    ]
    serialized = json.dumps(result.request_diagnostics)
    assert "REQUEST-SENTINEL" not in serialized
    assert "SCHEMA-SENTINEL" not in serialized
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
