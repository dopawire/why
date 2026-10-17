"""why: git blame for reasons."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import webbrowser
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .explain import BackendError, explain, pick_backend
from .gitlog import GitError, blame_url, repo_root
from .story import Story, gather


def parse_target(target: str, lines: str | None) -> tuple[Path, int, int]:
    """Accept file:LINE, file:START-END, or file plus -L START[,END]."""
    spec = lines
    m = re.fullmatch(r"(.+?):(\d+(?:[-,]\d+)?)", target)
    if m and not Path(target).exists():
        target, spec = m.group(1), m.group(2)
    if not spec:
        raise ValueError("say which lines: why FILE:LINE, why FILE:START-END, or -L START,END")
    start, _, end = spec.replace(",", "-").partition("-")
    start_n = int(start)
    end_n = int(end) if end else start_n
    if start_n < 1 or end_n < start_n:
        raise ValueError(f"bad line range: {spec}")
    return Path(target), start_n, end_n


def _style(code: str):
    on = sys.stdout.isatty()
    return lambda s: f"\033[{code}m{s}\033[0m" if on else s


bold, dim, yellow = _style("1"), _style("2"), _style("33")


def print_excerpt(text: str, max_lines: int = 8) -> None:
    """Show the human-written part of a source, so the evidence is readable without a model."""
    # Re-flow hard-wrapped paragraphs, then wrap to the terminal-friendly width.
    paras = [" ".join(p.split()) for p in text.strip().split("\n\n") if p.strip()]
    lines = [line for p in paras for line in textwrap.wrap(p, 88) + [""]][:-1]
    for line in lines[:max_lines]:
        print(f"       │ {line}")
    if len(lines) > max_lines:
        print(dim(f"       │ … {len(lines) - max_lines} more lines (open the link)"))


def print_story(story: Story, summary: str | None, backend: str) -> None:
    rng = f"{story.start}" if story.start == story.end else f"{story.start}-{story.end}"
    print(bold(f"why {story.path}:{rng}"))
    for i, line in enumerate(story.code[:8]):
        print(dim(f"{story.start + i:>6} │ ") + line)
    if len(story.code) > 8:
        print(dim(f"{'':>6} │ … {len(story.code) - 8} more lines"))
    print()

    if summary:
        for para in summary.split("\n"):
            print(textwrap.fill(para, 100) if para.strip() else "")
        print()
    elif not story.sources:
        print("No history found for these lines.")
        print()
    else:
        if not story.has_discussion:
            print(yellow("No linked pull request or issue found. Only commit messages below."))
        if backend == "none":
            print(dim("(no model configured; showing raw evidence. See `why --help` for backends.)"))
        print()

    print(bold("Sources"))
    for s in story.sources:
        date = s.date[:10]
        print(f"  [{s.id}] {dim(f'{s.kind:<6}')} {date}  {s.title[:70]}  {dim('@' + s.author)}")
        print(f"       {dim(s.url)}")
        if not summary and s.excerpt:
            print_excerpt(s.excerpt)
    for w in story.warnings:
        print(yellow(f"note: {w}"), file=sys.stderr)


def open_web(path: Path, start: int, end: int) -> int:
    root = repo_root(path.resolve())
    rel = path.resolve().relative_to(root).as_posix()
    url = blame_url(root, rel, start, end)
    if not url:
        raise ValueError("--web needs a GitHub remote named origin")
    print(url)
    webbrowser.open(url)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="why",
        description="Explain why code is the way it is, from its git history and linked discussions.",
        epilog=textwrap.dedent(
            """\
            examples:
              why src/retry.py:42
              why src/retry.py:40-58
              why src/retry.py -L 40,58 --backend none
              why src/retry.py:42 --web     open the line in GitHub's blame view

            backends (auto picks the first that works):
              claude      needs `pip install 'why-blame[claude]'` and ANTHROPIC_API_KEY
              openrouter  needs OPENROUTER_API_KEY; any model via --model or WHY_OPENROUTER_MODEL
              ollama      local models; set --model or WHY_OLLAMA_MODEL, OLLAMA_HOST
              none        just print the evidence, no model involved

            set defaults once in your shell profile:
              export WHY_BACKEND=openrouter
              export WHY_MODEL=anthropic/claude-sonnet-5.5

            GitHub data uses GITHUB_TOKEN, GH_TOKEN, or `gh auth token` when present."""
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", help="FILE:LINE or FILE:START-END")
    p.add_argument("-L", dest="lines", help="line range START[,END] (alternative to FILE:LINE)")
    backends = ["auto", "claude", "openrouter", "ollama", "none"]
    p.add_argument(
        "--backend",
        choices=backends,
        default=os.environ.get("WHY_BACKEND", "auto"),
        help="which model service to use (default: $WHY_BACKEND, else auto)",
    )
    p.add_argument(
        "--model",
        default=os.environ.get("WHY_MODEL"),
        help="model for the chosen backend (default: $WHY_MODEL, else the backend's default)",
    )
    p.add_argument("--max-commits", type=int, default=10, metavar="N")
    p.add_argument("--offline", action="store_true", help="don't contact GitHub; git history only")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--web", action="store_true", help="open these lines in GitHub's blame view and exit"
    )
    p.add_argument("--version", action="version", version=f"why {__version__}")
    args = p.parse_args(argv)
    # argparse doesn't check defaults against choices, so a typo in $WHY_BACKEND needs this.
    if args.backend not in backends:
        p.error(f"WHY_BACKEND={args.backend!r} is not one of: {', '.join(backends)}")

    try:
        path, start, end = parse_target(args.target, args.lines)
        if not path.is_file():
            raise ValueError(f"no such file: {path}")
        if args.web:
            return open_web(path, start, end)
        story = gather(path, start, end, max_commits=args.max_commits, use_forge=not args.offline)
        backend = pick_backend(args.backend)
        summary = explain(story, backend, args.model)
    except (ValueError, GitError, BackendError) as e:
        print(f"why: {e}", file=sys.stderr)
        return 1

    if args.json:
        out = asdict(story) | {"summary": summary, "backend": backend}
        json.dump(out, sys.stdout, indent=2)
        print()
    else:
        print_story(story, summary, backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
