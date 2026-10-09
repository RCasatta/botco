"""What the main thread and the worker threads share: config, policy, state,
Zulip, X, the read-only sources, and the lock that serializes access to them."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config
from .external import Issues
from .lab import Lab
from .policy import Policy
from .store import Store
from .team import Team


def quote(s: str) -> str:
    return f"```quote\n{s}\n```"


@dataclass
class TaskChanged:
    """A write to the tracker: the dispatcher wakes whoever the task waits
    on, except the account that made the change."""

    task: int
    actor: str
    what: str


class World:
    def __init__(self, cfg: Config, store: Store, team: Team, x, inbox: queue.Queue | None = None,
                 issues: Issues | None = None):
        self.cfg = cfg
        self.store = store
        store.clock = self.now
        self.team = team
        self.x = x
        self.policy = Policy(cfg)
        self.issues = issues if issues is not None else Issues(cfg.sources)
        lab = cfg.sources.lab
        self.lab = Lab(lab.dir, lab.exclude) if lab.dir else None
        self.tz = ZoneInfo(cfg.timezone)
        # Events from the main thread and the workers: Zulip events, tracker
        # changes, finished turns and sessions.
        self.inbox: queue.Queue = inbox if inbox is not None else queue.Queue()
        # Held for every store or Zulip access; never while waiting for the
        # model.
        self.lock = threading.RLock()
        self.halted = threading.Event()

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        return self.now().date().isoformat()

    def week(self) -> str:
        return self.now().strftime("%G-W%V")

    def midnight(self) -> str:
        return self.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    def activity(self, line: str) -> None:
        if self.cfg.dispatcher.activity_log:
            self.team.notify(line, "activity")

    def changed(self, task: int, actor: str, what: str) -> None:
        self.inbox.put(TaskChanged(task, actor, what))

    def published_on(self, day: str) -> int:
        return sum(1 for t in self.store.published(50) if t.closed_at and t.closed_at.startswith(day))

    def voice(self, account: str) -> tuple[str, str]:
        """The bot that posts for `account`, and a prefix naming the account
        when that bot is not its own."""
        if account in self.team.bots:
            return account, ""
        return self.cfg.publisher, f"**{account}**: "
