"""The tools agents act through, as OpenAI function-calling specs plus their
implementations. Tools are where the hard rules live: a model can call any
tool it is offered, so each implementation checks what must hold whatever the
model thinks (X's post rules, who may review, that the CEO really spoke)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from . import text as T
from .store import Draft
from .team import Blocked
from .world import World, quote

log = logging.getLogger(__name__)


@dataclass
class Trigger:
    """Why an agent got a turn."""

    kind: str  # human, bot or heartbeat
    sender: str = ""
    stream: str = ""
    topic: str = ""
    content: str = ""
    msg_id: int = 0


@dataclass
class Turn:
    agent: str
    triggers: list[Trigger]
    # Whether the agent said anything in Zulip during this turn.
    spoke: bool = False

    @property
    def human(self) -> bool:
        return any(t.kind == "human" for t in self.triggers)


class ToolError(Exception):
    """A refused call; the message goes back to the model so it can fix it."""


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    run: Callable[[World, Turn, dict], str]
    agents: tuple[str, ...] = ()  # empty: everyone
    human_turn_only: bool = False
    needs_lab: bool = False
    required: list[str] = field(default_factory=list)

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.params, "required": self.required},
            },
        }


def _open_draft(w: World, draft_id) -> Draft:
    d = w.store.draft(int(draft_id))
    if d is None:
        raise ToolError(f"there is no draft #{draft_id}")
    if d.status != "draft":
        raise ToolError(f"draft #{d.id} is {d.status}, not open")
    return d


def fix_mentions(w: World, content: str) -> str:
    """Models often write @writer or @ceo; Zulip only notifies on @**writer**
    and @**<the CEO's name>**."""
    names = "|".join(re.escape(n) for n in w.team.names())
    content = re.sub(rf"(?<![\w*])@({names})\b(?!\*)", r"@**\1**", content, flags=re.IGNORECASE)
    return re.sub(r"(?<![\w*])@(\*\*)?ceo\b(\*\*)?", f"@**{w.team.ceo}**", content, flags=re.IGNORECASE)


def send_message(w: World, turn: Turn, a: dict) -> str:
    stream, topic, content = a["stream"].lstrip("#"), a["topic"], fix_mentions(w, a["content"].strip())
    if not content:
        raise ToolError("empty message")
    try:
        msg_id = w.team.send(turn.agent, stream, topic, content)
    except Blocked as e:
        raise ToolError(f"not sent, a circuit breaker stopped it: {e}")
    turn.spoke = True
    return f"sent to #{stream} > {topic} (message {msg_id})"


def create_draft(w: World, turn: Turn, a: dict) -> str:
    post = T.clean_post(a["text"])
    problems = w.check(post)
    if problems:
        raise ToolError("not saved, the post breaks the rules: " + "; ".join(problems))
    note = fix_mentions(w, a.get("note", "").strip())
    draft_id = w.store.add_draft(w.today(), post, turn.agent, note)
    d = w.store.draft(draft_id)
    msg = f"New draft **#{d.id}** by {turn.agent}, {T.x_length(post)} characters:\n{quote(post)}"
    w.post_about(turn.agent, d, msg + (f"\n{note}" if note else ""), check=False)
    turn.spoke = True
    return f"saved as draft #{d.id} and posted in #{w.cfg.streams.drafts} > {d.topic}"


def revise_draft(w: World, turn: Turn, a: dict) -> str:
    d = _open_draft(w, a["draft_id"])
    post = T.clean_post(a["text"])
    if post == d.text:
        raise ToolError(f"the text is identical to draft #{d.id}'s current version; nothing to revise")
    problems = w.check(post, exclude=d.id)
    if problems:
        raise ToolError("not saved, the post breaks the rules: " + "; ".join(problems))
    n = d.revisions + 1
    note = fix_mentions(w, a.get("note", "").strip())
    w.store.update_draft(d.id, text=post, revisions=n, editor_ok=0, ceo_ok=0, note=note or d.note)
    msg = f"Revision {n} of **#{d.id}** by {turn.agent}, {T.x_length(post)} characters:\n{quote(post)}"
    w.post_about(turn.agent, d, msg + (f"\n{note}" if note else ""), check=False)
    turn.spoke = True
    return f"draft #{d.id} updated; it needs a new review from the editor"


