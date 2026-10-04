"""What the main thread and the agent threads share: config, state, Zulip,
X, and the lock that serializes access to them."""

from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from . import text as T
from .config import Config
from .lab import Lab
from .store import Draft, Store
from .team import Team


def quote(s: str) -> str:
    return f"```quote\n{s}\n```"


class World:
    def __init__(self, cfg: Config, store: Store, team: Team, x):
        self.cfg = cfg
        self.store = store
        self.team = team
        self.x = x
        self.lab = Lab(cfg.lab.dir, cfg.lab.exclude) if cfg.lab.dir else None
        self.tz = ZoneInfo(cfg.timezone)
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

    def activity(self, line: str) -> None:
        if self.cfg.agents.activity_log:
            self.team.notify(line, "activity")

    def previous_posts(self, exclude: int | None = None) -> list[str]:
        """Texts a new post must not repeat: published and open drafts."""
        drafts = [d.text for d in self.store.open_drafts() if d.id != exclude]
        return [d.text for d in self.store.published(50)] + drafts

    def check(self, text: str, exclude: int | None = None) -> list[str]:
        return T.check_post(text, self.cfg.publishing.max_chars, self.previous_posts(exclude))

    def post_about(self, persona: str, d: Draft, content: str, check: bool = True) -> int:
        """Post in the draft's own topic and remember that the message is about
        it, so reactions and replies there can be traced back."""
        msg_id = self.team.send(persona, self.cfg.streams.drafts, d.topic, content, check=check)
        self.store.link_message(msg_id, d.id)
        return msg_id

    def published_on(self, day: str) -> int:
        return sum(1 for d in self.store.published(50) if d.published_at and d.published_at.startswith(day))
