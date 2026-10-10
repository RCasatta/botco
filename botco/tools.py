"""The five tools turn accounts act through, addressed by refs, as
OpenAI function-calling specs plus their implementations.

Every account is offered the same five tools, so the beginning of every
request stays identical and the server can reuse its cache; what an
account may actually do is decided by the tracker's policy when a tool
runs, and a refusal goes back to the model with its reason."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .external import ExternalError, summary
from .policy import Ref, parse_ref
from .store import External, Task
from .team import Blocked
from .tracker import Refused, Tracker
from .world import World, quote

log = logging.getLogger(__name__)


@dataclass
class Trigger:
    """Why an account got a turn."""

    kind: str  # human, bot, heartbeat, lab or task
    sender: str = ""
    stream: str = ""
    topic: str = ""
    content: str = ""
    msg_id: int = 0
    # The sender's account, if it has one.
    account: str | None = None
    # The task that changed, for task triggers.
    task: int = 0


@dataclass
class Turn:
    agent: str
    triggers: list[Trigger]
    # Whether the account said anything in Zulip during this turn.
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


def fix_mentions(w: World, content: str) -> str:
    """Models often write @writer or @owner; Zulip only notifies on
    @**writer** and @**<the person's full name>**."""
    names = "|".join(re.escape(n) for n in w.team.names())
    if names:
        content = re.sub(rf"(?<![\w*])@({names})\b(?!\*)", r"@**\1**", content, flags=re.IGNORECASE)
    for account in w.cfg.accounts:
        mention = w.team.mention(account)
        if mention not in (account, f"@**{account}**"):
            content = re.sub(rf"(?<![\w*])@(\*\*)?{re.escape(account)}\b(\*\*)?", mention, content, flags=re.IGNORECASE)
    owners = [a for a in w.policy.holders("owner")] if "owner" in w.cfg.roles else []
    if owners:
        content = re.sub(r"(?<![\w*])@(\*\*)?ceo\b(\*\*)?", w.team.mention(owners[0]), content, flags=re.IGNORECASE)
    return content


def _ref(a: dict, key: str = "ref") -> Ref:
    try:
        return parse_ref(str(a[key]))
    except ValueError as e:
        raise ToolError(str(e)) from None


def _task(w: World, ref: Ref) -> Task:
    """The local task a ref names: #42, or the Zulip topic of a task."""
    if ref.kind == "zulip":
        t = w.store.task_by_topic(ref.stream, ref.topic)
    elif ref.kind == "task":
        t = w.store.task(ref.task)
    else:
        t = None
    if t is None:
        raise ToolError(f"there is no task {ref.text}")
    return t


def _list(v) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s) for s in v]


def creation_depth(w: World, turn: Turn, refs: list[str]) -> tuple[int, int | None]:
    """Depth 0 for work a person asked for or an external issue calls for;
    one more than the task being handled otherwise."""
    if any(parse_ref(r).external for r in refs) or turn.human:
        return 0, None
    handling = [w.store.task(t.task) for t in turn.triggers if t.kind == "task" and t.task]
    handling = [t for t in handling if t is not None]
    if handling:
        parent = max(handling, key=lambda t: t.depth)
        return parent.depth + 1, parent.id
    return 1, None


def external(w: World, ref: Ref) -> str:
    """An external issue, from the cache when it is fresh."""
    cached = w.store.external(ref.text)
    fresh = cached and cached.fetched_at and \
        w.now() - datetime.fromisoformat(cached.fetched_at) < timedelta(minutes=w.cfg.sources.poll_minutes)
    if not fresh:
        try:
            got = w.issues.fetch(ref)
            w.store.put_external(got)
            cached = w.store.external(ref.text)
        except ExternalError as e:
            if not cached:
                raise ToolError(f"cannot read {ref.text}: {e}") from None
    return summary(cached, 6000)


