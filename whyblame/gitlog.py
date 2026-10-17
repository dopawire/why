"""Read the history of a range of lines straight from git."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Field/record separators unlikely to appear in commit messages.
_REC, _FLD, _END = "\x1e", "\x1f", "\x1d"
_FORMAT = f"{_REC}%H{_FLD}%an{_FLD}%aI{_FLD}%s{_FLD}%b{_END}"

# PR/issue references in commit messages: "#123", "GH-123", or full URLs.
_REF_PATTERNS = [
    re.compile(r"(?<![\w/&])#(\d+)\b"),
    re.compile(r"\bGH-(\d+)\b", re.IGNORECASE),
    re.compile(r"github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/(\d+)"),
]


class GitError(RuntimeError):
    pass


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    body: str
    patch: str = ""
    refs: list[int] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.sha[:10]


def run_git(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as e:
        raise GitError("git is not installed or not on PATH") from e
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def repo_root(path: Path) -> Path:
    start = path if path.is_dir() else path.parent
    return Path(run_git(["rev-parse", "--show-toplevel"], start).strip())


def extract_refs(text: str) -> list[int]:
    """Return PR/issue numbers mentioned in text, in order of first appearance."""
    seen: dict[int, None] = {}
    for pattern in _REF_PATTERNS:
        for m in pattern.finditer(text):
            seen.setdefault(int(m.group(1)), None)
    return list(seen)


def read_file(root: Path, rel: str, start: int) -> list[str]:
    lines = (root / rel).read_text(errors="replace").splitlines()
    if start < 1 or start > len(lines):
        raise GitError(f"{rel} has {len(lines)} lines; line {start} is out of range")
    return lines


def blame_url(root: Path, rel: str, start: int, end: int) -> str | None:
    """GitHub's blame page for these lines, at the pushed commit if there is one."""
    slug = remote_slug(root)
    if not slug:
        return None
    try:
        # Prefer the upstream branch: HEAD may hold commits GitHub has never seen.
        ref = run_git(["rev-parse", "@{upstream}"], root).strip()
    except GitError:
        ref = run_git(["rev-parse", "HEAD"], root).strip()
    lines = f"L{start}" if start == end else f"L{start}-L{end}"
    return f"https://github.com/{slug[0]}/{slug[1]}/blame/{ref}/{rel}#{lines}"


def history(root: Path, rel: str, start: int, end: int, max_commits: int) -> list[Commit]:
    """Commits that touched lines start..end of rel, newest first, with range-scoped patches."""
    out = run_git(
        ["log", f"-L{start},{end}:{rel}", f"--max-count={max_commits}", f"--format={_FORMAT}"],
        root,
    )
    commits = []
    for record in out.split(_REC)[1:]:
        header, _, patch = record.partition(_END)
        sha, author, date, subject, body = header.split(_FLD, 4)
        body = body.strip()
        commits.append(
            Commit(
                sha=sha,
                author=author,
                date=date,
                subject=subject,
                body=body,
                patch=patch.strip(),
                refs=extract_refs(f"{subject}\n{body}"),
            )
        )
    return commits


def remote_slug(root: Path, remote: str = "origin") -> tuple[str, str] | None:
    """(owner, repo) for a GitHub remote, or None if there isn't one."""
    try:
        url = run_git(["remote", "get-url", remote], root).strip()
    except GitError:
        return None
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    return (m.group(1), m.group(2)) if m else None
