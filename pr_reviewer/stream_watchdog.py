"""Online repetition detection for streamed specialist model output."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any


_DATA_PREFIX = "data:"
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_WORD_RE = re.compile(r"\S+")


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


class StreamWatchdog:
    """Detect obvious paragraph/block degeneration while an SSE stream runs.

    This deliberately uses exact normalized repetition rather than semantic
    similarity. It is intended to stop an autoregressive loop, not judge the
    quality of ordinary prose. Native tool-call output disables the detector
    for the rest of the turn because tool arguments and repeated protocol
    fields are legitimate.
    """

    def __init__(
        self,
        api_format: str,
        *,
        window_chars: int = 12000,
        ngram_words: int = 18,
        min_repetitions: int = 3,
    ) -> None:
        self.api_format = api_format.strip().lower()
        self.window_chars = max(1000, int(window_chars))
        self.ngram_words = max(4, int(ngram_words))
        self.min_repetitions = max(2, int(min_repetitions))
        self.triggered = False
        self.reason = ""
        self.native_tool_seen = False
        self.text_words_seen = 0
        self._text = ""

    def feed_sse_line(self, line: str) -> bool:
        """Consume one raw SSE line and return whether the stream should stop."""

        if self.triggered or self.native_tool_seen:
            return self.triggered
        stripped = line.strip()
        if not stripped.startswith(_DATA_PREFIX):
            return False
        raw = stripped[len(_DATA_PREFIX):].strip()
        if not raw or raw == "[DONE]":
            return False
        try:
            event = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(event, dict):
            return False

        text, has_tool = self._extract_delta(event)
        if has_tool:
            self.native_tool_seen = True
            return False
        if not text:
            return False

        self._text = (self._text + text)[-self.window_chars:]
        self.text_words_seen = len(_WORD_RE.findall(_normalise(self._text)))
        self._check_paragraphs()
        if not self.triggered:
            self._check_blocks()
        return self.triggered

    def _extract_delta(self, event: dict[str, Any]) -> tuple[str, bool]:
        if self.api_format == "anthropic":
            event_type = event.get("type")
            if event_type == "content_block_start":
                block = event.get("content_block") or {}
                return "", isinstance(block, dict) and block.get("type") == "tool_use"
            if event_type != "content_block_delta":
                return "", False
            delta = event.get("delta") or {}
            if not isinstance(delta, dict):
                return "", False
            if delta.get("type") in {"text_delta", "thinking_delta", "text"}:
                text = delta.get("text")
                return (text, False) if isinstance(text, str) else ("", False)
            return "", False

        text_parts: list[str] = []
        has_tool = False
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            if delta.get("tool_calls"):
                has_tool = True
            for key in ("content", "reasoning_content"):
                value = delta.get(key)
                if isinstance(value, str):
                    text_parts.append(value)
        return "".join(text_parts), has_tool

    def _check_paragraphs(self) -> None:
        paragraphs = [
            _normalise(part)
            for part in _PARAGRAPH_RE.split(self._text)
            if len(_normalise(part)) >= 80
        ]
        counts: dict[str, int] = defaultdict(int)
        for paragraph in paragraphs:
            counts[paragraph] += 1
            if counts[paragraph] >= self.min_repetitions:
                self.triggered = True
                self.reason = "repeated-paragraph"
                return

    def _check_blocks(self) -> None:
        words = _WORD_RE.findall(_normalise(self._text))
        n = self.ngram_words
        if len(words) < n * self.min_repetitions:
            return
        positions: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for start in range(len(words) - n + 1):
            block = tuple(words[start:start + n])
            if len(set(block)) < max(3, n // 4):
                continue
            positions[block].append(start)
            selected = [positions[block][0]]
            for position in positions[block][1:]:
                if position - selected[-1] >= n:
                    selected.append(position)
                if len(selected) >= self.min_repetitions:
                    self.triggered = True
                    self.reason = "repeated-block"
                    return
