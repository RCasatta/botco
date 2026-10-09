"""Configuration, loaded from a TOML file. See config.example.toml.

The policy lives here, not in code: accounts (people or models) hold roles,
kinds say which roles create, edit, approve and close each kind of task, and
an account's engine says how it works, if it is a model.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path


@dataclass
class Model:
    name: str
    # OpenAI-compatible endpoint, e.g. TabbyAPI.
    url: str
    # The model's id on the server. Turns send it when set; pi needs it.
    id: str = ""
    # TabbyAPI on ripper currently runs without auth; set the variable to
    # send a key anyway.
    api_key_env: str = "TABBY_API_KEY"
    # Seconds a single request may take. The server can be busy with other
    # work, so this is generous: a busy server is waited for, not failed.
    timeout: int = 1800
    # Requests the server runs at once. Turns may use every slot; sessions
    # only `session_slots` of them, so a chat message never waits behind a
    # session.
    slots: int = 2
    session_slots: int = 1


@dataclass
class TurnEngine:
    """Short turns inside botco: a model-call loop over the five tools."""

    model: str
    # prompts/{persona}.md describes the role.
    persona: str
    # Qwen thinking mode. Every turn account should use the same setting: the
    # chat template starts the prompt with it, so a different one would cost
    # the shared cache prefix.
    thinking: bool = True
    max_tokens: int = 8000
    temperature: float = 0.7
    kind: str = "turn"


@dataclass
class SessionEngine:
    """One pi session per task, in a sandbox, reporting only its final
    summary."""

    model: str
    cmd: str = "pi"
    # prompts/{persona}.md is added to the session's prompt when set.
    persona: str | None = None
    # {task} is replaced by the task number; relative to state_dir.
    workspace: str = "work/{task}"
    # Paths visible read-only: "/src" or "/src:/where/inside".
    ro: list[str] = field(default_factory=list)
    # Paths made inaccessible inside the sandbox, e.g. credentials under a
    # read-only home. Missing ones are fine.
    hide: list[str] = field(default_factory=list)
    # Extra groups for the session, e.g. to read a home directory that is
    # only open to its group.
    groups: list[str] = field(default_factory=list)
    max_minutes: int = 60
    # Internet access. Without it the sandbox still reaches the model.
    network: bool = True
    # GPU devices stay with the inference server unless this is set.
    gpu: bool = False
    # Run under systemd-run with the restrictions above. Off: run directly,
    # for development only.
    sandbox: bool = True
    kind: str = "session"


@dataclass
class Account:
    name: str
    roles: list[str]
    engine: TurnEngine | SessionEngine | None = None
    # A bot's Zulip credentials. Turn accounts need one.
    zuliprc: Path | None = None
    # A person in Zulip, to know who wrote a message: their email, full name
    # or user id. The realm may hide emails from bots; a name or id always
    # works.
    zulip: str | None = None
    github: str | None = None


@dataclass
class Role:
    name: str
    # Roles this one also holds: an owner can do what an editor can.
    includes: list[str] = field(default_factory=list)
    # Turns that requests from this role wake run first (lower first).
    priority: int = 10
    # Turns a day for accounts with this role; None: the dispatcher default.
    turns_per_day: int | None = None
    # Who gets a message from this role that mentions nobody.
    unaddressed: str | None = None


@dataclass
class Kind:
    """A label plus the policy for tasks with it."""

    name: str
    label: str = ""
    # Zulip stream for the tasks' topics.
    stream: str = "tasks"
    # Roles that may create, edit or close; None: any account. Author and
    # assignee may always edit and close their task.
    create: list[str] | None = None
    edit: list[str] | None = None
    close: list[str] | None = None
    # One approval of the current body from each of these roles.
    approve: list[str] = field(default_factory=list)
    # A check the body must pass: "x_post".
    validate: str | None = None
    # Revise verdicts before the task waits on `escalate` instead of the
    # author.
    max_revisions: int | None = None
    escalate: str = "owner"
    # What takes a task once every role approved: "x". Such a task is never
    # closed as done by hand.
    sink: str | None = None
    # A new task of this kind closes the open one.
    one_open: bool = False


@dataclass
class Schedule:
    """Creates a task at fixed times, as a person would."""

    name: str
    cron: str
    assignee: str
    title: str
    kind: str = "task"
    body: str = ""


@dataclass
class Dispatcher:
    # Turn accounts get a turn this often even when nothing woke them; it is
    # skipped when they would see what they saw last time.
    heartbeat_minutes: int = 60
    # Heartbeats only in this window; events wake accounts at any time.
    active_from: time = time(7, 30)
    active_to: time = time(23, 0)
    # Model calls in one turn before it is cut off.
    max_steps: int = 10
    # Turns and sessions a day per account, unless a role says otherwise.
    turns_per_day: int = 300
    # Every tool call is logged in #ops > activity.
    activity_log: bool = True
    # An account woken again within this many minutes, the same day,
    # continues its last conversation with an update instead of a fresh
    # situation...
    continue_minutes: int = 15
    # ...unless that conversation has grown past this size (~60K tokens).
    continue_max_chars: int = 200000
    # Guardrails on tasks created by agents.
    max_depth: int = 1
    max_new_tasks_per_day: int = 10
    max_open_per_assignee: int = 20


@dataclass
class XSink:
    """The X publisher: no model, posts approved posts at fixed times."""

    account: str = "publisher"
    times: list[time] = field(default_factory=lambda: [time(10, 0), time(14, 0), time(19, 0)])
    min_gap_minutes: int = 60
    max_chars: int = 280
    metrics_time: time = time(23, 30)
    dry_run: bool = True
    env_file: Path = Path("x.env")


@dataclass
class LabConfig:
    # The lab notebook: markdown reports of the experiments. None: no notebook.
    dir: Path | None = None
    # File or directory names to leave out: vendored repos, models, notes
    # meant for other tools.
    exclude: list[str] = field(default_factory=lambda: ["CLAUDE.md", "models", "llama.cpp", "Strata", "src", ".git"])
    # Who is woken when reports appear or change.
    notify: str | None = None
    # Where `dir` is on the host, when the service sees it elsewhere: the
    # session sandbox mounts it from there.
    host_dir: Path | None = None


@dataclass
class GitHub:
    # Repositories `find` searches. Issues anywhere can be read by ref.
    repos: list[str] = field(default_factory=list)
    # A read-only token; public repos need none.
    token_env: str = "GITHUB_TOKEN"
    api: str = "https://api.github.com"


@dataclass
class GitLab:
    url: str = "https://gitlab.com"
    projects: list[str] = field(default_factory=list)
    token_env: str = "GITLAB_TOKEN"


@dataclass
class Sources:
    lab: LabConfig = field(default_factory=LabConfig)
    github: GitHub | None = None
    gitlab: GitLab | None = None
    # How often referenced external issues are checked for changes.
    poll_minutes: int = 15


@dataclass
class Streams:
    published: str = "published"
    metrics: str = "metrics"
    ops: str = "ops"


@dataclass
class Breakers:
    bot_messages_per_hour: int = 40
    # Consecutive bot messages in one topic without a human in between.
    bot_streak_per_topic: int = 20


@dataclass
class Config:
    state_dir: Path
    timezone: str
    models: dict[str, Model]
    accounts: dict[str, Account]
    roles: dict[str, Role]
    kinds: dict[str, Kind]
    schedules: dict[str, Schedule] = field(default_factory=dict)
    dispatcher: Dispatcher = field(default_factory=Dispatcher)
    x: XSink = field(default_factory=XSink)
    sources: Sources = field(default_factory=Sources)
    streams: Streams = field(default_factory=Streams)
    breakers: Breakers = field(default_factory=Breakers)
    # Optional file with example posts to learn tone and topics from (never
    # copy), shown to accounts whose persona is the strategist.
    reference_file: Path | None = None
    # The bot that posts operational messages and records; no model.
    publisher: str = "publisher"

    def turn_accounts(self) -> list[str]:
        return [a.name for a in self.accounts.values() if a.engine and a.engine.kind == "turn"]

    def task_streams(self) -> list[str]:
        return sorted({k.stream for k in self.kinds.values()})

    def all_streams(self) -> list[str]:
        s = self.streams
        return sorted({*self.task_streams(), s.published, s.metrics, s.ops})

    def validate(self) -> None:
        for a in self.accounts.values():
            for r in a.roles:
                if r not in self.roles:
                    raise ValueError(f"config: account {a.name} has unknown role {r!r}")
            if a.engine:
                if a.engine.model not in self.models:
                    raise ValueError(f"config: account {a.name} uses unknown model {a.engine.model!r}")
                if a.engine.kind == "turn" and not a.zuliprc:
                    raise ValueError(f"config: account {a.name} has a turn engine but no zuliprc")
                if a.engine.kind == "session" and not self.models[a.engine.model].id:
                    raise ValueError(f"config: account {a.name} runs sessions; [models.{a.engine.model}] needs an id")
        for r in self.roles.values():
            for inc in r.includes:
                if inc not in self.roles:
                    raise ValueError(f"config: role {r.name} includes unknown role {inc!r}")
            if r.unaddressed and r.unaddressed not in self.accounts:
                raise ValueError(f"config: role {r.name} routes to unknown account {r.unaddressed!r}")
        for k in self.kinds.values():
            for role in [*k.approve, *(k.create or []), *(k.edit or []), *(k.close or [])]:
                if role not in self.roles:
                    raise ValueError(f"config: kind {k.name} names unknown role {role!r}")
            if k.sink not in (None, "x"):
                raise ValueError(f"config: kind {k.name} has unknown sink {k.sink!r}")
        for s in self.schedules.values():
            if s.kind not in self.kinds or s.assignee not in self.accounts:
                raise ValueError(f"config: schedule {s.name} names an unknown kind or assignee")
        pub = self.accounts.get(self.publisher)
        if not pub or not pub.zuliprc:
            raise ValueError(f"config: [accounts.{self.publisher}] with a zuliprc is required")
        if self.sources.lab.notify and self.sources.lab.notify not in self.accounts:
            raise ValueError("config: [sources.lab] notify names an unknown account")


def _time(s: str) -> time:
    return time.fromisoformat(s)


def _times(raw: dict, *keys: str) -> dict:
    return {k: _time(v) if k in keys else v for k, v in raw.items()}


def load(path: Path) -> Config:
    return parse(tomllib.loads(path.read_text()), path.parent.resolve())


def parse(raw: dict, base: Path) -> Config:
    def rel(p: str | Path) -> Path:
        q = Path(p)
        return q if q.is_absolute() else base / q

    models = {n: Model(name=n, **m) for n, m in raw.get("models", {}).items()}
    accounts = {}
    for name, a in raw.get("accounts", {}).items():
        a = dict(a)
        engine = a.pop("engine", None)
        if engine is not None:
            engine = dict(engine)
            kind = engine.pop("kind", "turn")
            engine = TurnEngine(**engine) if kind == "turn" else SessionEngine(**engine)
        zuliprc = a.pop("zuliprc", None)
        accounts[name] = Account(name=name, engine=engine, zuliprc=rel(zuliprc) if zuliprc else None, **a)
    roles = {n: Role(name=n, **r) for n, r in raw.get("roles", {}).items()}
    kinds = {n: Kind(name=n, **{"label": n, **k}) for n, k in raw.get("kinds", {}).items()}
    kinds.setdefault("task", Kind(name="task", label="task"))
    schedules = {n: Schedule(name=n, **s) for n, s in raw.get("schedules", {}).items()}

    x = dict(raw.get("sinks", {}).get("x", {}))
    if "times" in x:
        x["times"] = [_time(t) for t in x["times"]]
    if "metrics_time" in x:
        x["metrics_time"] = _time(x["metrics_time"])
    if "env_file" in x:
        x["env_file"] = rel(x["env_file"])
    else:
        x["env_file"] = rel("x.env")

    src = dict(raw.get("sources", {}))
    lab = dict(src.pop("lab", {}))
    for k in ("dir", "host_dir"):
        if lab.get(k):
            lab[k] = rel(lab[k])
    gh, gl = src.pop("github", None), src.pop("gitlab", None)
    sources = Sources(lab=LabConfig(**lab), github=GitHub(**gh) if gh is not None else None,
                      gitlab=GitLab(**gl) if gl is not None else None, **src)

    ref = raw.get("reference_file")
    cfg = Config(
        state_dir=rel(raw.get("state_dir", "state")),
        timezone=raw.get("timezone", "Europe/Rome"),
        models=models,
        accounts=accounts,
        roles=roles,
        kinds=kinds,
        schedules=schedules,
        dispatcher=Dispatcher(**_times(raw.get("dispatcher", {}), "active_from", "active_to")),
        x=XSink(**x),
        sources=sources,
        streams=Streams(**raw.get("streams", {})),
        breakers=Breakers(**raw.get("breakers", {})),
        reference_file=rel(ref) if ref else None,
        publisher=raw.get("publisher", "publisher"),
    )
    cfg.validate()
    return cfg
