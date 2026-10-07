"""The agent loop end to end, with fake Zulip, a scripted model and a fixed clock."""

import json
import queue
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from botco.agents import Runner, TurnDone, clock_line, fingerprint_key, situation, system_prompt
from botco.breakers import Breakers
from botco.company import Company
from botco.config import AGENTS, Agents, Breakers as BreakerConfig, Config, LLMConfig, Persona, Publishing
from botco.config import Streams, XConfig
from botco.store import Store
from botco.tools import Trigger, Turn, available, fix_mentions, offered
from botco.world import World
from botco.xclient import DryRunX

TZ = ZoneInfo("Europe/Rome")
CEO = 1
IDS = {"strategist": 9, "writer": 10, "editor": 11, "publisher": 13}


class FakeTeam:
    """Records what bots send, and turns it into the events Zulip would echo."""

    def __init__(self):
        self.breakers = Breakers(BreakerConfig())
        self.bot_ids = set(IDS.values())
        self.ceo = "RCasatta"
        self.events = []  # drained by pump()
        self.log = []
        self.next_id = 1000

    def names(self):
        return dict(IDS)

    def persona_of(self, x):
        return {v: k for k, v in IDS.items()}.get(x, x if x in IDS else None)

    def mention(self, persona):
        return f"@**{persona}**"

    def is_bot(self, uid):
        return uid in self.bot_ids

    def send(self, persona, stream, topic, content, check=True):
        self.next_id += 1
        event = message(IDS[persona], stream, topic, content, self.next_id, persona)
        self.events.append(event)
        self.log.append(event)
        return self.next_id

    def notify(self, text, topic="alerts"):
        self.send("publisher", "ops", topic, text, check=False)

    def history(self, stream=None, topic=None, n=30):
        return []

    def sent(self, persona=None, stream=None):
        return [e["message"] for e in self.log
                if (persona is None or e["message"]["sender_id"] == IDS[persona])
                and (stream is None or e["message"]["display_recipient"] == stream)]


def message(sender_id, stream, topic, content, msg_id, name="CEO"):
    return {"type": "message", "message": {
        "type": "stream", "sender_id": sender_id, "sender_full_name": name, "display_recipient": stream,
        "subject": topic, "content": content, "id": msg_id, "timestamp": 0}}


def call(name, **args):
    return {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM:
    """Plays each agent from a list of replies: a list of tool calls, or a
    final text."""

    def __init__(self):
        self.script = {a: [] for a in AGENTS}
        self.seen = {a: [] for a in AGENTS}

    def healthy(self):
        return True

    def chat(self, persona, messages, tools=None):
        self.seen[persona.name].append(messages)
        step = self.script[persona.name].pop(0)
        thought = f"thinking before step {len(self.seen[persona.name])}"
        if isinstance(step, str):
            return {"role": "assistant", "content": step, "reasoning_content": thought, "tool_calls": []}
        return {"role": "assistant", "content": "", "reasoning_content": thought, "tool_calls": step}


class Clock(World):
    at = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)

    def now(self):
        return self.at


@pytest.fixture
def env(tmp_path: Path):
    personas = {n: Persona(n, Path("unused")) for n in (*AGENTS, "publisher")}
    cfg = Config(
        state_dir=tmp_path, timezone="Europe/Rome", llm=LLMConfig(), streams=Streams(),
        agents=Agents(activity_log=False), publishing=Publishing(), breakers=BreakerConfig(),
        x=XConfig(), personas=personas,
    )
    world = Clock(cfg, Store(tmp_path / "db.sqlite3"), FakeTeam(), DryRunX())
    llm = ScriptedLLM()
    inbox = queue.Queue()
    runner = Runner(world, llm, inbox)
    company = Company(world, runner, inbox)
    company.last_turn = {a: world.now() for a in AGENTS}  # no heartbeats unless a test moves the clock
    return world, llm, runner, company


