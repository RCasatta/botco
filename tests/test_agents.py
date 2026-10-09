"""The task engine end to end, with fake Zulip, fake issue trackers, a
scripted model, a fake pi and a fixed clock."""

import json
import sqlite3
import stat
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from botco.agents import Runner, TurnDone, clock_line, fingerprint_key, situation, system_prompt
from botco.breakers import Breakers
from botco.company import Company
from botco.config import Breakers as BreakerConfig, parse
from botco.external import ExternalError
from botco.policy import parse_ref
from botco.sessions import SessionDone, SessionJob
from botco.store import External, Store
from botco.tools import TOOLS, Trigger, Turn, fix_mentions
from botco.tracker import Refused, Tracker
from botco.world import World
from botco.xclient import DryRunX

TZ = ZoneInfo("Europe/Rome")
OWNER = 1
IDS = {"strategist": 9, "writer": 10, "editor": 11, "publisher": 13}
TURN = ("strategist", "writer", "editor")


class FakeTeam:
    """Records what bots send, and turns it into the events Zulip would echo."""

    def __init__(self):
        self.breakers = Breakers(BreakerConfig())
        self.bots = dict(IDS)
        self.bot_ids = set(IDS.values())
        self.people = {"riccardo": (OWNER, "Riccardo")}
        self.events = []  # drained by pump()
        self.log = []
        self.reactions = []
        self.next_id = 1000

    def names(self):
        return dict(IDS) | {"Riccardo": OWNER}

    def account_of(self, x):
        for name, uid in IDS.items():
            if x in (name, uid):
                return name
        return "riccardo" if x in (OWNER, "Riccardo") else None

    def mention(self, account):
        return {"riccardo": "@**Riccardo**"}.get(account, f"@**{account}**" if account in IDS else account)

    def is_bot(self, uid):
        return uid in self.bot_ids

    def send(self, persona, stream, topic, content, check=True):
        if check and (why := self.breakers.allow(persona, stream, topic)):
            from botco.team import Blocked
            raise Blocked(why)
        self.next_id += 1
        event = message(IDS[persona], stream, topic, content, self.next_id, persona)
        self.events.append(event)
        self.log.append(event)
        return self.next_id

    def notify(self, text, topic="alerts"):
        self.send("publisher", "ops", topic, text, check=False)

    def react(self, persona, msg_id, emoji):
        self.reactions.append((persona, msg_id, emoji))

    def history(self, stream=None, topic=None, n=30):
        return []

    def sent(self, persona=None, stream=None, topic=None):
        return [e["message"] for e in self.log
                if (persona is None or e["message"]["sender_id"] == IDS[persona])
                and (stream is None or e["message"]["display_recipient"] == stream)
                and (topic is None or e["message"]["subject"] == topic)]


class FakeIssues:
    def __init__(self):
        self.issues: dict[str, External] = {}
        self.fetched = 0

    def fetch(self, ref):
        self.fetched += 1
        if ref.text not in self.issues:
            raise ExternalError("not found")
        return self.issues[ref.text]

    def search(self, query):
        return [(r, e.state, e.title) for r, e in self.issues.items() if query.lower() in e.title.lower()]


def message(sender_id, stream, topic, content, msg_id, name="Riccardo"):
    return {"type": "message", "message": {
        "type": "stream", "sender_id": sender_id, "sender_full_name": name, "display_recipient": stream,
        "subject": topic, "content": content, "id": msg_id, "timestamp": 0}}


def call(name, **args):
    return {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM:
    """Plays each account from a list of replies: a list of tool calls, or a
    final text."""

    def __init__(self):
        self.script = {a: [] for a in TURN}
        self.seen = {a: [] for a in TURN}

    def healthy(self):
        return True

    def chat(self, account, engine, messages, tools=None):
        self.seen[account].append(messages)
        step = self.script[account].pop(0)
        thought = f"thinking before step {len(self.seen[account])}"
        if isinstance(step, str):
            return {"role": "assistant", "content": step, "reasoning_content": thought, "tool_calls": []}
        return {"role": "assistant", "content": "", "reasoning_content": thought, "tool_calls": step}


class Clock(World):
    at = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)  # a Monday

    def now(self):
        return self.at


FAKE_PI = """#!/bin/sh
test -f "$PI_CODING_AGENT_DIR/models.json" || exit 3
case "$*" in *"sleep please"*) sleep 30;; esac
echo "patched" > fix.txt
echo "Reproduced the crash and fixed it in fix.txt."
test -f "$PI_CODING_AGENT_DIR/AGENTS.md" && echo "Read the owner's AGENTS.md."
test -e "$PI_CODING_AGENT_DIR/auth.json" && echo "LEAKED auth.json"
true
"""


def turn(name, persona=None, **extra):
    return {"roles": [name] if name != "strategist" else ["strategist", "writer"], "zuliprc": "x",
            "engine": {"kind": "turn", "persona": persona or name, "model": "local", **extra}}