def review_draft(w: World, turn: Turn, a: dict) -> str:
    d = _open_draft(w, a["draft_id"])
    verdict, comments = a["verdict"], fix_mentions(w, a.get("comments", "").strip())
    if verdict == "approve":
        w.store.update_draft(d.id, editor_ok=1, feedback=comments)
        waits = " It now waits for the CEO's approval." if w.cfg.publishing.ceo_approval and not d.ceo_ok else ""
        result = f"draft #{d.id} approved by the editor.{waits}"
    elif verdict == "revise":
        w.store.update_draft(d.id, editor_ok=0, feedback=comments)
        result = f"draft #{d.id} sent back for revision"
    elif verdict == "reject":
        w.store.update_draft(d.id, status="rejected", feedback=comments)
        result = f"draft #{d.id} rejected"
    else:
        raise ToolError("verdict must be approve, revise or reject")
    w.post_about(turn.agent, d, f"Review of **#{d.id}**: **{verdict}**\n{comments}", check=False)
    turn.spoke = True
    return result


def ceo_decision(w: World, turn: Turn, a: dict) -> str:
    d = _open_draft(w, a["draft_id"])
    words = a.get("ceo_words", "").strip()
    if a["decision"] == "approve":
        # The CEO outranks the editor.
        w.store.update_draft(d.id, ceo_ok=1, editor_ok=1)
        what, result = "approved", f"draft #{d.id} approved; the publisher will post it at the next free slot"
    elif a["decision"] == "reject":
        w.store.update_draft(d.id, status="rejected")
        what, result = "rejected", f"draft #{d.id} rejected"
    else:
        raise ToolError("decision must be approve or reject")
    # Echoed by the publisher, not the agent: this is the record of what the
    # model understood, for the CEO to catch a misunderstanding.
    said = f"\n> {words}" if words else ""
    w.post_about(w.cfg.publisher, d, f"Recorded: the CEO **{what}** draft #{d.id} "
                 f"(understood by {turn.agent} from:{said or ' a message'})", check=False)
    return result


def update_plan(w: World, turn: Turn, a: dict) -> str:
    plan = a["text"].strip()
    if not plan:
        raise ToolError("empty plan")
    week = w.week()
    w.store.add_plan(week, plan)
    w.team.send(turn.agent, w.cfg.streams.plan, f"plan {week}", plan, check=False)
    turn.spoke = True
    return f"plan for {week} saved and posted in #{w.cfg.streams.plan}"


def read_topic(w: World, turn: Turn, a: dict) -> str:
    msgs = w.team.history(a["stream"].lstrip("#"), a["topic"], n=30)
    if not msgs:
        return "no messages there"
    return "\n".join(f"{m['sender_full_name']}: {m['content'][:1500]}" for m in msgs)


def read_lab_note(w: World, turn: Turn, a: dict) -> str:
    try:
        return w.lab.read(a["path"], a.get("section", ""), int(a.get("part", 1)))
    except FileNotFoundError as e:
        raise ToolError(str(e))


def search_lab_notes(w: World, turn: Turn, a: dict) -> str:
    return w.lab.search(a["query"])


def remember(w: World, turn: Turn, a: dict) -> str:
    note = a["note"].strip()
    if not note:
        raise ToolError("empty note")
    w.store.add_note(turn.agent, note)
    return "noted; you will see it at the start of your next turns"


S = {"type": "string"}
I = {"type": "integer"}

