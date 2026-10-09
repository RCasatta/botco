"""The dispatcher: the orchestrator's main loop.

Accounts decide what to do; this loop only decides *when* each one works,
and runs the parts that stay deterministic on purpose:

- waking accounts: it reacts to changes, never walks every open task on a
  timer. A tracker change wakes whoever the task waits on, except the
  account that made it; a mention wakes the mentioned account; a person's
  message that mentions nobody goes where their role routes it; turn
  accounts also get a heartbeat, skipped when they would see what they saw
  last time;
- engines: a turn account gets one turn for everything waiting on it, a
  session account one session per task, a person a mention in Zulip;
- the model server's slots: turns may use any slot, sessions only their own;
- guardrails: `halt`/`resume`/`status`/`help` in #ops work without the
  model, the daily budget per account, the breakers (in Team);
- schedules, which create tasks; the X publisher and the daily metrics;
- people's direct reviews: `/approve`, `/revise`, `/reject`, ✅ / ❌, and
  `/stop` for a session.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta

from . import text as T
from .agents import Notice, Runner, TurnDone, fingerprint_key, request
from .cron import Cron
from .external import ExternalError
from .policy import parse_ref
from .sessions import SessionDone, SessionJob
from .sinks import XPublisher
from .store import Task
from .tools import Trigger, Turn
from .tracker import Refused, Tracker
from .world import TaskChanged, World

log = logging.getLogger(__name__)

APPROVE = {"check", "white_check_mark", "heavy_check_mark", "check_mark", "+1", "thumbs_up", "like"}
REJECT = {"x", "cross_mark", "-1", "thumbs_down", "no_entry", "cross"}
VERDICT_COMMANDS = {"approve": "approve", "revise": "revise", "reject": "reject"}

HELP = """\
Talk to the team in plain words, anywhere. A message that mentions a bot \
wakes that bot; one that mentions nobody goes where your role routes it. \
A message in a task's topic is a comment on the task and wakes whoever it \
waits on. For example: "make another post about quantization", "#5 is good", \
"drop 6, it repeats yesterday".

Your own reviews, in a task's topic or with its number:
- `/approve [#42] [comments]`, `/revise [#42] what to change`, `/reject [#42] [why]`
- or react ✅ / ❌ on a task's message
- `/stop #42` ends a running session early; its workspace is kept