def raw_config(tmp_path: Path) -> dict:
    pi = tmp_path / "fake-pi"
    pi.write_text(FAKE_PI)
    pi.chmod(pi.stat().st_mode | stat.S_IEXEC)
    return {
        "state_dir": str(tmp_path),
        "models": {"local": {"url": "http://llm/v1", "id": "qwen", "slots": 2, "session_slots": 1}},
        "accounts": {
            "riccardo": {"roles": ["owner"], "zulip": "r@example.com"},
            "strategist": turn("strategist"), "writer": turn("writer"), "editor": turn("editor"),
            "dev": {"roles": ["dev"], "engine": {"kind": "session", "cmd": str(pi), "model": "local",
                                                 "persona": "dev", "sandbox": False, "max_minutes": 1}},
            "publisher": {"roles": [], "zuliprc": "x"},
        },
        "roles": {"owner": {"includes": ["editor", "strategist", "writer"], "unaddressed": "strategist",
                            "priority": 0},
                  "strategist": {}, "writer": {}, "editor": {}, "dev": {}},
        "kinds": {
            "post": {"stream": "drafts", "create": ["writer", "strategist"], "edit": ["writer", "strategist"],
                     "validate": "x_post", "approve": ["editor", "owner"], "max_revisions": 3, "sink": "x"},
            "plan": {"stream": "plan", "create": ["strategist"], "edit": ["strategist"], "one_open": True},
            "task": {"stream": "tasks", "create": ["strategist", "owner"]},
        },
        "schedules": {
            "weekly-plan": {"cron": "0 8 * * MON", "kind": "plan", "assignee": "strategist",
                            "title": "Plan for week {iso_week}", "body": "Set three goals."},
            "triage": {"cron": "0 9 * * *", "kind": "task", "assignee": "strategist", "title": "Triage {date}"},
        },
        "dispatcher": {"activity_log": False},
    }


@pytest.fixture
def make(tmp_path: Path):
    def build(**changes):
        raw = raw_config(tmp_path)
        for path, value in changes.items():
            node = raw
            *parents, leaf = path.split(".")
            for p in parents:
                node = node.setdefault(p, {})
            node[leaf] = value
        cfg = parse(raw, tmp_path)
        world = Clock(cfg, Store(tmp_path / "db.sqlite3", owner="riccardo"), FakeTeam(), DryRunX(),
                      issues=FakeIssues())
        llm = ScriptedLLM()
        runner = Runner(world, {"local": llm})
        company = Company(world, runner)
        company.last_turn = {a: world.now() for a in TURN}  # no heartbeats unless a test moves the clock
        return world, llm, runner, company
    return build


@pytest.fixture
def env(make):
    return make()


def pump(world, runner, company):
    """Deliver Zulip echoes and tracker events, and run queued jobs until
    nothing moves."""
    while True:
        while world.team.events or not world.inbox.empty():
            while world.team.events:
                company.handle(world.team.events.pop(0))
            while not world.inbox.empty():
                company.handle(world.inbox.get())
        company.schedule()
        if not runner.queued:
            return
        job = runner.queued.pop(0)
        if isinstance(job, Turn):
            steps, outcome = runner.run_turn(job)
            company.handle(TurnDone(job.agent, steps, outcome))
        else:
            company.handle(SessionDone(job.account, job.task, runner.sessions.run(job)))


def post(world, text="A 6-bit KV cache roughly halves its VRAM and barely changes quality.", author="writer"):
    t, _ = Tracker(world).create(author, "post", body=text, depth=0)
    return t


def waits(world, task_id):
    return world.policy.waits_on(world.store, world.store.task(task_id))


def drain(world, company):
    while not world.inbox.empty():
        company.handle(world.inbox.get())


def tool_results(llm, account):
    return [m["content"] for m in llm.seen[account][-1] if m["role"] == "tool"]


# the post lifecycle

def test_owner_asks_in_words_and_the_team_delivers(env):
    world, llm, runner, company = env
    llm.script["strategist"] = [
        [call("comment", ref="zulip:drafts/requests",
              text="@writer the owner wants a post about KV cache quantization")],
        "delegated",
    ]
    llm.script["writer"] = [
        [call("write", kind="post", body="A 6-bit KV cache roughly halves its VRAM and barely changes quality.",
              note="source: KV-CACHE.md")],
        "drafted",
    ]
    llm.script["editor"] = [[call("comment", ref="#1", text="Clear and accurate.", verdict="approve")], "reviewed"]

    company.handle(message(OWNER, "general", "ideas", "make another post about KV cache quantization", 1))
    pump(world, runner, company)
    assert "Riccardo (a person, riccardo) wrote" in llm.seen["strategist"][0][1]["content"]
    assert "#1: created (by writer)" in llm.seen["editor"][0][1]["content"], "the new post woke the editor"
    w = waits(world, 1)
    assert w.accounts == ["riccardo"] and "owner" in w.why
    # The owner is mentioned once in the post's topic, with how to answer.
    (asked,) = [m for m in world.team.sent("publisher", "drafts", "post #1") if "@**Riccardo**" in m["content"]]
    assert "/approve" in asked["content"]

    company.handle(message(OWNER, "drafts", "post #1", "/approve", 2))
    pump(world, runner, company)
    assert waits(world, 1).ready
    assert world.team.reactions == [("publisher", 2, "check")]
    assert world.team.sent("publisher", "drafts", "post #1")[-1]["content"] == "Recorded: riccardo **approve** #1."

    world.at = world.at.replace(hour=10, minute=1)
    company.tick()
    (p,) = world.store.published()
    assert p.id == 1 and p.x_id == "dry-run" and p.resolution == "done"
    assert world.team.sent("publisher", "published")
    assert not any(llm.script.values()), "every scripted step was used"


