"""The orchestrator's main loop and the content pipeline.

Everything that changes state runs on this thread: Zulip events and LLM
results arrive through `inbox`, and `reconcile` periodically compares the
stored state with the schedule and queues whatever work is missing. Work is
derived from state, so a restart, a halt or a down inference server only
delays it.

Pipeline, one draft at a time per slot:

    strategist: weekly plan (#plan)
    writer: draft          -> automatic checks -> editor: review (#drafts)
      REVISE  -> writer: revision -> checks -> editor ... (max_revisions)
      REJECT  -> slot is retried with a new draft (attempts_per_slot)
      APPROVE -> CEO reaction (if ceo_approval) -> publisher (#published)
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import Counter
from datetime import datetime, timedelta
from importlib import resources
from zoneinfo import ZoneInfo

from . import text as T
from .config import Config
from .llm import Done, Job, Notice, Workers
from .store import TERMINAL, Draft, Store
from .team import Blocked, Team

log = logging.getLogger(__name__)

LLM_PERSONAS = ("strategist", "writer", "editor")
APPROVE = {"check", "white_check_mark", "heavy_check_mark", "check_mark", "+1", "thumbs_up", "like"}
REJECT = {"x", "cross_mark", "-1", "thumbs_down", "no_entry", "cross"}
MAX_FAILURES = 3

HELP = """\
Commands in #ops (humans only):
- `status`: what the team is doing
- `halt`: stop all bot activity (survives restarts)
- `resume`: start again
- `replan`: the strategist rewrites this week's plan

Mention a bot (@**strategist**, @**writer**, @**editor**) anywhere to talk to it.
In #drafts, react ✅ to approve a draft for publishing, ❌ to discard it.
Write to the strategist in #plan: it reads the CEO's messages there when planning."""


def prompt(name: str) -> str:
    return resources.files("botco").joinpath(f"prompts/{name}.md").read_text()


def quote(s: str) -> str:
    return f"```quote\n{s}\n```"


