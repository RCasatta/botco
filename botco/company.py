"""The orchestrator's main loop.

The agents decide what to do; this loop only decides *when* each one gets a
turn, and runs the parts that stay deterministic on purpose:

- waking agents: a mention wakes the mentioned agent; a human message that
  mentions nobody wakes the coordinator; idle agents get a periodic
  heartbeat;
- guardrails: `halt`/`resume`/`status`/`help` in #ops work without the model,
  the daily turn budget, the breakers (in Team);
- the publisher, which posts approved drafts to X at fixed times, and the
  daily metrics;
- ✅/❌ reactions on a draft as a shortcut for the CEO's decision.
"""

from __future__ import annotations

import json
import logging
import queue
from collections import defaultdict
from datetime import datetime, timedelta

from . import text as T
from .agents import Notice, Runner, TurnDone
from .config import AGENTS
from .tools import Trigger, Turn
from .world import World, quote

log = logging.getLogger(__name__)

APPROVE = {"check", "white_check_mark", "heavy_check_mark", "check_mark", "+1", "thumbs_up", "like"}
REJECT = {"x", "cross_mark", "-1", "thumbs_down", "no_entry", "cross"}

HELP = """\
Talk to the team in plain words, anywhere. A message that mentions a bot \
(@**strategist**, @**writer**, @**editor**) wakes that bot; one that mentions \
nobody goes to the {coordinator}. For example: "make another post about \
quantization", "draft 5 is good", "drop 6, it repeats yesterday".

React ✅ or ❌ on a draft's message as a shortcut to approve or discard it.

Commands in #ops that work even when the model is down:
- `status`: what the team is doing
- `halt`: stop all agents and publishing (survives restarts)
- `resume`: start again"""


