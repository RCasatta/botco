"""Agent turns: an agent wakes up, reads the situation, and acts through
tools until it has nothing more to do.

Turns run on worker threads. The model is called without holding the world
lock; tool calls and context building hold it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources

import requests

from . import tools
from .llm import LLM, LLMDown
from .store import Draft
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


def system_prompt(w: World) -> str:
    """The same for every agent: the role comes later, in the situation, so
    the server can reuse its cache for this part across agents."""
    team = prompt("team").format(
        drafts=w.cfg.streams.drafts, plan=w.cfg.streams.plan, ops=w.cfg.streams.ops,
        published=w.cfg.streams.published, coordinator=w.cfg.agents.coordinator, ceo=w.team.ceo,
    )
    return f"{prompt('common')}\n\n{team}"


def describe_trigger(t: Trigger) -> str:
    if t.kind == "heartbeat":
        return "- Routine check-in: nobody called you. Look at the situation and do what your role needs, if anything."
    if t.kind == "lab":
        return (f"- The lab notebook changed: {t.content}. New results can be material for posts; "
                "read what changed and decide whether the plan or the writer should use it.")
    who = "the CEO" if t.kind == "human" else t.sender
    return f"- {who} wrote in #{t.stream} > {t.topic}:\n{quote(t.content[:2000])}"


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
    return f"- `{n.path}` ({n.modified:%Y-%m-%d}, {n.size // 1000} KB): {n.title}"


def lab_section(w: World) -> str | None:
    if not (w.lab and w.lab.available()):
        return None
    return ("## Lab notebook (our own experiments on this machine; read with read_lab_note)\n"
            + "\n".join(lab_line(n) for n in w.lab.notes()))


def plan_section(w: World) -> str:
    plan = w.store.latest_plan(w.week())
    if plan:
        return f"## Content plan for {w.week()}\n{plan['text']}"
    last = w.store.latest_plan()
    return (f"## Content plan\nThere is no plan for {w.week()} yet."
            + (f" Last week's plan ({last['week']}):\n{last['text']}" if last else ""))


def published_line(d: Draft) -> str:
    return f"- [{d.published_at[:16]}] {d.text}"


def publishing_section(w: World) -> str:
    cfg = w.cfg
    times = ", ".join(t.strftime("%H:%M") for t in cfg.publishing.times)
    gate = "the editor and the CEO" if cfg.publishing.ceo_approval else "the editor"
    pub = [f"The publisher (a script) posts to X at {times}: each time, the oldest draft approved by {gate}."]
    recent = w.store.published(10)
    if recent:
        pub.append("Recently published, newest first:\n" + "\n".join(published_line(d) for d in recent))
    return "## Publishing\n" + "\n".join(pub)


def followers_line(m) -> str:
    return f"- {m['day']}: {m['followers']}"


def note_line(n) -> str:
    return f"- [{n['created_at'][:10]}] {n['text']}"


def draft_block(w: World, d: Draft) -> str:
    state = f"editor: {'approved' if d.editor_ok else 'not approved'}"
    if w.cfg.publishing.ceo_approval:
        state += f", CEO: {'approved' if d.ceo_ok else 'not approved'}"
    lines = [f"### #{d.id} by {d.author}, version {d.revisions + 1} ({state})\n{quote(d.text)}"]
    if d.note:
        lines.append(f"Author's note: {d.note}")
    if d.feedback:
        lines.append(f"Editor's latest comments: {d.feedback}")
    return "\n".join(lines)


def queue_section(w: World) -> str:
    ready = w.store.ready(w.cfg.publishing.ceo_approval)
    return (f"## Publishing queue\nPublished today: {w.published_on(w.today())}. "
            f"Ready to publish: {', '.join(f'#{d.id}' for d in ready) or 'none'}.")


def awake_section(turn: Turn) -> str:
    awake = "## Why you are awake\n" + "\n".join(describe_trigger(t) for t in turn.triggers)
    if turn.human:
        awake += ("\nA human wrote to you: answer them in the same topic with send_message. "
                  "If they stated a decision about a draft, record it with ceo_decision.")
    return awake


def situation(w: World, turn: Turn, clock: bool = True) -> str:
    """Everything the agent knows at the start of a fresh turn.

    Ordered from what changes least to what changes most, because the server
    reuses its cache only up to the first difference from an earlier
    request: first what every agent shares and rarely changes (lab index,
    plan, published posts), then this agent's role and notes, then what moves
    during the day (chat, drafts, queue), and the reason for the turn and the
    time last."""
    cfg, store = w.cfg, w.store
    parts = []

    # Shared by every agent, changes rarely.
    if lab := lab_section(w):
        parts.append(lab)
    parts.append(plan_section(w))
    parts.append(publishing_section(w))
    metrics = store.metrics(7)
    if metrics:
        parts.append("## Followers\n" + "\n".join(followers_line(m) for m in metrics))

    # This agent.
    parts.append(f"## Your role\n{prompt(turn.agent)}")
    notes = store.notes(turn.agent)
    if notes:
        parts.append("## Your notes\n" + "\n".join(note_line(n) for n in notes))
    if turn.agent == "strategist" and cfg.reference_file and cfg.reference_file.exists():
        parts.append("## Posts by accounts the CEO likes (for tone and topics; never copy)\n"
                     + cfg.reference_file.read_text()[:6000])

    # Moves during the day.
    msgs = chat_window(w)
    if msgs:
        parts.append("## Team chat, oldest first\n" + "\n".join(chat_line(m) for m in msgs))
    blocks = [draft_block(w, d) for d in store.open_drafts()]
    parts.append("## Open drafts\n" + ("\n".join(blocks) if blocks else "None."))
    parts.append(queue_section(w))

    # This turn.
    parts.append(awake_section(turn))
    if clock:
        parts.append(clock_line(w, turn.agent))
    return "\n\n".join(parts)


@dataclass
class Snapshot:
    """What an agent saw at the start of a turn, to tell it later what changed."""

    chat_id: int
    drafts: dict[int, tuple]
    plan_id: int | None
    published: set[int]
    metrics: set[str]
    lab: dict[str, str]
    notes: set[int]


def snapshot(w: World, agent: str, msgs: list[dict]) -> Snapshot:
    plan = w.store.latest_plan(w.week())
    return Snapshot(
        chat_id=max((m["id"] for m in msgs), default=0),
        drafts={d.id: (d.status, d.editor_ok, d.ceo_ok, d.revisions, d.text, d.note, d.feedback)
                for d in w.store.open_drafts()},
        plan_id=plan["id"] if plan else None,
        published={d.id for d in w.store.published(10)},
        metrics={m["day"] for m in w.store.metrics(7)},
        lab={n.path: n.modified.isoformat() for n in w.lab.notes()} if w.lab and w.lab.available() else {},
        notes={n["id"] for n in w.store.notes(agent)},
    )


def delta(w: World, turn: Turn, since: datetime, old: Snapshot, new: Snapshot, msgs: list[dict]) -> str:
    """The user message that continues a conversation: what changed since
    the agent's last turn, then the queue, the reason and the time, as at the
    end of a fresh situation."""
    store, changes = w.store, []
    chat = [chat_line(m) for m in msgs if m["id"] > old.chat_id]
    if chat:
        changes.append("### New chat messages\n" + "\n".join(chat))
    drafts = [draft_block(w, store.draft(i)) for i, state in new.drafts.items() if old.drafts.get(i) != state]
    drafts += [f"#{i} is no longer open: it is {store.draft(i).status}." for i in old.drafts if i not in new.drafts]
    if drafts:
        changes.append("### Drafts, new or changed\n" + "\n".join(drafts))
    if new.plan_id != old.plan_id:
        changes.append("### The plan changed\n" + plan_section(w).split("\n", 1)[1])
    published = [published_line(d) for d in store.published(10) if d.id not in old.published]
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
    return "\n\n".join([head, body, queue_section(w), awake_section(turn), clock_line(w, turn.agent)])


def clock_line(w: World, agent: str) -> str:
    """The only part of the prompt that changes by itself, last on purpose."""
    return (f"It is {w.now():%A %Y-%m-%d %H:%M} ({w.cfg.timezone}). You are the {agent}. "
            "Now act. Use tools; when you have nothing more to do, reply with a one-line summary.")


def request(w: World, turn: Turn) -> tuple[list[dict], list[dict], str]:
    """The messages and tools of a turn's first model call, and a fingerprint
    of everything in them except the clock line: equal fingerprints mean the
    agent would see exactly what it saw before. Call it holding w.lock."""
    system, body = system_prompt(w), situation(w, turn, clock=False)
    clock = clock_line(w, turn.agent)
    specs = [t.spec() for t in tools.offered(w.lab is not None)]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": f"{body}\n\n{clock}"}]
    data = json.dumps([system, body, [t["function"]["name"] for t in specs]])
    return messages, specs, hashlib.sha256(data.encode()).hexdigest()


def fingerprint_key(agent: str) -> str:
    return f"fingerprint:{agent}"


@dataclass
class Conversation:
    """An agent's last completed turn, kept so a turn soon after can continue
    it: the server then finds the whole earlier conversation in its cache and
    only reads the update."""

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
    def __init__(self, world: World, llm: LLM, inbox: queue.Queue):
        self.w = world
        self.llm = llm
        self.inbox = inbox
        self.turns: queue.PriorityQueue = queue.PriorityQueue()
        self.seq = itertools.count()
        self.down = False
        self._down_lock = threading.Lock()
        # Per agent; only touched by the agent's own turn, under w.lock.
        self.convos: dict[str, Conversation] = {}

    def can_continue(self, convo: Conversation | None, specs: list[dict]) -> bool:
        a, w = self.w.cfg.agents, self.w
        return (convo is not None and convo.day == w.today() and convo.specs == specs
                and w.now() - convo.ended_at <= timedelta(minutes=a.continue_minutes)
                and convo.chars() < a.continue_max_chars)

    def start(self) -> None:
        for i in range(self.w.cfg.llm.concurrency):
            threading.Thread(target=self._loop, name=f"agent-{i}", daemon=True).start()

    def submit(self, turn: Turn) -> None:
        self.turns.put((0 if turn.human else 1, next(self.seq), turn))

    def _set_down(self, down: bool, why: str = "") -> None:
        with self._down_lock:
            if self.down == down:
                return
            self.down = down
        self.inbox.put(Notice(f":warning: Inference server unreachable ({why}). Agents wait for it."
                              if down else ":check: Inference server is back."))

    def _wait_healthy(self) -> None:
        delay = 15
        while not self.llm.healthy():
            self._set_down(True, "health check failed")
            time.sleep(delay)
            delay = min(delay * 2, 300)
        self._set_down(False)

    def _loop(self) -> None:
        while True:
            _, _, turn = self.turns.get()
            while self.w.halted.is_set():
                time.sleep(5)
            steps, outcome = 0, "error"
            try:
                steps, outcome = self.run_turn(turn)
            except Exception as e:  # noqa: BLE001 - reported to #ops
                log.exception("turn of %s", turn.agent)
                self.inbox.put(Notice(f":warning: {turn.agent}'s turn failed: `{type(e).__name__}: {e}`"))
            finally:
                self.inbox.put(TurnDone(turn.agent, steps, outcome))

    def run_turn(self, turn: Turn) -> tuple[int, str]:
        w, persona = self.w, self.w.cfg.personas[turn.agent]
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
        while steps < w.cfg.agents.max_steps:
            if w.halted.is_set():
                return steps, "halted"
            self._wait_healthy()
            try:
                reply = self.llm.chat(persona, messages, specs)
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
        # A human asked something and the agent answered in plain text
        # instead of with send_message: deliver the text where they asked.
        asked = [t for t in turn.triggers if t.kind == "human"]
        if reply and reply["content"] and asked and not turn.spoke:
            t = asked[-1]
            with w.lock:
                tools.execute(w, turn, "send_message", json.dumps({"stream": t.stream, "topic": t.topic, "content": reply["content"]}))
        # Only a turn that ran to its end records anything: after a timeout or
        # a halt, the next heartbeat must not be skipped as "seen". What it
        # records is what a heartbeat would see now, after the agent's own
        # actions (its notes, messages, drafts): otherwise every turn that did
        # anything would make the next heartbeat look new.
        with w.lock:
            w.store.put(fingerprint_key(turn.agent), request(w, Turn(turn.agent, [Trigger("heartbeat")]))[2])
            self.convos[turn.agent] = Conversation(messages, specs, seen, started, w.now(), w.today())
        return steps, f"{mode}, " + (reply["content"][:200] if reply else "")