def x_post(w: World, ref: Ref) -> str:
    """A post on X. Each read is paid, so it is read once and kept."""
    cached = w.store.external(ref.text)
    if cached is None:
        try:
            p = w.x.read_post(str(ref.number))
        except Exception as e:  # noqa: BLE001 - the model sees why
            raise ToolError(f"cannot read {ref.text}: {e}") from None
        w.store.put_external(External(ref.text, f"@{p['username']} ({p['name']})", "posted", p["url"], p["text"],
                                      [], p["created_at"]))
        cached = w.store.external(ref.text)
    return f"{ref.text}: {cached.title} on {cached.updated_at[:16]} ({cached.url}):\n{quote(cached.body)}"


# the tools

def find(w: World, turn: Turn, a: dict) -> str:
    query = a["query"].strip()
    words = query.split()
    if not words:
        raise ToolError("empty query")
    out = []
    tasks = w.store.search(words, 15)
    if tasks:
        out.append("Tasks:\n" + "\n".join(f"- #{t.id} [{t.status if t.open else t.resolution}] {t.kind} by "
                                           f"{t.author}: {t.title}" for t in tasks))
    if w.lab and w.lab.available():
        hits = w.lab.search(query, limit=15)
        if hits not in ("no matches", "empty query"):
            out.append("Lab notebook:\n" + "\n".join(f"- lab:{h}" for h in hits.splitlines()))
    issues = w.issues.search(query)
    if issues:
        out.append("External issues:\n" + "\n".join(f"- {r} [{s}] {t}" if r else f"- {t}" for r, s, t in issues))
    return "\n\n".join(out) or "nothing found"


def read(w: World, turn: Turn, a: dict) -> str:
    ref = _ref(a)
    if ref.kind == "task":
        from .agents import task_block  # agents imports tools
        t = _task(w, ref)
        lines = [task_block(w, t).replace("### ", "", 1)]
        if not t.open:
            lines.insert(1, f"Closed: {t.resolution} ({(t.closed_at or '')[:16]})" + (f", X id {t.x_id}" if t.x_id else ""))
        if t.version > 1:
            lines.append(f"{t.version} versions; earlier reviews: {len(w.store.reviews(t.id))} in all")
        comments = w.store.comments(t.id, 20)
        if len(comments) > 5:
            lines.append("All recent comments:\n" + "\n".join(f"- {c.author} ({c.at[:16]}): {c.text[:1500]}"
                                                               for c in comments))
        for r in t.refs:
            other = parse_ref(r)
            if other.external:
                try:
                    lines.append("Referenced: " + external(w, other))
                except ToolError as e:
                    lines.append(f"Referenced {r}: {e}")
        lines.append(f"Topic: zulip:{t.stream}/{t.topic}")
        return "\n\n".join(lines)
    if ref.external:
        text = external(w, ref)
        local = next((t for t in w.store.open_tasks() if ref.text in t.refs), None)
        return text + (f"\n\nLocal task: #{local.id}" if local else "\n\nNo open local task refers to it.")
    if ref.kind == "x":
        text = x_post(w, ref)
        local = [t for t in w.store.open_tasks() if ref.text in t.refs]
        return text + "".join(f"\nOpen task about it: #{t.id} {t.kind}: {t.title}" for t in local)
    if ref.kind == "lab":
        if w.lab is None:
            raise ToolError("there is no lab notebook")
        section, part = ref.section, 1
        if section.isdigit():
            section, part = "", int(section)
        try:
            return w.lab.read(ref.path, section, part)
        except FileNotFoundError as e:
            raise ToolError(str(e)) from None
    msgs = w.team.history(ref.stream, ref.topic, n=30)
    task = w.store.task_by_topic(ref.stream, ref.topic)
    head = f"This is the topic of #{task.id}.\n" if task else ""
    if not msgs:
        return head + "no messages there"
    return head + "\n".join(f"{m['sender_full_name']}: {m['content'][:1500]}" for m in msgs)


