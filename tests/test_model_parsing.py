#!/usr/bin/env python3
"""Fixture tests for model output parsing and SSE reassembly.

Covers the acceptance criteria from issue #32:
  - OpenAI non-stream response parsing
  - Anthropic non-stream response parsing
  - OpenAI SSE reassembly
  - Anthropic SSE reassembly
  - Invalid/malformed outputs
  - Non-text Anthropic blocks ignored
  - CI runs these tests

Tests are self-contained (no external services) and run via:
    python3 tests/test_model_parsing.py
"""

import json
import sys


def parse_and_validate(response_dict):
    """Parse model response dict and return the extracted review dict."""
    content = None
    if isinstance(response_dict.get("choices"), list):
        content = (
            (response_dict.get("choices") or [{}])[0]
            .get("message") or {}
        ).get("content")
    elif isinstance(response_dict.get("content"), list):
        parts = []
        for item in response_dict.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        content = "".join(parts)
    elif isinstance(response_dict.get("content"), str):
        content = response_dict.get("content")

    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type in (None, "text"):
                    text_part = item.get("text")
                    if isinstance(text_part, str):
                        parts.append(text_part)
        text = "".join(parts).strip()
    elif content is None:
        text = ""
    else:
        text = str(content).strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    parsed = None
    for start in range(len(text)):
        if text[start] not in "[{":
            continue
        try:
            candidate, _end = decoder.raw_decode(text[start:])
            parsed = candidate
            break
        except json.JSONDecodeError:
            continue

    if parsed is None:
        raise SystemExit("Could not extract JSON object from model response")

    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]

    if not isinstance(parsed, dict):
        raise SystemExit(f"Expected JSON object but got {type(parsed).__name__}")

    return parsed


def reassemble_anthropic_sse(sse_text):
    """Reassemble Anthropic streaming SSE into a chat.completion-like dict."""
    content_parts = []
    stop_reason = None
    model = None
    message_id = None
    input_tokens = 0
    output_tokens = 0

    for line in sse_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")
        if etype == "message_start":
            message_id = event.get("message", {}).get("id")
            model = event.get("message", {}).get("model")
            input_tokens = event.get("message", {}).get("usage", {}).get("input_tokens", 0)
            output_tokens = event.get("message", {}).get("output_tokens", 0)
        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") in ("text_delta", "text"):
                text_chunk = delta.get("text", "")
                if isinstance(text_chunk, str):
                    content_parts.append(text_chunk)
        elif etype == "message_delta":
            delta = event.get("delta", {})
            stop_reason = delta.get("stop_reason")
            usage = delta.get("usage", {})
            if usage:
                output_tokens += usage.get("output_tokens", 0)

    content_text = "".join(content_parts)
    return {
        "id": message_id or "",
        "object": "chat.completion",
        "model": model or "",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content_text},
            "finish_reason": stop_reason or "stop"
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens
        }
    }


def reassemble_openai_sse(sse_text):
    """Reassemble OpenAI streaming SSE into a chat.completion-like dict."""
    content_parts = []
    finish_reason = None
    model = None
    usage_prompt_tokens = 0
    usage_completion_tokens = 0
    id_val = ""

    for line in sse_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        id_val = chunk.get("id", id_val)
        model = chunk.get("model", model)
        choices = chunk.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            if isinstance(delta, dict):
                c = delta.get("content")
                if isinstance(c, str):
                    content_parts.append(c)
            fr = choice.get("finish_reason")
            if fr is not None:
                finish_reason = fr
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            usage_prompt_tokens += usage.get("prompt_tokens", 0)
            usage_completion_tokens += usage.get("completion_tokens", 0)

    content_text = "".join(content_parts)
    return {
        "id": id_val,
        "object": "chat.completion",
        "model": model or "",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content_text},
            "finish_reason": finish_reason or "stop"
        }],
        "usage": {
            "prompt_tokens": usage_prompt_tokens,
            "completion_tokens": usage_completion_tokens,
            "total_tokens": usage_prompt_tokens + usage_completion_tokens
        }
    }


