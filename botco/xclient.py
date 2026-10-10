"""Posting to X and reading the account's numbers.

X's API is pay-per-use (about $0.015 per post, $0.01 per profile read), so
this module makes as few calls as possible: one per post, one profile read a
day, one read a day of the latest posts' numbers, and one read per post
an agent asks to read. Posts with links cost much more and are refused earlier, by the checks
in text.check_post.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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


@dataclass
class PostNumbers:
    views: int
    likes: int
    reposts: int
    replies: int
    quotes: int
    bookmarks: int


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

    def read_post(self, post_id: str) -> dict:
        """A post on X: its author, text, time and numbers."""
        r = requests.get(f"{API}/tweets/{post_id}", params={
            "tweet.fields": "created_at,public_metrics,conversation_id", "expansions": "author_id",
            "user.fields": "username,name"}, auth=self.auth, timeout=30)
        if not r.ok:
            raise RuntimeError(f"X post read failed: HTTP {r.status_code} {r.text[:300]}")
        body = r.json()
        if "data" not in body:
            raise RuntimeError(f"no such post on X: {post_id}")
        t, user = body["data"], (body.get("includes", {}).get("users") or [{}])[0]
        m = t.get("public_metrics", {})
        return {"username": user.get("username", "?"), "name": user.get("name", ""), "text": t["text"],
                "created_at": t.get("created_at", ""), "likes": m.get("like_count", 0),
                "replies": m.get("reply_count", 0), "views": m.get("impression_count", 0),
                "url": f"https://x.com/{user.get('username', 'i')}/status/{post_id}"}

    def following(self) -> list[dict]:
        """The accounts we follow: id, username, name, followers."""
        me = requests.get(f"{API}/users/me", auth=self.auth, timeout=30)
        if not me.ok:
            raise RuntimeError(f"X profile read failed: HTTP {me.status_code} {me.text[:300]}")
        uid, out, token = me.json()["data"]["id"], [], None
        while True:
            params = {"max_results": 1000, "user.fields": "username,name,public_metrics"}
            if token:
                params["pagination_token"] = token
            r = requests.get(f"{API}/users/{uid}/following", params=params, auth=self.auth, timeout=30)
            if not r.ok:
                raise RuntimeError(f"X following read failed: HTTP {r.status_code} {r.text[:300]}")
            body = r.json()
            out += [{"id": u["id"], "username": u["username"], "name": u["name"],
                     "followers": u.get("public_metrics", {}).get("followers_count", 0)} for u in body.get("data", [])]
            token = body.get("meta", {}).get("next_token")
            if not token:
                return out

    def recent_posts(self, usernames: list[str], since: str, limit: int) -> list[dict]:
        """At most `limit` original posts (no reposts or replies) by these
        accounts since `since` (ISO time, at most 7 days ago), newest first.
        The window is cut in slices of 10 posts each (the search's minimum),
        so a busy last hour does not crowd out the rest of the day; long
        account lists take one search per group the query length allows."""
        groups, group = [], []
        for u in usernames:
            if group and len(" OR ".join(f"from:{g}" for g in [*group, u])) > 400:
                groups.append(group)
                group = []
            group.append(u)
        if group:
            groups.append(group)
        start, end = datetime.fromisoformat(since), datetime.now(timezone.utc) - timedelta(seconds=30)
        out = []
        for i, g in enumerate(groups):
            budget = (limit - len(out)) // (len(groups) - i)
            slices = budget // 10
            for j in range(slices):
                params = {
                    "query": "(" + " OR ".join(f"from:{u}" for u in g) + ") -is:retweet -is:reply",
                    "start_time": (start + (end - start) * j / slices).isoformat(timespec="seconds"),
                    "end_time": (start + (end - start) * (j + 1) / slices).isoformat(timespec="seconds"),
                    "max_results": min(budget // slices, 100), "sort_order": "relevancy",
                    "tweet.fields": "created_at,public_metrics", "expansions": "author_id",
                    "user.fields": "username,name"}
                r = requests.get(f"{API}/tweets/search/recent", params=params, auth=self.auth, timeout=30)
                if not r.ok:
                    raise RuntimeError(f"X search failed: HTTP {r.status_code} {r.text[:300]}")
                body = r.json()
                users = {u["id"]: u for u in body.get("includes", {}).get("users", [])}
                for t in body.get("data", []):
                    u, m = users.get(t["author_id"], {}), t.get("public_metrics", {})
                    out.append({"id": t["id"], "username": u.get("username", "?"), "name": u.get("name", ""),
                                "text": t["text"], "created_at": t.get("created_at", ""),
                                "views": m.get("impression_count", 0), "likes": m.get("like_count", 0),
                                "replies": m.get("reply_count", 0), "reposts": m.get("retweet_count", 0)})
        return sorted(out, key=lambda p: p["created_at"], reverse=True)[:limit]

    def post_numbers(self, ids: list[str]) -> dict[str, PostNumbers]:
        """Views, likes and the rest for our posts, 100 per request. Deleted
        posts are left out."""
        out = {}
        for i in range(0, len(ids), 100):
            r = requests.get(f"{API}/tweets", params={"ids": ",".join(ids[i:i + 100]), "tweet.fields": "public_metrics"},
                             auth=self.auth, timeout=30)
            if not r.ok:
                raise RuntimeError(f"X post metrics read failed: HTTP {r.status_code} {r.text[:300]}")
            for t in r.json().get("data", []):
                m = t["public_metrics"]
                out[t["id"]] = PostNumbers(m.get("impression_count", 0), m["like_count"], m["retweet_count"],
                                           m["reply_count"], m["quote_count"], m.get("bookmark_count", 0))
        return out


class DryRunX:
    """Stands in for X until the account exists: nothing leaves the machine."""

    def post(self, text: str) -> str:
        return "dry-run"

    def numbers(self) -> Numbers | None:
        return None

    def post_numbers(self, ids: list[str]) -> dict[str, PostNumbers]:
        return {}

    def read_post(self, post_id: str) -> dict:
        raise RuntimeError("X is in dry run: posts on X cannot be read")

    def following(self) -> list[dict]:
        return []

    def recent_posts(self, usernames: list[str], since: str, limit: int) -> list[dict]:
        return []


def make(dry_run: bool, env_file: Path) -> X | DryRunX:
    if dry_run:
        return DryRunX()
    return X(read_env(env_file))
