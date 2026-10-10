"""The turn engine: an account wakes up, reads the situation, and acts
through the five tools until it has nothing more to do.

Turns run on worker threads. The model is called without holding the world
lock; tool calls and context building hold it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources

import requests

from . import tools
from .config import TurnEngine
from .llm import LLMDown
from .sessions import SessionDone, SessionJob, Sessions
from .sinks import metrics_text
from .store import Task
from .tools import Trigger, Turn
from .world import World, quote

log = logging.getLogger(__name__)


@dataclass
class TurnDone:
    agent: str
    steps: int
    outcome: str


@dataclass
class Notice:
    """Something for #ops, raised from a worker thread."""

    text: str


def prompt(name: str) -> str:
    return resources.files("botco").joinpath(f"prompts/{name}.md").read_text()


def has_prompt(name: str) -> bool:
    return resources.files("botco").joinpath(f"prompts/{name}.md").is_file()


def system_prompt(w: World) -> str:
    """The same for every account: the role comes later, in the situation,
    so the server can reuse its cache for this part across accounts."""
    cfg = w.cfg
    people, members = [], []
    for a in cfg.accounts.values():
        roles = ", ".join(a.roles) or "no role"
        if a.engine is None and a.zulip:
            people.append(f"  - {a.name} ({roles}), a person: mention as {w.team.mention(a.name)}")
        elif a.engine is not None:
            how = "works in short turns like you" if a.engine.kind == "turn" else \
                "works alone on one task at a time in a sandbox and reports a summary; it cannot chat"
            members.append(f"  - {a.name} ({roles}): {how}")
    kinds = []
    for k in cfg.kinds.values():
        rule = (f"needs an approval of its current body from each of: {', '.join(k.approve)}" if k.approve
                else "waits on its assignee until they close it")
        extra = (", then the publisher posts it to X" if k.sink == "x" else "") + \
                (f", then the {k.posted_by} posts it on X by hand" if k.posted_by else "") + \
                ("; only one is open at a time" if k.one_open else "")
        kinds.append(f"  - {k.name}: {rule}{extra}. Topics in #{k.stream}.")
    team = prompt("team").format(
        people="\n".join(people) or "  - (none configured)", members="\n".join(members),
        kinds="\n".join(kinds), ops=cfg.streams.ops, published=cfg.streams.published,
    )
    return f"{prompt('common')}\n\n{team}"


def describe_trigger(t: Trigger) -> str:
    if t.kind == "heartbeat":
        return "- Routine check-in: nobody called you. Look at the situation and do what your role needs, if anything."
    if t.kind == "lab":
        return (f"- The lab notebook changed: {t.content}. New results can be material for posts; "
                "read what changed and decide whether the plan or the writer should use it.")
    if t.kind == "task":
        return f"- #{t.task}: {t.content}"
    who = t.sender if t.kind == "bot" else f"{t.sender} (a person{', ' + t.account if t.account else ''})"
    return f"- {who} wrote in #{t.stream} > {t.topic} (message {t.msg_id}):\n{quote(t.content[:2000])}"


def team_messages(w: World) -> list[dict]:
    """Recent stream messages, oldest first, without the activity log."""
    ops = w.cfg.streams.ops
    return [m for m in w.team.history(n=150)
            if isinstance(m.get("display_recipient"), str)
            and not (m["display_recipient"] == ops and m.get("subject") == "activity")]


def chat_window(w: World, msgs: list[dict] | None = None) -> list[dict]:
    """Today's messages, so the section only grows during the day and its
    beginning stays reusable; at least the latest 20, at most 80."""
    msgs = team_messages(w) if msgs is None else msgs
    midnight = w.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [m for m in msgs if m["timestamp"] >= midnight]
    return (today if len(today) >= 20 else msgs[-20:])[-80:]


# Section helpers, shared by the full situation and the delta of a continued
# turn, so both always render the same way.

def chat_line(m: dict) -> str:
    return (f"[{time.strftime('%a %H:%M', time.localtime(m['timestamp']))}] "
            f"#{m['display_recipient']} > {m['subject']} | {m['sender_full_name']}: {m['content'][:600]}")


def lab_line(n) -> str:
    return f"- `lab:{n.path}` ({n.modified:%Y-%m-%d}, {n.size // 1000} KB): {n.title}"


