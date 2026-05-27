"""Reassemble Server-Sent Events (SSE) streaming responses into consolidated JSON.

Ported from the ``reassemble_sse_response`` function in ``scripts/run_review.sh``.
Handles OpenAI chat completions streaming and Anthropic messages streaming formats,
normalising both to a unified OpenAI-style response structure.
"""

from __future__ import annotations

import json
from pathlib import Path


def reassemble_sse(response_text: str, api_format: str) -> dict:
    """Reassemble SSE lines from a streaming response into a structured dict.

    Parameters
    ----------
    response_text : str
        Raw response text containing SSE data lines (one ``data: ...`` per line).
    api_format : str
        Either ``"openai"`` or ``"anthropic"``.

    Returns
    -------
    dict
        A normalised OpenAI-style response dict with an ``id``, ``model``,
        ``choices``, and ``usage`` field.
    """
    lines = response_text.splitlines()
    content_parts: list[str] = []

    if api_format == "anthropic":
        return _reassemble_anthropic(lines, content_parts)
    return _reassemble_openai(lines, content_parts)


def _reassemble_anthropic(lines: list[str], content_parts: list[str]) -> dict:
    stop_reason: str | None = None
    stop_sequence: str | None = None
    message_id: str | None = None
    model: str | None = None
    input_tokens = 0
    output_tokens = 0

    for line in lines:
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
            output_tokens = event.get("message", {}).get("usage", {}).get("output_tokens", 0)
        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") in ("text_delta", "text"):
                text_chunk = delta.get("text", "")
                if isinstance(text_chunk, str):
                    content_parts.append(text_chunk)
        elif etype == "message_delta":
            delta = event.get("delta", {})
            stop_reason = delta.get("stop_reason")
            stop_sequence = delta.get("stop_sequence")
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
            "finish_reason": stop_reason or "stop",
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _reassemble_openai(lines: list[str], content_parts: list[str]) -> dict:
    finish_reason: str | None = None
    model: str | None = None
    usage_prompt_tokens = 0
    usage_completion_tokens = 0
    id_val = ""

    for line in lines:
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
            "finish_reason": finish_reason or "stop",
        }],
        "usage": {
            "prompt_tokens": usage_prompt_tokens,
            "completion_tokens": usage_completion_tokens,
            "total_tokens": usage_prompt_tokens + usage_completion_tokens,
        },
    }


def reassemble_sse_file(response_path: str, api_format: str) -> dict:
    """Convenience wrapper that reads a response file and reassembles SSE.

    Parameters
    ----------
    response_path : str
        Path to the SSE response file.
    api_format : str
        Either ``"openai"`` or ``"anthropic"``.

    Returns
    -------
    dict
        The normalised response dict.
    """
    text = Path(response_path).read_text(encoding="utf-8", errors="replace")
    return reassemble_sse(text, api_format)


def reassemble_sse_to_file(response_path: str, api_format: str) -> None:
    """Read a SSE response file, reassemble it, and overwrite ``response_path``.

    Parameters
    ----------
    response_path : str
        Path to the SSE response file (will be overwritten with normalised JSON).
    api_format : str
        Either ``"openai"`` or ``"anthropic"``.
    """
    result = reassemble_sse_file(response_path, api_format)
    Path(response_path).write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")