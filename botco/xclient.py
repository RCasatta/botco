"""Posting to X and reading the account's numbers.

X's API is pay-per-use (about $0.015 per post, $0.01 per profile read), so
this module makes as few calls as possible: one per post, one profile read a
day. Posts with links cost much more and are refused earlier, by the checks
in text.check_post.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

log = logging.getLogger(__name__)

API = "https://api.x.com/2"
KEYS = ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")


@dataclass
class Numbers:
    followers: int
    following: int
    posts: int


def read_env(path: Path) -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
    return env


class X:
    def __init__(self, env: dict[str, str]):
        missing = [k for k in KEYS if not env.get(k)]
        if missing:
            raise ValueError(f"x.env is missing {', '.join(missing)}")
        self.auth = OAuth1(*(env[k] for k in KEYS))

    def post(self, text: str) -> str:
        r = requests.post(f"{API}/tweets", json={"text": text}, auth=self.auth, timeout=30)
        if not r.ok:
            raise RuntimeError(f"X post failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json()["data"]["id"]

    def numbers(self) -> Numbers:
        r = requests.get(f"{API}/users/me", params={"user.fields": "public_metrics"}, auth=self.auth, timeout=30)
        if not r.ok:
            raise RuntimeError(f"X profile read failed: HTTP {r.status_code} {r.text[:300]}")
        m = r.json()["data"]["public_metrics"]
        return Numbers(m["followers_count"], m["following_count"], m["tweet_count"])


class DryRunX:
    """Stands in for X until the account exists: nothing leaves the machine."""

    def post(self, text: str) -> str:
        return "dry-run"

    def numbers(self) -> Numbers | None:
        return None


def make(dry_run: bool, env_file: Path) -> X | DryRunX:
    if dry_run:
        return DryRunX()
    return X(read_env(env_file))