def pump(world, runner, company):
    """Deliver Zulip echoes and run queued turns until nothing moves."""
    while True:
        while world.team.events:
            company.handle(world.team.events.pop(0))
        company.schedule()
        if runner.turns.empty():
            return
        _, _, turn = runner.turns.get()
        steps, outcome = runner.run_turn(turn)
        company.handle(TurnDone(turn.agent, steps, outcome))


def test_ceo_asks_in_words_and_the_team_delivers(env):
    world, llm, runner, company = env
    llm.script["strategist"] = [
        [call("send_message", stream="drafts", topic="requests",
              content="@**writer** the CEO wants a post about KV cache quantization")],
        "delegated",
        # Second turn: the CEO approves in words.
        [call("ceo_decision", draft_id=1, decision="approve", ceo_words="draft 1 is good")],
        [call("send_message", stream="drafts", topic="draft #1", content="Approved, it goes out at 10:00.")],
        "done",
    ]
    llm.script["writer"] = [
        [call("create_draft", text="A 6-bit KV cache roughly halves its VRAM and barely changes quality.",
              note="@**editor** please review")],
        "drafted",
    ]
    llm.script["editor"] = [
        [call("review_draft", draft_id=1, verdict="approve", comments="Clear and accurate.")],
        "reviewed",
    ]

    company.handle(message(CEO, "general", "ideas", "make another post about KV cache quantization", 1))
    pump(world, runner, company)
    d = world.store.draft(1)
    assert d.author == "writer" and d.editor_ok and not d.ceo_ok
    assert "CEO wrote" in llm.seen["strategist"][0][1]["content"]

    company.handle(message(CEO, "drafts", "draft #1", "draft 1 is good", 2))
    pump(world, runner, company)
    assert world.store.draft(1).ceo_ok
    assert any("Recorded: the CEO **approved**" in m["content"] for m in world.team.sent("publisher"))

    world.at = world.at.replace(hour=10, minute=1)
    company.tick()
    (p,) = world.store.published()
    assert p.id == 1
    assert world.team.sent("publisher", "published")
    assert not any(llm.script.values()), "every scripted step was used"


def test_tools_enforce_the_rules(env):
    world, llm, runner, company = env
    llm.script["writer"] = [
        [call("create_draft", text="Read more at https://example.com")],
        [call("review_draft", draft_id=1, verdict="approve", comments="self-approval")],
        "gave up",
    ]
    turn = Turn("writer", [Trigger("heartbeat")])
    runner.run_turn(turn)
    results = [m["content"] for m in llm.seen["writer"][-1] if m["role"] == "tool"]
    assert "link" in results[0] and "not saved" in results[0]
    assert "review_draft is for the editor" in results[1]
    assert world.store.open_drafts() == []
    world.store.add_draft("2026-10-05", "Same text", "writer")
    llm.script["writer"] = [[call("revise_draft", draft_id=1, text="Same text")], "ok"]
    runner.run_turn(Turn("writer", [Trigger("heartbeat")]))
    assert "identical" in [m for m in llm.seen["writer"][-1] if m["role"] == "tool"][-1]["content"]
    # The CEO's decision can only be recorded in a turn a human started.
    assert "ceo_decision" not in {t.name for t in available("strategist", turn)}
    assert "ceo_decision" in {t.name for t in available("strategist", Turn("strategist", [Trigger("human")]))}


def test_a_review_calls_the_writer_from_its_comments(env):
    world, llm, runner, company = env
    world.store.add_draft("2026-10-05", "A post", "writer")
    llm.script["editor"] = [[call("review_draft", draft_id=1, verdict="revise", comments="@writer name the source")],
                            "done"]
    runner.run_turn(Turn("editor", [Trigger("heartbeat")]))
    (review,) = world.team.sent("editor")
    assert "@**writer** name the source" in review["content"]
    company.handle({"type": "message", "message": review})
    assert company.pending["writer"], "the mention in the review wakes the writer"


def test_plain_text_answer_to_a_bot_is_not_posted(env):
    world, llm, runner, company = env
    llm.script["writer"] = ["Nothing to do."]
    company.handle(message(IDS["editor"], "drafts", "x", "@**writer** fyi", 4, "editor"))
    pump(world, runner, company)
    assert not world.team.sent("writer")


