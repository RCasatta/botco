"""The session engine: one pi session per task, in a sandbox.

Botco builds the prompt from the task, its comments and its referenced
issues, starts pi in non-interactive mode, waits for it to exit and posts
its final message as a comment on the task, followed by the files changed
in the workspace. A session has no tracker access while it runs, so it
cannot create tasks or approve anything; patches stay in the workspace for a
person to review.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

from .config import SessionEngine
from .external import summary
from .policy import parse_ref
from .store import Task
from .tracker import Tracker
from .world import World

log = logging.getLogger(__name__)

SUMMARY_CHARS = 8000
LISTED_FILES = 40


@dataclass
class SessionJob:
    account: str
    task: int
    reason: str = ""

    @property
    def agent(self) -> str:
        return self.account


@dataclass
class SessionDone:
    account: str
    task: int
    outcome: str


@dataclass
class Running:
    job: SessionJob
    proc: subprocess.Popen
    unit: str | None
    started: float
    stopped_by: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def files(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.lstat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def changed_files(before: dict, after: dict) -> list[str]:
    out = [f"{p} (new)" for p in sorted(after) if p not in before]
    out += [p for p in sorted(after) if p in before and before[p] != after[p]]
    out += [f"{p} (deleted)" for p in sorted(before) if p not in after]
    return out


def _persona(name: str | None) -> str:
    if not name:
        return ""
    f = resources.files("botco").joinpath(f"prompts/{name}.md")
    return f.read_text() if f.is_file() else ""


class Sessions:
    def __init__(self, world: World):
        self.w = world
        self.running: dict[int, Running] = {}

    def engine(self, account: str) -> SessionEngine:
        e = self.w.cfg.accounts[account].engine
        assert e is not None and e.kind == "session"
        return e

    def workspace(self, account: str, task: int) -> Path:
        p = Path(self.engine(account).workspace.format(task=task))
        return p if p.is_absolute() else self.w.cfg.state_dir / p

    def agent_dir(self, task: int) -> Path:
        return self.w.cfg.state_dir / "pi" / str(task)

    def mounts(self, e: SessionEngine) -> list[tuple[str, str]]:
        out = []
        for spec in e.ro:
            src, _, dst = spec.partition(":")
            out.append((src, dst or src))
        if self.w.lab is not None:
            lab = self.w.cfg.sources.lab
            out.append((str(lab.host_dir or lab.dir), "/run/botco-lab"))
        return out

    def prompt(self, account: str, t: Task) -> str:
        """Call holding w.lock."""
        w, e = self.w, self.engine(account)
        parts = []
        if persona := _persona(e.persona):
            parts.append(persona)
        parts.append(f"# Task #{t.id} ({t.kind}): {t.title}\nCreated by {t.author}, assigned to {t.assignee}."
                     + (f" Refs: {', '.join(t.refs)}." if t.refs else "") + (f"\n\n{t.body}" if t.body else ""))
        comments = w.store.comments(t.id, 30)
        if comments:
            parts.append("## Comments, oldest first\n" + "\n\n".join(f"{c.author} ({c.at[:16]}):\n{c.text}"
                                                                    for c in comments))
        issues = []
        for r in t.refs:
            ref = parse_ref(r)
            if ref.external and (cached := w.store.external(ref.text)):
                issues.append(summary(cached, 4000))
            elif ref.kind == "task" and (other := w.store.task(ref.task)):
                issues.append(f"#{other.id} [{other.status}] {other.title}\n{other.body[:2000]}")
        if issues:
            parts.append("## Referenced issues\n" + "\n\n".join(issues))
        ws = self.workspace(account, t.id)
        mounts = self.mounts(e)
        sandbox = [f"Your working directory is {ws}. It is kept between sessions on this task, so earlier work "
                   "is still there."]
        if mounts:
            sandbox.append("Read-only: " + ", ".join(dst for _, dst in mounts)
                           + (" (/run/botco-lab is the lab notebook)." if w.lab is not None else "."))
        sandbox.append("Internet access: " + ("yes." if e.network else "no, only the model server."))
        if not e.gpu:
            sandbox.append("There is no GPU here: do not load models or run benchmarks; people run benchmarks.")
        parts.append("## Your sandbox\n" + " ".join(sandbox))
        parts.append(
            "## How to finish\nYou cannot reach the tracker or the team chat while you work. When you stop, your "
            f"final message is posted as a comment on #{t.id}, followed by the list of files you changed. Make it "
            "a summary for the team: what you did, what you found with the evidence, what is left, and what a "
            "person should check. Patches stay in your working directory: you cannot push or post anywhere.")
        return "\n\n".join(parts)

    def _models_json(self, e: SessionEngine) -> str:
        m = self.w.cfg.models[e.model]
        key = os.environ.get(m.api_key_env) or "none"
        return json.dumps({"providers": {"botco": {
            "baseUrl": m.url, "api": "openai-completions", "apiKey": key,
            "models": [{"id": m.id, "name": m.id, "reasoning": True, "contextWindow": 262144, "maxTokens": 32768}],
        }}}, indent=2)

    def command(self, account: str, task: int, prompt: str) -> tuple[list[str], dict, str | None]:
        """argv, environment and systemd unit of a session."""
        e, w = self.engine(account), self.w
        ws, agent = self.workspace(account, task), self.agent_dir(task)
        m = w.cfg.models[e.model]
        exe = shutil.which(e.cmd) or e.cmd
        pi = [exe, "-p", "--provider", "botco", "--model", m.id, "--no-session", "--", prompt]
        env = {"PI_CODING_AGENT_DIR": str(agent), "HOME": str(ws), "PI_OFFLINE": "1", "PI_TELEMETRY": "0"}
        if not e.gpu:
            env["CUDA_VISIBLE_DEVICES"] = ""
        if not e.sandbox:
            return pi, {**os.environ, **env}, None
        unit = f"botco-session-{task}-{int(time.time())}"
        props = [
            f"User={getpass.getuser()}", f"RuntimeMaxSec={e.max_minutes * 60}",
            "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "NoNewPrivileges=yes",
            f"ReadWritePaths={ws} {agent}",
        ]
        if not e.gpu:
            props.append("PrivateDevices=yes")
        if not e.network:
            host = urlparse(m.url).hostname or "localhost"
            try:
                addr = socket.gethostbyname(host)
            except OSError:
                addr = host
            props += ["IPAddressDeny=any", f"IPAddressAllow=localhost {addr}"]
        for src, dst in self.mounts(e):
            props.append(f"BindReadOnlyPaths={src}:{dst}")
        argv = ["systemd-run", f"--unit={unit}", "--quiet", "--wait", "--pipe", "--collect",
                f"--working-directory={ws}"]
        for p in props:
            argv += ["-p", p]
        for k, v in {"PATH": os.environ.get("PATH", ""), **env}.items():
            argv += ["-E", f"{k}={v}"]
        return argv + pi, dict(os.environ), unit

    def run(self, job: SessionJob) -> str:
        w = self.w
        e = self.engine(job.account)
        with w.lock:
            t = w.store.task(job.task)
            if t is None:
                return "no such task"
            prompt = self.prompt(job.account, t)
        ws, agent = self.workspace(job.account, job.task), self.agent_dir(job.task)
        ws.mkdir(parents=True, exist_ok=True)
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "models.json").write_text(self._models_json(e))
        before = files(ws)
        argv, env, unit = self.command(job.account, job.task, prompt)
        log.info("session on #%d for %s starts%s", job.task, job.account, f" as {unit}" if unit else "")
        # Its own process group, so stopping it stops what it started.
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                cwd=None if unit else ws, start_new_session=True)
        run = Running(job, proc, unit, time.monotonic())
        self.running[job.task] = run
        timed_out = False
        try:
            out, err = proc.communicate(timeout=e.max_minutes * 60 + 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(run)
            out, err = proc.communicate()
        finally:
            self.running.pop(job.task, None)
        minutes = (time.monotonic() - run.started) / 60
        changed = changed_files(before, files(ws))
        final = (out or "").strip()
        if run.stopped_by:
            head, outcome = f"Session stopped by {run.stopped_by} after {minutes:.0f} minutes.", "stopped"
        elif timed_out or (unit and proc.returncode != 0 and minutes >= e.max_minutes):
            head, outcome = f"Session hit its limit of {e.max_minutes} minutes.", "timed out"
        elif proc.returncode != 0:
            tail = (err or "").strip()[-1500:]
            head, outcome = f"Session failed (exit {proc.returncode}) after {minutes:.0f} minutes.", "failed"
            if tail:
                head += f"\n```\n{tail}\n```"
        else:
            head, outcome = "", "done"
        text = "\n\n".join(p for p in [
            head,
            final[:SUMMARY_CHARS] + ("\n[cut]" if len(final) > SUMMARY_CHARS else "") if final else
            ("(no final message)" if outcome == "done" else ""),
            (f"Files changed in the workspace (`{ws}`):\n" + "\n".join(f"- {f}" for f in changed[:LISTED_FILES])
             + (f"\n- and {len(changed) - LISTED_FILES} more" if len(changed) > LISTED_FILES else ""))
            if changed else "No files changed in the workspace.",
        ] if p)
        with w.lock:
            t = w.store.task(job.task)
            Tracker(w).comment(job.account, t, text, check=False)
        return f"{outcome}, {minutes:.0f} min, {len(changed)} files changed"

    def _kill(self, run: Running) -> None:
        if run.unit:
            subprocess.run(["systemctl", "stop", run.unit], capture_output=True, timeout=60)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if run.proc.poll() is not None:
                return
            try:
                os.killpg(run.proc.pid, sig)
                run.proc.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def stop(self, task: int, by: str) -> bool:
        run = self.running.get(task)
        if run is None:
            return False
        run.stopped_by = by
        threading.Thread(target=self._kill, args=(run,), daemon=True).start()
        return True
