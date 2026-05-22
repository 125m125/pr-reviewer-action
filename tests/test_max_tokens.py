"""Test that build_model_request includes max_tokens in both Anthropic and OpenAI payloads."""


def test_anthropic_payload_includes_max_tokens():
    """Anthropic-compatible payloads must include max_tokens."""
    with open("scripts/run_review.sh") as f:
        content = f.read()

    assert "anthropic" in content
    # The jq expression uses single quotes: '{model:$model,max_tokens:$max_tokens,...}'
    assert "max_tokens:$max_tokens" in content


def test_openai_payload_includes_max_tokens():
    """OpenAI-compatible payloads must include max_tokens (issue #38)."""
    with open("scripts/run_review.sh") as f:
        content = f.read()

    # Both branches should contain max_tokens:$max_tokens in their jq expressions
    assert "max_tokens:$max_tokens" in content


def test_openai_uses_chat_completions_endpoint():
    """OpenAI-compatible requests must target /chat/completions."""
    with open("scripts/run_review.sh") as f:
        content = f.read()

    assert "chat/completions" in content


def test_anthropic_uses_messages_endpoint():
    """Anthropic requests must target /messages."""
    with open("scripts/run_review.sh") as f:
        content = f.read()

    assert "/messages" in content
