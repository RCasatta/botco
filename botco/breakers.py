"""Circuit breakers. Every bot message goes through `allow` first."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from .config import Breakers as BreakerConfig


class Breakers:
    def __init__(self, cfg: BreakerConfig, clock=time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.sent: dict[str, deque[float]] = defaultdict(deque)
        # (stream, topic) -> bot messages since the last human message there.
        self.streak: dict[tuple[str, str], int] = defaultdict(int)

    def observe(self, stream: str, topic: str, from_bot: bool) -> None:
        """Called for every message seen in Zulip, ours included."""
        if from_bot:
            self.streak[(stream, topic)] += 1
        else:
            self.streak.pop((stream, topic), None)

    def allow(self, persona: str, stream: str, topic: str) -> str | None:
        """None if `persona` may post in this topic now, else the reason not."""
        window = self.sent[persona]
        now = self.clock()
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= self.cfg.bot_messages_per_hour:
            return f"{persona} sent {len(window)} messages in the last hour"
        streak = self.streak[(stream, topic)]
        if streak >= self.cfg.bot_streak_per_topic:
            return f"{streak} bot messages in a row in #{stream} > {topic} without a human"
        return None

    def record(self, persona: str) -> None:
        self.sent[persona].append(self.clock())