Commands in #ops that work even when the model is down:
- `status`: what the team is doing and what each task waits on
- `halt`: stop all agents, sessions and publishing (survives restarts)
- `resume`: start again"""


class Company:
    def __init__(self, world: World, runner: Runner):
        self.w = world
        self.cfg = world.cfg
        self.policy = world.policy
        self.runner = runner
        self.tracker = Tracker(world)
        self.x = XPublisher(world)
        self.turn_accounts = self.cfg.turn_accounts()
        self.pending: dict[str, list[Trigger]] = defaultdict(list)
        # (account, task) -> job, in arrival order.
        self.session_queue: dict[tuple[str, int], SessionJob] = {}
        # account -> model of its running turn; task -> (account, model).
        self.running: dict[str, str] = {}
        self.running_sessions: dict[int, tuple[str, str]] = {}
        now = self.w.now()
        # Stagger the first heartbeats over one interval: first the accounts
        # people's messages go to, the coordinators, then the others.
        routed = {r.unaddressed for r in self.cfg.roles.values()}
        order = sorted(self.turn_accounts, key=lambda a: a not in routed)
        beat = timedelta(minutes=self.cfg.dispatcher.heartbeat_minutes)
        n = max(len(order), 1)
        self.last_turn = {a: now - beat + i * beat / n for i, a in enumerate(order)}
        self.notified: set[str] = set()
        self.lab_checked = datetime.min.replace(tzinfo=self.w.tz)

    # main loop

    def run(self) -> None:
        w = self.w
        with w.lock:
            if w.store.get("halted") == "1":
                w.halted.set()
            w.team.setup_streams()
        w.team.listen(w.inbox)
        self.runner.start()
        threading.Thread(target=self._poll_loop, name="external", daemon=True).start()
        state = "**halted** (send `resume` to start)" if w.halted.is_set() else "running"
        mode = "dry run, nothing is posted to X" if self.cfg.x.dry_run else "live on X"
        with w.lock:
            w.team.notify(f"Orchestrator online (task engine): {state}, {mode}. Send `help`.", "status")
        last_tick = datetime.min.replace(tzinfo=w.tz)
        while True:
            try:
                item = w.inbox.get(timeout=30)
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
            self.running.pop(item.agent, None)
            self.last_turn[item.agent] = self.w.now()
            log.info("%s: %d steps, %s", item.agent, item.steps, item.outcome)
        elif isinstance(item, SessionDone):
            self.running_sessions.pop(item.task, None)
            log.info("session of %s on #%d: %s", item.account, item.task, item.outcome)
            self.session_ended(item)
        elif isinstance(item, TaskChanged):
            self.on_task_changed(item)
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
        d = self.cfg.dispatcher
        if d.active_from <= now.time() <= d.active_to:
            for agent in self.turn_accounts:
                idle = agent not in self.running and not self.pending[agent]
                if idle and now - self.last_turn[agent] >= timedelta(minutes=d.heartbeat_minutes):
                    self.pending[agent].append(Trigger("heartbeat"))
        self.run_schedules()
        self.x.tick()
        if now - self.lab_checked >= timedelta(minutes=10):
            self.lab_checked = now
            self.watch_lab()

    # waking

    def wake(self, account: str, t: Task, trigger: Trigger) -> None:
        a = self.cfg.accounts.get(account)
        if a is None:
            return
        if a.engine is None:
            self.notify_person(account, t)
        elif a.engine.kind == "turn":
            self.add_trigger(account, trigger)
        else:
            self.session_queue.setdefault((account, t.id), SessionJob(account, t.id, trigger.content))

    def add_trigger(self, account: str, trigger: Trigger) -> None:
        mine = self.pending[account]
        if trigger.msg_id and any(t.msg_id == trigger.msg_id for t in mine):
            return
        mine.append(trigger)

    def notify_person(self, account: str, t: Task) -> None:
        """Mention a person in the task's topic when it starts waiting on
        them, once per state of the task."""
        w = self.w
        waiting = self.policy.waits_on(w.store, t)
        key, state = f"notified:{t.id}:{account}", f"{t.hash}:{waiting.why}"
        if w.store.get(key) == state:
            return
        w.store.put(key, state)
        how = (" Reply `/approve`, `/revise <what to change>` or `/reject`, or react ✅ / ❌."
               if self.policy.may(account, "approve", self.policy.kind(t.kind)) else "")
        self.tracker.post(self.cfg.publisher, t, f"{w.team.mention(account)} #{t.id} waits on you ({waiting.why}).{how}")

    def on_task_changed(self, ev: TaskChanged) -> None:
        t = self.w.store.task(ev.task)
        if t is None:
            return
        waiting = self.policy.waits_on(self.w.store, t)
        for account in waiting.accounts:
            if account != ev.actor:
                self.wake(account, t, Trigger("task", content=f"{ev.what} (by {ev.actor}). It waits on you: "
                                                              f"{waiting.why}.", task=t.id, account=ev.actor))

    def session_ended(self, done: SessionDone) -> None:
        """Whoever asked for the work hears that its summary is in."""
        t = self.w.store.task(done.task)
        if t is None or t.author == done.account:
            return
        trigger = Trigger("task", content=f"{done.account}'s session ended ({done.outcome}); its summary is the "
                                          "latest comment.", task=t.id, account=done.account)
        a = self.cfg.accounts.get(t.author)
        if a and a.engine and a.engine.kind == "turn":
            self.add_trigger(t.author, trigger)
        elif a and a.engine is None:
            self.tracker.post(self.cfg.publisher, t, f"{self.w.team.mention(t.author)} {done.account}'s session on "
                                                     f"#{t.id} ended ({done.outcome}).")

    # starting work

    def model_load(self, model: str) -> tuple[int, int]:
        turns = sum(1 for m in self.running.values() if m == model)
        sessions = sum(1 for _, m in self.running_sessions.values() if m == model)
        return turns, sessions

    def free(self, model: str, session: bool) -> bool:
        m = self.cfg.models[model]
        turns, sessions = self.model_load(model)
        if turns + sessions >= m.slots:
            return False
        return not session or sessions < min(m.session_slots, m.slots)

    def turn_priority(self, triggers: list[Trigger]) -> int:
        def one(t: Trigger) -> int:
            if t.kind == "heartbeat":
                return 100
            if t.kind == "lab":
                return 50
            return self.policy.priority(t.account)
        return min(one(t) for t in triggers)

    def over_budget(self, account: str) -> bool:
        w = self.w
        used = int(w.store.get(f"turns:{w.today()}:{account}", "0"))
        budget = self.policy.budget(account)
        if used < budget:
            return False
        self.notify_once(f"budget:{w.today()}:{account}",
                         f":octagonal_sign: {account} used its {budget} turns today: it is not woken again until "
                         "tomorrow; what waits on it keeps waiting.")
        return True

    def count(self, account: str) -> None:
        w = self.w
        w.store.incr(f"turns:{w.today()}")
        w.store.incr(f"turns:{w.today()}:{account}")

    def schedule(self) -> None:
        """Start turns and sessions with a reason to run, as slots free up.
        Turns start only when they can run, so triggers that arrive while an
        account waits merge into its next turn; higher-priority requests go
        first."""
        if self.w.halted.is_set():
            return
        waiting = [a for a in self.turn_accounts if self.pending[a] and a not in self.running]
        waiting.sort(key=lambda a: self.turn_priority(self.pending[a]))
        for agent in waiting:
            model = self.cfg.accounts[agent].engine.model
            if not self.free(model, session=False):
                continue
            turn = Turn(agent, self.pending[agent])
            if self.unchanged(turn):
                self.pending[agent] = []
                self.last_turn[agent] = self.w.now()
                self.w.store.incr(f"skipped:{self.w.today()}")
                log.info("%s: heartbeat skipped, nothing changed since its last turn", agent)
                continue
            self.pending[agent] = []
            if self.over_budget(agent):
                continue
            self.count(agent)
            self.running[agent] = model
            self.runner.submit(turn)
        for key, job in list(self.session_queue.items()):
            if job.task in self.running_sessions:
                continue
            t = self.w.store.task(job.task)
            if t is None or job.account not in self.policy.waits_on(self.w.store, t).accounts:
                del self.session_queue[key]
                continue
            model = self.cfg.accounts[job.account].engine.model
            if not self.free(model, session=True):
                continue
            del self.session_queue[key]
            if self.over_budget(job.account):
                continue
            self.count(job.account)
            self.running_sessions[job.task] = (job.account, model)
            self.runner.submit(job)

    def unchanged(self, turn: Turn) -> bool:
        """A heartbeat whose account would see exactly what it saw at the
        start of its last completed turn, apart from the clock: running it
        would cost a full prefill and some thinking to conclude "nothing to
        do" again. Turns anything else asked for always run."""
        if any(t.kind != "heartbeat" for t in turn.triggers):
            return False
        seen = self.w.store.get(fingerprint_key(turn.agent))
        return seen is not None and seen == request(self.w, turn)[2]

    # Zulip events

    def on_message(self, msg: dict) -> None:
        if msg.get("type") != "stream":
            return
        w = self.w
        stream, topic, content, sender = msg["display_recipient"], msg["subject"], msg["content"], msg["sender_id"]
        from_bot = w.team.is_bot(sender)
        w.team.breakers.observe(stream, topic, from_bot)
        author = w.team.account_of(sender)
        if author == self.cfg.publisher or (from_bot and author is None):
            return  # operational messages and foreign bots never wake anyone
        if not from_bot and self.command(content.strip(), stream, topic, author, msg):
            return
        trigger = Trigger("bot" if from_bot else "human", msg["sender_full_name"], stream, topic, content,
                          msg["id"], account=author)
        woken = set()
        task = w.store.task_by_topic(stream, topic)
        if task is not None and not from_bot:
            # A person's message in a task's topic is a comment on the task:
            # it wakes whoever the task waits on, with the message itself.
            w.store.add_comment(task.id, author or msg["sender_full_name"], content, msg["id"])
            w.store.link_message(msg["id"], task.id)
            for account in self.policy.waits_on(w.store, task).accounts:
                a = self.cfg.accounts[account]
                if account == author or a.engine is None:
                    continue
                if a.engine.kind == "turn":
                    woken.add(account)
                else:
                    self.wake(account, task, Trigger("task", content=f"{msg['sender_full_name']} commented",
                                                     task=task.id, account=author))
        called = {w.team.account_of(n) for n in T.mentioned(content, w.team.names())} & set(self.turn_accounts)
        woken |= called
        woken.discard(author)
        if not woken and not from_bot:
            target = self.policy.unaddressed(author)
            if target and target != author:
                woken = {target}
        for account in woken:
            if account in self.turn_accounts:
                self.add_trigger(account, trigger)

    def _task_arg(self, words: list[str], stream: str, topic: str) -> tuple[Task | None, list[str]]:
        if words and words[0].lstrip("#").isdigit():
            return self.w.store.task(int(words[0].lstrip("#"))), words[1:]
        return self.w.store.task_by_topic(stream, topic), words

    def command(self, content: str, stream: str, topic: str, author: str | None, msg: dict) -> bool:
        w = self.w
        if content.startswith("/"):
            words = content[1:].split()
            word = words[0].lower() if words else ""
            if word in VERDICT_COMMANDS or word == "stop":
                self.slash(word, words[1:], stream, topic, author, msg)
                return True
        if stream != self.cfg.streams.ops:
            return False
        word = content.lower().lstrip("/").split(maxsplit=1)[0] if content else ""
        if word not in ("halt", "resume", "status", "help") or len(content.split()) > 2:
            return False
        if word == "halt":
            w.halted.set()
            w.store.put("halted", "1")
            reply = (":octagonal_sign: **Halted.** Agents stop at their next step, no session starts and nothing "
                     "is published. Send `resume` to restart.")
        elif word == "resume":
            w.halted.clear()
            w.store.put("halted", "0")
            self.notified.clear()
            reply = ":check: Resumed."
        elif word == "status":
            reply = self.status()
        else:
            reply = HELP
        w.team.send(self.cfg.publisher, self.cfg.streams.ops, topic, reply, check=False)
        return True

    def slash(self, word: str, args: list[str], stream: str, topic: str, author: str | None, msg: dict) -> None:
        w = self.w

        def reply(text: str) -> None:
            w.team.send(self.cfg.publisher, stream, topic, text, check=False)

        t, rest = self._task_arg(args, stream, topic)
        if t is None:
            reply(f"`/{word}` needs a task: write it in the task's topic, or as `/{word} #42`.")
            return
        if author is None:
            reply(f"{msg['sender_full_name']} has no account here, so `/{word}` counts for nothing.")
            return
        if word == "stop":
            if self.runner.sessions.stop(t.id, author):
                reply(f"Stopping the session on #{t.id}; its workspace is kept.")
            else:
                reply(f"No session is running on #{t.id}.")
            return
        try:
            self.tracker.review(author, t, VERDICT_COMMANDS[word], " ".join(rest), msg_id=msg["id"], post=False)
        except Refused as e:
            reply(f"Not recorded: {e}.")
            return
        w.store.link_message(msg["id"], t.id)
        # In words too, not only as a reaction: the agents read the chat,
        # not the reactions, and a refusal earlier in the topic would
        # otherwise look like the last word.
        where = "" if (stream, topic) == (t.stream, t.topic) else f" (in #{stream} > {topic})"
        self.tracker.post(self.cfg.publisher, t, f"Recorded: {author} **{word}** #{t.id}{where}.")
        try:
            w.team.react(self.cfg.publisher, msg["id"], "check")
        except Exception:  # noqa: BLE001 - the review is recorded either way
            log.warning("could not react to message %s", msg["id"])

    def on_reaction(self, ev: dict) -> None:
        w = self.w
        if ev.get("op") != "add" or w.team.is_bot(ev["user_id"]):
            return
        t = w.store.task_by_message(ev["message_id"])
        if not t or not t.open:
            return
        verdict = "approve" if ev["emoji_name"] in APPROVE else "reject" if ev["emoji_name"] in REJECT else None
        author = w.team.account_of(ev["user_id"])
        if verdict is None or author is None:
            return
        try:
            self.tracker.review(author, t, verdict, "", msg_id=ev["message_id"], post=False)
        except Refused as e:
            self.tracker.post(self.cfg.publisher, t, f"Not recorded: {e}.")
            return
        mark = "✅" if verdict == "approve" else "❌"
        self.tracker.post(self.cfg.publisher, t, f"Recorded: {author} **{verdict}** #{t.id} ({mark}).")

    # schedules and sources

    def run_schedules(self) -> None:
        w = self.w
        now = w.now()
        checked = w.store.get("schedules_checked")
        w.store.put("schedules_checked", now.isoformat())
        if checked is None:
            return  # first start: nothing in the past fires
        after = datetime.fromisoformat(checked)
        for s in self.cfg.schedules.values():
            if Cron(s.cron).fired(after, now):
                self.fire(s.name)

    def fire(self, name: str) -> Task | None:
        """Create a schedule's task, as if a person had: depth 0, and it wakes
        its assignee like any new task. Skipped while the last one is open,
        unless its kind keeps one open at a time: then it replaces it."""
        s, w = self.cfg.schedules[name], self.w
        open_one = self.tracker.find_open(schedule=name)
        if open_one and not self.policy.kind(s.kind).one_open:
            log.info("schedule %s: #%d is still open, skipped", name, open_one.id)
            return None
        fmt = {"iso_week": w.week(), "date": w.today()}
        try:
            t, _ = self.tracker.create(f"schedule:{name}", s.kind, s.title.format(**fmt), s.body.format(**fmt),
                                       assignee=s.assignee, schedule=name, system=True)
        except Refused as e:
            w.team.notify(f":warning: Schedule {name} could not create its task: {e}")
            return None
        return t

    def _poll_loop(self) -> None:
        while True:
            _time.sleep(self.cfg.sources.poll_minutes * 60)
            if not self.w.halted.is_set():
                try:
                    self.poll_external()
                except Exception:  # noqa: BLE001 - logged, tried again next time
                    log.exception("polling external issues")

    def poll_external(self) -> None:
        """Check the external issues open tasks refer to; a change wakes
        whoever the local task waits on. The requests run without the lock."""
        w = self.w
        with w.lock:
            wanted: dict[str, list[int]] = defaultdict(list)
            for t in w.store.open_tasks():
                for r in t.refs:
                    if parse_ref(r).external:
                        wanted[r].append(t.id)
        for ref, tasks in wanted.items():
            try:
                fresh = w.issues.fetch(parse_ref(ref))
            except ExternalError as e:
                log.warning("cannot read %s: %s", ref, e)
                continue
            with w.lock:
                old = w.store.external(ref)
                w.store.put_external(fresh)
                if old is None or (old.updated_at, old.comments) == (fresh.updated_at, fresh.comments):
                    continue
                what = f"the referenced issue {ref} changed"
                if len(fresh.comments) and fresh.comments != old.comments:
                    what += f": new comment by {fresh.comments[-1]['author']}"
                for task_id in tasks:
                    w.changed(task_id, ref, what)

    def watch_lab(self) -> None:
        """Wake the configured account when reports in the lab notebook
        appear or change: new results are the best material the team has."""
        lab, notify = self.w.lab, self.cfg.sources.lab.notify
        if not lab or not lab.available() or notify not in self.turn_accounts:
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
            what = "new or updated: " + ", ".join(f"`lab:{p}`" for p in changed[:10])
        self.pending[notify].append(Trigger("lab", content=what))

    # status

    def status(self) -> str:
        w = self.w
        today = w.today()
        accounts = []
        for name, a in self.cfg.accounts.items():
            if a.engine is None:
                continue
            used, budget = int(w.store.get(f"turns:{today}:{name}", "0")), self.policy.budget(name)
            line = f"{name} {used}/{budget}"
            if used >= budget:
                line += " (**over budget**, sleeps until tomorrow)"
            accounts.append(line)
        tasks = []
        for t in w.store.open_tasks()[-20:]:
            waiting = self.policy.waits_on(w.store, t)
            tasks.append(f"- #{t.id} {t.kind}: {t.title[:60]} → {', '.join(waiting.accounts) or 'nobody'} ({waiting.why})")
        sessions = [f"#{task} ({acc})" for task, (acc, _) in self.running_sessions.items()]
        queued = [f"#{task} ({acc})" for acc, task in self.session_queue]
        pending = {a: len(t) for a, t in self.pending.items() if t}
        return "\n".join([
            f"**State**: {'halted' if w.halted.is_set() else 'running'}, "
            f"{'dry run' if self.cfg.x.dry_run else 'live on X'}",
            f"**Inference**: {'down' if self.runner.down else 'up'}",
            f"**Turns**: running: {', '.join(sorted(self.running)) or 'none'}; waiting to wake: {pending or 'none'}",
            f"**Sessions**: running: {', '.join(sessions) or 'none'}; queued: {', '.join(queued) or 'none'}",
            f"**Turns today**: {w.store.get(f'turns:{today}', '0')} ({'; '.join(accounts)}), "
            f"{w.store.get(f'continued:{today}', '0')} continuing a conversation, "
            f"{w.store.get(f'skipped:{today}', '0')} heartbeats skipped because nothing had changed",
            f"**Ready to publish**: {', '.join(f'#{t.id}' for t in self.x.ready()) or 'none'}; "
            f"published today: {w.published_on(today)}",
            "**Open tasks**:" + ("\n" + "\n".join(tasks) if tasks else " none"),
        ])
