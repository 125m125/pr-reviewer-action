"""Reconcile owned access requests against current authorization, without retrieval."""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from pr_reviewer.platform import _validate_endpoint
from pr_reviewer.specialist_runtime.policy import ReviewPolicy, parse_review_policy
from pr_reviewer.specialist_runtime.web_evidence import (
    SourceDenied, SourcePolicy, authorize_search_target, github_search_target,
)


_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_UPDATE_MARKER = "<!-- ai-pr-review-access-granted:"
_UPDATE = re.compile(
    r"\A<!-- ai-pr-review-access-granted:v1:([0-9a-f]{64}) -->\n"
    r"\*\*Update — access granted\*\*\n\n"
    r"Current authorization confirmed by \[this run\]\((https://[^\s<>\[\]()]+)\) "
    r"at head `([0-9a-fA-F]{40,64})`\. "
    r"This does not confirm retrieval or resolve the underlying review issue\.\n\n---\n\n"
)
_IDENTITY = re.compile(
    r"\A### (Repository|Source) access request\n\n"
    r"\*\*(Repository|Host):\*\* <code>([^<>\n]+)</code>\n\n"
    r"\*\*(GitHub API endpoint|Candidate URL):\*\* <code>([^<>\n]+)</code>(?=\n\n)"
)


def _recognized_resource(body: str) -> tuple[str, str, str] | None:
    match = _IDENTITY.match(body)
    if match is None or len(re.findall(r"^### (?:Repository|Source) access request$", body, re.M)) != 1:
        return None
    kind, label, identity, resource_label, resource = match.groups()
    expected = ("Repository", "GitHub API endpoint") if kind == "Repository" else ("Host", "Candidate URL")
    if (label, resource_label) != expected:
        return None
    for field in ("Repository", "GitHub API endpoint", "Host", "Candidate URL"):
        if len(re.findall(r"^\*\*" + field + r":\*\*", body, re.M)) != int(field in expected):
            return None
    if not re.search(r"^\*\*Purposes?:\*\*", body, re.M) or not re.search(
        r"^\*\*Why human input is needed:\*\* <code>[^<>\n]+</code>$", body, re.M,
    ):
        return None
    pending = (
        "No repository content was retrieved; access remains pending current-branch human authorization."
        if kind == "Repository" else
        "Discovery metadata is not review evidence; retrieval remains pending approval."
    )
    if pending not in body.splitlines():
        return None
    identity, resource = html.unescape(identity), html.unescape(resource)
    if any(char.isspace() for char in identity + resource) or "REDACTED" in resource.upper():
        return None
    return kind, identity, resource


def _authorized(
    kind: str, identity: str, resource: str, *, policy: Mapping[str, Any] | ReviewPolicy | None,
    allowed: set[str], current_repository: str,
) -> bool:
    if kind == "Repository":
        if not _REPOSITORY.fullmatch(identity) or identity.casefold() not in allowed:
            return False
        validated = _validate_endpoint(resource, {identity}, "")
        return not validated.get("error") and validated.get("repo_key") == identity
    if urlsplit(resource).hostname != identity:
        return False
    target = github_search_target(resource)
    if target is not None:
        args = dict(target.arguments)
        repository = args.get("repository") or "/".join(args["endpoint"].split("/")[1:3])
        if repository.casefold() not in allowed:
            return False
        authorize_search_target(target, {repository}, current_repository)
        return True
    if not isinstance(policy, ReviewPolicy):
        policy = parse_review_policy(policy)
    return SourcePolicy.from_review_policy(policy).classify(resource).approved


def reconcile_access_request_comment(
    body: str, *, policy: Mapping[str, Any] | ReviewPolicy | None,
    allowed_github_repositories: Iterable[str], current_repository: str,
    run_url: str, head_sha: str,
) -> str | None:
    """Return an authorized update, or None for an unrecognized/denied request.

    The caller must verify publisher ownership and supply trusted effective
    configuration. A returned unchanged body means the update already exists;
    the caller may retry minimization. No retrieval or issue-resolution claim
    is made, and absence from the current run's requests is never a grant.
    """
    if not isinstance(body, str):
        return None
    original = body
    update = _UPDATE.match(body)
    if update:
        original = body[update.end():]
        if hashlib.sha256(original.encode()).hexdigest() != update[1]:
            return None
    if _UPDATE_MARKER in original:
        return None
    resource = _recognized_resource(original)
    if resource is None:
        return None
    try:
        allowed = {
            value.strip().strip("/").casefold() for value in allowed_github_repositories
            if isinstance(value, str) and _REPOSITORY.fullmatch(value.strip().strip("/"))
        }
        if not _authorized(*resource, policy=policy, allowed=allowed, current_repository=current_repository):
            return None
        run = urlsplit(run_url)
        if (
            not re.fullmatch(r"https://[^\s<>\[\]()]+", run_url)
            or not run.hostname or run.username is not None or run.password is not None
            or run.port not in (None, 443)
            or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head_sha)
        ):
            return None
    except (SourceDenied, ValueError, TypeError, KeyError):
        return None
    if update:
        return body
    digest = hashlib.sha256(original.encode()).hexdigest()
    return (
        f"{_UPDATE_MARKER}v1:{digest} -->\n"
        "**Update — access granted**\n\n"
        f"Current authorization confirmed by [this run]({run_url}) at head `{head_sha}`. "
        "This does not confirm retrieval or resolve the underlying review issue.\n\n---\n\n"
        + original
    )
