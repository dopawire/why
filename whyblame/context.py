"""Pick the code around the asked-about lines that a reader needs to understand them."""

from __future__ import annotations

import re

WINDOW = 25  # lines kept on each side of the range
MAX_DEFS = 10
MAX_DEF_LINES = 12
SMALL_FILE = 120  # files this short are sent whole

_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_DEF_KEYWORDS = (
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:const|let|var|def|class|function|fn|func|type|interface|struct|enum)"
)


def _definition_line(lines: list[str], name: str) -> int | None:
    """0-based index of the line that defines name, if it looks like a definition."""
    by_keyword = re.compile(rf"^\s*{_DEF_KEYWORDS}\s+{re.escape(name)}\b")
    by_assignment = re.compile(rf"^\s*(?:self\.)?{re.escape(name)}\s*(?::[^=]+)?=(?!=)")
    for i, line in enumerate(lines):
        if by_keyword.match(line) or by_assignment.match(line):
            return i
    return None


def _block(lines: list[str], i: int) -> range:
    """The definition at i plus its continuation, up to a blank line."""
    end = i + 1
    while end < len(lines) and end - i < MAX_DEF_LINES and lines[end].strip():
        end += 1
    return range(i, end)


def surrounding(lines: list[str], start: int, end: int) -> list[tuple[int, str]]:
    """(1-based line number, text) pairs worth showing alongside lines start..end.

    Returns the whole file when it's small; otherwise a window around the range, the
    file's opening comment, and the definitions of names the range uses.
    """
    if len(lines) <= SMALL_FILE:
        keep = set(range(len(lines)))
    else:
        lo, hi = max(0, start - 1 - WINDOW), min(len(lines), end + WINDOW)
        keep = set(range(lo, hi))
        # Header comments often carry file-wide rules ("bump X when Y changes").
        for i, line in enumerate(lines[:15]):
            if not line.strip().startswith(("#", "//", "/*", "*", '"""', "'''")):
                break
            keep.add(i)
        names = dict.fromkeys(_IDENT.findall("\n".join(lines[start - 1 : end])))
        found = 0
        for name in names:
            if found >= MAX_DEFS or len(name) < 2:
                continue
            i = _definition_line(lines, name)
            if i is not None and i not in keep:
                keep.update(_block(lines, i))
                found += 1
    return [(i + 1, lines[i]) for i in sorted(keep)]