def lab_section(w: World) -> str | None:
    if not (w.lab and w.lab.available()):
        return None
    return ("## Lab notebook (our own experiments on this machine; read a report with read, e.g. "
            "`lab:FILE.md#Section`)\n" + "\n".join(lab_line(n) for n in w.lab.notes()))


def standing_tasks(w: World) -> list[Task]:
    """Open tasks of kinds with one open at a time, like the weekly plan:
    everyone works from them."""
    out = []
    for k in w.cfg.kinds.values():
        if k.one_open:
            out += w.store.open_tasks(k.name)
    return out


def standing_section(w: World) -> str | None:
    parts = []
    for k in w.cfg.kinds.values():
        if not k.one_open:
            continue
        found = w.store.open_tasks(k.name)
        if found:
            t = found[-1]
            parts.append(f"## Current {k.name}: #{t.id} {t.title} (version {t.version}, by {t.author})\n{t.body}")
        else:
            parts.append(f"## Current {k.name}\nThere is no open {k.name}.")
    return "\n\n".join(parts) or None


def published_line(t: Task, numbers=None) -> str:
    seen = f" ({metrics_text(numbers)} on {numbers['day']})" if numbers else ""
    return f"- [{(t.closed_at or '')[:16]}]{seen} {t.body}"


def publishing_section(w: World) -> str | None:
    kinds = [k for k in w.cfg.kinds.values() if k.sink == "x"]
    if not kinds:
        return None
    times = ", ".join(t.strftime("%H:%M") for t in w.cfg.x.times)
    pub = [f"The publisher (a script) posts to X at {times}: each time, the oldest {kinds[0].name} "
           f"approved by {' and '.join(kinds[0].approve) or 'nobody'}."]
    recent = w.store.published(10)
    if recent:
        numbers = w.store.post_metrics()
        pub.append("Recently published, newest first, with their numbers on X when read (once a day):\n"
                   + "\n".join(published_line(t, numbers.get(t.id)) for t in recent))
    return "## Publishing\n" + "\n".join(pub)


def followers_line(m) -> str:
    return f"- {m['day']}: {m['followers']}"


def note_line(n) -> str:
    return f"- [{n['created_at'][:10]}] {n['text']}"


def task_block(w: World, t: Task) -> str:
    """A task in full, as an account it waits on needs it."""
    policy, store = w.policy, w.store
    waiting = policy.waits_on(store, t)
    head = f"### #{t.id} {t.kind} by {t.author}, version {t.version}: {t.title}"
    state = f"Waits on: {', '.join(waiting.accounts) or 'nobody'} ({waiting.why})"
    if policy.kind(t.kind).approve:
        state += f". Approvals of this version: {policy.approvals(store, t)}"
    lines = [head, state]
    extra = [f"assignee: {t.assignee}" if t.assignee and t.assignee != t.author else "",
             f"refs: {', '.join(t.refs)}" if t.refs else "", f"labels: {', '.join(t.labels)}" if t.labels else ""]
    if any(extra):
        lines.append("; ".join(e for e in extra if e))
    if t.body:
        lines.append(quote(t.body))
    if t.note:
        lines.append(f"Author's note: {t.note}")
    reviews = [r for r in store.reviews(t.id) if r.body_hash == t.hash]
    if reviews:
        lines.append("Reviews of this version:\n" + "\n".join(
            f"- {r.account}: {r.verdict}" + (f" ({r.comments})" if r.comments else "") for r in reviews))
    comments = store.comments(t.id, 5)
    if comments:
        lines.append("Latest comments:\n" + "\n".join(f"- {c.author}: {c.text[:500]}" for c in comments))
    return "\n".join(lines)


def task_line(w: World, t: Task) -> str:
    waiting = w.policy.waits_on(w.store, t)
    return f"- #{t.id} {t.kind} by {t.author}: {t.title}. Waits on {', '.join(waiting.accounts) or 'nobody'} ({waiting.why})"


def waiting_on(w: World, account: str) -> list[Task]:
    return [t for t in w.store.open_tasks() if account in w.policy.waits_on(w.store, t).accounts]


def queue_section(w: World) -> str | None:
    kinds = {k.name for k in w.cfg.kinds.values() if k.sink == "x"}
    if not kinds:
        return None
    ready = [t for t in w.store.open_tasks() if t.kind in kinds and w.policy.waits_on(w.store, t).ready]
    return (f"## Publishing queue\nPublished today: {w.published_on(w.today())}. "
            f"Ready to publish: {', '.join(f'#{t.id}' for t in ready) or 'none'}.")


