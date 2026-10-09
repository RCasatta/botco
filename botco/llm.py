"""Client for the OpenAI-compatible inference server (TabbyAPI on ripper).

The server is shared and can be busy or down. A busy server just answers
slowly, so requests get a long timeout. A down server raises LLMDown, and
the agent runner waits for it to come back.
"""

from __future__ import annotations

import logging
import os
import re

import requests

from .config import Model, TurnEngine

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMDown(Exception):
    pass


class LLM:
    def __init__(self, cfg: Model):
        self.cfg = cfg
        self.session = requests.Session()
        key = os.environ.get(cfg.api_key_env)
        if key:
            self.session.headers["Authorization"] = f"Bearer {key}"

    def healthy(self) -> bool:
        root = self.cfg.url.removesuffix("/v1")
        try:
            r = self.session.get(f"{root}/health", timeout=10)
            return r.ok and r.json().get("status") == "healthy"
        except (requests.RequestException, ValueError):
            return False

    def chat(self, account: str, engine: TurnEngine, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """One completion. Returns the assistant message: `content` without
        the thinking, `tool_calls` if the model called tools, and the thinking
        as `reasoning_content`. Sent back within a turn, the reasoning keeps
        the model's earlier thoughts and lets the server reuse its cache: the
        chat template replays it in a <think> block, as it was generated."""
        body = {
            "messages": messages,
            "max_tokens": engine.max_tokens,
            "temperature": engine.temperature,
            "top_p": 0.95,
            "top_k": 20,
            "chat_template_kwargs": {"enable_thinking": engine.thinking},
        }
        if self.cfg.id:
            body["model"] = self.cfg.id
        if tools:
            body["tools"] = tools
        try:
            r = self.session.post(f"{self.cfg.url}/chat/completions", json=body, timeout=(10, self.cfg.timeout))
        except requests.ConnectionError as e:
            raise LLMDown(str(e)) from e
        if r.status_code >= 500:
            raise LLMDown(f"HTTP {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        choice = r.json()["choices"][0]
        msg = choice["message"]
        if choice.get("finish_reason") == "length":
            log.warning("%s hit max_tokens=%d", account, engine.max_tokens)
        return {
            "role": "assistant",
            "content": THINK_RE.sub("", msg.get("content") or "").strip(),
            "reasoning_content": msg.get("reasoning_content") or "",
            "tool_calls": msg.get("tool_calls") or [],
        }
