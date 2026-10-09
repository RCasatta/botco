"""The bots' Zulip accounts: sending, reading history, and listening."""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

import zulip

from .breakers import Breakers
from .config import Config

log = logging.getLogger(__name__)


@dataclass
class Bot:
    persona: str
    client: zulip.Client
    user_id: int
    full_name: str


class Blocked(Exception):
    """A circuit breaker stopped a message."""


def find_member(members: list[dict], key: str) -> dict | None:
    """The member a configured `zulip` names: an email, a full name or a
    user id. Most realms hide real addresses from bots, so `email` is then
    a placeholder like user8@zulip.example.com and the real one is only in
    `delivery_email`, when the bot may see it."""
    want = key.strip().lower()
    for m in members:
        keys = {str(m["user_id"]), m["full_name"].lower(), m["email"].lower(),
                (m.get("delivery_email") or "").lower()}
        if want in keys:
            return m
    return None


class Team:
    """Bots are accounts with a zuliprc; people are accounts with a Zulip
    email. Anyone else in the realm can chat but holds no role."""

    def __init__(self, cfg: Config, breakers: Breakers):
        self.cfg = cfg
        self.breakers = breakers
        self.bots: dict[str, Bot] = {}
        for name, account in cfg.accounts.items():
            if not account.zuliprc:
                continue
            client = zulip.Client(config_file=str(account.zuliprc), client="botco")
            me = client.get_profile()
            if me.get("result") != "success":
                raise RuntimeError(f"{name}: cannot log in to Zulip: {me.get('msg')}")
            self.bots[name] = Bot(name, client, me["user_id"], me["full_name"])
        self.bot_ids = {b.user_id for b in self.bots.values()}
        self._is_bot: dict[int, bool] = {}
        # People: account name -> (user id, full name).
        self.people: dict[str, tuple[int, str]] = {}
        humans = [m for m in self.ops.get_members()["members"] if not m["is_bot"] and m.get("is_active", True)]
        for name, account in cfg.accounts.items():
            if account.zulip:
                m = find_member(humans, account.zulip)
                if m is None:
                    seen = ", ".join(f"{h['full_name']} (id {h['user_id']}, {h['email']})" for h in humans)
                    log.warning("account %s: no Zulip user %s; the people botco sees: %s", name, account.zulip, seen)
                else:
                    self.people[name] = (m["user_id"], m["full_name"])

    @property
    def ops(self) -> zulip.Client:
        return self.bots[self.cfg.publisher].client

    def names(self) -> dict[str, int]:
        """Zulip full name -> user id, for mention parsing."""
        return {b.full_name: b.user_id for b in self.bots.values()} | {n: uid for uid, n in self.people.values()}

    def account_of(self, full_name_or_id: str | int) -> str | None:
        for b in self.bots.values():
            if full_name_or_id in (b.full_name, b.user_id):
                return b.persona
        for name, (uid, full) in self.people.items():
            if full_name_or_id in (full, uid):
                return name
        return None

    def mention(self, account: str) -> str:
        if account in self.bots:
            return f"@**{self.bots[account].full_name}**"
        if account in self.people:
            return f"@**{self.people[account][1]}**"
        return account

    def is_bot(self, user_id: int) -> bool:
        """Any bot account counts, not only ours."""
        if user_id in self.bot_ids:
            return True
        if user_id not in self._is_bot:
            r = self.ops.get_user_by_id(user_id)
            self._is_bot[user_id] = bool(r.get("user", {}).get("is_bot", False))
        return self._is_bot[user_id]

    def setup_streams(self) -> None:
        """Create the streams if needed and subscribe every bot and human."""
        subs = [{"name": s} for s in self.cfg.all_streams()]
        for bot in self.bots.values():
            r = bot.client.add_subscriptions(streams=subs)
            if r.get("result") != "success":
                raise RuntimeError(f"{bot.persona}: cannot subscribe: {r.get('msg')}")
        members = self.ops.get_members()["members"]
        humans = [m["user_id"] for m in members if not m["is_bot"] and m.get("is_active", True)]
        r = self.ops.add_subscriptions(streams=subs, principals=humans)
        if r.get("result") != "success":
            log.warning("could not subscribe humans: %s", r.get("msg"))

    def send(self, persona: str, stream: str, topic: str, content: str, check: bool = True) -> int:
        """Post as `persona`, a bot account. Raises Blocked if a breaker
        trips."""
        if check:
            why = self.breakers.allow(persona, stream, topic)
            if why:
                raise Blocked(why)
        r = self.bots[persona].client.send_message(
            {"type": "stream", "to": stream, "topic": topic, "content": content}
        )
        if r.get("result") != "success":
            raise RuntimeError(f"{persona}: send to #{stream} failed: {r.get('msg')}")
        self.breakers.record(persona)
        return r["id"]

    def notify(self, text: str, topic: str = "alerts") -> None:
        """Operational message to #ops. Never blocked: alerts must get through."""
        try:
            self.send(self.cfg.publisher, self.cfg.streams.ops, topic, text, check=False)
        except Exception:
            log.exception("cannot post to #ops: %s", text)

    def react(self, persona: str, msg_id: int, emoji: str) -> None:
        self.bots[persona].client.add_reaction({"message_id": msg_id, "emoji_name": emoji})

    def history(self, stream: str | None = None, topic: str | None = None, n: int = 30) -> list[dict]:
        """Latest messages, oldest first: one topic, one stream, or every
        stream the publisher is subscribed to."""
        narrow = []
        if stream is not None:
            narrow.append({"operator": "channel", "operand": stream})
        if topic is not None:
            narrow.append({"operator": "topic", "operand": topic})
        r = self.ops.get_messages(
            {"anchor": "newest", "num_before": n, "num_after": 0, "narrow": narrow, "apply_markdown": False}
        )
        return r.get("messages", [])

    def listen(self, inbox: queue.Queue) -> None:
        """Push every message and reaction event into `inbox`, from a thread,
        on a client of its own. The zulip library re-registers the queue
        after errors by itself."""
        client = zulip.Client(config_file=str(self.cfg.accounts[self.cfg.publisher].zuliprc), client="botco-events")

        def run() -> None:
            client.call_on_each_event(inbox.put, event_types=["message", "reaction"], apply_markdown=False)

        threading.Thread(target=run, name="zulip-events", daemon=True).start()
