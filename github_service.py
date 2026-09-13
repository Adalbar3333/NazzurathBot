from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import aiohttp


POST_TAG = "tazzurath-homebrew-post"
EVENT_TAG = "tazzurath-homebrew-event"
TAG_PATTERN = re.compile(r"^<!--\s+([^:]+):([A-Za-z0-9_-]+)\s+-->\s*", re.DOTALL)


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class Proposal:
    number: int
    title: str
    body: str
    url: str
    author: str
    destination: str
    source_url: str
    created_at: datetime
    status: str = "open"
    approve: int = 0
    disapprove: int = 0

    @property
    def status_label(self) -> str:
        return "formal voting" if self.status == "voting" else "discussion open"


@dataclass(frozen=True)
class SitePage:
    path: str
    title: str
    url: str


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def decode_tagged_body(body: str, expected_tag: str) -> tuple[dict[str, Any], str] | None:
    match = TAG_PATTERN.match(body or "")
    if not match or match.group(1) != expected_tag:
        return None
    encoded = match.group(2)
    try:
        padding = "=" * (-len(encoded) % 4)
        metadata = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata, (body[match.end():] or "").strip()


def metadata_is_valid(metadata: dict[str, Any], secret: str) -> bool:
    if not secret:
        return True
    signature = metadata.get("signature")
    if not isinstance(signature, str):
        return False
    unsigned = {key: value for key, value in metadata.items() if key != "signature"}
    payload = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"))
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(signature, expected)


def proposal_from_issue(issue: dict[str, Any], signing_secret: str = "") -> Proposal | None:
    if "pull_request" in issue:
        return None
    tagged = decode_tagged_body(str(issue.get("body") or ""), POST_TAG)
    if not tagged:
        return None
    metadata, content = tagged
    if not metadata_is_valid(metadata, signing_secret):
        return None
    destination = metadata.get("destination") or {}
    destination_name = destination.get("kind", "homebrew") if isinstance(destination, dict) else "homebrew"
    author = metadata.get("authorNickname") or metadata.get("authorName") or "Member"
    return Proposal(
        number=int(issue["number"]),
        title=str(issue.get("title") or f"Homebrew #{issue['number']}"),
        body=content,
        url=str(issue.get("html_url") or ""),
        author=str(author),
        destination=str(destination_name).replace("-", " ").title(),
        source_url=str(metadata.get("sourceUrl") or ""),
        created_at=parse_timestamp(str(issue["created_at"])),
    )


def apply_comments(proposal: Proposal, comments: list[dict[str, Any]], signing_secret: str = "") -> Proposal:
    status = "open"
    votes: dict[str, tuple[str, int]] = {}
    for comment in comments:
        tagged = decode_tagged_body(str(comment.get("body") or ""), EVENT_TAG)
        if not tagged:
            continue
        metadata, _ = tagged
        if not metadata_is_valid(metadata, signing_secret):
            continue
        event_type = metadata.get("type")
        if event_type == "call-vote":
            status = "voting"
        elif event_type == "decision" and metadata.get("decision") in {"approved", "disapproved"}:
            status = str(metadata["decision"])
        elif event_type == "vote" and metadata.get("authorKey"):
            author_key = str(metadata["authorKey"])
            choice = metadata.get("choice")
            if choice in {"approve", "disapprove"}:
                try:
                    weight = max(1, min(10, int(metadata.get("weight") or 1)))
                except (TypeError, ValueError):
                    weight = 1
                votes[author_key] = (str(choice), weight)
            else:
                votes.pop(author_key, None)
    approve = sum(weight for choice, weight in votes.values() if choice == "approve")
    disapprove = sum(weight for choice, weight in votes.values() if choice == "disapprove")
    return replace(proposal, status=status, approve=approve, disapprove=disapprove)


