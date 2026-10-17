"""Gather every piece of evidence about why a range of lines looks the way it does."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import gitlog
from .context import surrounding
from .forge import GitHub

MAX_PATCH = 3000
MAX_DISCUSSIONS = 12


@dataclass
class Source:
    id: int
    kind: str  # "commit", "pull", "issue"
    title: str
    url: str
    author: str
    date: str
    text: str
    excerpt: str = ""  # the human-written part, shown when no model summarizes


@dataclass
class Story:
    path: str
    start: int
    end: int
    code: list[str]
    context: list[tuple[int, str]] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_discussion(self) -> bool:
        return any(s.kind != "commit" for s in self.sources)

    def add(self, **kw) -> Source:
        src = Source(id=len(self.sources) + 1, **kw)
        self.sources.append(src)
        return src


def gather(
    file: Path, start: int, end: int, *, max_commits: int = 10, use_forge: bool = True
) -> Story:
    root = gitlog.repo_root(file.resolve())
    rel = file.resolve().relative_to(root).as_posix()
    lines = gitlog.read_file(root, rel, start)
    end = min(end, len(lines))
    story = Story(rel, start, end, lines[start - 1 : end], surrounding(lines, start, end))

    commits = gitlog.history(root, rel, start, end, max_commits)
    slug = gitlog.remote_slug(root)
    gh = GitHub(*slug) if (slug and use_forge) else None

    for c in commits:
        patch = c.patch if len(c.patch) <= MAX_PATCH else c.patch[:MAX_PATCH] + "\n…[truncated]"
        story.add(
            kind="commit",
            title=c.subject,
            url=f"https://github.com/{slug[0]}/{slug[1]}/commit/{c.sha}" if slug else c.sha,
            author=c.author,
            date=c.date,
            text=f"commit {c.sha}\n\n{c.subject}\n\n{c.body}\n\n{patch}".strip(),
            excerpt=c.body,
        )

    if gh:
        seen: set[int] = set()
        queue: list[int] = []
        for c in commits:
            for n in gh.pulls_for_commit(c.sha) + c.refs:
                if n not in seen:
                    seen.add(n)
                    queue.append(n)
        while queue and sum(s.kind != "commit" for s in story.sources) < MAX_DISCUSSIONS:
            d = gh.discussion(queue.pop(0), path_filter=rel)
            if d is None:
                continue
            thread = "\n\n".join(f"@{a}: {t}" for a, t in d.comments)
            story.add(
                kind=d.kind,
                title=f"#{d.number} {d.title}",
                url=d.url,
                author=d.author,
                date=d.date,
                text=f"{d.body}\n\n--- comments ---\n{thread}" if thread else d.body,
                excerpt=d.body,
            )
            if d.kind == "pull":
                for n in gh.linked_issues(d):
                    if n not in seen:
                        seen.add(n)
                        queue.append(n)
        story.warnings.extend(gh.warnings)
    elif use_forge and not slug:
        story.warnings.append("no GitHub remote found; using git history only")

    if not commits:
        story.warnings.append("git has no history for these lines (uncommitted?)")
    return story
