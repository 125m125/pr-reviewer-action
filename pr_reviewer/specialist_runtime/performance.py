"""Provider-reported cache and timing measurements; missing is not zero."""

from collections.abc import Mapping, Sequence
from math import isfinite


def _number(value: object, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not isfinite(value) or value < 0 or (integer and int(value) != value):
            return None
    except OverflowError:
        return None
    return int(value) if integer else value


def request_performance(usage: object, timings: object) -> dict[str, int | float | None]:
    """Normalize OpenAI cache usage and llama.cpp/ik_llama.cpp timings."""
    usage = usage if isinstance(usage, Mapping) else {}
    timings = timings if isinstance(timings, Mapping) else {}
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
    if cached is None:
        cached = timings.get("cache_n")
    return {
        "measured_prompt_tokens": _number(usage.get("prompt_tokens"), integer=True),
        "cached_prompt_tokens": _number(cached, integer=True),
        "prefill_tokens": _number(timings.get("prompt_n"), integer=True),
        "prefill_ms": _number(timings.get("prompt_ms")),
        "generated_tokens": _number(timings.get("predicted_n"), integer=True),
        "generation_ms": _number(timings.get("predicted_ms")),
        "draft_tokens": _number(timings.get("draft_n"), integer=True),
        "accepted_draft_tokens": _number(timings.get("draft_n_accepted"), integer=True),
    }


def performance_category(purpose: str, previous: str) -> str:
    if purpose.startswith("delegated-tool-summary"):
        return "delegated-summary"
    if purpose.startswith("checkpoint"):
        return "checkpoint"
    if purpose == "exploration":
        if previous.startswith("delegated-tool-summary"):
            return "delegation-resume"
        if previous.startswith("checkpoint"):
            return "checkpoint-resume"
        return "exploration"
    return "other"


def performance_summary(attempts: Sequence[Mapping[str, object]]) -> list[str]:
    """Render weighted measurements for completed logical specialist requests."""
    completed = [item for item in attempts if item.get("status") == "completed"]
    if not completed:
        return []
    categories = {
        "exploration": "Specialist exploration",
        "delegated-summary": "Delegated summaries (including repairs)",
        "delegation-resume": "Resumes after delegation",
        "checkpoint": "Checkpoints (including repairs)",
        "checkpoint-resume": "Checkpoint resumes",
        "other": "Other specialist calls",
    }
    lines = [
        "", "## Model cache and performance", "",
        "Completed specialist requests only; measurements describe the final provider response "
        "of each logical request, excluding retry costs. Cache coverage is measured requests / "
        "completed requests. Missing or invalid measurements are unavailable, not cache misses.", "",
        "| Call type | Calls | Cache hit (coverage) | Cached / prompt tokens | Prefilled tokens | Prefill seconds | Prefill tok/s | Generation tok/s | Draft acceptance |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def pairs(rows, first, second, *, bounded=False):
        values = []
        for row in rows:
            a, b = _number(row.get(first)), _number(row.get(second))
            if a is not None and b is not None and (not bounded or a <= b):
                values.append((a, b))
        return values

    def rate(values, scale=1.0, suffix=""):
        denominator = sum(b for _, b in values)
        if not values or denominator <= 0:
            return "unavailable"
        return f"{scale * sum(a for a, _ in values) / denominator:.1f}{suffix}"

    for category, label in categories.items():
        rows = [row for row in completed if row.get("performance_category", "other") == category]
        if not rows:
            continue
        cache = pairs(rows, "cached_prompt_tokens", "measured_prompt_tokens", bounded=True)
        prefill = pairs(rows, "prefill_tokens", "prefill_ms")
        generation = pairs(rows, "generated_tokens", "generation_ms")
        draft = pairs(rows, "accepted_draft_tokens", "draft_tokens", bounded=True)
        cache_hit = rate(cache, 100, "%") + f" ({len(cache)}/{len(rows)})"
        cache_tokens = (
            f"{sum(a for a, _ in cache):,} / {sum(b for _, b in cache):,}"
            if cache else "unavailable"
        )
        prefill_tokens = f"{sum(a for a, _ in prefill):,}" if prefill else "unavailable"
        prefill_seconds = f"{sum(b for _, b in prefill) / 1000:.2f}" if prefill else "unavailable"
        lines.append("| " + " | ".join((
            label, str(len(rows)), cache_hit, cache_tokens, prefill_tokens,
            prefill_seconds, rate(prefill, 1000), rate(generation, 1000), rate(draft, 100, "%"),
        )) + " |")
    lines.extend((
        "", "Rates use summed tokens / summed time for requests reporting both. "
        "Prefill and draft totals also include only paired measurements. "
        "Resume rows are the first exploration request after that session's helper or checkpoint "
        "request. Checkpoints disable tools; checkpoint resumes re-enable them. "
        "Low reuse shows reprocessing, but does not identify its cause or distinguish RAM restoration "
        "from an already resident cache.",
    ))
    return lines
