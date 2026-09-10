"""Tests for the allowlisted remote text-file tool."""

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from pr_reviewer.tool_executors import (  # noqa: E402
    execute_tool_request,
    read_remote_file,
)
from pr_reviewer.specialist_runtime.evidence import EvidenceStore  # noqa: E402

def _call(**kwargs):
    defaults = {
        "repository": "other/project",
        "path": "docs/contract.md",
        "ref": "a" * 40,
        "allowed_repos": {"other/project"},
        "current_repo": "current/repository",
    }
    defaults.update(kwargs)
    return read_remote_file(**defaults)


def test_raw_remote_file_with_empty_metadata_content(monkeypatch):
    monkeypatch.setattr("pr_reviewer.platform.gh_api", lambda *a: {
        "data": {"type": "file", "encoding": "none", "content": "", "size": 1200000},
    })
    monkeypatch.setattr("pr_reviewer.platform.gh_raw_file", lambda *a: {
        "content": b"first\nsecond\n",
    }, raising=False)
    assert _call()["content"] == "first\nsecond\n"


def test_oversized_remote_file_rejected_before_content_download(monkeypatch):
    monkeypatch.setattr("pr_reviewer.platform.gh_api", lambda *a: {
        "data": {"type": "file", "encoding": "none", "size": 100000000},
    })
    def fail(*args):
        raise AssertionError("must not download oversized content")
    monkeypatch.setattr("pr_reviewer.platform.gh_raw_file", fail, raising=False)
    assert "download limit" in _call()["error"]


def test_reads_allowlisted_remote_text_file_with_a_bounded_line_window(monkeypatch):
    seen = {}
    content = "first\nsecond\nthird\n"
    def fetch(endpoint, allowed_repos, current_repo, timeout):
        seen.update({
            "endpoint": endpoint,
            "allowed_repos": allowed_repos,
            "current_repo": current_repo,
            "timeout": timeout,
        })
        return {"data": {
            "type": "file", "size": len(content.encode()),
        }}
    monkeypatch.setattr(
        "pr_reviewer.platform.gh_api",
        fetch,
    )
    monkeypatch.setattr("pr_reviewer.platform.gh_raw_file", lambda *a: {"content": content.encode()})

    result = _call(offset=2, limit=2, include_line_numbers=True)

    assert result["content"] == "LINE 2 | second\nLINE 3 | third\n"
    assert result["range"] == {
        "offset": 2, "lines": 2, "total_lines": 3,
        "truncated": False, "has_more": False,
    }
    assert seen == {
        "endpoint": (
            "repos/other/project/contents/docs/contract.md?ref=" + "a" * 40
        ),
        "allowed_repos": {"other/project"},
        "current_repo": "",
        "timeout": 25,
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repository": "current/repository"}, "current repository"),
        ({"repository": "Current/Repository"}, "current repository"),
        ({"repository": "unknown/project"}, "not allowed"),
        ({"allowed_repos": {"*"}}, "not allowed"),
        ({"current_repo": ""}, "current repository identity"),
        ({"ref": "main"}, "immutable"),
        ({"path": "../secret.txt"}, "path"),
    ],
)
def test_rejects_unsafe_remote_file_requests_before_network(monkeypatch, kwargs, message):
    def fail(*_args, **_kwargs):
        raise AssertionError("network must not be contacted")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = _call(**kwargs)
    assert message in result["error"].lower()


def test_rejects_binary_remote_content(monkeypatch):
    monkeypatch.setattr(
        "pr_reviewer.platform.gh_api",
        lambda *_args, **_kwargs: {"data": {
            "type": "file", "size": 13,
        }},
    )

    monkeypatch.setattr("pr_reviewer.platform.gh_raw_file", lambda *a: {"content": b"text\x00not-text"})
    result = _call()

    assert "binary" in result["error"].lower()


def test_remote_text_reports_byte_truncation(monkeypatch):
    monkeypatch.setattr(
        "pr_reviewer.platform.gh_api",
        lambda *_args, **_kwargs: {"data": {
            "type": "file", "size": 500,
        }},
    )

    monkeypatch.setattr("pr_reviewer.platform.gh_raw_file", lambda *a: {"content": b"line\n" * 100})
    result = _call(max_response_bytes=80)

    assert result["range"]["truncated"] is True
    assert result["range"]["has_more"] is True
    assert result["content"].endswith("[truncated]")


@pytest.mark.parametrize("length,body", ((None, b"x" * 100), ("999", b"x" * 100), ("5", b"hello")))
def test_raw_transfer_enforces_limit_even_with_missing_length(monkeypatch, length, body):
    from pr_reviewer.platform import gh_raw_file
    response = io.BytesIO(body)
    response.headers = {} if length is None else {"Content-Length": length}
    reads = []
    original_read = response.read
    def read(size):
        reads.append(size)
        return original_read(size)
    response.read = read
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Accept") == "application/vnd.github.raw+json"
            assert "ref=" + "a" * 40 in request.full_url
            return response
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("PLATFORM", "github")
    monkeypatch.setattr("urllib.request.build_opener", lambda *a: Opener())
    result = gh_raw_file("repos/other/project/contents/a.js?ref=" + "a" * 40,
                         {"other/project"}, 25, 10)
    if len(body) > 10:
        assert "download limit" in result["error"]
    else:
        assert result == {"content": b"hello"}
    assert reads == ([] if length == "999" else [11])
    assert response.closed


def test_gh_api_does_not_read_contents_files(monkeypatch, tmp_path):
    result = execute_tool_request(
        "gh_api",
        {"endpoint": "repos/other/project/contents/docs/contract.md"},
        str(tmp_path),
        {"other/project"},
        "current/repository",
        set(),
        8000,
        1,
    )

    assert result["status"] == "error"
    assert "read_remote_file" in result["result"]["error"]


@pytest.mark.parametrize("endpoint", [
    "repos/other/project/git/blobs/" + "a" * 40,
    "repos/other/project/readme",
    "repos/other/project/readme?ref=main",
    "repos/other/project/readme/docs",
])
def test_gh_api_file_endpoints_require_remote_file_tool(tmp_path, endpoint):
    result = execute_tool_request(
        "gh_api",
        {"endpoint": endpoint},
        str(tmp_path),
        {"other/project"},
        "current/repository",
        set(),
        8000,
        1,
    )

    assert result["status"] == "error"
    assert "read_remote_file" in result["result"]["error"]


def test_remote_file_evidence_cannot_look_like_a_current_repository_path():
    store = EvidenceStore()
    record, _collection = store.add_tool_result_with_collection(
        session_id="session-1",
        tool="read_remote_file",
        arguments={
            "repository": "other/project",
            "path": "action.yml",
            "ref": "a" * 40,
        },
        result={"status": "ok", "result": {"content": "name: remote"}},
    )

    assert record.source_path == "@remote/other/project@" + "a" * 40 + "/action.yml"