TOOLS = [
    Tool("send_message",
         "Post a message in a Zulip stream and topic. Mention a teammate as @**name** to wake them up.",
         {"stream": S, "topic": S, "content": S}, send_message, required=["stream", "topic", "content"]),
    Tool("create_draft",
         "Writer and strategist. Save a new X post draft and show it in #drafts. The text must be the exact post. "
         "Use the note to tell the team something about it, e.g. ask @**editor** for a review.",
         {"text": S, "note": S}, create_draft, agents=("writer", "strategist"), required=["text"]),
    Tool("revise_draft",
         "Writer and strategist. Replace the text of an open draft with a new version. "
         "The editor must review it again.",
         {"draft_id": I, "text": S, "note": S}, revise_draft, agents=("writer", "strategist"),
         required=["draft_id", "text"]),
    Tool("review_draft",
         "Editor only. Give your verdict on an open draft: approve (publishable as is), revise (fixable, "
         "say how in comments) or reject (not worth fixing). The review is posted in the draft's topic for you; "
         "mention a teammate in the comments to wake them.",
         {"draft_id": I, "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]}, "comments": S},
         review_draft, agents=("editor",), required=["draft_id", "verdict", "comments"]),
    Tool("ceo_decision",
         "Only in a turn started by the CEO's message. Record a decision the CEO clearly stated about a draft "
         "in that message, e.g. 'post 5 is good' or 'drop 6'. Never use it on your own judgment. "
         "Quote the CEO's words.",
         {"draft_id": I, "decision": {"type": "string", "enum": ["approve", "reject"]}, "ceo_words": S},
         ceo_decision, human_turn_only=True, required=["draft_id", "decision", "ceo_words"]),
    Tool("update_plan",
         "Strategist only. Replace this week's content plan with a new version and post it in #plan.",
         {"text": S}, update_plan, agents=("strategist",), required=["text"]),
    Tool("read_topic",
         "Read the latest 30 messages of a Zulip topic.",
         {"stream": S, "topic": S}, read_topic, required=["stream", "topic"]),
    Tool("read_lab_note",
         "Read a report from the lab notebook (our own inference experiments). Long reports come in parts; "
         "pass a section name to read just that section.",
         {"path": S, "section": S, "part": I}, read_lab_note, needs_lab=True, required=["path"]),
    Tool("search_lab_notes",
         "Find lines in the lab notebook that contain all the given words, e.g. 'decode 160K' or 'rejected SGLang'.",
         {"query": S}, search_lab_notes, needs_lab=True, required=["query"]),
    Tool("remember",
         "Write a short note to yourself: a lesson, a commitment, something to follow up. "
         "You see your notes at the start of every turn.",
         {"note": S}, remember, required=["note"]),
]
BY_NAME = {t.name: t for t in TOOLS}


def offered(lab: bool) -> list[Tool]:
    """The tools in every request, the same for every agent and turn: the
    chat template puts them before everything else, so one fixed list keeps
    the prompt's beginning identical and lets the server reuse its cache.
    Who may use what is checked when a tool runs."""
    return [t for t in TOOLS if lab or not t.needs_lab]


def refusal(tool: Tool, turn: Turn) -> str | None:
    if tool.agents and turn.agent not in tool.agents:
        return f"{tool.name} is for the {' and '.join(tool.agents)}; ask them by mentioning them"
    if tool.human_turn_only and not turn.human:
        return f"{tool.name} only works in a turn started by the CEO's message"
    return None


def available(agent: str, turn: Turn, lab: bool = False) -> list[Tool]:
    """The tools this agent may actually use in this turn."""
    return [t for t in offered(lab) if refusal(t, Turn(agent, turn.triggers)) is None]


def execute(w: World, turn: Turn, name: str, arguments: str) -> str:
    """Run one tool call and return what the model sees as its result."""
    tool = BY_NAME.get(name)
    if tool is None or tool not in offered(w.lab is not None):
        result = f"error: there is no tool {name}"
    elif why := refusal(tool, turn):
        result = f"error: {why}"
    else:
        try:
            args = json.loads(arguments or "{}")
            missing = [k for k in tool.required if k not in args]
            if missing:
                raise ToolError(f"missing arguments: {', '.join(missing)}")
            result = tool.run(w, turn, args)
        except (ToolError, json.JSONDecodeError, ValueError, KeyError) as e:
            result = f"error: {e}"
        except Exception as e:  # noqa: BLE001 - the model sees it, the log keeps the trace
            log.exception("tool %s", name)
            result = f"error: {type(e).__name__}: {e}"
    w.activity(f"**{turn.agent}** `{name}` → {result[:200]}")
    return result