def test_triggers_merge_while_an_agent_waits_for_a_worker(env):
    world, llm, runner, company = env
    company.handle(message(CEO, "drafts", "x", "@**writer** one", 5))
    company.handle(message(CEO, "drafts", "x", "@**editor** two", 6))
    company.schedule()  # one worker: only the writer starts
    company.handle(message(IDS["strategist"], "plan", "y", "@**editor** three", 7, "strategist"))
    company.handle(TurnDone("writer", 1, "done"))
    company.schedule()
    turns = []
    while not runner.turns.empty():
        turns.append(runner.turns.get()[2])
    assert [(t.agent, len(t.triggers)) for t in turns] == [("writer", 1), ("editor", 2)]


def test_plain_text_answer_to_a_human_is_delivered(env):
    world, llm, runner, company = env
    llm.script["editor"] = ["Nothing is waiting for review right now."]
    company.handle(message(CEO, "drafts", "chat", "@**editor** anything to review?", 3))
    pump(world, runner, company)
    (reply,) = world.team.sent("editor")
    assert reply["subject"] == "chat" and "Nothing is waiting" in reply["content"]


def test_plain_at_mentions_are_fixed(env):
    world, *_ = env
    assert fix_mentions(world, "@writer and @Editor, not @**strategist** or me@writer.com") == \
        "@**writer** and @**Editor**, not @**strategist** or me@writer.com"
    assert fix_mentions(world, "@ceo and @**ceo**") == "@**RCasatta** and @**RCasatta**"


def test_who_wakes_whom(env):
    world, llm, runner, company = env
    # The publisher's messages never wake anyone, even with a mention in them.
    company.handle(message(IDS["publisher"], "ops", "activity", "@**writer** create_draft", 4, "publisher"))
    # A bot mentioning itself does not wake itself.
    company.handle(message(IDS["editor"], "drafts", "x", "@**editor** note to self", 5, "editor"))
    assert not any(company.pending.values())
    # A bot mention wakes the mentioned agent only.
    company.handle(message(IDS["editor"], "drafts", "x", "@**writer** please revise", 6, "editor"))
    assert set(a for a, t in company.pending.items() if t) == {"writer"}
    # Heartbeats once the interval has passed, inside active hours.
    world.at = world.at.replace(hour=10, minute=30)
    company.tick()
    assert all(company.pending[a] for a in AGENTS)


def test_halt_command_stops_turns(env):
    world, llm, runner, company = env
    company.handle(message(CEO, "ops", "x", "halt", 7))
    assert world.halted.is_set() and world.store.get("halted") == "1"
    company.handle(message(CEO, "drafts", "x", "@**writer** write something", 8))
    company.schedule()
    assert runner.turns.empty()
    company.handle(message(CEO, "ops", "x", "resume", 9))
    company.schedule()
    assert not runner.turns.empty()


def test_situation_shows_drafts_and_approvals(env):
    world, *_ = env
    world.store.add_draft("2026-10-05", "Some post", "writer")
    text = situation(world, Turn("editor", [Trigger("heartbeat")]))
    assert "#1 by writer, version 1 (editor: not approved, CEO: not approved)" in text
    assert "no plan for 2026-W41" in text


def test_lab_changes_wake_the_coordinator(env, tmp_path):
    world, llm, runner, company = env
    from botco.lab import Lab
    notebook = tmp_path / "lab"
    notebook.mkdir()
    (notebook / "A.md").write_text("# A\n")
    world.lab = Lab(notebook, [])
    company.watch_lab()
    (t,) = company.pending["strategist"]
    assert t.kind == "lab" and "1 reports" in t.content
    company.pending["strategist"] = []
    company.watch_lab()
    assert not company.pending["strategist"]  # nothing changed
    (notebook / "B.md").write_text("# B\n")
    company.watch_lab()
    assert "`B.md`" in company.pending["strategist"][0].content
    text = situation(world, Turn("writer", [Trigger("heartbeat")]))
    assert "## Lab notebook" in text and "`B.md`" in text


