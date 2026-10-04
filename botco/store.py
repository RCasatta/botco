"""Durable state in SQLite. Used only from the main thread.

The pipeline is driven by this state, not by an in-memory job list: after a
restart, `Company.reconcile` looks at what is unfinished and queues the work
again.
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
    day TEXT NOT NULL,             -- the day it was written for
    slot INTEGER NOT NULL,
    text TEXT NOT NULL,
    -- review -> (revise -> review)* -> awaiting_ceo -> approved -> published
    -- terminal failures: rejected, dropped
    status TEXT NOT NULL,
    revisions INTEGER NOT NULL DEFAULT 0,
    feedback TEXT,
    topic TEXT NOT NULL,
    msg_id INTEGER,                -- writer's latest version in Zulip
    review_msg_id INTEGER,         -- editor's latest review in Zulip
    x_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS drafts_day ON drafts(day, slot);
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

TERMINAL = ("rejected", "dropped", "published")


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


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

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

    def _drafts(self, where: str, args: tuple = ()) -> list[Draft]:
        return [Draft(**dict(r)) for r in self.db.execute(f"SELECT * FROM drafts WHERE {where} ORDER BY id", args)]

    def draft(self, draft_id: int) -> Draft | None:
        found = self._drafts("id = ?", (draft_id,))
        return found[0] if found else None

    def drafts_for_day(self, day: str) -> list[Draft]:
        return self._drafts("day = ?", (day,))

    def drafts_with_status(self, *statuses: str) -> list[Draft]:
        marks = ",".join("?" * len(statuses))
        return self._drafts(f"status IN ({marks})", statuses)

    def draft_by_message(self, msg_id: int) -> Draft | None:
        found = self._drafts("msg_id = ? OR review_msg_id = ?", (msg_id, msg_id))
        return found[-1] if found else None

    def published(self, limit: int = 30) -> list[Draft]:
        rows = self.db.execute(
            "SELECT * FROM drafts WHERE status = 'published' ORDER BY published_at DESC LIMIT ?", (limit,)
        )
        return [Draft(**dict(r)) for r in rows]

    def add_draft(self, day: str, slot: int, text: str, topic: str, msg_id: int | None) -> int:
        now = self.now()
        cur = self.db.execute(
            "INSERT INTO drafts(day, slot, text, status, topic, msg_id, created_at, updated_at)"
            " VALUES (?, ?, ?, 'review', ?, ?, ?, ?)",
            (day, slot, text, topic, msg_id, now, now),
        )
        return cur.lastrowid

    def update_draft(self, draft_id: int, **fields) -> None:
        fields["updated_at"] = self.now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE drafts SET {cols} WHERE id = ?", (*fields.values(), draft_id))

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
