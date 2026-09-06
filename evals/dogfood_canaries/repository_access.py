"""Evaluation-only example used by offline review exercises."""

from pathlib import Path


def read_repository_file(
    repository_root: Path, relative_path: str,
) -> str:
    """Load a requested repository file."""
    return (
        (repository_root / relative_path).read_text(encoding="utf-8")
    )


def delivery_failure_diagnostic(api_token: str, status_code: int) -> str:
    return f"Webhook delivery failed ({status_code}); api_token={api_token}"
