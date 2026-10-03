from pr_reviewer.specialist_runtime import performance


def test_provider_statistics_preserve_zero_and_reject_invalid_values():
    stats = performance.request_performance(
        {"prompt_tokens": 20, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 0}},
        {"prompt_n": 20, "prompt_ms": 2.5, "predicted_n": 4, "predicted_ms": 80,
         "draft_n": 5, "draft_n_accepted": 3},
    )
    assert stats == {"measured_prompt_tokens": 20, "measured_completion_tokens": 4, "cached_prompt_tokens": 0, "prefill_tokens": 20, "prefill_ms": 2.5,
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


def test_total_row_counts_uncached_usage_and_weights_rates_across_categories():
    report = performance.performance_summary([
        dict(status="completed", performance_category="exploration",
             measured_prompt_tokens=1000, measured_completion_tokens=100,
             cached_prompt_tokens=900, prefill_tokens=100, prefill_ms=1000,
             generated_tokens=100, generation_ms=1000),
        dict(status="completed", performance_category="checkpoint",
             measured_prompt_tokens=2000, measured_completion_tokens=200,
             prefill_tokens=2000, prefill_ms=9000,
             generated_tokens=200, generation_ms=9000),
        dict(status="completed", performance_category="checkpoint"),
        dict(status="failed", measured_prompt_tokens=9000, measured_completion_tokens=9000),
    ])
    headers = next(line for line in report if line.startswith("| Call type"))
    total = next(line for line in report if line.startswith("| Total"))
    cells = dict(zip((s.strip() for s in headers.split("|")[1:-1]),
                     (s.strip() for s in total.split("|")[1:-1])))
    assert cells["Calls"] == "3"
    assert cells["Prompt tokens incl. cached (coverage)"] == "3,000 (2/3)"
    assert cells["Completion tokens (coverage)"] == "300 (2/3)"
    assert cells["Prefill tok/s"] == "210.0 (2/3)"
    assert cells["Generation tok/s"] == "30.0 (2/3)"
    assert cells["Cached / prompt tokens"] == "900 / 1,000"


def test_completion_usage_preserves_missing_and_zero_through_journal():
    from dataclasses import asdict
    from pr_reviewer.specialist_runtime.request_attempts import RequestAttemptJournal

    journal = RequestAttemptJournal()
    for index, usage in enumerate(({}, {"completion_tokens": 0}, {"completion_tokens": -1})):
        journal.start(request_id=str(index), session_id="s", assignment_id="a",
                      phase="exploration", turn=index, input_tokens=0,
                      max_output_tokens=1, admission_tokens=1, admission_source="test")
        journal.finish(str(index), "completed", **performance.request_performance(usage, {}))
    attempts = [asdict(row) for row in journal.close_since(0)]
    assert [row["measured_completion_tokens"] for row in attempts] == [None, 0, None]
    total = next(line for line in performance.performance_summary(attempts) if line.startswith("| Total"))
    assert "| unavailable (0/3) | 0 (1/3) |" in total


def test_whole_run_summary_includes_unmeasured_attempts_in_coverage():
    lines = performance.performance_summary([
        performance.request_performance({"prompt_tokens": 1000, "completion_tokens": 100},
                                        {"predicted_n": 100, "predicted_ms": 1000}),
        performance.request_performance({"prompt_tokens": 2000, "completion_tokens": 200},
                                        {"predicted_n": 200, "predicted_ms": 9000}),
        performance.request_performance({}, {}),
    ], all_model_requests=True)
    row = next(line for line in lines if line.startswith("| Overall"))
    assert "| 3 |" in row
    assert "30.0 (2/3)" in row
    assert "3,000 (2/3) | 300 (2/3)" in row
    assert not any(line.startswith("| Specialist") for line in lines)
