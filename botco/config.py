"""Configuration, loaded from a TOML file. See config.example.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path


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
    # Requests in flight at once. TabbyAPI's max_batch_size is 3 and other
    # users share it.
    concurrency: int = 1
    max_jobs_per_day: int = 150


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
class Schedule:
    writing_starts: time = time(8, 0)
    publish_times: list[time] = field(default_factory=lambda: [time(10, 0), time(14, 0), time(19, 0)])
    min_gap_minutes: int = 60
    metrics_time: time = time(23, 30)


@dataclass
class Pipeline:
    posts_per_day: int = 3
    attempts_per_slot: int = 2
    max_revisions: int = 3
    # Approved drafts wait for the CEO's reaction before they can be published.
    ceo_approval: bool = True
    max_chars: int = 280
    # Stop writing when this many approved drafts wait to be published.
    max_backlog: int = 6


@dataclass
class Breakers:
    bot_messages_per_hour: int = 30
    # Consecutive bot messages in one topic without a human in between.
    bot_streak_per_topic: int = 12
    # Bots answer @-mentions from humans; mentions between bots are ignored
    # unless this is set.
    answer_bot_mentions: bool = False


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
    schedule: Schedule
    pipeline: Pipeline
    breakers: Breakers
    x: XConfig
    personas: dict[str, Persona]
    # Optional file with example posts the team should learn tone and topics
    # from (never copy).
    reference_file: Path | None = None
    # Bot whose event queue the orchestrator listens on.
    listener: str = "publisher"


def _time(s: str) -> time:
    return time.fromisoformat(s)


def load(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    base = path.parent.resolve()

    def rel(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else base / q

    sched = raw.get("schedule", {})
    schedule = Schedule(
        writing_starts=_time(sched.get("writing_starts", "08:00")),
        publish_times=[_time(t) for t in sched.get("publish_times", ["10:00", "14:00", "19:00"])],
        min_gap_minutes=sched.get("min_gap_minutes", 60),
        metrics_time=_time(sched.get("metrics_time", "23:30")),
    )
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
    for required in ("strategist", "writer", "editor", "publisher"):
        if required not in personas:
            raise ValueError(f"config: missing [personas.{required}]")
    ref = raw.get("reference_file")
    return Config(
        state_dir=rel(raw.get("state_dir", "state")),
        timezone=raw.get("timezone", "Europe/Rome"),
        llm=LLMConfig(**raw.get("llm", {})),
        streams=Streams(**raw.get("streams", {})),
        schedule=schedule,
        pipeline=Pipeline(**raw.get("pipeline", {})),
        breakers=Breakers(**raw.get("breakers", {})),
        x=XConfig(dry_run=x.get("dry_run", True), env_file=rel(x.get("env_file", "x.env"))),
        personas=personas,
        reference_file=rel(ref) if ref else None,
        listener=raw.get("listener", "publisher"),
    )
