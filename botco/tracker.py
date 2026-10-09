"""Every write to the tracker goes through here, whoever makes it: a tool
call, a person's command or reaction, a schedule, a session's summary, a
sink. This is where the policy and the guardrails are enforced, whatever a
model intends, and where each change is shown in the task's Zulip topic and
reported to the dispatcher."""

from __future__ import annotations

from . import text as T
from .policy import parse_ref
from .store import Task, title_of
from .world import World, quote

RESOLUTIONS = ("done", "dropped")
VERDICTS = ("approve", "revise", "reject")


class Refused(Exception):
    """A write the policy does not allow; the message says why."""


class Tracker:
    def __init__(self, world: World):
        self.w = world

    # helpers

    def post(self, actor: str, t: Task, content: str, check: bool = False) -> int:
        """Show something in the task's topic, in the actor's voice, and
        remember the message is about the task, so reactions and replies
        there can be traced back."""
        bot, prefix = self.w.voice(actor)
        msg_id = self.w.team.send(bot, t.stream, t.topic, prefix + content, check=check)
        self.w.store.link_message(msg_id, t.id)
        return msg_id

    def validate(self, kind: str, body: str, exclude: int | None = None) -> list[str]:
        k = self.w.policy.kind(kind)
        if k.validate is None:
            return []
        if k.validate == "x_post":
            previous = [p.body for p in self.w.store.published(50)]
            previous += [t.body for t in self.w.store.open_tasks(kind) if t.id != exclude]
            return T.check_post(body, self.w.cfg.x.max_chars, previous)
        raise ValueError(f"unknown validator {k.validate!r}")

    def _clean(self, kind: str, body: str) -> str:
        return T.clean_post(body) if self.w.policy.kind(kind).validate == "x_post" else body.strip()

    def _account(self, name: str | None) -> str | None:
        if name in (None, ""):
            return None
        name = name.lstrip("@*").rstrip("*")
        if name not in self.w.cfg.accounts:
            raise Refused(f"there is no account {name!r}; accounts: {', '.join(self.w.cfg.accounts)}")
        return name

    def _refs(self, refs) -> list[str]:
        out = []
        for r in refs or []:
            ref = parse_ref(str(r))
            if ref.kind == "task" and self.w.store.task(ref.task) is None:
                raise Refused(f"there is no task {ref.text}")
            out.append(ref.text)
        return out

    def _open_cap(self, assignee: str | None) -> None:
        if assignee is None:
            return
        cap = self.w.cfg.dispatcher.max_open_per_assignee
        n = sum(1 for t in self.w.store.open_tasks() if t.assignee == assignee)
        if n >= cap:
            raise Refused(f"{assignee} already has {n} open tasks, the limit is {cap}")

    def find_open(self, *, external_id: str | None = None, schedule: str | None = None) -> Task | None:
        for t in self.w.store.open_tasks():
            if (external_id and t.external_id == external_id) or (schedule and t.schedule == schedule):
                return t
        return None

    # writes

    def create(self, actor: str, kind: str, title: str = "", body: str = "", note: str = "",
               labels: list[str] | None = None, assignee: str | None = None, refs: list[str] | None = None,
               depth: int = 0, parent: int | None = None, schedule: str | None = None,
               system: bool = False) -> tuple[Task, bool]:
        """The new task, or the open one for the same external issue (and
        False). `system` is for schedules, which create as a person would
        and hold no role."""
        w, policy = self.w, self.w.policy
        try:
            k = policy.kind(kind)
            refs = self._refs(refs)
        except ValueError as e:
            raise Refused(str(e)) from None
        if not system and not policy.may(actor, "create", k):
            raise Refused(f"{actor} may not create {k.name} tasks; that takes the role {' or '.join(k.create or [])}")
        external_id = next((r for r in refs if parse_ref(r).external), None)
        if external_id and (existing := self.find_open(external_id=external_id)):
            return existing, False
        if depth > w.cfg.dispatcher.max_depth:
            raise Refused(f"not created: it would have depth {depth}, the limit is {w.cfg.dispatcher.max_depth}. "
                          "Tasks created while handling a task an agent created cannot create more tasks; "
                          "comment on the task instead, or ask a person")
        engine = w.cfg.accounts[actor].engine if actor in w.cfg.accounts else None
        if engine is not None:
            made = w.store.created_by(actor, w.midnight())
            if made >= w.cfg.dispatcher.max_new_tasks_per_day:
                raise Refused(f"{actor} already created {made} tasks today, the limit is "
                              f"{w.cfg.dispatcher.max_new_tasks_per_day}")
        assignee = self._account(assignee) or (actor if actor in w.cfg.accounts else None)
        self._open_cap(assignee)
        body = self._clean(kind, body)
        problems = self.validate(kind, body)
        if problems:
            raise Refused("not saved, the body breaks the rules: " + "; ".join(problems))
        title = (title or "").strip() or (title_of(body) if body else "")
        if not title:
            raise Refused("a task needs a title or a body")
        replaced = w.store.open_tasks(kind) if k.one_open else []
        task_id = w.store.add_task(kind, title, body, note.strip(), actor, assignee, k.stream, list(labels or []),
                                   refs, depth, parent, external_id, schedule)
        t = w.store.task(task_id)
        lines = [f"New {kind} **#{t.id}** by {actor}: **{title}**"]
        details = [f"assigned to {assignee}" if assignee and assignee != actor else "",
                   f"refs: {', '.join(refs)}" if refs else "",
                   f"labels: {', '.join(t.labels)}" if t.labels else ""]
        if any(details):
            lines.append("; ".join(d for d in details if d))
        if body:
            lines.append(quote(body) + (f"\n{T.x_length(body)} characters" if k.validate == "x_post" else ""))
        if note:
            lines.append(note.strip())
        self.post(actor, t, "\n".join(lines))
        for old in replaced:
            w.store.close_task(old.id, "replaced")
            self.post(actor, old, f"Closed: replaced by #{t.id}.")
            w.changed(old.id, actor, f"closed, replaced by #{t.id}")
        w.changed(t.id, actor, "created")
        return t, True

    def edit(self, actor: str, t: Task, title: str | None = None, body: str | None = None, note: str | None = None,
             labels: list[str] | None = None, assignee: str | None = None, refs: list[str] | None = None) -> list[str]:
        w, policy = self.w, self.w.policy
        k = policy.kind(t.kind)
        if not t.open:
            raise Refused(f"#{t.id} is closed ({t.resolution}); reopen it first")
        if not policy.may(actor, "edit", k, t):
            raise Refused(f"{actor} may not edit {k.name} tasks; that takes the role {' or '.join(k.edit or [])}, "
                          "or being the author or assignee")
        changes, fields = [], {}
        if title is not None and title.strip() and title.strip() != t.title:
            fields["title"] = title.strip()
            changes.append(f"title: {title.strip()}")
        if labels is not None and list(labels) != t.labels:
            fields["labels"] = list(labels)
            changes.append(f"labels: {', '.join(labels) or 'none'}")
        if refs is not None:
            try:
                new_refs = self._refs(refs)
            except ValueError as e:
                raise Refused(str(e)) from None
            if new_refs != t.refs:
                fields["refs"] = new_refs
                changes.append(f"refs: {', '.join(new_refs) or 'none'}")
        if assignee is not None:
            who = self._account(assignee)
            if who != t.assignee:
                self._open_cap(who)
                fields["assignee"] = who
                changes.append(f"assigned to {who}")
        new_body = self._clean(t.kind, body) if body is not None else None
        if new_body is not None and new_body == t.body and not changes and note is None:
            raise Refused(f"the body is identical to #{t.id}'s current version; nothing to change")
        if new_body is not None and new_body != t.body:
            problems = self.validate(t.kind, new_body, exclude=t.id)
            if problems:
                raise Refused("not saved, the body breaks the rules: " + "; ".join(problems))
        if fields:
            w.store.update_task(t.id, **fields)
        if new_body is not None and new_body != t.body:
            n = w.store.add_revision(t.id, new_body, (note or "").strip(), actor)
            msg = f"Revision {n} of **#{t.id}** by {actor}:\n{quote(new_body)}"
            if k.validate == "x_post":
                msg += f"\n{T.x_length(new_body)} characters"
            if k.approve:
                msg += "\nEarlier approvals no longer count."
            if note:
                msg += f"\n{note.strip()}"
            self.post(actor, t, msg)
            changes.insert(0, f"body (revision {n})")
        elif note:
            self.post(actor, t, note.strip())
        if fields:
            self.post(actor, t, f"{actor} changed #{t.id}: " + "; ".join(c for c in changes if not c.startswith("body")))
        if not changes and not note:
            raise Refused("nothing to change")
        w.changed(t.id, actor, "edited: " + "; ".join(changes or ["note"]))
        return changes

    def close(self, actor: str, t: Task, resolution: str, by_sink: bool = False, **fields) -> None:
        w, k = self.w, self.w.policy.kind(t.kind)
        if not t.open:
            raise Refused(f"#{t.id} is already closed ({t.resolution})")
        if resolution not in RESOLUTIONS:
            raise Refused(f"close as {' or '.join(RESOLUTIONS)}")
        if not by_sink:
            if not w.policy.may(actor, "close", k, t):
                raise Refused(f"{actor} may not close {k.name} tasks")
            if resolution == "done" and k.sink:
                raise Refused(f"{k.name} tasks are closed as done by the {k.sink} publisher once every role "
                              "approved; close it as dropped to give it up")
        w.store.close_task(t.id, resolution, **fields)
        if not by_sink:
            self.post(actor, t, f"{actor} closed #{t.id} as **{resolution}**.")
        w.changed(t.id, actor, f"closed as {resolution}")

    def reopen(self, actor: str, t: Task) -> None:
        w, k = self.w, self.w.policy.kind(t.kind)
        if t.open:
            raise Refused(f"#{t.id} is open")
        if t.x_id or not w.policy.may(actor, "close", k, t):
            raise Refused(f"{actor} may not reopen #{t.id}")
        w.store.update_task(t.id, status="open", resolution=None, closed_at=None)
        self.post(actor, t, f"{actor} reopened #{t.id}.")
        w.changed(t.id, actor, "reopened")

    def comment(self, actor: str, t: Task, text: str, msg_id: int | None = None, check: bool = True) -> None:
        """A comment; `msg_id` when it is already a message in Zulip."""
        text = text.strip()
        if not text:
            raise Refused("empty comment")
        if msg_id is None:
            msg_id = self.post(actor, t, text, check=check)
        self.w.store.add_comment(t.id, actor, text, msg_id)
        self.w.changed(t.id, actor, "commented")

    def review(self, account: str, t: Task, verdict: str, comments: str = "", recorded_by: str | None = None,
               msg_id: int | None = None, post: bool = True) -> str:
        w, policy = self.w, self.w.policy
        k = policy.kind(t.kind)
        if not t.open:
            raise Refused(f"#{t.id} is closed ({t.resolution})")
        if verdict not in VERDICTS:
            raise Refused(f"the verdict is {', '.join(VERDICTS)}")
        if not k.approve:
            raise Refused(f"{k.name} tasks take no reviews; comment without a verdict")
        if not policy.may(account, "approve", k):
            raise Refused(f"{account} may not review {k.name} tasks; that takes the role {' or '.join(k.approve)}")
        w.store.add_review(t.id, t.hash, account, verdict, comments.strip(), recorded_by or account, msg_id)
        if post:
            self.post(recorded_by or account, t, f"Review of **#{t.id}** by {account}: **{verdict}**"
                      + (f"\n{comments.strip()}" if comments.strip() else ""))
        if verdict == "reject":
            w.store.close_task(t.id, "rejected")
            w.changed(t.id, recorded_by or account, f"rejected by {account}")
            return f"#{t.id} rejected and closed"
        w.changed(t.id, recorded_by or account, f"{verdict} from {account}")
        waiting = policy.waits_on(w.store, w.store.task(t.id))
        return f"#{t.id}: {verdict} recorded; it now waits on {', '.join(waiting.accounts) or 'nobody'} ({waiting.why})"
