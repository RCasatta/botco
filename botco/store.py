"""Durable state in SQLite: the tracker (tasks, revisions, reviews,
comments), notes, metrics, and a small key/value table.

Shared by the main thread and the worker threads, always under
`World.lock`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open or closed
    resolution TEXT,                       -- when closed: done, rejected, dropped, replaced
    author TEXT NOT NULL,
    assignee TEXT,
    labels TEXT NOT NULL DEFAULT '[]',
    refs TEXT NOT NULL DEFAULT '[]',
    external_id TEXT,                      -- the external issue it was created for
    schedule TEXT,                         -- the schedule that created it
    parent INTEGER,                        -- the task its creator was handling
    depth INTEGER NOT NULL DEFAULT 0,
    stream TEXT NOT NULL,
    topic TEXT NOT NULL,
    x_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
    task_id INTEGER NOT NULL,
    n INTEGER NOT NULL,
    body TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (task_id, n)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    body_hash TEXT NOT NULL,               -- the body it is about
    account TEXT NOT NULL,                 -- whose review it is
    verdict TEXT NOT NULL,                 -- approve, revise or reject
    comments TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,             -- an agent quoting a person, or the account itself
    msg_id INTEGER,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    msg_id INTEGER,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_msgs (
    msg_id INTEGER PRIMARY KEY,            -- any Zulip message about a task
    task_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS external (
    ref TEXT PRIMARY KEY,                  -- gh:owner/repo#12, gl:group/proj#7
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    url TEXT NOT NULL,
    body TEXT NOT NULL,
    comments TEXT NOT NULL,                -- JSON list of {author, at, text}, latest last
    updated_at TEXT NOT NULL,              -- as the platform reports it
    fetched_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS post_metrics (
    task_id INTEGER NOT NULL,              -- a published task
    day TEXT NOT NULL,
    views INTEGER NOT NULL,
    likes INTEGER NOT NULL,
    reposts INTEGER NOT NULL,
    replies INTEGER NOT NULL,
    quotes INTEGER NOT NULL,
    bookmarks INTEGER NOT NULL,
    taken_at TEXT NOT NULL,
    PRIMARY KEY (task_id, day)
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

# The fixed pipeline's draft statuses, mapped onto the columns of the first
# agents version, before both move into tasks.
DRAFTS_V1 = """
ALTER TABLE drafts ADD COLUMN author TEXT NOT NULL DEFAULT 'writer';
ALTER TABLE drafts ADD COLUMN editor_ok INTEGER NOT NULL DEFAULT 0;
ALTER TABLE drafts ADD COLUMN ceo_ok INTEGER NOT NULL DEFAULT 0;
UPDATE drafts SET editor_ok = 1 WHERE status IN ('awaiting_ceo', 'approved');
UPDATE drafts SET ceo_ok = 1 WHERE status = 'approved';
UPDATE drafts SET status = 'draft' WHERE status IN ('review', 'revise', 'awaiting_ceo', 'approved');
UPDATE drafts SET status = 'rejected' WHERE status = 'dropped';
"""


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:16]


@dataclass
class Task:
    id: int
    kind: str
    title: str
    status: str
    resolution: str | None
    author: str
    assignee: str | None
    labels: list[str]
    refs: list[str]
    external_id: str | None
    schedule: str | None
    parent: int | None
    depth: int
    stream: str
    topic: str
    x_id: str | None
    created_at: str
    updated_at: str
    closed_at: str | None
    # From the latest revision.
    body: str = ""
    note: str = ""
    version: int = 0

    @property
    def ref(self) -> str:
        return f"#{self.id}"

    @property
    def open(self) -> bool:
        return self.status == "open"

    @property
    def hash(self) -> str:
        return body_hash(self.body)


@dataclass
class Review:
    id: int
    task_id: int
    body_hash: str
    account: str
    verdict: str
    comments: str
    recorded_by: str
    msg_id: int | None
    at: str


@dataclass
class Comment:
    id: int
    task_id: int
    author: str
    text: str
    msg_id: int | None
    at: str


@dataclass
class External:
    ref: str
    title: str
    state: str
    url: str
    body: str
    comments: list[dict] = field(default_factory=list)
    updated_at: str = ""
    fetched_at: str = ""


class Store:
    def __init__(self, path: Path, owner: str = "owner"):
        """`owner` is the account that approvals recorded as the CEO's in an
        older database belong to."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # World points this at its own clock.
        self.clock = lambda: datetime.now().astimezone()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate(owner)

    def now(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    # migration from the drafts and plans of the agents version

    def _tables(self) -> set[str]:
        return {r["name"] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def _migrate(self, owner: str) -> None:
        tables = self._tables()
        if "drafts" not in tables and "plans" not in tables:
            return
        self.db.execute("BEGIN")
        try:
            if "drafts" in tables:
                cols = {r["name"] for r in self.db.execute("PRAGMA table_info(drafts)")}
                if "editor_ok" not in cols:
                    for stmt in DRAFTS_V1.strip().split(";\n"):
                        self.db.execute(stmt)
                if "note" not in cols:
                    self.db.execute("ALTER TABLE drafts ADD COLUMN note TEXT")
                self._migrate_drafts(owner, "draft_msgs" in tables)
                self.db.execute("ALTER TABLE drafts RENAME TO legacy_drafts")
                if "draft_msgs" in tables:
                    self.db.execute("ALTER TABLE draft_msgs RENAME TO legacy_draft_msgs")
            if "plans" in tables:
                self._migrate_plans()
                self.db.execute("ALTER TABLE plans RENAME TO legacy_plans")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _migrate_drafts(self, owner: str, has_msgs: bool) -> None:
        resolution = {"published": "done", "rejected": "rejected"}
        for d in self.db.execute("SELECT * FROM drafts ORDER BY id").fetchall():
            closed = d["status"] != "draft"
            closed_at = (d["published_at"] or d["updated_at"]) if closed else None
            self.db.execute(
                "INSERT INTO tasks(id, kind, title, status, resolution, author, assignee, stream, topic, x_id,"
                " created_at, updated_at, closed_at) VALUES (?, 'post', ?, ?, ?, ?, ?, 'drafts', ?, ?, ?, ?, ?)",
                (d["id"], title_of(d["text"]), "closed" if closed else "open", resolution.get(d["status"]) if closed else None,
                 d["author"], d["author"], d["topic"], d["x_id"], d["created_at"], d["updated_at"], closed_at))
            self.db.execute("INSERT INTO revisions(task_id, n, body, note, author, at) VALUES (?, ?, ?, ?, ?, ?)",
                            (d["id"], d["revisions"] + 1, d["text"], d["note"] or "", d["author"], d["updated_at"]))
            h = body_hash(d["text"])
            if d["editor_ok"]:
                self._insert_review(d["id"], h, "editor", "approve", d["feedback"] or "", "editor", None, d["updated_at"])
            if d["ceo_ok"]:
                self._insert_review(d["id"], h, owner, "approve", "", owner, None, d["updated_at"])
            if d["feedback"] and not d["editor_ok"] and not closed:
                self._insert_review(d["id"], h, "editor", "revise", d["feedback"], "editor", None, d["updated_at"])
            for m in (d["msg_id"], d["review_msg_id"]):
                if m:
                    self.db.execute("INSERT OR IGNORE INTO task_msgs VALUES (?, ?)", (m, d["id"]))
        if has_msgs:
            self.db.execute("INSERT OR IGNORE INTO task_msgs SELECT msg_id, draft_id FROM draft_msgs")

    def _migrate_plans(self) -> None:
        plan = self.db.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        if plan is None:
            return
        now = self.now()
        cur = self.db.execute(
            "INSERT INTO tasks(kind, title, author, assignee, stream, topic, created_at, updated_at)"
            " VALUES ('plan', ?, 'strategist', 'strategist', 'plan', '', ?, ?)",
            (f"Plan for week {plan['week']}", plan["created_at"], now))
        self.db.execute("UPDATE tasks SET topic = ? WHERE id = ?", (f"plan #{cur.lastrowid}", cur.lastrowid))
        self.db.execute("INSERT INTO revisions(task_id, n, body, note, author, at) VALUES (?, 1, ?, '', 'strategist', ?)",
                        (cur.lastrowid, plan["text"], plan["created_at"]))

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

    # tasks

    def _tasks(self, where: str, args: tuple = (), order: str = "id") -> list[Task]:
        rows = self.db.execute(
            "SELECT t.*, r.body, r.note, r.n AS version FROM tasks t LEFT JOIN revisions r"
            " ON r.task_id = t.id AND r.n = (SELECT MAX(n) FROM revisions WHERE task_id = t.id)"
            f" WHERE {where} ORDER BY {order}", args)
        out = []
        for r in rows:
            d = dict(r)
            d["labels"], d["refs"] = json.loads(d["labels"]), json.loads(d["refs"])
            d["body"], d["note"], d["version"] = d["body"] or "", d["note"] or "", d["version"] or 0
            out.append(Task(**d))
        return out

    def task(self, task_id: int) -> Task | None:
        found = self._tasks("t.id = ?", (int(task_id),))
        return found[0] if found else None

    def open_tasks(self, kind: str | None = None) -> list[Task]:
        if kind:
            return self._tasks("t.status = 'open' AND t.kind = ?", (kind,))
        return self._tasks("t.status = 'open'")

    def published(self, limit: int = 30) -> list[Task]:
        """Tasks a sink published, newest first."""
        return self._tasks("t.x_id IS NOT NULL AND t.resolution = 'done'", order=f"t.closed_at DESC LIMIT {int(limit)}")

    def search(self, words: list[str], limit: int = 20) -> list[Task]:
        cond = " AND ".join(["(t.title LIKE ? OR r.body LIKE ?)"] * len(words)) or "1"
        args = tuple(a for w in words for a in (f"%{w}%", f"%{w}%"))
        return self._tasks(cond, args, order=f"t.status = 'closed', t.id DESC LIMIT {int(limit)}")

    def add_task(self, kind: str, title: str, body: str, note: str, author: str, assignee: str | None,
                 stream: str, labels: list[str], refs: list[str], depth: int, parent: int | None = None,
                 external_id: str | None = None, schedule: str | None = None) -> int:
        now = self.now()
        cur = self.db.execute(
            "INSERT INTO tasks(kind, title, author, assignee, labels, refs, external_id, schedule, parent, depth,"
            " stream, topic, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
            (kind, title, author, assignee, json.dumps(labels), json.dumps(refs), external_id, schedule, parent,
             depth, stream, now, now))
        task_id = cur.lastrowid
        self.db.execute("UPDATE tasks SET topic = ? WHERE id = ?", (f"{kind} #{task_id}", task_id))
        self.add_revision(task_id, body, note, author)
        return task_id

    def update_task(self, task_id: int, **fields) -> None:
        for k in ("labels", "refs"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = self.now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))

    def close_task(self, task_id: int, resolution: str, **fields) -> None:
        self.update_task(task_id, status="closed", resolution=resolution, closed_at=self.now(), **fields)

    def add_revision(self, task_id: int, body: str, note: str, author: str) -> int:
        n = self.db.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM revisions WHERE task_id = ?", (task_id,)).fetchone()[0]
        self.db.execute("INSERT INTO revisions(task_id, n, body, note, author, at) VALUES (?, ?, ?, ?, ?, ?)",
                        (task_id, n, body, note, author, self.now()))
        self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (self.now(), task_id))
        return n

    def revisions(self, task_id: int) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM revisions WHERE task_id = ? ORDER BY n", (task_id,)))

    def _insert_review(self, task_id, h, account, verdict, comments, recorded_by, msg_id, at) -> None:
        self.db.execute(
            "INSERT INTO reviews(task_id, body_hash, account, verdict, comments, recorded_by, msg_id, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (task_id, h, account, verdict, comments, recorded_by, msg_id, at))

    def add_review(self, task_id: int, h: str, account: str, verdict: str, comments: str, recorded_by: str,
                   msg_id: int | None = None) -> None:
        self._insert_review(task_id, h, account, verdict, comments, recorded_by, msg_id, self.now())
        self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (self.now(), task_id))

    def reviews(self, task_id: int) -> list[Review]:
        return [Review(**dict(r)) for r in self.db.execute("SELECT * FROM reviews WHERE task_id = ? ORDER BY id", (task_id,))]

    def add_comment(self, task_id: int, author: str, text: str, msg_id: int | None = None) -> int:
        cur = self.db.execute("INSERT INTO comments(task_id, author, text, msg_id, at) VALUES (?, ?, ?, ?, ?)",
                              (task_id, author, text, msg_id, self.now()))
        self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (self.now(), task_id))
        return cur.lastrowid

    def comments(self, task_id: int, limit: int = 20) -> list[Comment]:
        rows = self.db.execute("SELECT * FROM comments WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit))
        return list(reversed([Comment(**dict(r)) for r in rows]))

    def created_by(self, author: str, since: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM tasks WHERE author = ? AND created_at >= ?",
                               (author, since)).fetchone()[0]

    def link_message(self, msg_id: int, task_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO task_msgs(msg_id, task_id) VALUES (?, ?)", (msg_id, task_id))

    def task_by_message(self, msg_id: int) -> Task | None:
        row = self.db.execute("SELECT task_id FROM task_msgs WHERE msg_id = ?", (msg_id,)).fetchone()
        return self.task(row["task_id"]) if row else None

    def task_by_topic(self, stream: str, topic: str) -> Task | None:
        found = self._tasks("t.stream = ? AND t.topic = ?", (stream, topic), order="t.id DESC LIMIT 1")
        return found[0] if found else None

    # external issues

    def external(self, ref: str) -> External | None:
        r = self.db.execute("SELECT * FROM external WHERE ref = ?", (ref,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["comments"] = json.loads(d["comments"])
        return External(**d)

    def put_external(self, e: External) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO external(ref, title, state, url, body, comments, updated_at, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (e.ref, e.title, e.state, e.url, e.body, json.dumps(e.comments), e.updated_at, self.now()))

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

    def add_post_metrics(self, task_id: int, day: str, n) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO post_metrics(task_id, day, views, likes, reposts, replies, quotes, bookmarks,"
            " taken_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, day, n.views, n.likes, n.reposts, n.replies, n.quotes, n.bookmarks, self.now()))

    def post_metrics(self) -> dict[int, sqlite3.Row]:
        """The latest numbers of each published task that has any."""
        rows = self.db.execute("SELECT * FROM post_metrics p WHERE day = (SELECT MAX(day) FROM post_metrics"
                               " WHERE task_id = p.task_id)")
        return {r["task_id"]: r for r in rows}

    def has_metrics(self, day: str) -> bool:
        return self.db.execute("SELECT 1 FROM metrics WHERE day = ?", (day,)).fetchone() is not None


def title_of(body: str) -> str:
    line = body.strip().splitlines()[0] if body.strip() else "untitled"
    return line if len(line) <= 60 else line[:57].rstrip() + "..."
