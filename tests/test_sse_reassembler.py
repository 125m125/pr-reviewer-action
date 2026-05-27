"""Tests for pr_reviewer.sse_reassembler."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import main as unittest_main

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from pr_reviewer.sse_reassembler import (
    reassemble_sse,
    reassemble_sse_file,
    reassemble_sse_to_file,
)


def _make_sse_line(data: dict) -> str:
    return f"data: {json.dumps(data)}"


class TestReassembleAnthropic:
    def test_message_start_only(self):
        lines = [
            _make_sse_line({
                "type": "message_start",
                "message": {
                    "id": "msg_123",
                    "model": "claude-3-5-sonnet",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            }),
        ]
        result = reassemble_sse("\n".join(lines), "anthropic")
        assert result["id"] == "msg_123"
        assert result["model"] == "claude-3-5-sonnet"
        assert result["choices"][0]["message"]["content"] == ""
        assert result["usage"]["prompt_tokens"] == 10

    def test_content_block_delta_text(self):
        lines = [
            _make_sse_line({"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 0}}}),
            _make_sse_line({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}),
            _make_sse_line({"type": "content_block_delta", "delta": {"type": "text_delta", "text": " world"}}),
            _make_sse_line({"type": "message_delta", "delta": {"stop_reason": "end_turn", "usage": {"output_tokens": 5}}}),
        ]
        result = reassemble_sse("\n".join(lines), "anthropic")
        assert result["choices"][0]["message"]["content"] == "Hello world"
        assert result["choices"][0]["finish_reason"] == "end_turn"
        assert result["usage"]["completion_tokens"] == 5

    def test_text_delta_type_text_accepted(self):
        lines = [
            _make_sse_line({"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 0}}}),
            _make_sse_line({"type": "content_block_delta", "delta": {"type": "text", "text": "Hi"}}),
            _make_sse_line({"type": "message_delta", "delta": {"stop_reason": "stop"}}),
        ]
        result = reassemble_sse("\n".join(lines), "anthropic")
        assert result["choices"][0]["message"]["content"] == "Hi"

    def test_thinking_delta_ignored(self):
        lines = [
            _make_sse_line({"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 0}}}),
            _make_sse_line({"type": "content_block_delta", "delta": {"type": "thinking_delta", "text": "..."}}),
            _make_sse_line({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Result"}}),
        ]
        result = reassemble_sse("\n".join(lines), "anthropic")
        assert result["choices"][0]["message"]["content"] == "Result"

    def test_dones_ignored(self):
        lines = [
            _make_sse_line({"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 1}}}),
            "data: [DONE]",
        ]
        result = reassemble_sse("\n".join(lines), "anthropic")
        assert result["choices"][0]["message"]["content"] == ""

    def test_invalid_json_skipped(self):
        lines = [
            _make_sse_line({"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 1}}}),
            "data: not valid json",
            _make_sse_line({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}}),
            _make_sse_line({"type": "message_delta", "delta": {"stop_reason": "stop"}}),
        ]
        result = reassemble_sse("\n".join(lines), "anthropic")
        assert result["choices"][0]["message"]["content"] == "OK"

    def test_output_tokens_accumulated_from_message_delta(self):
        lines = [
            _make_sse_line({"type": "message_start", "message": {"id": "m", "model": "c", "usage": {"input_tokens": 5, "output_tokens": 3}}}),
            _make_sse_line({"type": "message_delta", "delta": {"stop_reason": "end", "usage": {"output_tokens": 7}}}),
        ]
        result = reassemble_sse("\n".join(lines), "anthropic")
        assert result["usage"]["completion_tokens"] == 10


class TestReassembleOpenAI:
    def test_basic_completion(self):
        lines = [
            _make_sse_line({"id": "chat_1", "model": "gpt-4o", "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]}),
            _make_sse_line({"id": "chat_1", "model": "gpt-4o", "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": None}]}),
            _make_sse_line({"id": "chat_1", "model": "gpt-4o", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}),
        ]
        result = reassemble_sse("\n".join(lines), "openai")
        assert result["id"] == "chat_1"
        assert result["model"] == "gpt-4o"
        assert result["choices"][0]["message"]["content"] == "Hello world"
        assert result["choices"][0]["finish_reason"] == "stop"
        assert result["usage"]["prompt_tokens"] == 5
        assert result["usage"]["completion_tokens"] == 2

    def test_usage_accumulates_across_chunks(self):
        lines = [
            _make_sse_line({"id": "c", "choices": [{"delta": {"content": "a"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
            _make_sse_line({"id": "c", "choices": [{"delta": {"content": "b"}}], "usage": {"prompt_tokens": 0, "completion_tokens": 1}}),
            _make_sse_line({"id": "c", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 0, "completion_tokens": 1}}),
        ]
        result = reassemble_sse("\n".join(lines), "openai")
        assert result["usage"]["prompt_tokens"] == 1
        assert result["usage"]["completion_tokens"] == 3

    def test_dones_ignored(self):
        lines = [
            _make_sse_line({"id": "c", "choices": [{"delta": {"content": "Hi"}}]}),
            "data: [DONE]",
        ]
        result = reassemble_sse("\n".join(lines), "openai")
        assert result["choices"][0]["message"]["content"] == "Hi"

    def test_invalid_json_skipped(self):
        lines = [
            _make_sse_line({"id": "c", "choices": [{"delta": {"content": "A"}}]}),
            "data: not json",
            _make_sse_line({"id": "c", "choices": [{"delta": {"content": "B"}}]}),
        ]
        result = reassemble_sse("\n".join(lines), "openai")
        assert result["choices"][0]["message"]["content"] == "AB"


class TestReassembleSSEFile:
    def test_round_trip(self, tmp_path):
        lines = [
            _make_sse_line({"type": "message_start", "message": {"id": "msg_99", "model": "claude-3", "usage": {"input_tokens": 7, "output_tokens": 0}}}),
            _make_sse_line({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Done"}}),
            _make_sse_line({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
        ]
        path = tmp_path / "sse_response.txt"
        path.write_text("\n".join(lines))
        result = reassemble_sse_file(str(path), "anthropic")
        assert result["id"] == "msg_99"
        assert result["choices"][0]["message"]["content"] == "Done"


class TestReassembleSSEToFile:
    def test_overwrites_with_normalised_json(self, tmp_path):
        lines = [
            _make_sse_line({"type": "message_start", "message": {"id": "msg_1", "model": "c", "usage": {"input_tokens": 2, "output_tokens": 0}}}),
            _make_sse_line({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Written"}}),
            _make_sse_line({"type": "message_delta", "delta": {"stop_reason": "stop"}}),
        ]
        path = tmp_path / "out.txt"
        path.write_text("\n".join(lines))
        reassemble_sse_to_file(str(path), "anthropic")
        result = json.loads(path.read_text())
        assert result["id"] == "msg_1"
        assert result["choices"][0]["message"]["content"] == "Written"


if __name__ == "__main__":
    unittest_main()