def awake_section(turn: Turn) -> str:
    awake = "## Why you are awake\n" + "\n".join(describe_trigger(t) for t in turn.triggers)
    if turn.human:
        awake += ("\nA person wrote to you: answer them with comment, on the task or in the same topic. If they "
                  "stated a verdict on a task, record it with comment, the verdict and on_behalf_of set to the id "
                  "of their message.")
    return awake


def situation(w: World, turn: Turn, clock: bool = True) -> str:
    """Everything the account knows at the start of a fresh turn.

    Ordered from what changes least to what changes most, because the server
    reuses its cache only up to the first difference from an earlier
    request: first what every account shares and rarely changes (lab index,
    plan, published posts), then this account's role and notes, then what
    moves during the day (chat, tasks, queue), and the reason for the turn and
    the time last."""
    cfg, store = w.cfg, w.store
    engine = cfg.accounts[turn.agent].engine
    parts = []

    # Shared by every account, changes rarely.
    for section in (lab_section(w), standing_section(w), publishing_section(w)):
        if section:
            parts.append(section)
    metrics = store.metrics(7)
    if metrics:
        parts.append("## Followers\n" + "\n".join(followers_line(m) for m in metrics))

    # This account.
    parts.append(f"## Your role\n{prompt(engine.persona)}")
    notes = store.notes(turn.agent)
    if notes:
        parts.append("## Your notes\n" + "\n".join(note_line(n) for n in notes))
    if engine.persona == "strategist" and cfg.reference_file and cfg.reference_file.exists():
        parts.append("## Posts by accounts the owner likes (for tone and topics; never copy)\n"
                     + cfg.reference_file.read_text()[:6000])

    # Moves during the day.
    msgs = chat_window(w)
    if msgs:
        parts.append("## Team chat, oldest first\n" + "\n".join(chat_line(m) for m in msgs))
    mine = waiting_on(w, turn.agent)
    skip = {t.id for t in mine} | {t.id for t in standing_tasks(w)}
    others = [t for t in store.open_tasks() if t.id not in skip]
    parts.append("## Waiting on you\n" + ("\n\n".join(task_block(w, t) for t in mine) if mine else "Nothing."))
    parts.append("## Other open tasks\n" + ("\n".join(task_line(w, t) for t in others) if others else "None."))
    if q := queue_section(w):
        parts.append(q)

    # This turn.
    parts.append(awake_section(turn))
    if clock:
        parts.append(clock_line(w, turn.agent))
    return "\n\n".join(parts)


def task_state(w: World, t: Task) -> tuple:
    return (t.status, t.version, t.hash, t.assignee, t.title, len(w.store.reviews(t.id)),
            len(w.store.comments(t.id, 1000)), tuple(w.policy.waits_on(w.store, t).accounts))


@dataclass
class Snapshot:
    """What an account saw at the start of a turn, to tell it later what
    changed."""

    chat_id: int
    tasks: dict[int, tuple]
    published: set[int]
    metrics: set[str]
    lab: dict[str, str]
    notes: set[int]


def snapshot(w: World, agent: str, msgs: list[dict]) -> Snapshot:
    return Snapshot(
        chat_id=max((m["id"] for m in msgs), default=0),
        tasks={t.id: task_state(w, t) for t in w.store.open_tasks()},
        published={t.id for t in w.store.published(10)},
        metrics={m["day"] for m in w.store.metrics(7)},
        lab={n.path: n.modified.isoformat() for n in w.lab.notes()} if w.lab and w.lab.available() else {},
        notes={n["id"] for n in w.store.notes(agent)},
    )