class Result:
    """Track pass/fail counts."""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def ok(self, desc, got, expected):
        if got == expected:
            self.passed += 1
            print(f"  PASS: {desc}")
        else:
            self.failed += 1
            msg = f"  FAIL: {desc} (got {got!r}, expected {expected!r})"
            self.failures.append(msg)
            print(msg)

    def ok_raises(self, desc, fn, expected_msg_sub=None):
        try:
            fn()
            self.failed += 1
            msg = f"  FAIL: {desc} (expected SystemExit, got success)"
            self.failures.append(msg)
            print(msg)
        except SystemExit as e:
            if expected_msg_sub and expected_msg_sub not in str(e):
                self.failed += 1
                msg = f"  FAIL: {desc} (expected '{expected_msg_sub}' in error, got '{e}')"
                self.failures.append(msg)
                print(msg)
            else:
                self.passed += 1
                print(f"  PASS: {desc}")


def _sse_line(obj):
    """Build a single SSE data line from a Python dict."""
    return "data: " + json.dumps(obj)


def _sse_block(lines):
    """Join SSE lines with blank-line separators."""
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Tests: OpenAI non-stream response parsing
# ---------------------------------------------------------------------------

def test_openai_nonstream_standard():
    r = Result()
    resp = {
        "id": "chatcmpl-test",
        "choices": [{
            "message": {
                "content": json.dumps({"verdict":"approve","review_markdown":"Looks good.","packages":[]})
            },
            "finish_reason": "stop"
        }]
    }
    parsed = parse_and_validate(resp)
    r.ok("verdict=approve", parsed["verdict"], "approve")
    r.ok("review_markdown present", len(parsed.get("review_markdown", "")), 11)
    return r


def test_openai_nonstream_array_wrapped():
    r = Result()
    resp = {
        "id": "chatcmpl-test",
        "choices": [{
            "message": {
                "content": json.dumps([{"verdict":"request_changes","review_markdown":"Needs work."}])
            },
            "finish_reason": "stop"
        }]
    }
    parsed = parse_and_validate(resp)
    r.ok("verdict=request_changes", parsed["verdict"], "request_changes")
    r.ok("review_markdown present", len(parsed.get("review_markdown", "")), 11)
    return r


def test_openai_nonstream_string_content():
    r = Result()
    resp = {
        "id": "msg-test",
        "content": json.dumps({"verdict":"approve","review_markdown":"Direct string."})
    }
    parsed = parse_and_validate(resp)
    r.ok("verdict=approve from string content", parsed["verdict"], "approve")
    return r


# ---------------------------------------------------------------------------
# Tests: Anthropic non-stream response parsing
# ---------------------------------------------------------------------------

def test_anthropic_nonstream_text_blocks():
    r = Result()
    resp = {
        "id": "msg-test",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "tool_use", "name": "read_file", "input": {}},
            {"type": "text", "text": json.dumps({"verdict":"approve","review_markdown":"Anthropic clean."})}
        ]
    }
    parsed = parse_and_validate(resp)
    r.ok("verdict=approve", parsed["verdict"], "approve")
    r.ok("review_markdown correct", parsed.get("review_markdown"), "Anthropic clean.")
    return r


def test_anthropic_nonstream_only_thinking():
    r = Result()
    resp = {
        "id": "msg-test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "just thinking"}]
    }
    r.ok_raises("rejects only-thinking response", lambda: parse_and_validate(resp))
    return r


def test_anthropic_nonstream_mixed_text_list():
    r = Result()
    resp = {
        "id": "msg-test",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "ignored"},
            "Some prose ",
            {"type": "text", "text": json.dumps({"verdict":"approve","review_markdown":"Mixed list."})}
        ]
    }
    parsed = parse_and_validate(resp)
    r.ok("verdict=approve from mixed list", parsed["verdict"], "approve")
    return r


# ---------------------------------------------------------------------------
# Tests: OpenAI SSE reassembly
# ---------------------------------------------------------------------------