def write(w: World, turn: Turn, a: dict) -> str:
    tracker = Tracker(w)
    fields = dict(title=a.get("title"), body=a.get("body"), note=fix_mentions(w, a["note"]) if a.get("note") else None,
                  labels=_list(a.get("labels")), assignee=a.get("assignee"), refs=_list(a.get("refs")))
    state = a.get("state")
    if not a.get("ref"):
        if state not in (None, "", "open"):
            raise ToolError("a new task is open; leave out state")
        refs = fields["refs"] or []
        try:
            depth, parent = creation_depth(w, turn, refs)
        except ValueError as e:
            raise ToolError(str(e)) from None
        t, new = tracker.create(turn.agent, a.get("kind") or "task", fields["title"] or "", fields["body"] or "",
                                fields["note"] or "", fields["labels"], fields["assignee"], refs, depth, parent)
        if not new:
            return f"not created: #{t.id} is already the open task for {t.external_id}; work there"
        turn.spoke = True
        waiting = w.policy.waits_on(w.store, t)
        return (f"created #{t.id} in zulip:{t.stream}/{t.topic}; it waits on "
                f"{', '.join(waiting.accounts) or 'nobody'} ({waiting.why})")
    ref = _ref(a)
    if ref.read_only:
        raise ToolError(f"{ref.text} is read-only")
    t = _task(w, ref)
    if a.get("kind") and a["kind"] != t.kind:
        raise ToolError("the kind of a task cannot change")
    done = []
    if state == "open" and not t.open:
        tracker.reopen(turn.agent, t)
        t, done = w.store.task(t.id), ["reopened"]
    if any(v is not None for v in fields.values()):
        done += tracker.edit(turn.agent, t, **fields)
        t = w.store.task(t.id)
        turn.spoke = True
    if state not in (None, "", "open"):
        tracker.close(turn.agent, t, state)
        done.append(f"closed as {state}")
        turn.spoke = True
    if not done:
        raise ToolError("nothing to change: give a field to set or a state")
    t = w.store.task(t.id)
    waiting = w.policy.waits_on(w.store, t)
    return f"#{t.id}: {'; '.join(done)}. It waits on {', '.join(waiting.accounts) or 'nobody'} ({waiting.why})"


def comment(w: World, turn: Turn, a: dict) -> str:
    ref = _ref(a)
    text = fix_mentions(w, (a.get("text") or "").strip())
    verdict, behalf = a.get("verdict") or None, a.get("on_behalf_of")
    if ref.read_only:
        raise ToolError(f"{ref.text} is read-only")
    tracker = Tracker(w)
    if ref.kind == "zulip" and w.store.task_by_topic(ref.stream, ref.topic) is None:
        if verdict:
            raise ToolError("a verdict is about a task: comment on its ref")
        if not text:
            raise ToolError("empty message")
        try:
            msg_id = w.team.send(turn.agent, ref.stream, ref.topic, text)
        except Blocked as e:
            raise ToolError(f"not sent, a circuit breaker stopped it: {e}") from None
        turn.spoke = True
        return f"sent to #{ref.stream} > {ref.topic} (message {msg_id})"
    t = _task(w, ref)
    try:
        if not verdict:
            if behalf:
                raise ToolError("on_behalf_of only goes with a verdict")
            tracker.comment(turn.agent, t, text)
            turn.spoke = True
            return f"commented on #{t.id}"
        if not behalf:
            result = tracker.review(turn.agent, t, verdict, text)
            turn.spoke = True
            return result
        # A person's verdict, stated in words: only from a message that woke
        # this turn, recorded under its author with their roles.
        said = next((tr for tr in turn.triggers if tr.kind == "human" and tr.msg_id == int(behalf)), None)
        if said is None:
            woke = [str(tr.msg_id) for tr in turn.triggers if tr.kind == "human"]
            raise ToolError("on_behalf_of must be the id of a person's message that woke you"
                            + (f": {', '.join(woke)}" if woke else "; no person's message woke you"))
        if not said.account:
            raise ToolError(f"{said.sender} has no account here, so their verdict counts for nothing")
        result = tracker.review(said.account, t, verdict, text, recorded_by=turn.agent, msg_id=said.msg_id, post=False)
        # Echoed by the publisher, not the agent: this is the record of what
        # the model understood, for the person to catch a misunderstanding.
        words = said.content.strip()[:300]
        tracker.post(w.cfg.publisher, t, f"Recorded: {said.account} **{verdict}** #{t.id} (understood by "
                     f"{turn.agent} from:\n{quote(words)})" + (f"\n{text}" if text else ""))
        turn.spoke = True
        return result
    except Blocked as e:
        raise ToolError(f"not sent, a circuit breaker stopped it: {e}") from None


