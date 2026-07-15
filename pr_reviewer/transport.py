#!/usr/bin/env python3
"""HTTP/subprocess transport for the tool harness (#304 split).

Owns the low-level model-call transport (curl-based chat requests + the simple
one-shot completion) and the shared subprocess runner. Split out of
scripts/run_tool_harness.py with no behaviour change.
"""

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# mask_secrets lives in scripts/redact.py; ensure scripts/ is importable when
# this package module is loaded on its own.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from redact import mask_secrets  # noqa: E402


class ModelRequestError(RuntimeError):
    """A redacted model transport or provider error."""

    def __init__(self, message, *, status=None, body="", timeout=False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.timeout = timeout

    @property
    def provider_rejected(self):
        return self.status is not None and 400 <= self.status < 500


@dataclass
class StreamingResult:
    """Captured result from a line-oriented curl streaming request."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    interrupted: bool = False
    watchdog_reason: str = ""


def safe_run(args, timeout_sec):
    """Run a command and capture stdout/stderr with a timeout."""
    try:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "timeout": True,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
        }


def safe_run_streaming(
    args,
    timeout_sec,
    on_line: Callable[[str], bool],
) -> StreamingResult:
    """Run curl while allowing a caller to interrupt an SSE response."""

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    output: list[str] = []
    stderr = ""
    interrupted = False
    timed_out = False
    try:
        if process.stdout is not None:
            for line in process.stdout:
                output.append(line)
                if on_line(line):
                    interrupted = True
                    process.terminate()
                    break
        try:
            tail_stdout, stderr = process.communicate(
                timeout=2 if interrupted else timeout_sec
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            process.kill()
            tail_stdout, stderr = process.communicate()
            if not stderr:
                stderr = exc.stderr or ""
        if not interrupted and tail_stdout:
            output.append(tail_stdout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        process.kill()
        tail_stdout, stderr = process.communicate()
        output.append(tail_stdout or "")
        if not stderr:
            stderr = exc.stderr or ""
    finally:
        if process.stdout is not None and hasattr(process.stdout, "close"):
            process.stdout.close()

    return StreamingResult(
        stdout="".join(output),
        stderr=stderr or "",
        returncode=process.returncode,
        timed_out=timed_out,
        interrupted=interrupted,
        watchdog_reason=(getattr(on_line, "reason", "") if interrupted else ""),
    )


def run_chat_request(
    base_url,
    api_format,
    payload,
    api_key,
    timeout_sec,
    *,
    stream_watchdog=None,
):
    """POST a wire-ready chat payload via curl and return the parsed JSON.

    Transport for the native tool-calling loop (#203): the payload is built
    by ``pr_reviewer.conversation.Conversation.to_request_payload``, so this
    function owns only the endpoint choice, auth, and JSON decode.
    """
    if api_format == "anthropic":
        endpoint = base_url.rstrip("/") + "/messages"
    else:
        endpoint = base_url.rstrip("/") + "/chat/completions"

    curl_args = [
        "curl",
        "-q",
        "-sSL",
        "--max-time",
        str(timeout_sec),
        endpoint,
        "-H",
        "Content-Type: application/json",
        "--write-out",
        "\n%{http_code}",
    ]
    if api_format == "anthropic":
        curl_args.extend(["-H", f"anthropic-version: {os.getenv('ANTHROPIC_VERSION', '2023-06-01')}"])

    # Streaming keeps bytes flowing so proxies with a short idle/read timeout
    # (Cloudflare's 100s edge timer etc.) don't 524 a long thinking-model turn.
    # --no-buffer flushes each SSE chunk; the body is reassembled below.
    streaming = bool(payload.get("stream"))
    if streaming:
        curl_args.append("--no-buffer")
        if api_format == "anthropic":
            curl_args.extend(["-H", "Accept: text/event-stream"])

    # The API key goes through a 0600 curl --config file rather than argv, so
    # it never appears in /proc/<pid>/cmdline or `ps` output on shared runners.
    auth_config_path = None
    if api_key:
        if api_format == "anthropic":
            auth_header = f"x-api-key: {api_key}"
        else:
            auth_header = f"Authorization: Bearer {api_key}"
        escaped = auth_header.replace("\\", "\\\\").replace('"', '\\"')
        fd, auth_config_path = tempfile.mkstemp()
        with os.fdopen(fd, "w", encoding="utf-8") as auth_file:
            auth_file.write(f'header = "{escaped}"\n')
        os.chmod(auth_config_path, 0o600)
        curl_args.extend(["--config", auth_config_path])

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as payload_file:
        json.dump(payload, payload_file)
        payload_path = payload_file.name

    try:
        command = curl_args + ["--data", f"@{payload_path}"]
        if streaming and stream_watchdog is not None:
            completed = safe_run_streaming(
                command,
                timeout_sec + 5,
                stream_watchdog,
            )
        else:
            completed = safe_run(command, timeout_sec + 5)
    finally:
        for cleanup_path in (payload_path, auth_config_path):
            if cleanup_path is None:
                continue
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass

    if (isinstance(completed, dict) and completed.get("timeout")) or (
        not isinstance(completed, dict) and getattr(completed, "timed_out", False)
    ):
        raise ModelRequestError("model request timed out", timeout=True)
    interrupted = bool(getattr(completed, "interrupted", False))
    if completed.returncode != 0 and not interrupted:
        stderr = mask_secrets((completed.stderr or "").strip())
        if len(stderr) > 500:
            stderr = stderr[:500] + "...[truncated]"
        raise ModelRequestError(
            f"model request failed with exit code {completed.returncode}"
            + (f": {stderr}" if stderr else "")
        )

    output = completed.stdout or ""
    body, separator, status_text = output.rpartition("\n")
    if separator and status_text.isdigit() and len(status_text) == 3:
        status = int(status_text)
    else:  # Test doubles and older curl wrappers may not append a status.
        body, status = output, 200
    if status >= 400:
        redacted = mask_secrets(body.strip())
        if len(redacted) > 1000:
            redacted = redacted[:1000] + "...[truncated]"
        raise ModelRequestError(
            f"model provider rejected request with HTTP {status}"
            + (f": {redacted}" if redacted else ""),
            status=status,
            body=redacted,
        )

    if streaming:
        # SSE deltas → the non-streaming response shape the loop parses. The
        # reassembler also surfaces a JSON error body returned mid-"stream"
        # (some servers reply 200 + an error object instead of events).
        from pr_reviewer.sse_reassembler import reassemble_sse  # noqa: PLC0415

        response = reassemble_sse(body, api_format)
        if interrupted:
            response["stream_watchdog_triggered"] = True
            response["stream_watchdog_reason"] = (
                getattr(completed, "watchdog_reason", "") or "stream-watchdog"
            )
        return response
    return json.loads(body)
