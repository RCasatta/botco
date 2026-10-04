"""The pipeline end to end, with fake Zulip, fake inference and a fixed clock."""

import queue
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from botco.breakers import Breakers
from botco.company import Company
from botco.config import Breakers as BreakerConfig
from botco.config import LLMConfig, Persona, Pipeline, Schedule, Streams, XConfig, Config
from botco.llm import Done
from botco.store import Store
from botco.xclient import DryRunX

TZ = ZoneInfo("Europe/Rome")
CEO = 1


class FakeTeam:
    def __init__(self):
        self.breakers = Breakers(BreakerConfig())
        self.bot_ids = {9, 10, 11, 13}
        self.sent = []
        self.ids = iter(range(1000, 10**6))

    def names(self):
        return {"strategist": 9, "writer": 10, "editor": 11, "publisher": 13}

    def persona_of(self, x):
        return {v: k for k, v in self.names().items()}.get(x, x if x in self.names() else None)

    def is_bot(self, uid):
        return uid in self.bot_ids

    def send(self, persona, stream, topic, content, check=True):
        msg_id = next(self.ids)
        self.sent.append((persona, stream, topic, content, msg_id))
        return msg_id

    def notify(self, text, topic="alerts"):
        self.send("publisher", "ops", topic, text, check=False)

    def history(self, stream, topic=None, n=30):
        return []


class FakeWorkers:
    def __init__(self):
        self.jobs = []
        self.down = False

    def submit(self, job):
        self.jobs.append(job)

    def pending(self):
        return len(self.jobs)


class Clocked(Company):
    at = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)

    def now(self):
        return self.at


@pytest.fixture
def company(tmp_path: Path):
    personas = {n: Persona(n, Path("unused")) for n in ("strategist", "writer", "editor", "publisher")}
    cfg = Config(
        state_dir=tmp_path, timezone="Europe/Rome", llm=LLMConfig(), streams=Streams(),
        schedule=Schedule(), pipeline=Pipeline(posts_per_day=1), breakers=BreakerConfig(),
        x=XConfig(), personas=personas,
    )
    return Clocked(cfg, Store(tmp_path / "db.sqlite3"), FakeTeam(), FakeWorkers(), DryRunX(),
                   queue.Queue(), threading.Event())


def run_next(c: Company, reply: str, kind: str):
    """Answer the oldest queued job, which must be of `kind`."""
    job = c.workers.jobs.pop(0)
    assert job.key.startswith(kind), job.key
    c.handle(Done(job, reply))


def test_plan_draft_review_approve_publish(company):
    c = company
    c.reconcile()
    run_next(c, "Plan: tips about VRAM.", "plan:")
    assert c.store.latest_plan("2026-W41")

    c.reconcile()
    run_next(c, '"Quantizing the KV cache to 6 bits barely changes output quality."', "write:")
    c.reconcile()
    run_next(c, "Add a concrete trade-off.\nVERDICT: REVISE", "review:")
    c.reconcile()
    run_next(c, "A 6-bit KV cache roughly halves its VRAM and barely changes quality.", "revise:")
    c.reconcile()
    run_next(c, "Good.\nVERDICT: APPROVE", "review:")

    (d,) = c.store.drafts_with_status("awaiting_ceo")
    assert d.revisions == 1
    c.reconcile()
    assert not c.store.published()  # not before the CEO approves

    c.handle({"type": "reaction", "op": "add", "user_id": CEO, "message_id": d.review_msg_id,
              "emoji_name": "check"})
    c.at = c.at.replace(hour=10, minute=5)
    c.reconcile()
    (p,) = c.store.published()
    assert p.text.startswith("A 6-bit KV cache")
    assert any(s[1] == "published" for s in c.team.sent)


def test_failed_checks_go_back_to_writer_and_drop_after_max(company):
    c = company
    c.cfg.pipeline.max_revisions = 1
    c.reconcile()
    run_next(c, "plan", "plan:")
    c.reconcile()
    run_next(c, "Read more at https://example.com", "write:")
    (d,) = c.store.drafts_with_status("revise")
    assert "link" in d.feedback
    c.reconcile()
    run_next(c, "Still https://example.com", "revise:")
    assert c.store.draft(d.id).status == "dropped"
    # The slot gets a second attempt with a fresh draft.
    c.reconcile()
    assert c.workers.jobs[0].key == "write:2026-10-05:1:2"


def test_halt_discards_results_and_stops_work(company):
    c = company
    c.handle({"type": "message", "message": {
        "type": "stream", "display_recipient": "ops", "subject": "x", "content": "halt",
        "sender_id": CEO, "id": 5}})
    assert c.halted.is_set() and c.store.get("halted") == "1"
    c.reconcile()
    assert not c.workers.jobs


def test_mention_from_human_queues_chat_but_bot_mentions_are_ignored(company):
    c = company
    msg = {"type": "stream", "display_recipient": "general", "subject": "hi",
           "content": "@**writer** what are you working on?", "id": 7}
    c.handle({"type": "message", "message": {**msg, "sender_id": 11}})
    assert not c.workers.jobs
    c.handle({"type": "message", "message": {**msg, "sender_id": CEO}})
    assert c.workers.jobs[0].key == "chat:7:writer"
    assert c.workers.jobs[0].priority == 0
