"""Tests for online repetition detection in streamed specialist output."""

from __future__ import annotations

import json

from pr_reviewer.stream_watchdog import StreamWatchdog


def _line(data: dict) -> str:
    return "data: " + json.dumps(data)


def _openai_text(text: str, *, reasoning: bool = False) -> str:
    key = "reasoning_content" if reasoning else "content"
    return _line({"choices": [{"delta": {key: text}}]})


def _anthropic_text(text: str) -> str:
    return _line({
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": text},
    })


def _openai_tool() -> str:
    return _line({
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call-1",
                    "function": {"name": "read_file", "arguments": "{}"},
                }]
            }
        }]
    })


PARAGRAPH = (
    "I'll start by listing the files in docs.\n"
    "I've already tried docs/runner-specs.adoc and it did not exist.\n"
    "I'll check the docs directory for any other files.\n"
    "I'll also check the review rules and specialist configuration.\n"
    "I'll look for model and runner information in the repository."
)


def test_repeated_reasoning_paragraphs_trigger_online_watchdog():
    watchdog = StreamWatchdog("openai", min_repetitions=3)

    assert watchdog.feed_sse_line(_openai_text(PARAGRAPH, reasoning=True)) is False
    assert watchdog.feed_sse_line(_openai_text("\n\n" + PARAGRAPH, reasoning=True)) is False
    assert watchdog.feed_sse_line(_openai_text("\n\n" + PARAGRAPH, reasoning=True)) is True
    assert watchdog.reason == "repeated-paragraph"


def test_repeated_paragraphs_without_blank_line_boundaries_trigger():
    watchdog = StreamWatchdog("openai", min_repetitions=3)
    block = " ".join(PARAGRAPH.splitlines())

    for index in range(2):
        assert watchdog.feed_sse_line(_openai_text((" " if index else "") + block)) is False
    assert watchdog.feed_sse_line(_openai_text(" " + block)) is True


def test_repeated_anthropic_text_paragraphs_trigger():
    watchdog = StreamWatchdog("anthropic", min_repetitions=3)

    assert watchdog.feed_sse_line(_anthropic_text(PARAGRAPH)) is False
    assert watchdog.feed_sse_line(_anthropic_text("\n\n" + PARAGRAPH)) is False
    assert watchdog.feed_sse_line(_anthropic_text("\n\n" + PARAGRAPH)) is True


def test_native_tool_call_disables_text_watchdog_for_the_turn():
    watchdog = StreamWatchdog("openai", min_repetitions=2)

    assert watchdog.feed_sse_line(_openai_tool()) is False
    for _ in range(4):
        assert watchdog.feed_sse_line(_openai_text(PARAGRAPH, reasoning=True)) is False
    assert watchdog.native_tool_seen is True


def test_malformed_and_short_repeated_input_is_ignored():
    watchdog = StreamWatchdog("openai", min_repetitions=2)

    assert watchdog.feed_sse_line("data: not-json") is False
    assert watchdog.feed_sse_line("event: message") is False
    assert watchdog.feed_sse_line(_openai_text("yes " * 100)) is False
    assert watchdog.triggered is False
