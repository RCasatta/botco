"""Who may do what, and who a task waits on.

A task's state is computed here from its kind's policy, its body and its
reviews; no model sets it. Each review carries the hash of the body it is
about, so any edit of the body makes earlier approvals stop counting without
a flag to reset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config, Kind
from .store import Store, Task

# Refs address everything the tools can read: local tasks are the only ones
# that can be written.
REF_RE = re.compile(
    r"^(?:#(?P<task>\d+)"
    r"|gh:(?P<gh>[\w.-]+/[\w.-]+)#(?P<gh_n>\d+)"
    r"|gl:(?P<gl>[\w./-]+)#(?P<gl_n>\d+)"
    r"|lab:(?P<lab>[^#]+)(?:#(?P<lab_section>.*))?"
    r"|zulip:(?P<stream>[^/]+)/(?P<topic>.+))$"
)


@dataclass
class Ref:
    kind: str  # task, gh, gl, lab, zulip
    text: str
    task: int = 0
    repo: str = ""
    number: int = 0
    path: str = ""
    section: str = ""
    stream: str = ""
    topic: str = ""

    @property
    def external(self) -> bool:
        return self.kind in ("gh", "gl")


def parse_ref(text: str) -> Ref:
    t = text.strip()
    if t.isdigit():
        t = f"#{t}"
    m = REF_RE.match(t)
    if not m:
        raise ValueError(f"not a ref: {text!r}; use #42, gh:owner/repo#12, gl:group/proj#7, lab:FILE.md#Section "
                         "or zulip:stream/topic")
    g = m.groupdict()
    if g["task"]:
        return Ref("task", t, task=int(g["task"]))
    if g["gh"]:
        return Ref("gh", t, repo=g["gh"], number=int(g["gh_n"]))
    if g["gl"]:
        return Ref("gl", t, repo=g["gl"], number=int(g["gl_n"]))
    if g["lab"]:
        return Ref("lab", t, path=g["lab"].strip(), section=(g["lab_section"] or "").strip())
    return Ref("zulip", t, stream=g["stream"].lstrip("#").strip(), topic=g["topic"].strip())


@dataclass
class Waiting:
    accounts: list[str]
    why: str
    # Every role the kind needs has approved the current body.
    ready: bool = False


class Policy:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def kind(self, name: str) -> Kind:
        try:
            return self.cfg.kinds[name]
        except KeyError:
            raise ValueError(f"unknown kind {name!r}; kinds: {', '.join(self.cfg.kinds)}") from None

    def roles_of(self, account: str | None) -> set[str]:
        """The account's roles with everything they include."""
        a = self.cfg.accounts.get(account or "")
        todo, seen = list(a.roles) if a else [], set()
        while todo:
            r = todo.pop()
            if r not in seen:
                seen.add(r)
                todo.extend(self.cfg.roles[r].includes)
        return seen

    def holders(self, role: str) -> list[str]:
        """Accounts a task waiting on `role` waits on: those that hold it
        themselves, before those that hold it through another role (a task
        waiting on the editor wakes the editor, not the owner)."""
        direct = [a.name for a in self.cfg.accounts.values() if role in a.roles]
        return direct or [a for a in self.cfg.accounts if role in self.roles_of(a)]

    def may(self, account: str, action: str, kind: Kind, task: Task | None = None) -> bool:
        """action: create, edit, close or approve."""
        roles = self.roles_of(account)
        if action == "approve":
            return bool(roles & set(kind.approve))
        if task is not None and account in (task.author, task.assignee):
            return True
        allowed = getattr(kind, action)
        if allowed is None:
            return account in self.cfg.accounts
        return bool(roles & set(allowed))

    def priority(self, account: str | None) -> int:
        roles = self.roles_of(account)
        return min((self.cfg.roles[r].priority for r in roles), default=10)

    def budget(self, account: str) -> int:
        own = [self.cfg.roles[r].turns_per_day for r in self.roles_of(account)]
        own = [n for n in own if n is not None]
        return max(own) if own else self.cfg.dispatcher.turns_per_day

    def unaddressed(self, account: str | None) -> str | None:
        """Who gets this account's message that mentions nobody."""
        a = self.cfg.accounts.get(account or "")
        for r in a.roles if a else []:
            if self.cfg.roles[r].unaddressed:
                return self.cfg.roles[r].unaddressed
        for r in sorted(self.roles_of(account)):
            if self.cfg.roles[r].unaddressed:
                return self.cfg.roles[r].unaddressed
        return None

    def waits_on(self, store: Store, t: Task) -> Waiting:
        if not t.open:
            return Waiting([], f"closed ({t.resolution})")
        kind = self.kind(t.kind)
        owner = t.assignee or t.author
        owner = [owner] if owner in self.cfg.accounts else []
        if not kind.approve:
            return Waiting(owner, "its assignee")
        reviews = store.reviews(t.id)
        current = [r for r in reviews if r.body_hash == t.hash]
        if current and current[-1].verdict == "revise":
            asked = sum(r.verdict == "revise" for r in reviews)
            if kind.max_revisions and asked >= kind.max_revisions:
                return Waiting(self.holders(kind.escalate),
                               f"the {kind.escalate}: {asked} revisions asked, they decide")
            return Waiting(owner, f"a revision ({current[-1].account} asked)")
        filled = set()
        for r in current:
            if r.verdict == "approve":
                filled |= self.roles_of(r.account) & set(kind.approve)
        for role in kind.approve:
            if role not in filled:
                return Waiting(self.holders(role), f"the {role}'s approval")
        if kind.sink:
            return Waiting([], f"ready, for the {kind.sink} publisher", ready=True)
        return Waiting(owner, "approved, to be closed", ready=True)

    def approvals(self, store: Store, t: Task) -> str:
        """`editor: approved, owner: not yet` for the current body."""
        kind = self.kind(t.kind)
        current = [r for r in store.reviews(t.id) if r.body_hash == t.hash and r.verdict == "approve"]
        filled = set()
        for r in current:
            filled |= self.roles_of(r.account)
        return ", ".join(f"{role}: {'approved' if role in filled else 'not yet'}" for role in kind.approve)
