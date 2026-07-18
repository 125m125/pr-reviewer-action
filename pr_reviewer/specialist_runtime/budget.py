"""Direct lifetime budget accounting and run deadline helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import isfinite
import time
from types import MappingProxyType
from typing import Mapping

from .types import BudgetLimits, BudgetUsage, PhaseShares, RunPhase


class BudgetExceeded(RuntimeError):
    """Raised before an operation would exceed a lifetime budget."""


class BudgetExhausted(BudgetExceeded):
    """Compatibility name for a direct budget exhaustion."""


def _normalize_reason(reason: str) -> str:
    normalized = " ".join(str(reason).split())
    return normalized or "unspecified"


class BudgetLedger:
    """The sole mutable owner of a run or session's lifetime budget."""

    def __init__(self, limits: BudgetLimits) -> None:
        self._validate_limits(limits)
        self._limits = limits
        self._usage = BudgetUsage()

    @staticmethod
    def _validate_limits(limits: BudgetLimits) -> None:
        for name in ("model_turns", "tool_calls", "recoveries"):
            if getattr(limits, name) <= 0:
                raise ValueError(f"{name} limit must be positive")
        for name in ("input_tokens", "output_tokens"):
            value = getattr(limits, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} limit must be positive when set")

    def record_model_turn(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        usage = self._usage
        if usage.model_turns + 1 > self._limits.model_turns:
            raise BudgetExhausted("model turn limit exhausted")
        if (
            self._limits.input_tokens is not None
            and usage.input_tokens + input_tokens > self._limits.input_tokens
        ):
            raise BudgetExhausted("input token limit exhausted")
        if (
            self._limits.output_tokens is not None
            and usage.output_tokens + output_tokens > self._limits.output_tokens
        ):
            raise BudgetExhausted("output token limit exhausted")
        self._usage = replace(
            usage,
            model_turns=usage.model_turns + 1,
            input_tokens=usage.input_tokens + input_tokens,
            output_tokens=usage.output_tokens + output_tokens,
        )

    def reserve_tool_calls(self, count: int) -> None:
        if not isinstance(count, int) or count <= 0:
            raise ValueError("tool call reservation must be positive")
        usage = self._usage
        if usage.tool_calls + count > self._limits.tool_calls:
            raise BudgetExhausted("tool call limit exhausted")
        self._usage = replace(usage, tool_calls=usage.tool_calls + count)

    def record_tool_rejection(self, reason: str) -> None:
        usage = self._usage
        self._usage = replace(
            usage,
            tool_rejections=usage.tool_rejections + 1,
            tool_rejection_reasons=usage.tool_rejection_reasons
            + (_normalize_reason(reason),),
        )

    def record_no_progress(self) -> int:
        usage = self._usage
        self._usage = replace(usage, no_progress_streak=usage.no_progress_streak + 1)
        return self._usage.no_progress_streak

    def reset_no_progress_streak(self, reason: str) -> None:
        usage = self._usage
        self._usage = replace(
            usage,
            no_progress_streak=0,
            no_progress_reset_reasons=usage.no_progress_reset_reasons
            + (_normalize_reason(reason),),
        )

    def record_recovery(self, reason: str) -> None:
        usage = self._usage
        if usage.recoveries + 1 > self._limits.recoveries:
            raise BudgetExhausted("recovery limit exhausted")
        self._usage = replace(
            usage,
            recoveries=usage.recoveries + 1,
            recovery_reasons=usage.recovery_reasons + (_normalize_reason(reason),),
        )

    def snapshot(self) -> BudgetUsage:
        return self._usage

    def remaining_model_turns(self) -> int:
        return max(0, self._limits.model_turns - self._usage.model_turns)

    def remaining_tool_calls(self) -> int:
        return max(0, self._limits.tool_calls - self._usage.tool_calls)


@dataclass(frozen=True)
class RunDeadline:
    """Absolute phase cutoffs that preserve the finalization reserve."""

    started_at: float
    deadline_sec: float
    phase_shares: PhaseShares
    _cutoffs: Mapping[RunPhase, float] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isfinite(self.started_at):
            raise ValueError("started_at must be finite")
        if not isfinite(self.deadline_sec) or self.deadline_sec <= 0:
            raise ValueError("deadline_sec must be positive")
        planning = self.started_at + self.deadline_sec * self.phase_shares.planning / 100
        initial = planning + self.deadline_sec * self.phase_shares.initial / 100
        followup = initial + self.deadline_sec * self.phase_shares.followup / 100
        object.__setattr__(
            self,
            "_cutoffs",
            MappingProxyType({
            RunPhase.PLANNING: planning,
            RunPhase.INITIAL: initial,
            RunPhase.FOLLOWUP: followup,
            RunPhase.FINALIZATION: self.started_at + self.deadline_sec,
            }),
        )

    def cutoff_for(self, phase: RunPhase) -> float:
        return self._cutoffs[RunPhase(phase)]

    def remaining(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.cutoff_for(RunPhase.FINALIZATION) - current)

    def phase_for(self, *, now: float | None = None) -> RunPhase:
        current = time.monotonic() if now is None else now
        for phase in RunPhase:
            if current < self.cutoff_for(phase):
                return phase
        return RunPhase.FINALIZATION

    def exploration_allowed(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return current < self.cutoff_for(RunPhase.FOLLOWUP)

    def remaining_for_exploration(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.cutoff_for(RunPhase.FOLLOWUP) - current)