def delta(w: World, turn: Turn, since: datetime, old: Snapshot, new: Snapshot, msgs: list[dict]) -> str:
    """The user message that continues a conversation: what changed since
    the account's last turn, then the queue, the reason and the time, as at
    the end of a fresh situation."""
    store, changes = w.store, []
    chat = [chat_line(m) for m in msgs if m["id"] > old.chat_id]
    if chat:
        changes.append("### New chat messages\n" + "\n".join(chat))
    mine = {t.id for t in waiting_on(w, turn.agent)}
    changed = [store.task(i) for i, state in new.tasks.items() if old.tasks.get(i) != state]
    blocks = [task_block(w, t) for t in changed if t.id in mine]
    lines = [task_line(w, t) for t in changed if t.id not in mine]
    lines += [f"- #{i} is closed: {store.task(i).resolution}." for i in old.tasks if i not in new.tasks]
    if blocks:
        changes.append("### Waiting on you, new or changed\n" + "\n\n".join(blocks))
    if lines:
        changes.append("### Other tasks, new, changed or closed\n" + "\n".join(lines))
    standing = [t for t in standing_tasks(w) if old.tasks.get(t.id) != new.tasks.get(t.id)]
    for t in standing:
        changes.append(f"### The current {t.kind} changed: #{t.id} {t.title} (version {t.version})\n{t.body}")
    published = [published_line(t) for t in store.published(10) if t.id not in old.published]
    if published:
        changes.append("### Newly published\n" + "\n".join(published))
    followers = [followers_line(m) for m in store.metrics(7) if m["day"] not in old.metrics]
    if followers:
        changes.append("### Followers\n" + "\n".join(followers))
    if w.lab and w.lab.available():
        lab = [lab_line(n) for n in w.lab.notes() if old.lab.get(n.path) != n.modified.isoformat()]
        if lab:
            changes.append("### Lab notebook, new or updated\n" + "\n".join(lab))
    notes = [note_line(n) for n in store.notes(turn.agent) if n["id"] not in old.notes]
    if notes:
        changes.append("### Your new notes\n" + "\n".join(notes))
    head = (f"## Since your last turn ({since:%H:%M})\n"
            "This continues your conversation; everything above still holds unless changed here.")
    body = "\n\n".join(changes) if changes else "Nothing changed besides what follows."
    tail = [s for s in (queue_section(w), awake_section(turn), clock_line(w, turn.agent)) if s]
    return "\n\n".join([head, body, *tail])


def clock_line(w: World, agent: str) -> str:
    """The only part of the prompt that changes by itself, last on purpose."""
    return (f"It is {w.now():%A %Y-%m-%d %H:%M} ({w.cfg.timezone}). You are the {agent}. "
            "Now act. Use tools; when you have nothing more to do, reply with a one-line summary.")


def request(w: World, turn: Turn) -> tuple[list[dict], list[dict], str]:
    """The messages and tools of a turn's first model call, and a fingerprint
    of everything in them except the clock line: equal fingerprints mean the
    account would see exactly what it saw before. Call it holding w.lock."""
    system, body = system_prompt(w), situation(w, turn, clock=False)
    clock = clock_line(w, turn.agent)
    specs = [t.spec() for t in tools.TOOLS]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": f"{body}\n\n{clock}"}]
    data = json.dumps([system, body, [t["function"]["name"] for t in specs]])
    return messages, specs, hashlib.sha256(data.encode()).hexdigest()


def fingerprint_key(agent: str) -> str:
    return f"fingerprint:{agent}"


@dataclass
class Conversation:
    """An account's last completed turn, kept so a turn soon after can
    continue it: the server then finds the whole earlier conversation in its
    cache and only reads the update."""

    messages: list[dict]
    specs: list[dict]
    snapshot: Snapshot
    started_at: datetime
    ended_at: datetime
    day: str

    def chars(self) -> int:
        return sum(len(m.get("content") or "") + len(m.get("reasoning_content") or "")
                   + len(json.dumps(m.get("tool_calls") or [])) for m in self.messages)


