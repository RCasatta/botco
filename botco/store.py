"""Durable state in SQLite.

Shared by the main thread and the agent threads, always under
`World.lock`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    week TEXT NOT NULL,            -- ISO week, e.g. 2026-W40
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,             -- the day it was created
    slot INTEGER NOT NULL DEFAULT 0,  -- unused since the agents version
    text TEXT NOT NULL,
    status TEXT NOT NULL,          -- draft, rejected or published
    revisions INTEGER NOT NULL DEFAULT 0,
    feedback TEXT,                 -- the editor's latest comments
    topic TEXT NOT NULL,
    msg_id INTEGER,
    review_msg_id INTEGER,         -- unused since the agents version
    x_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS draft_msgs (
    msg_id INTEGER PRIMARY KEY,    -- any Zulip message showing a draft
    draft_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    day TEXT PRIMARY KEY,
    followers INTEGER,
    following INTEGER,
    posts INTEGER,
    taken_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

# Columns added by the agents version, with how to fill them from the
# statuses of the earlier fixed pipeline.
MIGRATION = """
ALTER TABLE drafts ADD COLUMN author TEXT NOT NULL DEFAULT 'writer';
ALTER TABLE drafts ADD COLUMN editor_ok INTEGER NOT NULL DEFAULT 0;
ALTER TABLE drafts ADD COLUMN ceo_ok INTEGER NOT NULL DEFAULT 0;
UPDATE drafts SET editor_ok = 1 WHERE status IN ('awaiting_ceo', 'approved');
UPDATE drafts SET ceo_ok = 1 WHERE status = 'approved';
UPDATE drafts SET status = 'draft' WHERE status IN ('review', 'revise', 'awaiting_ceo', 'approved');
UPDATE drafts SET status = 'rejected' WHERE status = 'dropped';
INSERT OR IGNORE INTO draft_msgs SELECT msg_id, id FROM drafts WHERE msg_id IS NOT NULL;
INSERT OR IGNORE INTO draft_msgs SELECT review_msg_id, id FROM drafts WHERE review_msg_id IS NOT NULL;
"""


@dataclass
class Draft:
    id: int
    day: str
    slot: int
    text: str
    status: str
    revisions: int
    feedback: str | None
    topic: str
    msg_id: int | None
    review_msg_id: int | None
    x_id: str | None
    created_at: str
    updated_at: str
    published_at: str | None
    author: str
    editor_ok: int
    ceo_ok: int


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(drafts)")}
        if "editor_ok" not in cols:
            self.db.executescript(f"BEGIN; {MIGRATION} COMMIT;")

    @staticmethod
    def now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    # key/value

    def get(self, k: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else default

    def put(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))

    def incr(self, k: str) -> int:
        n = int(self.get(k, "0")) + 1
        self.put(k, str(n))
        return n

    # plans

    def latest_plan(self, week: str | None = None) -> sqlite3.Row | None:
        if week:
            q, args = "SELECT * FROM plans WHERE week = ? ORDER BY id DESC LIMIT 1", (week,)
        else:
            q, args = "SELECT * FROM plans ORDER BY id DESC LIMIT 1", ()
        return self.db.execute(q, args).fetchone()

    def add_plan(self, week: str, text: str) -> int:
        cur = self.db.execute("INSERT INTO plans(week, text, created_at) VALUES (?, ?, ?)", (week, text, self.now()))
        return cur.lastrowid

    # drafts

    def _drafts(self, where: str, args: tuple = (), order: str = "id") -> list[Draft]:
        return [Draft(**dict(r)) for r in self.db.execute(f"SELECT * FROM drafts WHERE {where} ORDER BY {order}", args)]

    def draft(self, draft_id: int) -> Draft | None:
        found = self._drafts("id = ?", (draft_id,))
        return found[0] if found else None

    def open_drafts(self) -> list[Draft]:
        return self._drafts("status = 'draft'")

    def ready(self, need_ceo: bool) -> list[Draft]:
        """Drafts that may be published, oldest first."""
        return self._drafts("status = 'draft' AND editor_ok = 1" + (" AND ceo_ok = 1" if need_ceo else ""))

    def published(self, limit: int = 30) -> list[Draft]:
        return self._drafts("status = 'published'", order=f"published_at DESC LIMIT {int(limit)}")

    def add_draft(self, day: str, text: str, author: str) -> int:
        now = self.now()
        cur = self.db.execute(
            # slot is set because databases from the fixed pipeline have it
            # without a default.
            "INSERT INTO drafts(day, slot, text, status, topic, author, created_at, updated_at)"
            " VALUES (?, 0, ?, 'draft', '', ?, ?, ?)",
            (day, text, author, now, now),
        )
        topic = f"draft #{cur.lastrowid}"
        self.db.execute("UPDATE drafts SET topic = ? WHERE id = ?", (topic, cur.lastrowid))
        return cur.lastrowid

    def update_draft(self, draft_id: int, **fields) -> None:
        fields["updated_at"] = self.now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE drafts SET {cols} WHERE id = ?", (*fields.values(), draft_id))

    def link_message(self, msg_id: int, draft_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO draft_msgs(msg_id, draft_id) VALUES (?, ?)", (msg_id, draft_id))

    def draft_by_message(self, msg_id: int) -> Draft | None:
        row = self.db.execute("SELECT draft_id FROM draft_msgs WHERE msg_id = ?", (msg_id,)).fetchone()
        return self.draft(row["draft_id"]) if row else None

    # notes

    def add_note(self, agent: str, text: str) -> None:
        self.db.execute("INSERT INTO notes(agent, text, created_at) VALUES (?, ?, ?)", (agent, text, self.now()))

    def notes(self, agent: str, limit: int = 30) -> list[sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM notes WHERE agent = ? ORDER BY id DESC LIMIT ?", (agent, limit))
        return list(reversed(list(rows)))

    # metrics

    def add_metrics(self, day: str, followers: int, following: int, posts: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO metrics(day, followers, following, posts, taken_at) VALUES (?, ?, ?, ?, ?)",
            (day, followers, following, posts, self.now()),
        )

    def metrics(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM metrics ORDER BY day DESC LIMIT ?", (limit,)))

    def has_metrics(self, day: str) -> bool:
        return self.db.execute("SELECT 1 FROM metrics WHERE day = ?", (day,)).fetchone() is not None