def page_from_path(path: str, website_url: str) -> SitePage | None:
    if not path.startswith("content/") or not path.lower().endswith(".md"):
        return None
    relative = path[len("content/"):-len(".md")]
    if not relative:
        return None
    parts = PurePosixPath(relative).parts
    encoded_path = "/".join(quote(part, safe="") for part in parts)
    filename = parts[-2] if parts[-1].lower() == "index" and len(parts) > 1 else parts[-1]
    title = filename.replace("-", " ").replace("_", " ").strip().title()
    return SitePage(path=path, title=title, url=f"{website_url.rstrip('/')}/read/{encoded_path}")


class GitHubService:
    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        branch: str,
        website_url: str,
        signing_secret: str = "",
        max_posts: int = 40,
    ) -> None:
        self.token = token
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.website_url = website_url.rstrip("/")
        self.registry_url = f"{self.website_url}/homebrew-registry"
        self.signing_secret = signing_secret
        self.max_posts = max_posts
        self.session: aiohttp.ClientSession | None = None

    @classmethod
    def from_env(cls) -> "GitHubService":
        return cls(
            token=os.getenv("GITHUB_TOKEN", ""),
            owner=os.getenv("GITHUB_OWNER", "Adalbar3333"),
            repo=os.getenv("GITHUB_REPO", "Tazzurath-Website"),
            branch=os.getenv("GITHUB_BRANCH", "main"),
            website_url=os.getenv("WEBSITE_URL", "https://www.tazzurath.com"),
            signing_secret=os.getenv("HOMEBREW_SIGNING_SECRET", ""),
            max_posts=int(os.getenv("MAX_HOMEBREW_POSTS", "40")),
        )

    @property
    def configured(self) -> bool:
        return bool(self.token and self.owner and self.repo)

    async def start(self) -> None:
        if self.session is None:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "NazzurathBot/1.0",
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self.session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=60))

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.session is None:
            await self.start()
        assert self.session is not None
        url = f"https://api.github.com{path}"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self.session.get(url, params=params) as response:
                    if response.status < 400:
                        return await response.json()
                    detail = (await response.text())[:500]
                    error = GitHubError(f"GitHub returned {response.status} for {path}: {detail}")
                    if response.status < 500:
                        raise error
                    last_error = error
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        raise GitHubError(f"GitHub request failed for {path}: {last_error}") from last_error

    async def list_proposals(self, include_comments: bool) -> list[Proposal]:
        issues = await self.request(
            f"/repos/{self.owner}/{self.repo}/issues",
            {
                "labels": "tazzurath-homebrew",
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": min(self.max_posts, 100),
            },
        )
        proposals: list[Proposal] = []
        for issue in issues[: self.max_posts]:
            proposal = proposal_from_issue(issue, self.signing_secret)
            if proposal is None:
                continue
            if include_comments:
                comments = await self.request(
                    f"/repos/{self.owner}/{self.repo}/issues/{proposal.number}/comments",
                    {"per_page": 100},
                )
                proposal = apply_comments(proposal, comments, self.signing_secret)
            proposals.append(proposal)
        return proposals

    async def new_pages_between(self, start: datetime, end: datetime) -> list[SitePage]:
        commits: list[dict[str, Any]] = []
        for page_number in range(1, 11):
            batch = await self.request(
                f"/repos/{self.owner}/{self.repo}/commits",
                {
                    "sha": self.branch,
                    "since": start.astimezone().isoformat(),
                    "until": end.astimezone().isoformat(),
                    "per_page": 100,
                    "page": page_number,
                },
            )
            commits.extend(batch)
            if len(batch) < 100:
                break

        semaphore = asyncio.Semaphore(5)

        async def load_commit(commit: dict[str, Any]) -> Any:
            async with semaphore:
                return await self.request(f"/repos/{self.owner}/{self.repo}/commits/{commit['sha']}")

        ordered_commits = list(reversed(commits))
        details = await asyncio.gather(*(load_commit(commit) for commit in ordered_commits))
        found: dict[str, SitePage] = {}
        for detail in details:
            for changed in detail.get("files", []):
                if changed.get("status") != "added":
                    continue
                page = page_from_path(str(changed.get("filename") or ""), self.website_url)
                if page:
                    found[page.path] = page
        return sorted(found.values(), key=lambda item: item.title.casefold())
