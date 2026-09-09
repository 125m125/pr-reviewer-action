from pr_reviewer.specialist_runtime import performance


def test_provider_statistics_preserve_zero_and_reject_invalid_values():
    stats = performance.request_performance(
        {"prompt_tokens": 20, "prompt_tokens_details": {"cached_tokens": 0}},
        {"prompt_n": 20, "prompt_ms": 2.5, "predicted_n": 4, "predicted_ms": 80,
         "draft_n": 5, "draft_n_accepted": 3},
    )
    assert stats == {"measured_prompt_tokens": 20, "cached_prompt_tokens": 0, "prefill_tokens": 20, "prefill_ms": 2.5,
                     "generated_tokens": 4, "generation_ms": 80,
                     "draft_tokens": 5, "accepted_draft_tokens": 3}
    for value in (None, True, -1, "5", float("nan"), float("inf")):
        assert performance.request_performance(
            {"prompt_tokens_details": {"cached_tokens": value}}, {"prompt_ms": value},
        )["cached_prompt_tokens"] is None
    assert performance.request_performance({}, {"cache_n": 42})["cached_prompt_tokens"] == 42


def test_report_uses_weighted_rates_and_only_observed_cache_denominators():
    attempts = [
        dict(status="completed", performance_category="checkpoint-resume",
             measured_prompt_tokens=1000, cached_prompt_tokens=900,
             prefill_tokens=100, prefill_ms=1000, generated_tokens=10, generation_ms=1000,
             draft_tokens=20, accepted_draft_tokens=10),
        dict(status="completed", performance_category="checkpoint-resume",
             measured_prompt_tokens=100, cached_prompt_tokens=0,
             prefill_tokens=100, prefill_ms=3000, generated_tokens=90, generation_ms=9000,
             draft_tokens=100, accepted_draft_tokens=20),
        dict(status="completed", performance_category="checkpoint-resume", actual_prompt_tokens=9000),
        dict(status="failed", performance_category="checkpoint-resume", actual_prompt_tokens=99999),
    ]
    report = "\n".join(performance.performance_summary(attempts))
    assert "Checkpoint resumes" in report
    assert "81.8% (2/3)" in report  # 900 / 1100; unknown usage excluded
    assert "900 / 1,100" in report
    assert "200" in report
    assert "4.00" in report
    assert "50.0" in report  # 200 / 4 seconds, not mean(100, 33.3)
    assert "10.0" in report
    assert "25.0%" in report  # 30 / 120


def test_report_marks_unavailable_statistics_and_omits_empty_runs():
    assert performance.performance_summary([]) == []
    report = "\n".join(performance.performance_summary([
        dict(status="completed", performance_category="checkpoint", actual_prompt_tokens=100),
    ]))
    assert "unavailable" in report
    assert "0.0%" not in report


def test_missing_prompt_usage_does_not_count_as_measured_cache_coverage():
    metrics = performance.request_performance({"prompt_tokens_details": {"cached_tokens": 0}}, {})
    report = "\n".join(performance.performance_summary([{
        "status": "completed", "performance_category": "exploration",
        "actual_prompt_tokens": 0, **metrics,
    }]))
    assert "unavailable (0/1)" in report
    assert "0 / 0" not in report
