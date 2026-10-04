"""Read-only access to the lab notebook: the markdown reports of the
inference experiments run on this machine.

It is internal material (addresses, paths, service names), so agents read it
through tools to turn findings into posts; text.check_post keeps addresses
and paths out of the posts themselves.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
PART_CHARS = 12000


@dataclass
class Note:
    path: str  # relative to the notebook
    title: str
    modified: datetime
    size: int


class Lab:
    def __init__(self, root: Path, exclude: list[str]):
        self.root = root
        # File names or directory names anywhere in the path.
        self.exclude = set(exclude)

    def available(self) -> bool:
        return self.root.is_dir()

    def notes(self) -> list[Note]:
        found = []
        if not self.available():
            return found
        # os.walk, so excluded directories (vendored repos, models) are never
        # entered: the notebook sits next to hundreds of GB of other files.
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in self.exclude]
            for name in filenames:
                if not name.endswith(".md") or name in self.exclude:
                    continue
                p = Path(dirpath) / name
                st = p.stat()
                found.append(Note(str(p.relative_to(self.root)), _title(p), datetime.fromtimestamp(st.st_mtime),
                                  st.st_size))
        return sorted(found, key=lambda n: n.modified, reverse=True)

    def _file(self, path: str) -> Path:
        """The file for a path an agent gave, only if it is a listed note."""
        wanted = path.strip().lstrip("/")
        for n in self.notes():
            if n.path == wanted or Path(n.path).name == wanted:
                return self.root / n.path
        raise FileNotFoundError(f"no lab note {path!r}; the index lists the available ones")

    def read(self, path: str, section: str = "", part: int = 1) -> str:
        p = self._file(path)
        lines = p.read_text(errors="replace").splitlines()
        headings = [(i, len(m.group(1)), m.group(2).strip()) for i, line in enumerate(lines)
                    if (m := HEADING_RE.match(line))]
        if section:
            want = section.lower().strip()
            match = [h for h in headings if want in h[2].lower()]
            # Prefer an exact heading; otherwise skip the document title,
            # which contains everything, when a narrower heading matches.
            match = [h for h in match if h[2].lower() == want] or [h for h in match if h[1] > 1] or match
            if not match:
                return f"no section matching {section!r}. Sections:\n" + _outline(headings)
            start, level, _ = match[0]
            end = next((i for i, lv, _ in headings if i > start and lv <= level), len(lines))
            text = "\n".join(lines[start:end])
            return text[:PART_CHARS] + ("\n[section truncated]" if len(text) > PART_CHARS else "")
        text = "\n".join(lines)
        parts = max(1, -(-len(text) // PART_CHARS))
        part = min(max(part, 1), parts)
        chunk = text[(part - 1) * PART_CHARS: part * PART_CHARS]
        if parts == 1:
            return chunk
        return (f"[part {part} of {parts}; ask for another part, or for a section by name]\n"
                f"Sections:\n{_outline(headings)}\n\n{chunk}")

    def search(self, query: str, limit: int = 40) -> str:
        words = [w.lower() for w in query.split() if w]
        if not words:
            return "empty query"
        hits = []
        for n in self.notes():
            lines = (self.root / n.path).read_text(errors="replace").splitlines()
            for i, line in enumerate(lines):
                if all(w in line.lower() for w in words):
                    hits.append(f"{n.path}:{i + 1}: {line.strip()[:300]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + "\n[more matches; narrow the query]"
        return "\n".join(hits) or "no matches"


def _title(p: Path) -> str:
    with p.open(errors="replace") as f:
        for line in f:
            if m := HEADING_RE.match(line):
                return m.group(2).strip()
    return p.stem


def _outline(headings: list[tuple[int, int, str]]) -> str:
    return "\n".join(f"{'  ' * (level - 1)}- {title}" for _, level, title in headings if level <= 3)
