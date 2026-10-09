"""Read-only access to GitHub and GitLab issues.

Botco never writes to either platform: it reads with a read token, or none
for public repositories, and keeps what it read in the store so a change
can be noticed and the agents can read issues without a request each time.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote as urlquote

import requests

from .config import GitHub, GitLab, Sources
from .policy import Ref
from .store import External

log = logging.getLogger(__name__)

COMMENTS = 5  # latest comments kept per issue


class ExternalError(Exception):
    pass


class Issues:
    def __init__(self, cfg: Sources):
        self.github = cfg.github
        self.gitlab = cfg.gitlab
        self.session = requests.Session()

    def _get(self, url: str, headers: dict, params: dict | None = None):
        try:
            r = self.session.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            raise ExternalError(f"cannot reach {url.split('/')[2]}: {e}") from e
        if r.status_code == 404:
            raise ExternalError("not found, or not readable with the configured token")
        if not r.ok:
            raise ExternalError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r

    # GitHub

    def _gh(self) -> tuple[GitHub, dict]:
        gh = self.github or GitHub()
        headers = {"Accept": "application/vnd.github+json"}
        if token := os.environ.get(gh.token_env):
            headers["Authorization"] = f"Bearer {token}"
        return gh, headers

    def _gh_issue(self, ref: Ref) -> External:
        gh, h = self._gh()
        base = f"{gh.api}/repos/{ref.repo}/issues/{ref.number}"
        i = self._get(base, h).json()
        comments = []
        if i.get("comments"):
            last = max(1, -(-i["comments"] // COMMENTS))
            page = self._get(f"{base}/comments", h, {"per_page": COMMENTS, "page": last}).json()
            if len(page) < COMMENTS and last > 1:
                page = self._get(f"{base}/comments", h, {"per_page": COMMENTS, "page": last - 1}).json() + page
            comments = [{"author": c["user"]["login"], "at": c["created_at"], "text": c.get("body") or ""}
                        for c in page[-COMMENTS:]]
        return External(ref.text, i["title"], i["state"], i["html_url"], i.get("body") or "", comments,
                        i["updated_at"])

    def _gh_search(self, query: str) -> list[tuple[str, str, str]]:
        gh, h = self._gh()
        if not gh.repos:
            return []
        q = " ".join([query, "is:issue", *(f"repo:{r}" for r in gh.repos)])
        items = self._get(f"{gh.api}/search/issues", h, {"q": q, "per_page": 10}).json().get("items", [])
        out = []
        for i in items:
            repo = i["repository_url"].split("/repos/", 1)[1]
            out.append((f"gh:{repo}#{i['number']}", i["state"], i["title"]))
        return out

    # GitLab

    def _gl(self) -> tuple[GitLab, dict]:
        gl = self.gitlab or GitLab()
        headers = {}
        if token := os.environ.get(gl.token_env):
            headers["PRIVATE-TOKEN"] = token
        return gl, headers

    def _gl_issue(self, ref: Ref) -> External:
        gl, h = self._gl()
        base = f"{gl.url}/api/v4/projects/{urlquote(ref.repo, safe='')}/issues/{ref.number}"
        i = self._get(base, h).json()
        notes = self._get(f"{base}/notes", h, {"sort": "desc", "order_by": "created_at", "per_page": COMMENTS}).json()
        comments = [{"author": n["author"]["username"], "at": n["created_at"], "text": n.get("body") or ""}
                    for n in reversed(notes) if not n.get("system")]
        return External(ref.text, i["title"], i["state"], i["web_url"], i.get("description") or "", comments,
                        i["updated_at"])

    def _gl_search(self, query: str) -> list[tuple[str, str, str]]:
        gl, h = self._gl()
        out = []
        for p in gl.projects:
            url = f"{gl.url}/api/v4/projects/{urlquote(p, safe='')}/issues"
            for i in self._get(url, h, {"search": query, "per_page": 10}).json():
                out.append((f"gl:{p}#{i['iid']}", i["state"], i["title"]))
        return out

    # both

    def fetch(self, ref: Ref) -> External:
        if ref.kind == "gh":
            return self._gh_issue(ref)
        if ref.kind == "gl":
            return self._gl_issue(ref)
        raise ExternalError(f"{ref.text} is not an external issue")

    def search(self, query: str) -> list[tuple[str, str, str]]:
        """(ref, state, title) of matching issues in the configured
        repositories and projects."""
        out = []
        for source, fn in (("GitHub", self._gh_search if self.github else None),
                           ("GitLab", self._gl_search if self.gitlab else None)):
            if fn is None:
                continue
            try:
                out += fn(query)
            except ExternalError as e:
                out.append(("", "error", f"{source} search failed: {e}"))
        return out


def summary(e: External, body_chars: int = 1500) -> str:
    lines = [f"{e.ref} [{e.state}] {e.title} ({e.url}, updated {e.updated_at[:16]})"]
    if e.body:
        lines.append(e.body[:body_chars] + ("..." if len(e.body) > body_chars else ""))
    for c in e.comments:
        lines.append(f"- {c['author']} ({c['at'][:10]}): {c['text'][:500]}")
    return "\n".join(lines)
