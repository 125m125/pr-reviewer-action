"""Process-global bounded execution for untrusted in-process callbacks."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from math import isfinite
from pathlib import Path
from queue import Empty, Queue
import re
from threading import BoundedSemaphore, Lock, Thread
from types import MappingProxyType
from typing import Callable, Mapping

from scripts.redact import mask_secrets


_MAX_CALLBACK_DEPTH = 12
_MAX_CALLBACK_ITEMS = 2_000
_MAX_CALLBACK_STRING = 16 * 1024
_INLINE_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth(?:orization)?|cookie|credential|"
    r"password|secret|token)\s*[:=]\s*[^\s,;]+"
)


def mask_runtime_text(value: object, *, limit: int = _MAX_CALLBACK_STRING) -> str:
    """Shared bounded redactor for callback, event, and artifact diagnostics."""
    try:
        return _INLINE_SECRET.sub(
            "[REDACTED]", mask_secrets(str(value)),
        )[:limit]
    except BaseException:
        return "[unserializable]"


def format_callback_error(
    exc: BaseException, *, limit: int = 1000,
) -> str:
    """Format even hostile exceptions without invoking unsafe repr fallbacks."""
    try:
        name = getattr(type(exc), "__name__", "BaseException")
        if type(name) is not str or not name:
            name = "BaseException"
    except BaseException:
        name = "BaseException"
    detail = mask_runtime_text(exc, limit=max(1, limit - len(name) - 2))
    return f"{name}: {detail}"[:limit]


class CallbackTimedOut(TimeoutError):
    """A callback did not return before its caller-owned timeout."""


class CallbackCapacityExceeded(RuntimeError):
    """All process-global callback slots are occupied, usually by orphans."""


class ProcessCallbackPool:
    """Bound thread creation across every controller instance in this process."""

    def __init__(self, capacity: int, *, thread_prefix: str) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("callback capacity must be a positive integer")
        self.capacity = capacity
        self.thread_prefix = thread_prefix
        self._slots = BoundedSemaphore(capacity)
        self._sequence = 0
        self._in_flight = 0
        self._lock = Lock()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def run(
        self,
        callback: Callable[[], object],
        *,
        timeout_sec: float,
        name: str,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> object:
        if not isfinite(timeout_sec) or timeout_sec <= 0:
            raise CallbackTimedOut(f"{name} has no remaining callback time")
        if not self._slots.acquire(blocking=False):
            raise CallbackCapacityExceeded(
                f"{name} callback capacity exhausted by in-flight work"
            )
        outcome: Queue[tuple[str, object]] = Queue(maxsize=1)
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            self._in_flight += 1

        def invoke() -> None:
            try:
                value = callback()
            except BaseException as exc:
                if on_error is not None:
                    try:
                        on_error(exc)
                    except BaseException:
                        pass
                try:
                    outcome.put_nowait(("error", exc))
                except BaseException:
                    pass
            else:
                try:
                    outcome.put_nowait(("ok", value))
                except BaseException:
                    pass
            finally:
                with self._lock:
                    self._in_flight -= 1
                self._slots.release()

        worker = Thread(
            target=invoke,
            name=f"{self.thread_prefix}-{name}-{sequence}",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            with self._lock:
                self._in_flight -= 1
            self._slots.release()
            raise
        try:
            status, value = outcome.get(timeout=timeout_sec)
        except Empty as exc:
            # The worker retains its process-global slot until it actually exits.
            raise CallbackTimedOut(f"{name} callback timed out") from exc
        if status == "error":
            assert isinstance(value, BaseException)
            raise value
        return value


CALLBACK_POOL = ProcessCallbackPool(8, thread_prefix="review-callback")
OBSERVER_POOL = ProcessCallbackPool(4, thread_prefix="review-observer")


def freeze_callback_value(value: object, *, depth: int = 0) -> object:
    """Detach and bound values before they cross an orphanable thread boundary."""
    if depth > _MAX_CALLBACK_DEPTH:
        return "[bounded]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else "[invalid-number]"
    if isinstance(value, Enum):
        return freeze_callback_value(value.value, depth=depth + 1)
    if isinstance(value, str):
        return mask_runtime_text(value)
    if isinstance(value, Path):
        return str(value)[:_MAX_CALLBACK_STRING]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("callback context keys must be strings")
        return MappingProxyType({
            key: freeze_callback_value(value[key], depth=depth + 1)
            for key in sorted(value)[:_MAX_CALLBACK_ITEMS]
        })
    if isinstance(value, (set, frozenset)):
        value = tuple(sorted(value, key=str))
    if isinstance(value, (tuple, list)):
        return tuple(
            freeze_callback_value(item, depth=depth + 1)
            for item in tuple(value)[:_MAX_CALLBACK_ITEMS]
        )
    if is_dataclass(value):
        detached = {
            item.name: freeze_callback_value(getattr(value, item.name), depth=depth + 1)
            for item in fields(value)
        }
        try:
            return type(value)(**detached)
        except (TypeError, ValueError):
            return MappingProxyType(detached)
    try:
        return mask_runtime_text(value)
    except BaseException:
        return "[unserializable]"
