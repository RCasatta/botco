"""Configuration, loaded from a TOML file. See config.example.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

AGENTS = ("strategist", "writer", "editor")


@dataclass
class Persona:
    name: str
    zuliprc: Path
    # Qwen thinking mode: slower, better for planning and critique.
    thinking: bool = False
    max_tokens: int = 4096
    temperature: float = 0.7


@dataclass
class LLMConfig:
    base_url: str = "http://100.110.202.124:8080/v1"
    # TabbyAPI on ripper currently runs without auth; set the variable to
    # send a key anyway.
    api_key_env: str = "TABBY_API_KEY"
    # Seconds a single request may take. The server can be busy with other
    # work, so this is generous: a busy server is waited for, not failed.
    timeout: int = 1800
    # Agent turns running at once. TabbyAPI's max_batch_size is 3 and other
    # users share it.
    concurrency: int = 1


@dataclass
class Streams:
    plan: str = "plan"
    drafts: str = "drafts"
    published: str = "published"
    metrics: str = "metrics"
    ops: str = "ops"

    def all(self) -> list[str]:
        return [self.plan, self.drafts, self.published, self.metrics, self.ops]


@dataclass
class Agents:
    # Who handles a human message that mentions no bot.
    coordinator: str = "strategist"
    # Each agent gets a turn this often even when nobody called it.
    heartbeat_minutes: int = 60
    # Heartbeats only in this window; mentions wake agents at any time.
    active_from: time = time(7, 30)
    active_to: time = time(23, 0)
    # Model calls in one turn before it is cut off.
    max_steps: int = 10
    # Turns per day. Past it, only turns a human asked for still run.
    max_turns_per_day: int = 300
    # Every tool call is logged in #ops > activity.
    activity_log: bool = True


@dataclass
class Publishing:
    times: list[time] = field(default_factory=lambda: [time(10, 0), time(14, 0), time(19, 0)])
    min_gap_minutes: int = 60
    # A draft also needs the CEO's approval (in words or ✅) to be published.
    ceo_approval: bool = True
    max_chars: int = 280
    metrics_time: time = time(23, 30)


@dataclass
class Breakers:
    bot_messages_per_hour: int = 40
    # Consecutive bot messages in one topic without a human in between.
    bot_streak_per_topic: int = 20


@dataclass
class LabConfig:
    # The lab notebook: markdown reports of the experiments. None: no notebook.
    dir: Path | None = None
    # File or directory names to leave out: vendored repos, models, notes
    # meant for other tools.
    exclude: list[str] = field(default_factory=lambda: ["CLAUDE.md", "models", "llama.cpp", "Strata", "src", ".git"])


@dataclass
class XConfig:
    dry_run: bool = True
    env_file: Path = Path("x.env")


@dataclass
class Config:
    state_dir: Path
    timezone: str
    llm: LLMConfig
    streams: Streams
    agents: Agents
    publishing: Publishing
    breakers: Breakers
    x: XConfig
    personas: dict[str, Persona]
    lab: LabConfig = field(default_factory=LabConfig)
    # Optional file with example posts the team should learn tone and topics
    # from (never copy).
    reference_file: Path | None = None
    # The bot that publishes and posts operational messages; no LLM.
    publisher: str = "publisher"


def _time(s: str) -> time:
    return time.fromisoformat(s)


def _times(raw: dict, *keys: str) -> dict:
    return {k: _time(v) for k, v in raw.items() if k in keys} | {k: v for k, v in raw.items() if k not in keys}


def load(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    base = path.parent.resolve()

    def rel(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else base / q

    pub = dict(raw.get("publishing", {}))
    if "times" in pub:
        pub["times"] = [_time(t) for t in pub["times"]]
    if "metrics_time" in pub:
        pub["metrics_time"] = _time(pub["metrics_time"])
    x = raw.get("x", {})
    personas = {
        name: Persona(
            name=name,
            zuliprc=rel(p["zuliprc"]),
            thinking=p.get("thinking", False),
            max_tokens=p.get("max_tokens", 4096),
            temperature=p.get("temperature", 0.7),
        )
        for name, p in raw["personas"].items()
    }
    for required in (*AGENTS, "publisher"):
        if required not in personas:
            raise ValueError(f"config: missing [personas.{required}]")
    lab = dict(raw.get("lab", {}))
    if lab.get("dir"):
        lab["dir"] = rel(lab["dir"])
    ref = raw.get("reference_file")
    return Config(
        state_dir=rel(raw.get("state_dir", "state")),
        timezone=raw.get("timezone", "Europe/Rome"),
        llm=LLMConfig(**raw.get("llm", {})),
        streams=Streams(**raw.get("streams", {})),
        agents=Agents(**_times(raw.get("agents", {}), "active_from", "active_to")),
        publishing=Publishing(**pub),
        breakers=Breakers(**raw.get("breakers", {})),
        x=XConfig(dry_run=x.get("dry_run", True), env_file=rel(x.get("env_file", "x.env"))),
        personas=personas,
        lab=LabConfig(**lab),
        reference_file=rel(ref) if ref else None,
    )