class Runner:
    """Runs turns and sessions on worker threads, one thread per job; the
    dispatcher decides when a job may start."""

    def __init__(self, world: World, llms: dict, sessions: Sessions | None = None):
        self.w = world
        # Model name -> LLM client.
        self.llms = llms
        self.sessions = sessions or Sessions(world)
        self.down = False
        self._down_lock = threading.Lock()
        # Per account; only touched by the account's own turn, under w.lock.
        self.convos: dict[str, Conversation] = {}
        # Without start(), jobs wait here for a test to run them.
        self.threaded = False
        self.queued: list = []

    def can_continue(self, convo: Conversation | None, specs: list[dict]) -> bool:
        d, w = self.w.cfg.dispatcher, self.w
        return (convo is not None and convo.day == w.today() and convo.specs == specs
                and w.now() - convo.ended_at <= timedelta(minutes=d.continue_minutes)
                and convo.chars() < d.continue_max_chars)

    def start(self) -> None:
        self.threaded = True

    def submit(self, job: Turn | SessionJob) -> None:
        if not self.threaded:
            self.queued.append(job)
            return
        target = self._turn if isinstance(job, Turn) else self._session
        name = f"turn-{job.agent}" if isinstance(job, Turn) else f"session-{job.task}"
        threading.Thread(target=target, args=(job,), name=name, daemon=True).start()

    def _set_down(self, down: bool, why: str = "") -> None:
        with self._down_lock:
            if self.down == down:
                return
            self.down = down
        self.w.inbox.put(Notice(f":warning: Inference server unreachable ({why}). Agents wait for it."
                                if down else ":check: Inference server is back."))

    def _wait_healthy(self, llm) -> None:
        delay = 15
        while not llm.healthy():
            self._set_down(True, "health check failed")
            time.sleep(delay)
            delay = min(delay * 2, 300)
        self._set_down(False)

    def _turn(self, turn: Turn) -> None:
        while self.w.halted.is_set():
            time.sleep(5)
        steps, outcome = 0, "error"
        try:
            steps, outcome = self.run_turn(turn)
        except Exception as e:  # noqa: BLE001 - reported to #ops
            log.exception("turn of %s", turn.agent)
            self.w.inbox.put(Notice(f":warning: {turn.agent}'s turn failed: `{type(e).__name__}: {e}`"))
        finally:
            self.w.inbox.put(TurnDone(turn.agent, steps, outcome))

    def _session(self, job: SessionJob) -> None:
        outcome = "error"
        try:
            outcome = self.sessions.run(job)
        except Exception as e:  # noqa: BLE001 - reported to #ops
            log.exception("session on #%d", job.task)
            self.w.inbox.put(Notice(f":warning: {job.account}'s session on #{job.task} failed: "
                                    f"`{type(e).__name__}: {e}`"))
        finally:
            self.w.inbox.put(SessionDone(job.account, job.task, outcome))

    def run_turn(self, turn: Turn) -> tuple[int, str]:
        w = self.w
        engine: TurnEngine = w.cfg.accounts[turn.agent].engine
        llm = self.llms[engine.model]
        with w.lock:
            fresh, specs, _ = request(w, turn)
            msgs = team_messages(w)
            seen = snapshot(w, turn.agent, msgs)
            # Popped: a turn that does not finish leaves nothing to continue.
            convo = self.convos.pop(turn.agent, None)
            if self.can_continue(convo, specs):
                update = delta(w, turn, convo.started_at, convo.snapshot, seen, msgs)
                messages, mode = convo.messages + [{"role": "user", "content": update}], "continued"
                w.store.incr(f"continued:{w.today()}")
            else:
                messages, mode = fresh, "fresh"
            started = w.now()
        steps, reply = 0, None
        while steps < w.cfg.dispatcher.max_steps:
            if w.halted.is_set():
                return steps, "halted"
            self._wait_healthy(llm)
            try:
                reply = llm.chat(turn.agent, engine, messages, specs)
            except LLMDown as e:
                self._set_down(True, str(e)[:100])
                continue
            except requests.Timeout:
                return steps, "timed out"
            steps += 1
            messages.append(reply)
            if not reply["tool_calls"]:
                break
            for call in reply["tool_calls"]:
                fn = call["function"]
                with w.lock:
                    if w.halted.is_set():
                        return steps, "halted"
                    result = tools.execute(w, turn, fn["name"], fn.get("arguments") or "{}")
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
        else:
            return steps, "out of steps"
        # A person asked something and the account answered in plain text
        # instead of with comment: deliver the text where they asked.
        asked = [t for t in turn.triggers if t.kind == "human"]
        if reply and reply["content"] and asked and not turn.spoke:
            t = asked[-1]
            with w.lock:
                tools.execute(w, turn, "comment", json.dumps({"ref": f"zulip:{t.stream}/{t.topic}",
                                                              "text": reply["content"]}))
        # Only a turn that ran to its end records anything: after a timeout or
        # a halt, the next heartbeat must not be skipped as "seen". What it
        # records is what a heartbeat would see now, after the account's own
        # actions (its notes, messages, tasks): otherwise every turn that did
        # anything would make the next heartbeat look new.
        with w.lock:
            w.store.put(fingerprint_key(turn.agent), request(w, Turn(turn.agent, [Trigger("heartbeat")]))[2])
            self.convos[turn.agent] = Conversation(messages, specs, seen, started, w.now(), w.today())
        return steps, f"{mode}, " + (reply["content"][:200] if reply else "")
