"""Fetch the discussion behind commits from GitHub (pull requests, issues, reviews)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from .gitlog import extract_refs

API = "https://api.github.com"
MAX_TEXT = 4000  # chars kept per source, so one huge thread can't drown the rest
MAX_COMMENTS = 30


@dataclass
class Discussion:
    number: int
    kind: str  # "pull" or "issue"
    title: str
    url: str
    author: str
    date: str
    body: str
    comments: list[tuple[str, str]]  # (author, text)


def _token() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def _clip(text: str | None, limit: int = MAX_TEXT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


class GitHub:
    def __init__(self, owner: str, repo: str):
        self.base = f"{API}/repos/{owner}/{repo}"
        self.token = _token()
        self.warnings: list[str] = []
        self._cache: dict[str, object] = {}

    def _get(self, path: str):
        if path in self._cache:
            return self._cache[path]
        req = urllib.request.Request(f"{self.base}{path}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                data = None
            elif e.code in (401, 403, 429):
                msg = f"GitHub API refused the request ({e.code})"
                if not self.token:
                    msg += "; set GITHUB_TOKEN or run `gh auth login` for higher rate limits"
                if msg not in self.warnings:
                    self.warnings.append(msg)
                data = None
            else:
                raise
        except urllib.error.URLError as e:
            msg = f"could not reach GitHub: {e.reason}"
            if msg not in self.warnings:
                self.warnings.append(msg)
            data = None
        self._cache[path] = data
        return data

    def pulls_for_commit(self, sha: str) -> list[int]:
        data = self._get(f"/commits/{sha}/pulls") or []
        return [pr["number"] for pr in data]

    def discussion(self, number: int, path_filter: str | None = None) -> Discussion | None:
        issue = self._get(f"/issues/{number}")
        if not issue:
            return None
        is_pull = "pull_request" in issue
        comments = [
            (c["user"]["login"], c["body"])
            for c in (self._get(f"/issues/{number}/comments?per_page={MAX_COMMENTS}") or [])
        ]
        if is_pull:
            for r in self._get(f"/pulls/{number}/reviews?per_page={MAX_COMMENTS}") or []:
                if (r.get("body") or "").strip():
                    comments.append((r["user"]["login"], f"(review, {r['state'].lower()}) {r['body']}"))
            for c in self._get(f"/pulls/{number}/comments?per_page=100") or []:
                # Line-level review comments are the gold, but only on the file we care about.
                if path_filter is None or c.get("path") == path_filter:
                    comments.append((c["user"]["login"], f"(on {c['path']}) {c['body']}"))
        return Discussion(
            number=number,
            kind="pull" if is_pull else "issue",
            title=issue["title"],
            url=issue["html_url"],
            author=issue["user"]["login"],
            date=issue["created_at"],
            body=_clip(issue.get("body")),
            comments=[(a, _clip(t, 1500)) for a, t in comments[:MAX_COMMENTS]],
        )

    def linked_issues(self, d: Discussion) -> list[int]:
        """Issue numbers a PR body points at ("Fixes #12", URLs, ...)."""
        return [n for n in extract_refs(d.body) if n != d.number]