def test_requests_share_their_beginning_across_agents_and_turns(env):
    """The server reuses its cache up to the first difference, and the chat
    template puts the tools first: system prompt and tools must not depend on
    the agent or on why it woke, and the situation must put the agent's role
    after the shared sections and the clock last."""
    world, *_ = env
    world.store.add_plan("2026-W41", "Plan text")
    world.store.add_draft("2026-10-05", "A post", "writer")
    turns = [Turn("strategist", [Trigger("human")]), Turn("writer", [Trigger("heartbeat")]),
             Turn("editor", [Trigger("bot", "writer")])]
    assert len({system_prompt(world) for _ in turns}) == 1
    assert {t.name for t in offered(False)} == {t.name for t in available("strategist", turns[0], False)} | \
        {t.name for t in available("editor", turns[2], False)}
    texts = [situation(world, t) for t in turns]
    shared = texts[0][:texts[0].index("## Your role")]
    assert "Plan text" in shared and "## Publishing" in shared
    assert all(t.startswith(shared) for t in texts)
    assert all(t.rindex("It is Monday") > t.index("## Open drafts") > t.index("## Your role") for t in texts)


def heartbeat(company, agent="strategist"):
    company.pending[agent].append(Trigger("heartbeat"))
    company.schedule()


def test_heartbeat_skipped_when_nothing_changed(env):
    world, llm, runner, company = env
    llm.script["strategist"] = ["Nothing to do."]
    heartbeat(company)
    pump(world, runner, company)  # runs, and records what it saw
    world.at = world.at.replace(hour=10)  # an hour later: only the clock moved
    heartbeat(company)
    assert runner.turns.empty() and not company.pending["strategist"]
    assert world.store.get("skipped:2026-10-05") == "1"
    assert world.store.get("turns:2026-10-05") == "1"  # skips use no budget

    world.store.add_draft("2026-10-05", "Something new", "writer")
    llm.script["strategist"] = ["Saw the new draft."]
    heartbeat(company)
    assert not runner.turns.empty(), "a change makes the heartbeat run"
    pump(world, runner, company)
    assert not any(llm.script.values())


def test_own_actions_and_the_trigger_kind_do_not_defeat_skipping(env):
    world, llm, runner, company = env
    # A turn started by the CEO, in which the agent only writes itself a note.
    llm.script["strategist"] = [[call("remember", note="CEO asked about the week; answered")], "done"]
    company.handle(message(CEO, "plan", "x", "how is the week going?", 9))
    pump(world, runner, company)
    world.at = world.at.replace(hour=10)
    heartbeat(company)
    assert runner.turns.empty(), "its own note and the CEO trigger are not news for the next heartbeat"


def test_requests_from_people_and_bots_are_never_skipped(env):
    world, llm, runner, company = env
    llm.script["strategist"] = ["Nothing to do.", "Answered."]
    heartbeat(company)
    pump(world, runner, company)
    company.handle(message(CEO, "plan", "x", "anything new?", 9))  # same state, but someone asked
    company.schedule()
    assert not runner.turns.empty()


def test_an_unfinished_turn_does_not_count_as_seen(env):
    world, llm, runner, company = env

    def timeout(*a, **k):
        raise requests.Timeout()
    llm.chat = timeout
    heartbeat(company)
    pump(world, runner, company)
    assert world.store.get(fingerprint_key("strategist")) is None
    heartbeat(company)
    assert not runner.turns.empty(), "the next heartbeat runs after a timed-out turn"


def editor_turn(runner, script, llm, minutes_later=None, world=None):
    if minutes_later is not None:
        world.at = world.at + timedelta(minutes=minutes_later)
    llm.script["editor"] = script
    steps, outcome = runner.run_turn(Turn("editor", [Trigger("bot", "writer", "drafts", "draft #1", "review", 1)]))
    return llm.seen["editor"][-1], outcome