def remember(w: World, turn: Turn, a: dict) -> str:
    note = a["note"].strip()
    if not note:
        raise ToolError("empty note")
    w.store.add_note(turn.agent, note)
    return "noted; you will see it at the start of your next turns"


S = {"type": "string"}
I = {"type": "integer"}
LIST = {"type": "array", "items": S}

REFS = ("Refs: #42 (a task), gh:owner/repo#12 and gl:group/proj#7 (external issues, read-only), "
        "x:123 or a post's x.com link (a post on X, read-only), "
        "lab:FILE.md#Section (lab notebook, read-only), zulip:stream/topic (a chat topic).")

TOOLS = [
    Tool("find",
         "Search tasks, the lab notebook and external issues for all the given words, e.g. 'KV cache' or "
         "'decode 160K'. Returns refs to read.",
         {"query": S}, find, required=["query"]),
    Tool("read",
         "Read what a ref points to: a task with its reviews, comments, who it waits on and its referenced issues; "
         "an external issue; a lab report or one section of it (lab:FILE.md#2 reads part 2 of a long one); the "
         "latest 30 messages of a Zulip topic. " + REFS,
         {"ref": S}, read, required=["ref"]),
    Tool("write",
         "Create a task (leave out ref) or change one (give its ref). kind is one of the kinds above, 'task' by "
         "default. For a post, body is the exact text to publish. Editing the body of a task makes earlier "
         "approvals stop counting. state 'done' or 'dropped' closes it, 'open' reopens it. note says something "
         "about the change to the team (sources, intent). The tracker refuses what your role may not do.",
         {"ref": S, "kind": S, "title": S, "body": S, "note": S, "labels": LIST, "assignee": S, "refs": LIST,
          "state": {"type": "string", "enum": ["open", "done", "dropped"]}}, write),
    Tool("comment",
         "Comment on a task (shown in its topic) or post in a Zulip topic (zulip:stream/topic). Mention someone "
         "as @**name** to call them. With a verdict (approve, revise, reject) on a task it is also your review; "
         "the review is posted for you. When a person stated a verdict in words, record it with the verdict and "
         "on_behalf_of set to the id of their message: it counts as theirs.",
         {"ref": S, "text": S, "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
          "on_behalf_of": I}, comment, required=["ref", "text"]),
    Tool("remember",
         "Write a short note to yourself: a lesson, a commitment, something to follow up. "
         "You see your notes at the start of every turn.",
         {"note": S}, remember, required=["note"]),
]
BY_NAME = {t.name: t for t in TOOLS}


def execute(w: World, turn: Turn, name: str, arguments: str) -> str:
    """Run one tool call and return what the model sees as its result."""
    tool = BY_NAME.get(name)
    if tool is None:
        result = f"error: there is no tool {name}"
    else:
        try:
            args = json.loads(arguments or "{}")
            missing = [k for k in tool.required if k not in args]
            if missing:
                raise ToolError(f"missing arguments: {', '.join(missing)}")
            result = tool.run(w, turn, args)
        except (ToolError, Refused, json.JSONDecodeError, ValueError, KeyError) as e:
            result = f"error: {e}"
        except Exception as e:  # noqa: BLE001 - the model sees it, the log keeps the trace
            log.exception("tool %s", name)
            result = f"error: {type(e).__name__}: {e}"
    w.activity(f"**{turn.agent}** `{name}` → {result[:200]}")
    return result