def test_a_body_edit_cancels_approvals_and_revisions_escalate(env):
    world, llm, runner, company = env
    tracker = Tracker(world)
    t = post(world)
    assert waits(world, t.id).accounts == ["editor"]
    tracker.review("editor", t, "approve", "fine")
    assert waits(world, t.id).accounts == ["riccardo"]
    tracker.edit("writer", world.store.task(t.id), body="A 6-bit KV cache halves its VRAM at almost no quality cost.")
    assert waits(world, t.id).accounts == ["editor"], "the edit sent it back to the editor"
    for n in range(3):
        tracker.review("editor", world.store.task(t.id), "revise", f"round {n}")
        if n < 2:
            assert waits(world, t.id).accounts == ["writer"]
            tracker.edit("writer", world.store.task(t.id), body=f"Version {n}: quantizing the KV cache to 6 bits "
                                                                   "halves it, and quality barely moves.")
    w = waits(world, t.id)
    assert w.accounts == ["riccardo"] and "3 revisions" in w.why


def test_an_owner_approval_fills_every_role_it_includes(env):
    world, *_ = env
    t = post(world)
    Tracker(world).review("riccardo", t, "approve")
    assert waits(world, t.id).ready


def test_a_person_s_words_count_only_from_the_message_that_woke_the_agent(env):
    world, llm, runner, company = env
    t = post(world)
    Tracker(world).review("editor", t, "approve")
    drain(world, company)
    llm.script["strategist"] = [
        [call("comment", ref="#1", text="Recorded.", verdict="approve", on_behalf_of=999),
         call("comment", ref="#1", text="I like it", verdict="approve"),
         call("comment", ref="#1", text="Recorded.", verdict="approve", on_behalf_of=50)],
        "done",
    ]
    # In the post's topic, waiting on the owner themselves: the comment wakes
    # nobody, so it goes where the owner's role routes it.
    company.handle(message(OWNER, "drafts", "post #1", "this one is good, ship it", 50))
    pump(world, runner, company)
    first, second, third = tool_results(llm, "strategist")
    assert "message that woke you: 50" in first
    assert "may not review" in second
    assert third.startswith("#1: approve recorded")
    review = world.store.reviews(1)[-1]
    assert (review.account, review.recorded_by, review.msg_id) == ("riccardo", "strategist", 50)
    assert any("Recorded: riccardo **approve**" in m["content"] and "ship it" in m["content"]
               for m in world.team.sent("publisher"))
    assert world.store.comments(1)[-1].text == "this one is good, ship it", "the message is a comment on #1"
    assert waits(world, 1).ready


def test_the_policy_refuses_what_a_role_may_not_do(env):
    world, llm, runner, company = env
    post(world)
    llm.script["writer"] = [
        [call("comment", ref="#1", text="self-approval", verdict="approve"),
         call("write", ref="#1", state="done"),
         call("write", kind="plan", title="My plan"),
         call("write", kind="post", body="Read more at https://example.com"),
         call("write", ref="gh:a/b#1", body="x")],
        "gave up",
    ]
    llm.script["editor"] = [[call("write", ref="#1", body="The editor rewrites it.")], "ok"]
    runner.run_turn(Turn("writer", [Trigger("heartbeat")]))
    runner.run_turn(Turn("editor", [Trigger("heartbeat")]))
    approve, done, plan, link, external = tool_results(llm, "writer")
    assert "may not review post" in approve
    assert "closed as done by the x publisher" in done
    assert "may not create plan" in plan
    assert "link" in link and "not saved" in link
    assert "read-only" in external
    assert "may not edit post" in tool_results(llm, "editor")[0]
    assert len(world.store.open_tasks()) == 1


def test_a_reaction_records_the_reacting_person_s_review(env):
    world, llm, runner, company = env
    t = post(world)
    msg_id = world.team.log[-1]["message"]["id"]
    company.handle({"type": "reaction", "op": "add", "user_id": 77, "message_id": msg_id, "emoji_name": "check"})
    assert not world.store.reviews(t.id), "someone without an account counts for nothing"
    company.handle({"type": "reaction", "op": "add", "user_id": OWNER, "message_id": msg_id, "emoji_name": "x"})
    assert world.store.task(t.id).resolution == "rejected"


# guardrails on creating tasks