def test_openai_sse_single_delta():
    r = Result()
    review_json = json.dumps({"verdict":"approve","review_markdown":"Single delta."})
    sse = _sse_block([
        _sse_line({"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"role":"assistant"}}]}),
        _sse_line({"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"content":review_json}}]}),
        _sse_line({"id":"chatcmpl-1","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"finish_reason":"stop"}]}),
        "data: [DONE]",
    ])
    assembled = reassemble_openai_sse(sse)
    parsed = parse_and_validate(assembled)
    r.ok("verdict=approve", parsed["verdict"], "approve")
    r.ok("review_markdown correct", parsed.get("review_markdown"), "Single delta.")
    r.ok("model propagated", assembled["model"], "gpt-4")
    r.ok("id propagated", assembled["id"], "chatcmpl-1")
    return r


def test_openai_sse_multiple_deltas():
    r = Result()
    review = json.dumps({"verdict":"request_changes","review_markdown":"Multi chunk."})
    mid_point = len(review) // 2
    part1 = review[:mid_point]
    part2 = review[mid_point:]

    sse = _sse_block([
        _sse_line({"id":"chatcmpl-2","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"role":"assistant"}}]}),
        _sse_line({"id":"chatcmpl-2","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"content":part1}}]}),
        _sse_line({"id":"chatcmpl-2","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"content":part2}}]}),
        _sse_line({"id":"chatcmpl-2","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"finish_reason":"stop"}]}),
        "data: [DONE]",
    ])
    assembled = reassemble_openai_sse(sse)
    parsed = parse_and_validate(assembled)
    r.ok("verdict=request_changes", parsed["verdict"], "request_changes")
    r.ok("review_markdown correct", parsed.get("review_markdown"), "Multi chunk.")
    return r


def test_openai_sse_with_usage():
    r = Result()
    review_json = json.dumps({"verdict":"approve","review_markdown":"With usage."})
    sse = _sse_block([
        _sse_line({"id":"chatcmpl-3","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"delta":{"content":review_json}}]}),
        _sse_line({"id":"chatcmpl-3","object":"chat.completion.chunk","model":"gpt-4","choices":[{"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":50}}),
        "data: [DONE]",
    ])
    assembled = reassemble_openai_sse(sse)
    r.ok("prompt_tokens tracked", assembled["usage"]["prompt_tokens"], 100)
    r.ok("completion_tokens tracked", assembled["usage"]["completion_tokens"], 50)
    r.ok("total_tokens correct", assembled["usage"]["total_tokens"], 150)
    parsed = parse_and_validate(assembled)
    r.ok("parsed verdict after reassembly", parsed["verdict"], "approve")
    return r


def test_openai_sse_with_blank_lines():
    r = Result()
    review_json = json.dumps({"verdict":"approve","review_markdown":"Blank lines."})
    sse = _sse_block([
        _sse_line({"id":"chatcmpl-4","choices":[{"delta":{"content":review_json}}]}),
        "data: [DONE]",
    ])
    assembled = reassemble_openai_sse(sse)
    parsed = parse_and_validate(assembled)
    r.ok("handles blank lines", parsed["verdict"], "approve")
    return r


# ---------------------------------------------------------------------------
# Tests: Anthropic SSE reassembly
# ---------------------------------------------------------------------------

def test_anthropic_sse_text_delta():
    r = Result()
    review_json = json.dumps({"verdict":"approve","review_markdown":"Streamed clean."})
    mid = len(review_json) // 2
    part1 = review_json[:mid]
    part2 = review_json[mid:]

    sse = _sse_block([
        _sse_line({"type":"message_start","message":{"id":"msg_smoke","model":"claude-3-5-sonnet","usage":{"input_tokens":10,"output_tokens":0}}}),
        _sse_line({"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}),
        _sse_line({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":part1}}),
        _sse_line({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":part2}}),
        _sse_line({"type":"content_block_stop","index":0}),
        _sse_line({"type":"message_delta","delta":{"stop_reason":"end_turn"}}),
        _sse_line({"type":"message_stop"}),
    ])
    assembled = reassemble_anthropic_sse(sse)
    parsed = parse_and_validate(assembled)
    r.ok("verdict=approve", parsed["verdict"], "approve")
    r.ok("review_markdown correct", parsed.get("review_markdown"), "Streamed clean.")
    r.ok("model propagated", assembled["model"], "claude-3-5-sonnet")
    r.ok("message_id propagated", assembled["id"], "msg_smoke")
    return r


def test_anthropic_sse_thinking_ignored():
    r = Result()
    review_json = json.dumps({"verdict":"request_changes","review_markdown":"After thinking."})
    sse = _sse_block([
        _sse_line({"type":"message_start","message":{"id":"msg-think","model":"claude-3-5-sonnet"}}),
        _sse_line({"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}),
        _sse_line({"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"private reasoning that must not leak"}}),
        _sse_line({"type":"content_block_stop","index":0}),
        _sse_line({"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}),
        _sse_line({"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":review_json}}),
        _sse_line({"type":"content_block_stop","index":1}),
        _sse_line({"type":"message_delta","delta":{"stop_reason":"end_turn"}}),
        _sse_line({"type":"message_stop"}),
    ])
    assembled = reassemble_anthropic_sse(sse)
    content = assembled["choices"][0]["message"]["content"]
    r.ok("thinking excluded from content", "private reasoning" not in content, True)
    parsed = parse_and_validate(assembled)
    r.ok("verdict=request_changes", parsed["verdict"], "request_changes")
    r.ok("review_markdown correct", parsed.get("review_markdown"), "After thinking.")
    return r


def test_anthropic_sse_tool_use_ignored():
    r = Result()
    review_json = json.dumps({"verdict":"approve","review_markdown":"Tool blocks ignored."})
    sse = _sse_block([
        _sse_line({"type":"message_start","message":{"id":"msg-tool","model":"claude-3-5-sonnet"}}),
        _sse_line({"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}),
        _sse_line({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":review_json}}),
        _sse_line({"type":"content_block_stop","index":0}),
        _sse_line({"type":"content_block_start","index":1,"content_block":{"type":"tool_use","name":"read_file"}}),
        _sse_line({"type":"content_block_delta","index":1,"delta":{"type":"input_json","input":{"path":"/etc/passwd"}}}),
        _sse_line({"type":"content_block_stop","index":1}),
        _sse_line({"type":"message_delta","delta":{"stop_reason":"end_turn"}}),
        _sse_line({"type":"message_stop"}),
    ])
    assembled = reassemble_anthropic_sse(sse)
    content = assembled["choices"][0]["message"]["content"]
    r.ok("tool_use input_json excluded", "input_json" not in content, True)
    r.ok("tool_use name excluded", "read_file" not in content, True)
    parsed = parse_and_validate(assembled)
    r.ok("verdict=approve", parsed["verdict"], "approve")
    r.ok("review_markdown correct", parsed.get("review_markdown"), "Tool blocks ignored.")
    return r


def test_anthropic_sse_empty_stream():
    r = Result()
    sse = _sse_block([
        _sse_line({"type":"message_start","message":{"id":"msg-empty","model":"claude-3-5-sonnet"}}),
        _sse_line({"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"only thinking"}}),
        _sse_line({"type":"message_delta","delta":{"stop_reason":"end_turn"}}),
        _sse_line({"type":"message_stop"}),
    ])
    assembled = reassemble_anthropic_sse(sse)
    r.ok("empty content after reassembly", assembled["choices"][0]["message"]["content"], "")
    r.ok_raises("empty stream rejected by parser", lambda: parse_and_validate(assembled))
    return r


def test_anthropic_sse_text_type_alias():
    r = Result()
    review_json = json.dumps({"verdict":"approve","review_markdown":"Type alias."})
    sse = _sse_block([
        _sse_line({"type":"message_start","message":{"id":"msg-alias","model":"claude-3-5-sonnet"}}),
        _sse_line({"type":"content_block_delta","index":0,"delta":{"type":"text","text":review_json}}),
        _sse_line({"type":"message_delta","delta":{"stop_reason":"end_turn"}}),
    ])
    assembled = reassemble_anthropic_sse(sse)
    parsed = parse_and_validate(assembled)
    r.ok("handles text type alias", parsed["verdict"], "approve")
    return r


# ---------------------------------------------------------------------------
# Tests: Invalid / malformed outputs
# ---------------------------------------------------------------------------

def test_malformed_bare_numeric_list():
    r = Result()
    resp = {"id": "chatcmpl-test", "choices": [{"message": {"content": "[1,2,3]"}}]}
    r.ok_raises("rejects bare numeric list", lambda: parse_and_validate(resp))
    return r


def test_malformed_empty_array():
    r = Result()
    resp = {"id": "chatcmpl-test", "choices": [{"message": {"content": "[]"}}]}
    r.ok_raises("rejects empty array", lambda: parse_and_validate(resp))
    return r


def test_malformed_non_json_prose():
    r = Result()
    resp = {"id": "chatcmpl-test", "choices": [{"message": {"content": "I can't help with that right now."}}]}
    r.ok_raises("rejects pure prose", lambda: parse_and_validate(resp))
    return r


def test_malformed_invalid_json():
    r = Result()
    resp = {"id": "chatcmpl-test", "choices": [{"message": {"content": '{"verdict":"approve","review_markdown":broken}'}}]}
    r.ok_raises("rejects broken JSON", lambda: parse_and_validate(resp))
    return r


def test_malformed_empty_content():
    r = Result()
    resp = {"id": "chatcmpl-test", "choices": [{"message": {"content": ""}}]}
    r.ok_raises("rejects empty content", lambda: parse_and_validate(resp))
    return r


def test_malformed_none_content():
    r = Result()
    resp = {"id": "chatcmpl-test", "choices": [{"message": {"content": None}}]}
    r.ok_raises("rejects null content", lambda: parse_and_validate(resp))
    return r


def test_malformed_missing_choices():
    r = Result()
    resp = {"id": "chatcmpl-test", "choices": []}
    r.ok_raises("rejects empty choices array", lambda: parse_and_validate(resp))
    return r


# ---------------------------------------------------------------------------
# Tests: Edge cases and boundary conditions
# ---------------------------------------------------------------------------

def test_edge_markdown_fence_variants():
    r = Result()
    for lang in ["json", "", "JSON"]:
        fence_start = f"```{lang}" if lang else "```"
        resp = {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": f"{fence_start}\n{{\"verdict\":\"approve\",\"review_markdown\":\"Fence: {lang}\"}}\n```"}}]
        }
        parsed = parse_and_validate(resp)
        r.ok(f"verdict with fence lang={lang!r}", parsed["verdict"], "approve")
    return r


def test_edge_leading_trailing_prose():
    r = Result()
    resp = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": "Here is my review:\n\n{\"verdict\":\"approve\",\"review_markdown\":\"Clean PR.\"}\n\nLet me know if you need anything else."}}]
    }
    parsed = parse_and_validate(resp)
    r.ok("verdict from prose-wrapped JSON", parsed["verdict"], "approve")
    r.ok("review_markdown correct", parsed.get("review_markdown"), "Clean PR.")
    return r


def test_edge_single_item_array():
    r = Result()
    resp = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": "[{\"verdict\":\"request_changes\",\"review_markdown\":\"Needs fixes.\"}]"}}]
    }
    parsed = parse_and_validate(resp)
    r.ok("unwrapped single-item array", parsed["verdict"], "request_changes")
    r.ok("review_markdown from unwrapped", parsed.get("review_markdown"), "Needs fixes.")
    return r


def test_edge_single_item_array_wrapped_in_fence():
    r = Result()
    resp = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": "```json\n[{\"verdict\":\"approve\",\"review_markdown\":\"Fenced array.\"}]\n```"}}]
    }
    parsed = parse_and_validate(resp)
    r.ok("unwrapped fenced array", parsed["verdict"], "approve")
    return r


def test_edge_invalid_verdict_values():
    r = Result()
    for verdict in ["unknown", "ERROR", "", "approve_extra"]:
        resp = {
            "id": "chatcmpl-test",
            "choices": [{"message": {"content": json.dumps({"verdict": verdict, "review_markdown": "test"})}}]
        }
        parsed = parse_and_validate(resp)
        r.ok(f"parser accepts verdict={verdict!r}", parsed.get("verdict"), verdict)
    return r


def test_edge_sse_corrupted_chunk():
    r = Result()
    review_json = json.dumps({"verdict":"approve","review_markdown":"Corrupted skip."})
    sse_lines = [
        _sse_line({"id":"chatcmpl-5","choices":[{"delta":{"content":review_json}}]}),
        "data: THIS IS NOT JSON AT ALL!!!",
        _sse_line({"id":"chatcmpl-5","choices":[{"delta":{"content":"more text"}}]}),
        "data: [DONE]",
    ]
    sse = "\n\n".join(sse_lines)
    assembled = reassemble_openai_sse(sse)
    parsed = parse_and_validate(assembled)
    r.ok("handles corrupted SSE chunk", parsed["verdict"], "approve")
    return r


def test_edge_anthropic_usage_accumulation():
    r = Result()
    review_json = json.dumps({"verdict":"approve","review_markdown":"Usage accumulation."})
    sse = _sse_block([
        _sse_line({"type":"message_start","message":{"id":"msg-usage","model":"claude-3-5-sonnet","usage":{"input_tokens":100,"output_tokens":0}}}),
        _sse_line({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":review_json}}),
        _sse_line({"type":"message_delta","delta":{"stop_reason":"end_turn","usage":{"output_tokens":50}}}),
        _sse_line({"type":"message_stop"}),
    ])
    assembled = reassemble_anthropic_sse(sse)
    r.ok("input_tokens from message_start", assembled["usage"]["prompt_tokens"], 100)
    r.ok("output_tokens accumulated", assembled["usage"]["completion_tokens"], 50)
    r.ok("total_tokens correct", assembled["usage"]["total_tokens"], 150)
    return r


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Model Parsing & SSE Reassembly Tests")
    print("=" * 60)

    all_results = []

    print("\n--- OpenAI Non-Stream Response Parsing ---")
    all_results.append(test_openai_nonstream_standard())
    all_results.append(test_openai_nonstream_array_wrapped())
    all_results.append(test_openai_nonstream_string_content())

    print("\n--- Anthropic Non-Stream Response Parsing ---")
    all_results.append(test_anthropic_nonstream_text_blocks())
    all_results.append(test_anthropic_nonstream_only_thinking())
    all_results.append(test_anthropic_nonstream_mixed_text_list())

    print("\n--- OpenAI SSE Reassembly ---")
    all_results.append(test_openai_sse_single_delta())
    all_results.append(test_openai_sse_multiple_deltas())
    all_results.append(test_openai_sse_with_usage())
    all_results.append(test_openai_sse_with_blank_lines())

    print("\n--- Anthropic SSE Reassembly ---")
    all_results.append(test_anthropic_sse_text_delta())
    all_results.append(test_anthropic_sse_thinking_ignored())
    all_results.append(test_anthropic_sse_tool_use_ignored())
    all_results.append(test_anthropic_sse_empty_stream())
    all_results.append(test_anthropic_sse_text_type_alias())

    print("\n--- Invalid / Malformed Outputs ---")
    all_results.append(test_malformed_bare_numeric_list())
    all_results.append(test_malformed_empty_array())
    all_results.append(test_malformed_non_json_prose())
    all_results.append(test_malformed_invalid_json())
    all_results.append(test_malformed_empty_content())
    all_results.append(test_malformed_none_content())
    all_results.append(test_malformed_missing_choices())

    print("\n--- Edge Cases & Boundary Conditions ---")
    all_results.append(test_edge_markdown_fence_variants())
    all_results.append(test_edge_leading_trailing_prose())
    all_results.append(test_edge_single_item_array())
    all_results.append(test_edge_single_item_array_wrapped_in_fence())
    all_results.append(test_edge_invalid_verdict_values())
    all_results.append(test_edge_sse_corrupted_chunk())
    all_results.append(test_edge_anthropic_usage_accumulation())

    total_passed = sum(r.passed for r in all_results)
    total_failed = sum(r.failed for r in all_results)
    total = total_passed + total_failed
    failures = []
    for r in all_results:
        failures.extend(r.failures)

    if failures:
        print("\n--- Failures ---")
        for f in failures:
            print(f)

    print(f"\n=== Results: {total_passed}/{total} passed, {total_failed} failed ===")

    if total_failed > 0:
        print("\nFAIL: Some tests did not pass.")
        sys.exit(1)
    else:
        print("\nPASS: All tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
