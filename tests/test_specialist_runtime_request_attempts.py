from dataclasses import asdict

from pr_reviewer.specialist_runtime.request_attempts import RequestAttemptJournal


def start_attempt(journal, **overrides):
    values = {
        "request_id": "S1:model:1",
        "session_id": "S1",
        "assignment_id": "assignment-1",
        "phase": "followup",
        "turn": 1,
        "input_tokens": 2_000,
        "max_output_tokens": 512,
        "admission_tokens": 2_768,
        "admission_source": "rendered-fallback",
        "purpose": "exploration",
    }
    values.update(overrides)
    return journal.start(**values)


def test_completed_attempt_retains_estimated_admission_and_actual_usage():
    transitions = []
    journal = RequestAttemptJournal(
        clock=iter((10.0, 12.0)).__next__,
        transition_sink=transitions.append,
    )
    start_attempt(journal)

    assert journal.finish(
        "S1:model:1",
        "completed",
        finish_reason="stop",
        text_source="content",
        actual_prompt_tokens=2_200,
        actual_completion_tokens=80,
    ) is True

    attempt = journal.close_since(0)[0]
    assert attempt.input_tokens == 2_000
    assert attempt.admission_tokens == 2_768
    assert attempt.admission_source == "rendered-fallback"
    assert attempt.actual_prompt_tokens == 2_200
    assert attempt.actual_completion_tokens == 80
    assert all(
        value is None or isinstance(value, (bool, int, float, str))
        for value in asdict(transitions[-1]).values()
    )


def test_failed_attempt_retains_admission_and_has_zero_actual_usage():
    journal = RequestAttemptJournal(clock=iter((20.0, 21.0)).__next__)
    start_attempt(
        journal,
        admission_tokens=3_100,
        admission_source="provider-calibrated",
    )

    assert journal.finish(
        "S1:model:1", "failed", error="provider unavailable",
    ) is True

    attempt = journal.close_since(0)[0]
    assert attempt.admission_tokens == 3_100
    assert attempt.admission_source == "provider-calibrated"
    assert attempt.actual_prompt_tokens == 0
    assert attempt.actual_completion_tokens == 0