class Company:
    def __init__(self, world: World, runner: Runner, inbox: queue.Queue):
        self.w = world
        self.cfg = world.cfg
        self.runner = runner
        self.inbox = inbox
        self.pending: dict[str, list[Trigger]] = defaultdict(list)
        self.running: set[str] = set()
        now = self.w.now()
        # Stagger the first heartbeats: the coordinator right away, then the
        # others spread over one interval.
        beat = timedelta(minutes=self.cfg.agents.heartbeat_minutes)
        order = [self.cfg.agents.coordinator] + [a for a in AGENTS if a != self.cfg.agents.coordinator]
        self.last_turn = {a: now - beat + i * beat / len(order) for i, a in enumerate(order)}
        self.notified: set[str] = set()
        self.lab_checked = datetime.min.replace(tzinfo=self.w.tz)

    # main loop

    def run(self) -> None:
        w = self.w
        with w.lock:
            if w.store.get("halted") == "1":
                w.halted.set()
            w.team.setup_streams()
        w.team.listen(self.inbox)
        self.runner.start()
        state = "**halted** (send `resume` to start)" if w.halted.is_set() else "running"
        mode = "dry run, nothing is posted to X" if self.cfg.x.dry_run else "live on X"
        with w.lock:
            w.team.notify(f"Orchestrator online (agents version): {state}, {mode}. Send `help`.", "status")
        last_tick = datetime.min.replace(tzinfo=w.tz)
        while True:
            try:
                item = self.inbox.get(timeout=30)
            except queue.Empty:
                item = None
            try:
                with w.lock:
                    if item is not None:
                        self.handle(item)
                    if item is None or w.now() - last_tick > timedelta(seconds=30):
                        self.tick()
                        last_tick = w.now()
                    self.schedule()
            except Exception as e:
                log.exception("main loop")
                self.notify_once(f"loop:{type(e).__name__}", f":warning: Orchestrator error: `{e}`")

    def handle(self, item) -> None:
        if isinstance(item, TurnDone):
            self.running.discard(item.agent)
            self.last_turn[item.agent] = self.w.now()
            log.info("%s: %d steps, %s", item.agent, item.steps, item.outcome)
        elif isinstance(item, Notice):
            self.w.team.notify(item.text, "inference")
        elif item.get("type") == "message":
            self.on_message(item["message"])
        elif item.get("type") == "reaction":
            self.on_reaction(item)

    def notify_once(self, key: str, text: str) -> None:
        if key not in self.notified:
            self.notified.add(key)
            self.w.team.notify(text)

    def tick(self) -> None:
        if self.w.halted.is_set():
            return
        now = self.w.now()
        a = self.cfg.agents
        if a.active_from <= now.time() <= a.active_to:
            for agent in AGENTS:
                idle = agent not in self.running and not self.pending[agent]
                if idle and now - self.last_turn[agent] >= timedelta(minutes=a.heartbeat_minutes):
                    self.pending[agent].append(Trigger("heartbeat"))
        self.publish()
        self.record_metrics()
        if now - self.lab_checked >= timedelta(minutes=10):
            self.lab_checked = now
            self.watch_lab()

    def schedule(self) -> None:
        """Start turns for agents with a reason to wake, as workers free up.
        Turns start only when they can run, so triggers that arrive while an
        agent waits merge into its next turn; humans go first."""
        if self.w.halted.is_set():
            return
        waiting = [a for a in AGENTS if self.pending[a] and a not in self.running]
        waiting.sort(key=lambda a: not any(t.kind == "human" for t in self.pending[a]))
        for agent in waiting:
            if len(self.running) >= self.cfg.llm.concurrency:
                return
            triggers = self.pending[agent]
            turn = Turn(agent, triggers)
            key = f"turns:{self.w.today()}"
            if int(self.w.store.get(key, "0")) >= self.cfg.agents.max_turns_per_day and not turn.human:
                self.notify_once(key, f":octagonal_sign: {self.cfg.agents.max_turns_per_day} agent turns today: "
                                      "until tomorrow, agents only wake when a human writes to them.")
                self.pending[agent] = []
                continue
            self.w.store.incr(key)
            self.pending[agent] = []
            self.running.add(agent)
            self.runner.submit(turn)

    # Zulip events

    def on_message(self, msg: dict) -> None:
        if msg.get("type") != "stream":
            return
        w = self.w
        stream, topic, content, sender = msg["display_recipient"], msg["subject"], msg["content"], msg["sender_id"]
        from_bot = w.team.is_bot(sender)
        w.team.breakers.observe(stream, topic, from_bot)
        author = w.team.persona_of(sender)
        if author == self.cfg.publisher or (from_bot and author is None):
            return  # operational messages and foreign bots never wake anyone
        if not from_bot and stream == self.cfg.streams.ops and self.command(content.strip(), topic):
            return
        trigger = Trigger("bot" if from_bot else "human", msg["sender_full_name"], stream, topic, content, msg["id"])
        called = {w.team.persona_of(n) for n in T.mentioned(content, w.team.names())} & set(AGENTS)
        called.discard(author)
        if not called and not from_bot:
            called = {self.cfg.agents.coordinator}
        for agent in called:
            self.pending[agent].append(trigger)

    def command(self, content: str, topic: str) -> bool:
        w = self.w
        word = content.lower().lstrip("/").split(maxsplit=1)[0] if content else ""
        if word not in ("halt", "resume", "status", "help") or len(content.split()) > 2:
            return False
        if word == "halt":
            w.halted.set()
            w.store.put("halted", "1")
            reply = (":octagonal_sign: **Halted.** Agents stop at their next step and nothing is published. "
                     "Send `resume` to restart.")
        elif word == "resume":
            w.halted.clear()
            w.store.put("halted", "0")
            self.notified.clear()
            reply = ":check: Resumed."
        elif word == "status":
            reply = self.status()
        else:
            reply = HELP.format(coordinator=self.cfg.agents.coordinator)
        w.team.send(self.cfg.publisher, self.cfg.streams.ops, topic, reply, check=False)
        return True

    def on_reaction(self, ev: dict) -> None:
        w = self.w
        if ev.get("op") != "add" or w.team.is_bot(ev["user_id"]):
            return
        d = w.store.draft_by_message(ev["message_id"])
        if not d or d.status != "draft":
            return
        if ev["emoji_name"] in APPROVE:
            w.store.update_draft(d.id, ceo_ok=1, editor_ok=1)
            w.post_about(self.cfg.publisher, d, f"Recorded: the CEO **approved** draft #{d.id} (✅).", check=False)
        elif ev["emoji_name"] in REJECT:
            w.store.update_draft(d.id, status="rejected")
            w.post_about(self.cfg.publisher, d, f"Recorded: the CEO **rejected** draft #{d.id} (❌).", check=False)

    def status(self) -> str:
        w = self.w
        need_ceo = self.cfg.publishing.ceo_approval
        drafts = w.store.open_drafts()
        ready = w.store.ready(need_ceo)
        pending = {a: len(t) for a, t in self.pending.items() if t}
        return "\n".join([
            f"**State**: {'halted' if w.halted.is_set() else 'running'}, "
            f"{'dry run' if self.cfg.x.dry_run else 'live on X'}",
            f"**Inference**: {'down' if self.runner.down else 'up'}",
            f"**Agents**: in a turn: {', '.join(sorted(self.running)) or 'none'}; "
            f"waiting to wake: {pending or 'none'}",
            f"**Turns today**: {w.store.get(f'turns:{w.today()}', '0')}/{self.cfg.agents.max_turns_per_day}",
            f"**Plan for {w.week()}**: {'yes' if w.store.latest_plan(w.week()) else 'not yet'}",
            f"**Open drafts**: {', '.join(f'#{d.id}' for d in drafts) or 'none'}; "
            f"ready to publish: {', '.join(f'#{d.id}' for d in ready) or 'none'}",
            f"**Published today**: {w.published_on(w.today())}",
        ])

    def watch_lab(self) -> None:
        """Wake the coordinator when reports in the lab notebook appear or
        change: new results are the best material the team has."""
        lab = self.w.lab
        if not lab or not lab.available():
            return
        current = {n.path: n.modified.isoformat() for n in lab.notes()}
        seen = self.w.store.get("lab_seen")
        self.w.store.put("lab_seen", json.dumps(current))
        if seen is None:
            what = f"the team can now read it ({len(current)} reports)"
        else:
            old = json.loads(seen)
            changed = [p for p, m in current.items() if old.get(p) != m]
            if not changed:
                return
            what = "new or updated: " + ", ".join(f"`{p}`" for p in changed[:10])
        self.pending[self.cfg.agents.coordinator].append(Trigger("lab", content=what))

    # publishing and metrics: no model involved

    def publish(self) -> None:
        w, pub = self.w, self.cfg.publishing
        now, today = w.now(), w.today()
        due = sum(1 for t in pub.times if t <= now.time())
        if w.published_on(today) >= due:
            return
        last = w.store.published(1)
        if last and now - datetime.fromisoformat(last[0].published_at) < timedelta(minutes=pub.min_gap_minutes):
            return
        retry_at = w.store.get("publish_retry_at")
        if retry_at and now < datetime.fromisoformat(retry_at):
            return
        ready = w.store.ready(pub.ceo_approval)
        if not ready:
            return
        d = ready[0]
        problems = T.check_post(d.text, pub.max_chars, [p.text for p in w.store.published(50)])
        if problems:
            w.store.update_draft(d.id, status="rejected", feedback="; ".join(problems))
            w.post_about(self.cfg.publisher, d, f"Not published: {'; '.join(problems)}", check=False)
            return
        try:
            x_id = w.x.post(d.text)
        except Exception as e:
            w.store.put("publish_retry_at", (now + timedelta(minutes=30)).isoformat())
            w.team.notify(f":warning: Publishing draft #{d.id} failed, retrying in 30 minutes: {e}")
            return
        w.store.update_draft(d.id, status="published", x_id=x_id, published_at=now.isoformat(timespec="seconds"))
        where = "dry run, not actually posted" if x_id == "dry-run" else f"https://x.com/i/status/{x_id}"
        w.team.send(self.cfg.publisher, self.cfg.streams.published, today,
                    f"Published draft #{d.id} ({where}):\n{quote(d.text)}", check=False)
        w.post_about(self.cfg.publisher, d, f"Published ({where}).", check=False)

    def record_metrics(self) -> None:
        w = self.w
        now, today = w.now(), w.today()
        if now.time() < self.cfg.publishing.metrics_time or w.store.has_metrics(today):
            return
        if w.store.get("metrics_tried") == today:
            return
        w.store.put("metrics_tried", today)
        try:
            n = w.x.numbers()
        except Exception as e:
            w.team.notify(f":warning: Reading X metrics failed: {e}")
            return
        if n is None:
            return  # dry run
        prev = w.store.metrics(1)
        w.store.add_metrics(today, n.followers, n.following, n.posts)
        delta = f" ({n.followers - prev[0]['followers']:+d})" if prev else ""
        w.team.send(self.cfg.publisher, self.cfg.streams.metrics, "followers",
                    f"{today}: **{n.followers}** followers{delta}, {n.posts} posts, "
                    f"{w.published_on(today)} published today.", check=False)