def test_a_turn_soon_after_continues_the_conversation(env):
    world, llm, runner, company = env
    world.store.add_plan("2026-W41", "Plan text")
    world.store.add_draft("2026-10-05", "First post", "writer")
    world.store.add_draft("2026-10-05", "Second post", "writer")
    first, outcome = editor_turn(runner, [[call("review_draft", draft_id=1, verdict="approve", comments="ok")],
                                          "reviewed #1"], llm)
    assert outcome.startswith("fresh") and len(first) == 2 + 2 + 1  # system, user | call, result, reply
    first_turn = llm.seen["editor"][-1]

    world.store.update_draft(2, status="rejected")
    world.store.add_draft("2026-10-05", "Third post", "writer")
    second, outcome = editor_turn(runner, ["nothing else"], llm, minutes_later=10, world=world)
    assert outcome.startswith("continued")
    assert second[:len(first_turn)] == first_turn  # the earlier turn, reasoning included, unchanged
    assert first_turn[2]["reasoning_content"].startswith("thinking before step")
    update = second[len(first_turn)]
    text = update["content"]
    assert update["role"] == "user" and text.startswith("## Since your last turn (09:00)")
    assert "#3 by writer" in text and "Third post" in text
    assert "#1 by writer" in text and "editor: approved" in text  # its own review, confirmed
    assert "#2 is no longer open: it is rejected" in text
    assert "Plan text" not in text  # unchanged, already above
    assert text.endswith(clock_line(world, "editor"))
    assert world.store.get("continued:2026-10-05") == "1"


@pytest.mark.parametrize("change", ["late", "next day", "too big", "timed out"])
def test_otherwise_the_turn_starts_fresh(env, change):
    world, llm, runner, company = env
    world.store.add_draft("2026-10-05", "First post", "writer")
    if change == "timed out":
        llm.chat = lambda *a, **k: (_ for _ in ()).throw(requests.Timeout())
        runner.run_turn(Turn("editor", [Trigger("heartbeat")]))
        llm.chat = ScriptedLLM.chat.__get__(llm)
    else:
        editor_turn(runner, ["looked"], llm)
    minutes = {"late": 16, "next day": 24 * 60}.get(change, 5)
    if change == "too big":
        world.cfg.agents.continue_max_chars = 10
    messages, outcome = editor_turn(runner, ["again"], llm, minutes_later=minutes, world=world)
    assert outcome.startswith("fresh") and len(messages) == 3  # system, situation, reply


def test_migrates_the_fixed_pipeline_database(tmp_path):
    db = sqlite3.connect(tmp_path / "old.sqlite3")
    db.executescript("""
        CREATE TABLE drafts (id INTEGER PRIMARY KEY, day TEXT NOT NULL, slot INTEGER NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL, revisions INTEGER NOT NULL DEFAULT 0, feedback TEXT, topic TEXT NOT NULL,
            msg_id INTEGER, review_msg_id INTEGER, x_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            published_at TEXT);
        INSERT INTO drafts VALUES (1, 'd', 1, 'a', 'awaiting_ceo', 1, NULL, 't', 100, 101, NULL, 'c', 'u', NULL);
        INSERT INTO drafts VALUES (2, 'd', 2, 'b', 'dropped', 3, NULL, 't', 102, NULL, NULL, 'c', 'u', NULL);
        INSERT INTO drafts VALUES (3, 'd', 3, 'c', 'published', 0, NULL, 't', 103, NULL, NULL, 'c', 'u', 'p');
    """)
    db.close()
    store = Store(tmp_path / "old.sqlite3")
    d1 = store.draft(1)
    assert (d1.status, d1.editor_ok, d1.ceo_ok) == ("draft", 1, 0)
    assert store.draft(2).status == "rejected"
    assert store.draft(3).status == "published"
    assert store.draft_by_message(101).id == 1
    new = store.draft(store.add_draft("e", "new", "writer"))
    assert new.topic == "draft #4" and new.editor_ok == 0
