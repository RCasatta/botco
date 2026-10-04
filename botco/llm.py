"""Client for the OpenAI-compatible inference server (TabbyAPI on ripper).

The server is shared and can be busy or down. A busy server just answers
slowly, so requests get a long timeout. A down server is waited for: workers
poll its health with backoff and never drop a job because of it.
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

from .config import LLMConfig, Persona

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMDown(Exception):
    pass


class LLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.session = requests.Session()
        key = os.environ.get(cfg.api_key_env)
        if key:
            self.session.headers["Authorization"] = f"Bearer {key}"

    def healthy(self) -> bool:
        root = self.cfg.base_url.removesuffix("/v1")
        try:
            r = self.session.get(f"{root}/health", timeout=10)
            return r.ok and r.json().get("status") == "healthy"
        except (requests.RequestException, ValueError):
            return False

    def chat(self, persona: Persona, messages: list[dict]) -> str:
        body = {
            "messages": messages,
            "max_tokens": persona.max_tokens,
            "temperature": persona.temperature,
            "top_p": 0.95,
            "top_k": 20,
            "chat_template_kwargs": {"enable_thinking": persona.thinking},
        }
        try:
            r = self.session.post(
                f"{self.cfg.base_url}/chat/completions",
                json=body,
                timeout=(10, self.cfg.timeout),
            )
        except requests.ConnectionError as e:
            raise LLMDown(str(e)) from e
        if r.status_code >= 500:
            raise LLMDown(f"HTTP {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        choice = r.json()["choices"][0]
        text = choice["message"].get("content") or ""
        if choice.get("finish_reason") == "length":
            log.warning("%s hit max_tokens=%d", persona.name, persona.max_tokens)
        return THINK_RE.sub("", text).strip()


@dataclass
class Job:
    """One LLM call. `on_done` runs on the main thread with the reply."""

    key: str
    persona: Persona
    messages: list[dict]
    on_done: Callable[[str], None]
    # Lower runs first: human requests before background pipeline work.
    priority: int = 1
    seq: int = field(default_factory=itertools.count().__next__)


@dataclass
class Done:
    job: Job
    text: str | None
    error: str | None = None


@dataclass
class Notice:
    """Something for #ops, raised from a worker thread."""

    text: str


class Workers:
    """Threads that run queued jobs against the server.

    Results and notices go to `inbox`, which the main thread drains, so all
    state changes and Zulip posts happen on one thread.
    """

    def __init__(self, llm: LLM, inbox: queue.Queue, halted: threading.Event):
        self.llm = llm
        self.inbox = inbox
        self.halted = halted
        self.jobs: queue.PriorityQueue = queue.PriorityQueue()
        self.down = False
        self._lock = threading.Lock()

    def start(self) -> None:
        for i in range(self.llm.cfg.concurrency):
            threading.Thread(target=self._run, name=f"llm-{i}", daemon=True).start()

    def submit(self, job: Job) -> None:
        self.jobs.put((job.priority, job.seq, job))

    def pending(self) -> int:
        return self.jobs.qsize()

    def _set_down(self, down: bool, why: str = "") -> None:
        with self._lock:
            if self.down == down:
                return
            self.down = down
        if down:
            self.inbox.put(Notice(f":warning: Inference server unreachable ({why}). Jobs are kept and will resume."))
        else:
            self.inbox.put(Notice(":check: Inference server is back."))

    def _wait_healthy(self) -> None:
        delay = 15
        while not self.llm.healthy():
            self._set_down(True, "health check failed")
            time.sleep(delay)
            delay = min(delay * 2, 300)
        self._set_down(False)

    def _run(self) -> None:
        while True:
            while self.halted.is_set():
                time.sleep(5)
            _, _, job = self.jobs.get()
            while True:
                self._wait_healthy()
                try:
                    text = self.llm.chat(job.persona, job.messages)
                    self.inbox.put(Done(job, text))
                    break
                except LLMDown as e:
                    log.warning("job %s: server down: %s", job.key, e)
                    self._set_down(True, str(e)[:100])
                except requests.Timeout:
                    # Busy for longer than the timeout. Retrying would only
                    # pile more work on it, so give the job back as failed;
                    # the pipeline schedules it again later.
                    self.inbox.put(Done(job, None, "timed out"))
                    break
                except Exception as e:  # noqa: BLE001 - reported to #ops
                    log.exception("job %s failed", job.key)
                    self.inbox.put(Done(job, None, f"{type(e).__name__}: {e}"[:300]))
                    break