def test_creation_is_limited_by_depth_ref_and_quota(make):
    world, llm, runner, company = make(**{"dispatcher.max_new_tasks_per_day": 3})
    world.issues.issues["gh:a/b#7"] = External("gh:a/b#7", "Crash on load", "open", "https://gh/7", "It crashes")
    tracker = Tracker(world)
    parent, _ = tracker.create("strategist", "task", "Look into it", depth=1)
    llm.script["strategist"] = [
        [call("write", title="Follow-up of a follow-up"),
         call("write", title="Fix the crash", refs=["gh:a/b#7"], assignee="dev"),
         call("write", title="Fix the crash again", refs=["gh:a/b#7"]),
         call("write", title="Unrelated", refs=["gh:a/b#8"]),
         call("write", title="One too many", refs=["gh:a/b#9"])],
        "done",
    ]
    runner.run_turn(Turn("strategist", [Trigger("task", task=parent.id, content="created")]))
    deep, first, dup, third, quota = tool_results(llm, "strategist")
    assert "depth 2" in deep
    assert first.startswith("created #2")
    assert "#2 is already the open task for gh:a/b#7" in dup
    assert third.startswith("created #3")
    assert "already created 3 tasks today" in quota
    assert world.store.task(2).depth == 0, "a task for an external issue starts at depth 0"


# schedules

def test_a_schedule_creates_the_weekly_plan_and_replaces_the_old_one(env):
    world, llm, runner, company = env
    world.at = world.at.replace(hour=7, minute=59)
    company.tick()  # first start: nothing in the past fires
    assert not world.store.open_tasks()
    world.at = world.at.replace(hour=8, minute=0, second=10)
    company.tick()
    (plan,) = world.store.open_tasks("plan")
    assert (plan.title, plan.assignee, plan.depth, plan.schedule) == ("Plan for week 2026-W41", "strategist", 0,
                                                                     "weekly-plan")
    drain(world, company)
    assert company.pending["strategist"][0].task == plan.id, "the new task wakes its assignee"
    company.tick()
    assert len(world.store.open_tasks("plan")) == 1, "it fires once"

    world.at = world.at + timedelta(days=7)
    company.tick()
    (new,) = world.store.open_tasks("plan")
    assert new.id != plan.id and world.store.task(plan.id).resolution == "replaced"
    text = situation(world, Turn("writer", [Trigger("heartbeat")]))
    assert "## Current plan: #" in text and "Set three goals." in text


def test_a_schedule_skips_while_its_last_task_is_open(env):
    world, *_, company = env
    world.at = world.at.replace(hour=8, minute=59)
    company.tick()
    world.at = world.at.replace(hour=9, minute=0, second=5)
    company.tick()
    world.at = world.at + timedelta(days=1)
    company.tick()
    assert [t.title for t in world.store.open_tasks("task")] == ["Triage 2026-10-05"]


# the session engine

def test_a_session_works_on_its_task_and_reports_a_summary(env):
    world, llm, runner, company = env
    llm.script["strategist"] = [
        [call("write", title="Reproduce the crash", body="Load the model with 2 GPUs.", assignee="dev")],
        "assigned",
        "read the summary",
    ]
    company.handle(message(OWNER, "ops", "chat", "someone should reproduce the crash", 5))
    pump(world, runner, company)
    (task,) = world.store.open_tasks("task")
    summary = world.store.comments(task.id)[-1]
    assert summary.author == "dev" and "Reproduced the crash" in summary.text
    assert "fix.txt (new)" in summary.text
    assert (Path(world.cfg.state_dir) / f"work/{task.id}/fix.txt").exists()
    prompt = runner.sessions.prompt("dev", task)
    assert "Load the model with 2 GPUs." in prompt and "never grep or find from their top" not in prompt
    world.cfg.accounts["dev"].engine.ro = ["/srv"]
    assert "never grep or find from their top" in runner.sessions.prompt("dev", task)
    # The author hears that the summary is in; dev does not wake itself.
    assert "dev's session ended (done" in llm.seen["strategist"][-1][-2]["content"]
    assert not company.session_queue
    # dev has no Zulip account: the publisher posts for it, naming it.
    assert any(m["content"].startswith("**dev**: Reproduced") for m in world.team.sent("publisher", "tasks"))
    # A new comment on the task starts a new session for that task.
    company.handle(message(OWNER, "tasks", task.topic, "also try one GPU", 6))
    assert ("dev", task.id) in company.session_queue