class Company:
    def __init__(self, cfg: Config, store: Store, team: Team, workers: Workers, x, inbox: queue.Queue,
                 halted: threading.Event):
        self.cfg = cfg
        self.store = store
        self.team = team
        self.workers = workers
        self.x = x
        self.inbox = inbox
        self.halted = halted
        self.tz = ZoneInfo(cfg.timezone)
        self.inflight: set[str] = set()
        self.failures: Counter[str] = Counter()
        self.notified: set[str] = set()

    # time

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        return self.now().date().isoformat()

    def week(self) -> str:
        return self.now().strftime("%G-W%V")

    # main loop

    def run(self) -> None:
        if self.store.get("halted") == "1":
            self.halted.set()
        self.team.setup_streams()
        self.team.listen(self.inbox)
        self.workers.start()
        state = "**halted** (send `resume` to start)" if self.halted.is_set() else "running"
        mode = "dry run, nothing is posted to X" if self.cfg.x.dry_run else "live on X"
        self.team.notify(f"Orchestrator online: {state}, {mode}. Send `help` for commands.", "status")
        last = datetime.min.replace(tzinfo=self.tz)
        while True:
            try:
                item = self.inbox.get(timeout=30)
            except queue.Empty:
                item = None
            if item is not None:
                try:
                    self.handle(item)
                except Exception:
                    log.exception("handling %r", item)
            if item is None or isinstance(item, Done) or self.now() - last > timedelta(seconds=30):
                try:
                    self.reconcile()
                except Exception as e:
                    log.exception("reconcile")
                    self.notify_once(f"reconcile:{type(e).__name__}", f":warning: Orchestrator error: `{e}`")
                last = self.now()

    def handle(self, item) -> None:
        if isinstance(item, Done):
            self.inflight.discard(item.job.key)
            if self.halted.is_set():
                log.info("halted: discarding result of %s", item.job.key)
            elif item.error:
                self.failures[item.job.key] += 1
                n = self.failures[item.job.key]
                give_up = " Giving up on it until restart." if n >= MAX_FAILURES else ""
                self.team.notify(f":warning: `{item.job.key}` failed ({n}/{MAX_FAILURES}): {item.error}.{give_up}", "errors")
            else:
                item.job.on_done(item.text)
        elif isinstance(item, Notice):
            self.team.notify(item.text, "inference")
        elif item.get("type") == "message":
            self.on_message(item["message"])
        elif item.get("type") == "reaction":
            self.on_reaction(item)

    def notify_once(self, key: str, text: str, topic: str = "alerts") -> None:
        if key not in self.notified:
            self.notified.add(key)
            self.team.notify(text, topic)

    def enqueue(self, key: str, persona: str, messages: list[dict], on_done, priority: int = 1) -> bool:
        if key in self.inflight or self.failures[key] >= MAX_FAILURES:
            return False
        jobs_key = f"jobs:{self.today()}"
        if int(self.store.get(jobs_key, "0")) >= self.cfg.llm.max_jobs_per_day:
            self.notify_once(
                jobs_key,
                f":octagonal_sign: Daily budget of {self.cfg.llm.max_jobs_per_day} LLM jobs reached; "
                "background work stops until tomorrow.",
            )
            return False
        self.store.incr(jobs_key)
        self.inflight.add(key)
        self.workers.submit(Job(key, self.cfg.personas[persona], messages, on_done, priority))
        return True

    def say(self, persona: str, stream: str, topic: str, content: str) -> int | None:
        """Post as a persona, reporting a tripped breaker in #ops."""
        try:
            return self.team.send(persona, stream, topic, content)
        except Blocked as e:
            self.notify_once(f"blocked:{stream}:{topic}", f":octagonal_sign: Breaker: {e}. Message from {persona} not sent.")
            return None

    # Zulip events

    def on_message(self, msg: dict) -> None:
        if msg.get("type") != "stream":
            return
        stream, topic, content = msg["display_recipient"], msg["subject"], msg["content"]
        from_bot = self.team.is_bot(msg["sender_id"])
        self.team.breakers.observe(stream, topic, from_bot)
        if msg["sender_id"] in self.team.bot_ids:
            return
        if from_bot and not self.cfg.breakers.answer_bot_mentions:
            return
        if stream == self.cfg.streams.ops and not from_bot and self.command(content.strip(), topic):
            return
        if self.halted.is_set():
            return
        for full_name in T.mentioned(content, self.team.names()):
            persona = self.team.persona_of(full_name)
            if persona in LLM_PERSONAS:
                self.chat(persona, stream, topic, msg["id"])
            elif persona == self.cfg.listener:
                self.say(persona, stream, topic, self.status())

    def command(self, content: str, topic: str) -> bool:
        word = content.lower().lstrip("/").split(maxsplit=1)[0] if content else ""
        reply = None
        if word == "halt":
            self.halted.set()
            self.store.put("halted", "1")
            reply = ":octagonal_sign: **Halted.** No new LLM jobs, no posts. Results still in flight are discarded. Send `resume` to restart."
        elif word == "resume":
            self.halted.clear()
            self.store.put("halted", "0")
            self.notified.clear()
            reply = ":check: Resumed."
        elif word == "status":
            reply = self.status()
        elif word == "replan":
            self.store.put("replan", self.week())
            reply = f"The strategist will rewrite the plan for {self.week()}."
        elif word == "help":
            reply = HELP
        if reply:
            self.team.send(self.cfg.listener, self.cfg.streams.ops, topic, reply, check=False)
        return reply is not None

    def on_reaction(self, ev: dict) -> None:
        if ev.get("op") != "add" or self.team.is_bot(ev["user_id"]):
            return
        draft = self.store.draft_by_message(ev["message_id"])
        if not draft or draft.status in TERMINAL:
            return
        emoji = ev["emoji_name"]
        if emoji in APPROVE and draft.status != "approved":
            self.store.update_draft(draft.id, status="approved")
            self.team.send(self.cfg.listener, self.cfg.streams.drafts, draft.topic,
                           "Approved by the CEO, queued for publishing.", check=False)
        elif emoji in REJECT:
            self.store.update_draft(draft.id, status="rejected")
            self.team.send(self.cfg.listener, self.cfg.streams.drafts, draft.topic,
                           "Discarded by the CEO.", check=False)

    def chat(self, persona: str, stream: str, topic: str, msg_id: int) -> None:
        history = self.team.history(stream, topic, n=30)
        transcript = "\n\n".join(f"{m['sender_full_name']}: {m['content']}" for m in history)
        system = (
            f"{prompt('common')}\n\n{prompt(persona)}\n\n"
            f"Right now you are in the Zulip conversation #{stream} > {topic}, where a human mentioned you. "
            "Answer the latest message that mentions you, briefly (under 150 words), in Zulip markdown. "
            "Do not mention other bots."
        )
        plan = self.store.latest_plan()
        if plan:
            system += f"\n\nThe current content plan:\n{plan['text']}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": transcript}]

        def done(reply: str) -> None:
            self.say(persona, stream, topic, reply or "(no answer)")

        self.enqueue(f"chat:{msg_id}:{persona}", persona, messages, done, priority=0)

    def status(self) -> str:
        today = self.today()
        counts = Counter(d.status for d in self.store.drafts_for_day(today))
        waiting = len(self.store.drafts_with_status("awaiting_ceo"))
        plan = self.store.latest_plan(self.week())
        lines = [
            f"**State**: {'halted' if self.halted.is_set() else 'running'}, "
            f"{'dry run' if self.cfg.x.dry_run else 'live on X'}",
            f"**Inference**: {'down' if self.workers.down else 'up'}, {self.workers.pending()} queued, "
            f"in flight: {', '.join(sorted(self.inflight)) or 'nothing'}",
            f"**LLM jobs today**: {self.store.get(f'jobs:{today}', '0')}/{self.cfg.llm.max_jobs_per_day}",
            f"**Plan for {self.week()}**: {'yes' if plan else 'not yet'}",
            f"**Drafts today**: {dict(counts) or 'none'}; waiting for CEO: {waiting}",
            f"**Published today**: {self.published_on(today)}",
        ]
        return "\n".join(lines)

    # pipeline

    def reconcile(self) -> None:
        if self.halted.is_set():
            return
        self.reconcile_plan()
        if self.store.latest_plan(self.week()):
            self.reconcile_drafts()
        self.reconcile_publish()
        self.reconcile_metrics()

    def recent_posts(self, n: int = 20) -> list[str]:
        return [d.text for d in self.store.published(n)]

    def context(self) -> str:
        """What every writing or reviewing job sees besides its task."""
        parts = []
        plan = self.store.latest_plan()
        if plan:
            parts.append(f"## This week's plan ({plan['week']})\n{plan['text']}")
        recent = self.recent_posts(15)
        queued = [d.text for d in self.store.drafts_with_status("approved", "awaiting_ceo")]
        if recent or queued:
            parts.append("## Recent and queued posts (do not repeat these)\n" +
                         "\n".join(f"- {p}" for p in recent + queued))
        return "\n\n".join(parts)

    def reconcile_plan(self) -> None:
        week = self.week()
        if self.store.latest_plan(week) and self.store.get("replan") != week:
            return
        prev = self.store.latest_plan()
        key = f"plan:{week}:{prev['id'] if prev else 0}"
        if key in self.inflight:
            return
        parts = [f"Write the content plan for week {week}."]
        since = datetime.fromisoformat(prev["created_at"]).timestamp() if prev else 0
        notes = [m for m in self.team.history(self.cfg.streams.plan, n=100)
                 if not self.team.is_bot(m["sender_id"]) and m["timestamp"] > since]
        if notes:
            parts.append("## Messages from the CEO since the last plan\n" +
                         "\n".join(f"- {m['content']}" for m in notes))
        if prev:
            parts.append(f"## Last plan ({prev['week']})\n{prev['text']}")
        posts = self.store.published(30)
        if posts:
            parts.append("## Published posts, newest first\n" +
                         "\n".join(f"- [{d.published_at[:10]}] {d.text}" for d in posts))
        else:
            parts.append("Nothing has been published yet: this is the first plan.")
        metrics = self.store.metrics(14)
        if metrics:
            parts.append("## Followers by day\n" + "\n".join(f"- {m['day']}: {m['followers']}" for m in metrics))
        if self.cfg.reference_file and self.cfg.reference_file.exists():
            ref = self.cfg.reference_file.read_text()[:8000]
            parts.append(f"## Posts by accounts the CEO likes (for tone and topics; never copy)\n{ref}")
        messages = [
            {"role": "system", "content": f"{prompt('common')}\n\n{prompt('strategist')}"},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

        def done(reply: str) -> None:
            if not reply:
                return
            self.store.add_plan(week, reply)
            if self.store.get("replan") == week:
                self.store.put("replan", "")
            self.say("strategist", self.cfg.streams.plan, f"plan {week}", reply)

        self.enqueue(key, "strategist", messages, done)

    def reconcile_drafts(self) -> None:
        now, today = self.now(), self.today()
        backlog = len(self.store.drafts_with_status("approved", "awaiting_ceo"))
        writing = any(k.startswith("write:") for k in self.inflight)
        if now.time() >= self.cfg.schedule.writing_starts and backlog < self.cfg.pipeline.max_backlog and not writing:
            drafts = self.store.drafts_for_day(today)
            for slot in range(1, self.cfg.pipeline.posts_per_day + 1):
                tries = [d for d in drafts if d.slot == slot]
                if any(d.status not in ("rejected", "dropped") for d in tries):
                    continue
                if len(tries) >= self.cfg.pipeline.attempts_per_slot:
                    continue
                if self.write(today, slot, len(tries) + 1):
                    break
        for d in self.store.drafts_with_status("review"):
            self.review(d)
        for d in self.store.drafts_with_status("revise"):
            self.revise(d)

    def write(self, day: str, slot: int, attempt: int) -> bool:
        task = f"Write post {slot} of {self.cfg.pipeline.posts_per_day} for {day}."
        others = [d.text for d in self.store.drafts_for_day(day) if d.status not in ("rejected", "dropped")]
        if others:
            task += " Today's other posts so far:\n" + "\n".join(f"- {t}" for t in others)
        messages = [
            {"role": "system", "content": f"{prompt('common')}\n\n{prompt('writer')}"},
            {"role": "user", "content": f"{self.context()}\n\n## Task\n{task}"},
        ]

        def done(reply: str) -> None:
            post = T.clean_post(reply)
            draft_id = self.store.add_draft(day, slot, post, topic="", msg_id=None)
            topic = f"{day} · post {slot} · #{draft_id}"
            msg_id = self.say("writer", self.cfg.streams.drafts, topic,
                              f"Draft for slot {slot} (attempt {attempt}), {T.x_length(post)} characters:\n{quote(post)}")
            self.store.update_draft(draft_id, topic=topic, msg_id=msg_id)
            if msg_id is None:
                self.store.update_draft(draft_id, status="dropped", feedback="blocked by a breaker")
                return
            self.after_writing(self.store.draft(draft_id))

        return self.enqueue(f"write:{day}:{slot}:{attempt}", "writer", messages, done)

    def after_writing(self, d: Draft) -> None:
        """Deterministic checks before a draft costs the editor any time."""
        previous = self.recent_posts(50) + [x.text for x in self.store.drafts_with_status("approved", "awaiting_ceo")
                                             if x.id != d.id]
        problems = T.check_post(d.text, self.cfg.pipeline.max_chars, previous)
        if not problems:
            return  # status is already "review"
        feedback = "Automatic checks failed:\n" + "\n".join(f"- {p}" for p in problems)
        self.team.send(self.cfg.listener, self.cfg.streams.drafts, d.topic, feedback, check=False)
        self.needs_revision(d, feedback)

    def needs_revision(self, d: Draft, feedback: str) -> None:
        if d.revisions >= self.cfg.pipeline.max_revisions:
            self.store.update_draft(d.id, status="dropped", feedback=feedback)
            self.team.send(self.cfg.listener, self.cfg.streams.drafts, d.topic,
                           f"Dropped after {d.revisions} revisions.", check=False)
        else:
            self.store.update_draft(d.id, status="revise", feedback=feedback)

    def review(self, d: Draft) -> None:
        task = f"Review this draft (revision {d.revisions}):\n{quote(d.text)}\n" \
               f"Length: {T.x_length(d.text)} characters. Automatic checks for length, links and mentions passed."
        if d.feedback:
            task += f"\n\nYour previous review was:\n{d.feedback}"
        messages = [
            {"role": "system", "content": f"{prompt('common')}\n\n{prompt('editor')}"},
            {"role": "user", "content": f"{self.context()}\n\n## Task\n{task}"},
        ]

        def done(reply: str) -> None:
            cur = self.store.draft(d.id)
            if cur.status != "review" or cur.revisions != d.revisions:
                return  # changed meanwhile, e.g. by the CEO
            verdict, feedback = T.parse_verdict(reply)
            if self.say("editor", self.cfg.streams.drafts, d.topic, reply) is None:
                self.store.update_draft(d.id, status="dropped", feedback="blocked by a breaker")
                return
            if verdict == "approve":
                if self.cfg.pipeline.ceo_approval:
                    ask = self.team.send(self.cfg.listener, self.cfg.streams.drafts, d.topic,
                                         "Ready to publish. CEO: react ✅ to this message to approve, ❌ to discard.",
                                         check=False)
                    self.store.update_draft(d.id, status="awaiting_ceo", feedback=feedback, review_msg_id=ask)
                else:
                    self.store.update_draft(d.id, status="approved", feedback=feedback)
            elif verdict == "reject":
                self.store.update_draft(d.id, status="rejected", feedback=feedback)
            else:
                self.needs_revision(cur, feedback)

        self.enqueue(f"review:{d.id}:{d.revisions}", "editor", messages, done)

    def revise(self, d: Draft) -> None:
        task = f"Revise your draft:\n{quote(d.text)}\n\nFeedback:\n{d.feedback}"
        messages = [
            {"role": "system", "content": f"{prompt('common')}\n\n{prompt('writer')}"},
            {"role": "user", "content": f"{self.context()}\n\n## Task\n{task}"},
        ]

        def done(reply: str) -> None:
            cur = self.store.draft(d.id)
            if cur.status != "revise" or cur.revisions != d.revisions:
                return
            post = T.clean_post(reply)
            n = d.revisions + 1
            msg_id = self.say("writer", self.cfg.streams.drafts, d.topic,
                              f"Revision {n}, {T.x_length(post)} characters:\n{quote(post)}")
            if msg_id is None:
                self.store.update_draft(d.id, status="dropped", feedback="blocked by a breaker")
                return
            self.store.update_draft(d.id, text=post, revisions=n, status="review", msg_id=msg_id)
            self.after_writing(self.store.draft(d.id))

        self.enqueue(f"revise:{d.id}:{d.revisions}", "writer", messages, done)

    # publishing, no LLM involved

    def published_on(self, day: str) -> int:
        return sum(1 for d in self.store.published(50) if d.published_at and d.published_at.startswith(day))

    def reconcile_publish(self) -> None:
        now, today = self.now(), self.today()
        due = sum(1 for t in self.cfg.schedule.publish_times if t <= now.time())
        if self.published_on(today) >= due:
            return
        last = self.store.published(1)
        if last and now - datetime.fromisoformat(last[0].published_at) < timedelta(
                minutes=self.cfg.schedule.min_gap_minutes):
            return
        retry_at = self.store.get("publish_retry_at")
        if retry_at and now < datetime.fromisoformat(retry_at):
            return
        ready = self.store.drafts_with_status("approved")
        if not ready:
            return
        d = ready[0]
        problems = T.check_post(d.text, self.cfg.pipeline.max_chars, self.recent_posts(50))
        if problems:
            self.store.update_draft(d.id, status="dropped", feedback="; ".join(problems))
            self.team.send(self.cfg.listener, self.cfg.streams.drafts, d.topic,
                           f"Not published: {'; '.join(problems)}", check=False)
            return
        try:
            x_id = self.x.post(d.text)
        except Exception as e:
            self.store.put("publish_retry_at", (now + timedelta(minutes=30)).isoformat())
            self.team.notify(f":warning: Publishing draft #{d.id} failed, retrying in 30 minutes: {e}")
            return
        self.store.update_draft(d.id, status="published", x_id=x_id, published_at=now.isoformat(timespec="seconds"))
        where = "dry run, not actually posted" if x_id == "dry-run" else f"https://x.com/i/status/{x_id}"
        self.team.send(self.cfg.listener, self.cfg.streams.published, today,
                       f"Published draft #{d.id} ({where}):\n{quote(d.text)}", check=False)

    def reconcile_metrics(self) -> None:
        now, today = self.now(), self.today()
        if now.time() < self.cfg.schedule.metrics_time or self.store.has_metrics(today):
            return
        if self.store.get("metrics_tried") == today:
            return
        self.store.put("metrics_tried", today)
        try:
            n = self.x.numbers()
        except Exception as e:
            self.team.notify(f":warning: Reading X metrics failed: {e}")
            return
        if n is None:
            return  # dry run
        prev = self.store.metrics(1)
        self.store.add_metrics(today, n.followers, n.following, n.posts)
        delta = f" ({n.followers - prev[0]['followers']:+d})" if prev else ""
        self.team.send(self.cfg.listener, self.cfg.streams.metrics, "followers",
                       f"{today}: **{n.followers}** followers{delta}, {n.posts} posts, "
                       f"{self.published_on(today)} published today.", check=False)
