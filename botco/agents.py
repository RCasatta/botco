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
from importlib import resources

import requests

from . import tools
from .llm import LLM, LLMDown
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


def chat_window(w: World) -> list[dict]:
    """Today's messages, so the section only grows during the day and its
    beginning stays reusable; at least the latest 20, at most 80."""
    cfg = w.cfg
    msgs = [m for m in w.team.history(n=150)
            if isinstance(m.get("display_recipient"), str)
            and not (m["display_recipient"] == cfg.streams.ops and m.get("subject") == "activity")]
    midnight = w.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [m for m in msgs if m["timestamp"] >= midnight]
    return (today if len(today) >= 20 else msgs[-20:])[-80:]


def situation(w: World, turn: Turn, clock: bool = True) -> str:
    """Everything the agent knows at the start of its turn.

    Ordered from what changes least to what changes most, because the server
    reuses its cache only up to the first difference from an earlier
    request: first what every agent shares and rarely changes (lab index,
    plan, published posts), then this agent's role and notes, then what moves
    during the day (chat, drafts, queue), and the reason for the turn and the
    time last."""
    cfg, store = w.cfg, w.store
    need_ceo = cfg.publishing.ceo_approval
    parts = []

    # Shared by every agent, changes rarely.
    if w.lab and w.lab.available():
        parts.append("## Lab notebook (our own experiments on this machine; read with read_lab_note)\n" + "\n".join(
            f"- `{n.path}` ({n.modified:%Y-%m-%d}, {n.size // 1000} KB): {n.title}" for n in w.lab.notes()))

    plan = store.latest_plan(w.week())
    if plan:
        parts.append(f"## Content plan for {w.week()}\n{plan['text']}")
    else:
        last = store.latest_plan()
        parts.append(f"## Content plan\nThere is no plan for {w.week()} yet."
                     + (f" Last week's plan ({last['week']}):\n{last['text']}" if last else ""))

    times = ", ".join(t.strftime("%H:%M") for t in cfg.publishing.times)
    gate = "the editor and the CEO" if need_ceo else "the editor"
    pub = [f"The publisher (a script) posts to X at {times}: each time, the oldest draft approved by {gate}."]
    recent = store.published(10)
    if recent:
        pub.append("Recently published, newest first:\n" +
                   "\n".join(f"- [{d.published_at[:16]}] {d.text}" for d in recent))
    parts.append("## Publishing\n" + "\n".join(pub))

    metrics = store.metrics(7)
    if metrics:
        parts.append("## Followers\n" + "\n".join(f"- {m['day']}: {m['followers']}" for m in metrics))

    # This agent.
    parts.append(f"## Your role\n{prompt(turn.agent)}")
    notes = store.notes(turn.agent)
    if notes:
        parts.append("## Your notes\n" + "\n".join(f"- [{n['created_at'][:10]}] {n['text']}" for n in notes))
    if turn.agent == "strategist" and cfg.reference_file and cfg.reference_file.exists():
        parts.append("## Posts by accounts the CEO likes (for tone and topics; never copy)\n"
                     + cfg.reference_file.read_text()[:6000])

    # Moves during the day.
    msgs = chat_window(w)
    if msgs:
        parts.append("## Team chat, oldest first\n" + "\n".join(
            f"[{time.strftime('%a %H:%M', time.localtime(m['timestamp']))}] "
            f"#{m['display_recipient']} > {m['subject']} | {m['sender_full_name']}: {m['content'][:600]}"
            for m in msgs))

    lines = []
    for d in store.open_drafts():
        state = f"editor: {'approved' if d.editor_ok else 'not approved'}"
        if need_ceo:
            state += f", CEO: {'approved' if d.ceo_ok else 'not approved'}"
        lines.append(f"### #{d.id} by {d.author}, version {d.revisions + 1} ({state})\n{quote(d.text)}")
        if d.note:
            lines.append(f"Author's note: {d.note}")
        if d.feedback:
            lines.append(f"Editor's latest comments: {d.feedback}")
    parts.append("## Open drafts\n" + ("\n".join(lines) if lines else "None."))

    ready = store.ready(need_ceo)
    parts.append(f"## Publishing queue\nPublished today: {w.published_on(w.today())}. "
                 f"Ready to publish: {', '.join(f'#{d.id}' for d in ready) or 'none'}.")

    # This turn.
    awake = "## Why you are awake\n" + "\n".join(describe_trigger(t) for t in turn.triggers)
    if turn.human:
        awake += ("\nA human wrote to you: answer them in the same topic with send_message. "
                  "If they stated a decision about a draft, record it with ceo_decision.")
    parts.append(awake)
    if clock:
        parts.append(clock_line(w, turn.agent))
    return "\n\n".join(parts)


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


class Runner:
    def __init__(self, world: World, llm: LLM, inbox: queue.Queue):
        self.w = world
        self.llm = llm
        self.inbox = inbox
        self.turns: queue.PriorityQueue = queue.PriorityQueue()
        self.seq = itertools.count()
        self.down = False
        self._down_lock = threading.Lock()

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
            messages, specs, fingerprint = request(w, turn)
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
        # Only a turn that ran to its end records what it saw: after a timeout
        # or a halt, the next heartbeat must not be skipped as "seen".
        with w.lock:
            w.store.put(fingerprint_key(turn.agent), fingerprint)
        return steps, (reply["content"][:200] if reply else "")