def test_a_session_starts_from_a_person_s_pi_config(env, tmp_path):
    world, llm, runner, company = env
    own = tmp_path / "home-pi"
    (own / "skills" / "demo").mkdir(parents=True)
    (own / "AGENTS.md").write_text("Use rg.")
    (own / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n")
    (own / "auth.json").write_text("{}")
    world.cfg.accounts["dev"].engine.pi_config = str(own)
    t, _ = Tracker(world).create("riccardo", "task", "Check the config", assignee="dev")
    runner.sessions.run(SessionJob("dev", t.id))
    text = world.store.comments(t.id)[-1].text
    assert "Read the owner's AGENTS.md." in text and "LEAKED" not in text
    agent = runner.sessions.agent_dir(t.id)
    assert (agent / "skills" / "demo" / "SKILL.md").exists() and json.loads((agent / "models.json").read_text())["providers"]["botco"]
    assert "botco-pi-config" not in runner.sessions.prompt("dev", t)
    # Server instructions replace the person's own.
    server = tmp_path / "server-agents.md"
    server.write_text("You run on a server.")
    world.cfg.accounts["dev"].engine.agents_md = str(server)
    runner.sessions.run(SessionJob("dev", t.id))
    assert (agent / "AGENTS.md").read_text() == "You run on a server." and (agent / "skills" / "demo").exists()


def test_a_person_can_stop_a_session(env):
    world, llm, runner, company = env
    t, _ = Tracker(world).create("riccardo", "task", "Long job", "sleep please", assignee="dev")
    worker = threading.Thread(target=lambda: runner.sessions.run(SessionJob("dev", t.id)))
    worker.start()
    for _ in range(100):
        if t.id in runner.sessions.running:
            break
        time.sleep(0.05)
    company.handle(message(OWNER, "tasks", t.topic, "/stop", 7))
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert "Session stopped by riccardo" in world.store.comments(t.id)[-1].text
    assert any("Stopping the session" in m["content"] for m in world.team.sent("publisher"))


def test_sessions_left_by_an_earlier_botco_are_stopped_and_started_again(env):
    world, llm, runner, company = env
    t, _ = Tracker(world).create("riccardo", "task", "Long job", assignee="dev")
    drain(world, company)
    company.session_queue.clear()
    calls = []
    runner.sessions.systemctl = lambda *a: calls.append(a) or (
        f"botco-session-{t.id}-1791554809.service loaded active running [systemd-run] pi\n" if a[0] == "list-units" else "")
    assert runner.sessions.stop_leftovers() == [], "unsandboxed sessions die with botco"
    world.cfg.accounts["dev"].engine.sandbox = True
    company.restart_sessions(runner.sessions.stop_leftovers())
    assert ("stop", f"botco-session-{t.id}-1791554809.service") in calls
    assert "interrupted by a botco restart" in world.store.comments(t.id)[-1].text
    assert ("dev", t.id) in company.session_queue


def test_sessions_use_only_their_slot(env):
    world, llm, runner, company = env
    company.running_sessions[1] = ("dev", "local")
    assert company.free("local", session=False), "a turn still has a slot"
    assert not company.free("local", session=True), "a second session waits"
    company.running["writer"] = "local"
    assert not company.free("local", session=False)


def test_the_sandbox_command(env):
    world, llm, runner, company = env
    world.cfg.accounts["dev"].engine.sandbox = False
    argv, envs, unit = runner.sessions.command("dev", 4, "do it")
    assert unit is None and argv[-2:] == ["--", "do it"] and "--no-session" in argv
    world.cfg.accounts["dev"].engine.skills = ["/home/me/skills", "/opt/skills"]
    argv, envs, unit = runner.sessions.command("dev", 4, "do it")
    assert argv[-6:-2] == ["--skill", "/home/me/skills", "--skill", "/opt/skills"]
    world.cfg.accounts["dev"].engine.sandbox = True
    world.cfg.accounts["dev"].engine.network = False
    world.cfg.accounts["dev"].engine.ro = ["/srv/repos"]
    world.cfg.accounts["dev"].engine.hide = ["/srv/repos/.ssh", "/srv/repos/.pi"]
    world.cfg.accounts["dev"].engine.groups = ["users"]
    argv, envs, unit = runner.sessions.command("dev", 4, "do it")
    assert argv[0] == "systemd-run" and unit.startswith("botco-session-4-")
    props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert "PrivateDevices=yes" in props and "IPAddressDeny=any" in props
    assert "BindReadOnlyPaths=/srv/repos:/srv/repos" in props
    assert "GIT_CONFIG_VALUE_0=*" in [argv[i + 1] for i, a in enumerate(argv) if a == "-E"]
    assert "InaccessiblePaths=-/srv/repos/.ssh -/srv/repos/.pi" in props and "SupplementaryGroups=users" in props
    world.cfg.accounts["dev"].engine.skills = ["/srv/skills"]
    argv, envs, unit = runner.sessions.command("dev", 4, "do it")
    assert "BindReadOnlyPaths=/srv/skills:/srv/skills" in [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    world.cfg.accounts["dev"].engine.pi_config = "/srv/repos/.pi/agent"
    argv, envs, unit = runner.sessions.command("dev", 4, "do it")
    props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert "BindReadOnlyPaths=/srv/repos/.pi/agent:/run/botco-pi-config" in props
    assert any(p.startswith("InaccessiblePaths=") and "-/run/botco-pi-config/sessions" in p for p in props)
    i = argv.index("botco-pi-config")
    assert argv[i - 2] == "-c" and argv[i + 1] == "/run/botco-pi-config" and argv[i + 2].endswith("fake-pi")
    assert any(p.startswith("ReadWritePaths=") and "work/4" in p for p in props)


# external issues

def test_a_change_on_a_referenced_issue_wakes_whoever_the_task_waits_on(env):
    world, llm, runner, company = env
    issues = world.issues
    issues.issues["gh:a/b#7"] = External("gh:a/b#7", "Crash on load", "open", "https://gh/7", "It crashes", [],
                                         "2026-10-05T08:00:00Z")
    t, _ = Tracker(world).create("riccardo", "task", "Look at the crash", refs=["gh:a/b#7"], assignee="strategist")
    drain(world, company)
    company.pending["strategist"].clear()
    company.poll_external()  # first look: cached, nothing to report
    drain(world, company)
    assert not company.pending["strategist"]
    issues.issues["gh:a/b#7"] = External("gh:a/b#7", "Crash on load", "open", "https://gh/7", "It crashes",
                                         [{"author": "bob", "at": "2026-10-05T09:00:00Z", "text": "same here"}],
                                         "2026-10-05T09:00:00Z")
    company.poll_external()
    drain(world, company)
    (trigger,) = company.pending["strategist"]
    assert trigger.task == t.id and "new comment by bob" in trigger.content


def test_find_and_read_by_ref(env, tmp_path):
    world, llm, runner, company = env
    from botco.lab import Lab
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "KV.md").write_text("# KV cache\n\n## Result\n6-bit halves VRAM\n")
    world.lab = Lab(tmp_path / "lab", [])
    world.issues.issues["gh:a/b#7"] = External("gh:a/b#7", "KV cache crash", "open", "https://gh/7", "It crashes")
    post(world)
    llm.script["writer"] = [
        [call("find", query="KV cache"), call("read", ref="gh:a/b#7"), call("read", ref="lab:KV.md#Result"),
         call("read", ref="#1"), call("comment", ref="lab:KV.md", text="nice")],
        "done",
    ]
    runner.run_turn(Turn("writer", [Trigger("heartbeat")]))
    found, issue, lab, task, refused = tool_results(llm, "writer")
    assert "#1 [open] post by writer" in found and "lab:KV.md:1" in found and "gh:a/b#7 [open] KV cache crash" in found
    assert "It crashes" in issue and "No open local task" in issue
    assert "6-bit halves VRAM" in lab
    assert "Waits on: editor" in task and "Topic: zulip:drafts/post #1" in task
    assert "read-only" in refused
    assert parse_ref("12").text == "#12"


# waking and scheduling

def test_who_wakes_whom(env):
    world, llm, runner, company = env
    # The publisher's messages never wake anyone, even with a mention in them.
    company.handle(message(IDS["publisher"], "ops", "activity", "@**writer** write", 4, "publisher"))
    # A bot mentioning itself does not wake itself.
    company.handle(message(IDS["editor"], "drafts", "x", "@**editor** note to self", 5, "editor"))
    # A bot message that mentions nobody wakes nobody.
    company.handle(message(IDS["editor"], "drafts", "x", "just thinking", 6, "editor"))
    assert not any(company.pending.values())
    # A bot mention wakes the mentioned account only.
    company.handle(message(IDS["editor"], "drafts", "x", "@**writer** please revise", 7, "editor"))
    assert {a for a, t in company.pending.items() if t} == {"writer"}
    # An account's own change does not wake it; it wakes who the task waits on.
    post(world)
    drain(world, company)
    assert {a for a, t in company.pending.items() if t} == {"writer", "editor"}
    # Heartbeats once the interval has passed, inside active hours.
    world.at = world.at.replace(hour=10, minute=30)
    company.tick()
    assert all(company.pending[a] for a in TURN)


def test_requests_from_the_owner_run_first(env):
    world, llm, runner, company = env
    company.handle(message(IDS["editor"], "drafts", "x", "@**writer** one", 5, "editor"))
    company.pending["strategist"].append(Trigger("heartbeat"))
    company.handle(message(OWNER, "plan", "x", "@**editor** two", 6))
    world.cfg.models["local"].slots = 1
    company.schedule()
    assert [j.agent for j in runner.queued] == ["editor"]


def test_an_account_over_its_budget_sleeps_until_tomorrow(make):
    world, llm, runner, company = make(**{"roles.writer": {"turns_per_day": 1}})
    llm.script["writer"] = ["ok"]
    company.handle(message(OWNER, "drafts", "x", "@**writer** one", 5))
    pump(world, runner, company)
    company.handle(message(OWNER, "drafts", "x", "@**writer** two", 6))
    pump(world, runner, company)
    assert len(llm.seen["writer"]) == 1
    assert any("writer used its 1 turns today" in m["content"] for m in world.team.sent("publisher", "ops"))
    assert "writer 1/1 (**over budget**" in company.status()


def test_halt_command_stops_turns(env):
    world, llm, runner, company = env
    company.handle(message(OWNER, "ops", "x", "halt", 7))
    assert world.halted.is_set() and world.store.get("halted") == "1"
    company.handle(message(OWNER, "drafts", "x", "@**writer** write something", 8))
    company.schedule()
    assert not runner.queued
    company.handle(message(OWNER, "ops", "x", "resume", 9))
    company.schedule()
    assert runner.queued


def test_a_person_is_mentioned_once_per_state(env):
    world, *_, company = env
    t = post(world)
    Tracker(world).review("editor", t, "approve")
    drain(world, company)
    t = world.store.task(t.id)
    Tracker(world).comment("writer", t, "any news?")
    drain(world, company)
    assert len([m for m in world.team.sent("publisher") if "@**Riccardo**" in m["content"]]) == 1


def test_plain_text_answer_to_a_person_is_delivered(env):
    world, llm, runner, company = env
    llm.script["editor"] = ["Nothing is waiting for review right now."]
    company.handle(message(OWNER, "drafts", "chat", "@**editor** anything to review?", 3))
    pump(world, runner, company)
    (reply,) = world.team.sent("editor")
    assert reply["subject"] == "chat" and "Nothing is waiting" in reply["content"]


def test_plain_text_answer_to_a_bot_is_not_posted(env):
    world, llm, runner, company = env
    llm.script["writer"] = ["Nothing to do."]
    company.handle(message(IDS["editor"], "drafts", "x", "@**writer** fyi", 4, "editor"))
    pump(world, runner, company)
    assert not world.team.sent("writer")


def test_plain_at_mentions_are_fixed(env):
    world, *_ = env
    assert fix_mentions(world, "@writer and @Editor, not @**strategist** or me@writer.com") == \
        "@**writer** and @**Editor**, not @**strategist** or me@writer.com"
    assert fix_mentions(world, "@ceo, @riccardo and @**Riccardo**") == "@**Riccardo**, @**Riccardo** and @**Riccardo**"


# what the model sees, and the prefix cache

def test_situation_shows_what_waits_on_the_account(env):
    world, *_ = env
    post(world)
    text = situation(world, Turn("editor", [Trigger("heartbeat")]))
    mine = text[text.index("## Waiting on you"):text.index("## Other open tasks")]
    assert "#1 post by writer, version 1" in mine and "editor: not yet, owner: not yet" in mine
    text = situation(world, Turn("writer", [Trigger("heartbeat")]))
    assert "## Waiting on you\nNothing." in text and "- #1 post by writer" in text
    assert "There is no open plan." in text


def test_requests_share_their_beginning_across_accounts_and_turns(env):
    """The server reuses its cache up to the first difference, and the chat
    template puts the tools first: system prompt and tools must not depend on
    the account or on why it woke, and the situation must put the account's
    role after the shared sections and the clock last."""
    world, *_ = env
    Tracker(world).create("strategist", "plan", "Plan", "Plan text")
    post(world)
    turns = [Turn("strategist", [Trigger("human")]), Turn("writer", [Trigger("heartbeat")]),
             Turn("editor", [Trigger("bot", "writer")])]
    assert len({system_prompt(world) for _ in turns}) == 1
    assert [t.name for t in TOOLS] == ["find", "read", "write", "comment", "remember"]
    texts = [situation(world, t) for t in turns]
    shared = texts[0][:texts[0].index("## Your role")]
    assert "Plan text" in shared and "## Publishing" in shared
    assert all(t.startswith(shared) for t in texts)
    assert all(t.rindex("It is Monday") > t.index("## Waiting on you") > t.index("## Your role") for t in texts)


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
    assert not runner.queued and not company.pending["strategist"]
    assert world.store.get("skipped:2026-10-05") == "1"
    assert world.store.get("turns:2026-10-05") == "1"  # skips use no budget

    post(world)
    drain(world, company)
    company.pending["editor"].clear()
    llm.script["strategist"] = ["Saw the new post."]
    heartbeat(company)
    assert runner.queued, "a change makes the heartbeat run"
    pump(world, runner, company)
    assert not any(llm.script.values())


def test_own_actions_and_the_trigger_kind_do_not_defeat_skipping(env):
    world, llm, runner, company = env
    llm.script["strategist"] = [[call("remember", note="owner asked about the week; answered")], "done"]
    company.handle(message(OWNER, "plan", "x", "how is the week going?", 9))
    pump(world, runner, company)
    world.at = world.at.replace(hour=10)
    heartbeat(company)
    assert not runner.queued, "its own note and the owner's trigger are not news for the next heartbeat"


def test_an_unfinished_turn_does_not_count_as_seen(env):
    world, llm, runner, company = env

    def timeout(*a, **k):
        raise requests.Timeout()
    llm.chat = timeout
    heartbeat(company)
    pump(world, runner, company)
    assert world.store.get(fingerprint_key("strategist")) is None
    heartbeat(company)
    assert runner.queued, "the next heartbeat runs after a timed-out turn"


def editor_turn(runner, script, llm, minutes_later=None, world=None):
    if minutes_later is not None:
        world.at = world.at + timedelta(minutes=minutes_later)
    llm.script["editor"] = script
    steps, outcome = runner.run_turn(Turn("editor", [Trigger("task", task=1, content="created")]))
    return llm.seen["editor"][-1], outcome


def test_a_turn_soon_after_continues_the_conversation(env):
    world, llm, runner, company = env
    Tracker(world).create("strategist", "plan", "Plan", "Plan text")
    post(world, "First post about KV cache quantization at six bits.")
    post(world, "Second post: speculative decoding acceptance rates matter more than you think.")
    first, outcome = editor_turn(runner, [[call("comment", ref="#2", text="ok", verdict="approve")],
                                          "reviewed #2"], llm)
    assert outcome.startswith("fresh") and len(first) == 2 + 2 + 1  # system, user | call, result, reply
    first_turn = llm.seen["editor"][-1]

    Tracker(world).close("writer", world.store.task(3), "dropped")
    post(world, "Third post: a 27B model at 4 bits fits two 16 GB cards with room for context.")
    second, outcome = editor_turn(runner, ["nothing else"], llm, minutes_later=10, world=world)
    assert outcome.startswith("continued")
    assert second[:len(first_turn)] == first_turn  # the earlier turn, reasoning included, unchanged
    assert first_turn[2]["reasoning_content"].startswith("thinking before step")
    update = second[len(first_turn)]
    text = update["content"]
    assert update["role"] == "user" and text.startswith("## Since your last turn (09:00)")
    assert "#4 post by writer" in text and "Third post" in text
    assert "- #2 post by writer" in text and "Waits on riccardo" in text  # its own review, confirmed
    assert "#3 is closed: dropped" in text
    assert "Plan text" not in text  # unchanged, already above
    assert text.endswith(clock_line(world, "editor"))
    assert world.store.get("continued:2026-10-05") == "1"


@pytest.mark.parametrize("change", ["late", "next day", "too big", "timed out"])
def test_otherwise_the_turn_starts_fresh(env, change):
    world, llm, runner, company = env
    post(world)
    if change == "timed out":
        llm.chat = lambda *a, **k: (_ for _ in ()).throw(requests.Timeout())
        runner.run_turn(Turn("editor", [Trigger("heartbeat")]))
        llm.chat = ScriptedLLM.chat.__get__(llm)
    else:
        editor_turn(runner, ["looked"], llm)
    minutes = {"late": 16, "next day": 24 * 60}.get(change, 5)
    if change == "too big":
        world.cfg.dispatcher.continue_max_chars = 10
    messages, outcome = editor_turn(runner, ["again"], llm, minutes_later=minutes, world=world)
    assert outcome.startswith("fresh") and len(messages) == 3  # system, situation, reply


# migration

def test_migrates_the_fixed_pipeline_database(tmp_path):
    db = sqlite3.connect(tmp_path / "old.sqlite3")
    db.executescript("""
        CREATE TABLE drafts (id INTEGER PRIMARY KEY, day TEXT NOT NULL, slot INTEGER NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL, revisions INTEGER NOT NULL DEFAULT 0, feedback TEXT, topic TEXT NOT NULL,
            msg_id INTEGER, review_msg_id INTEGER, x_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            published_at TEXT);
        INSERT INTO drafts VALUES (1, 'd', 1, 'a', 'awaiting_ceo', 1, NULL, 'draft #1', 100, 101, NULL, 'c', 'u', NULL);
        INSERT INTO drafts VALUES (2, 'd', 2, 'b', 'dropped', 3, NULL, 'draft #2', 102, NULL, NULL, 'c', 'u', NULL);
        INSERT INTO drafts VALUES (3, 'd', 3, 'c', 'published', 0, NULL, 'draft #3', 103, NULL, 'x1', 'c', 'u', 'p');
        CREATE TABLE plans (id INTEGER PRIMARY KEY, week TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO plans VALUES (1, '2026-W40', 'old plan', 'c');
        INSERT INTO plans VALUES (2, '2026-W41', 'this week', 'c');
    """)
    db.close()
    store = Store(tmp_path / "old.sqlite3", owner="riccardo")
    t1 = store.task(1)
    assert (t1.kind, t1.status, t1.body, t1.version, t1.topic) == ("post", "open", "a", 2, "draft #1")
    assert [(r.account, r.verdict) for r in store.reviews(1)] == [("editor", "approve")]
    assert store.task(2).resolution == "rejected"
    t3 = store.task(3)
    assert (t3.resolution, t3.x_id) == ("done", "x1") and store.published()[0].id == 3
    assert store.task_by_message(101).id == 1
    (plan,) = store.open_tasks("plan")
    assert plan.body == "this week" and plan.title == "Plan for week 2026-W41"
    new = store.task(store.add_task("post", "t", "new", "", "writer", "writer", "drafts", [], [], 0))
    assert new.topic == "post #5"
    Store(tmp_path / "old.sqlite3")  # opening it again changes nothing
    assert len(Store(tmp_path / "old.sqlite3").open_tasks()) == 3


def test_refused_is_raised_for_unknown_accounts(env):
    world, *_ = env
    with pytest.raises(Refused, match="no account"):
        Tracker(world).create("strategist", "task", "x", assignee="nobody")


def test_the_coordinator_gets_the_first_heartbeat(tmp_path):
    raw = raw_config(tmp_path)
    raw["accounts"] = dict(sorted(raw["accounts"].items()))  # as Nix writes TOML: editor first
    world = Clock(parse(raw, tmp_path), Store(tmp_path / "db.sqlite3"), FakeTeam(), DryRunX(), issues=FakeIssues())
    company = Company(world, Runner(world, {"local": ScriptedLLM()}))
    assert world.cfg.turn_accounts()[0] == "editor"
    assert min(company.last_turn, key=company.last_turn.get) == "strategist", "people's messages go to it"
