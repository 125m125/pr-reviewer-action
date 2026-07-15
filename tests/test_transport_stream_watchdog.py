"""Tests for interruptible streamed model transport."""

from __future__ import annotations

import json

from pr_reviewer import transport


def _sse(data: dict) -> str:
    return "data: " + json.dumps(data) + "\n"


class _FakeProcess:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.returncode = 0
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def communicate(self, timeout=None):
        return "", ""


def test_safe_run_streaming_terminates_when_watchdog_triggers(monkeypatch):
    process = _FakeProcess(["first\n", "repeat\n", "last\n"])

    def fake_popen(*args, **kwargs):
        return process

    monkeypatch.setattr(transport.subprocess, "Popen", fake_popen)

    result = transport.safe_run_streaming(
        ["curl"], 10, lambda line: line == "repeat\n"
    )

    assert result.interrupted is True
    assert process.terminated is True
    assert result.stdout == "first\nrepeat\n"


def test_safe_run_streaming_preserves_watchdog_reason(monkeypatch):
    process = _FakeProcess(["repeat\n"])

    class ReasonedWatchdog:
        reason = "repeated-block"

        def __call__(self, line):
            return line == "repeat\n"

    monkeypatch.setattr(transport.subprocess, "Popen", lambda *args, **kwargs: process)

    result = transport.safe_run_streaming(["curl"], 10, ReasonedWatchdog())

    assert result.interrupted is True
    assert result.watchdog_reason == "repeated-block"


def test_run_chat_request_marks_watchdog_interruption(monkeypatch):
    body = _sse({
        "choices": [{"delta": {"content": "partial"}}],
    })
    result = transport.StreamingResult(
        stdout=body,
        stderr="",
        returncode=-15,
        timed_out=False,
        interrupted=True,
        watchdog_reason="repeated-paragraph",
    )
    seen = []

    def fake_stream(args, timeout_sec, on_line):
        seen.append((args, timeout_sec, on_line))
        return result

    monkeypatch.setattr(transport, "safe_run_streaming", fake_stream)

    response = transport.run_chat_request(
        "http://model.local/v1",
        "openai",
        {"model": "m", "messages": [], "stream": True},
        "",
        20,
        stream_watchdog=lambda line: False,
    )

    assert response["stream_watchdog_triggered"] is True
    assert response["stream_watchdog_reason"] == "repeated-paragraph"
    assert response["choices"][0]["message"]["content"] == "partial"
    assert seen


def test_normal_streaming_result_is_not_marked_interrupted(monkeypatch):
    body = _sse({
        "choices": [{"delta": {"content": "complete"}}],
    })
    result = transport.StreamingResult(
        stdout=body,
        stderr="",
        returncode=0,
        timed_out=False,
        interrupted=False,
    )
    monkeypatch.setattr(
        transport,
        "safe_run_streaming",
        lambda args, timeout_sec, on_line: result,
    )

    response = transport.run_chat_request(
        "http://model.local/v1",
        "openai",
        {"model": "m", "messages": [], "stream": True},
        "",
        20,
        stream_watchdog=lambda line: False,
    )

    assert "stream_watchdog_triggered" not in response
    assert response["choices"][0]["message"]["content"] == "complete"
