"""Agent turns: an agent wakes up, reads the situation, and acts through
tools until it has nothing more to do.

Turns run on worker threads. The model is called without holding the world
lock; tool calls and context building hold it.
"""

from __future__ import annotations

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


def system_prompt(w: World, agent: str) -> str:
    team = prompt("team").format(
        drafts=w.cfg.streams.drafts, plan=w.cfg.streams.plan, ops=w.cfg.streams.ops,
        published=w.cfg.streams.published, coordinator=w.cfg.agents.coordinator, ceo=w.team.ceo,
    )
    return f"{prompt('common')}\n\n{team}\n\n{prompt(agent)}"


def describe_trigger(t: Trigger) -> str:
    if t.kind == "heartbeat":
        return "- Routine check-in: nobody called you. Look at the situation and do what your role needs, if anything."
    if t.kind == "lab":
        return (f"- The lab notebook changed: {t.content}. New results can be material for posts; "
                "read what changed and decide whether the plan or the writer should use it.")
    who = "the CEO" if t.kind == "human" else t.sender
    return f"- {who} wrote in #{t.stream} > {t.topic}:\n{quote(t.content[:2000])}"


def situation(w: World, turn: Turn) -> str:
    """Everything the agent knows at the start of its turn."""
    cfg, store, now = w.cfg, w.store, w.now()
    parts = [f"It is {now:%A %Y-%m-%d %H:%M} ({cfg.timezone}). You are the {turn.agent}."]

    parts.append("## Why you are awake\n" + "\n".join(describe_trigger(t) for t in turn.triggers))
    if turn.human:
        parts[-1] += ("\nA human wrote to you: answer them in the same topic with send_message. "
                      "If they stated a decision about a draft, record it with ceo_decision.")

    plan = store.latest_plan(w.week())
    if plan:
        parts.append(f"## Content plan for {w.week()}\n{plan['text']}")
    else:
        last = store.latest_plan()
        parts.append(f"## Content plan\nThere is no plan for {w.week()} yet."
                     + (f" Last week's plan ({last['week']}):\n{last['text']}" if last else ""))

    drafts = store.open_drafts()
    need_ceo = cfg.publishing.ceo_approval
    lines = []
    for d in drafts:
        state = f"editor: {'approved' if d.editor_ok else 'not approved'}"
        if need_ceo:
            state += f", CEO: {'approved' if d.ceo_ok else 'not approved'}"
        lines.append(f"### #{d.id} by {d.author}, version {d.revisions + 1} ({state})\n{quote(d.text)}")
        if d.note:
            lines.append(f"Author's note: {d.note}")
        if d.feedback:
            lines.append(f"Editor's latest comments: {d.feedback}")
    parts.append("## Open drafts\n" + ("\n".join(lines) if lines else "None."))

    times = ", ".join(t.strftime("%H:%M") for t in cfg.publishing.times)
    ready = store.ready(need_ceo)
    gate = "the editor and the CEO" if need_ceo else "the editor"
    pub = [f"The publisher (a script) posts to X at {times}: each time, the oldest draft approved by {gate}. "
           f"Published today: {w.published_on(w.today())}. "
           f"Ready to publish: {', '.join(f'#{d.id}' for d in ready) or 'none'}."]
    recent = store.published(10)
    if recent:
        pub.append("Recently published, newest first:\n" +
                   "\n".join(f"- [{d.published_at[:16]}] {d.text}" for d in recent))
    parts.append("## Publishing\n" + "\n".join(pub))

    metrics = store.metrics(7)
    if metrics:
        parts.append("## Followers\n" + "\n".join(f"- {m['day']}: {m['followers']}" for m in metrics))

    notes = store.notes(turn.agent)
    if notes:
        parts.append("## Your notes\n" + "\n".join(f"- [{n['created_at'][:10]}] {n['text']}" for n in notes))

    if w.lab and w.lab.available():
        notes = w.lab.notes()
        parts.append("## Lab notebook (our own experiments on this machine; read with read_lab_note)\n" + "\n".join(
            f"- `{n.path}` ({n.modified:%Y-%m-%d}, {n.size // 1000} KB): {n.title}" for n in notes))

    if turn.agent == "strategist" and cfg.reference_file and cfg.reference_file.exists():
        parts.append("## Posts by accounts the CEO likes (for tone and topics; never copy)\n"
                     + cfg.reference_file.read_text()[:6000])

    msgs = [m for m in w.team.history(n=50)
            if not (m.get("display_recipient") == cfg.streams.ops and m.get("subject") == "activity")][-35:]
    if msgs:
        parts.append("## Latest messages in the team chat, oldest first\n" + "\n".join(
            f"[{time.strftime('%a %H:%M', time.localtime(m['timestamp']))}] "
            f"#{m['display_recipient']} > {m['subject']} | {m['sender_full_name']}: {m['content'][:600]}"
            for m in msgs if isinstance(m.get("display_recipient"), str)))

    parts.append("Now act. Use tools; when you have nothing more to do, reply with a one-line summary.")
    return "\n\n".join(parts)


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
            messages = [
                {"role": "system", "content": system_prompt(w, turn.agent)},
                {"role": "user", "content": situation(w, turn)},
            ]
            specs = [t.spec() for t in tools.available(turn.agent, turn, w.lab is not None)]
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
        return steps, (reply["content"][:200] if reply else "")
