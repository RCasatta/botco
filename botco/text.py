"""Pure text helpers: cleaning model output, checking posts, parsing replies."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|ai|io|org|net|dev|co|it)\b", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w@])@\w{1,15}")
HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
# Internal details from the lab notebook that must not reach a public post.
ADDRESS_RE = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b|localhost|(?<![\w.])(~|/home|/var|/etc|/run|/nix|/tmp)/\S")
# Zulip markdown mentions: @**Name** or @**Name|id**; @_**...** is silent.
ZULIP_MENTION_RE = re.compile(r"(?<!_)@\*\*([^*|]+)(?:\|(\d+))?\*\*")


def clean_post(text: str) -> str:
    """Strip the wrapping models like to add around a post."""
    t = text.strip()
    t = re.sub(r"^```\w*\n?|\n?```$", "", t).strip()
    t = re.sub(r"^(post|draft|tweet)\s*(#?\d+)?\s*:\s*", "", t, flags=re.IGNORECASE).strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'“”":
        t = t[1:-1].strip()
    if t.startswith("“") and t.endswith("”"):
        t = t[1:-1].strip()
    return t


def x_length(text: str) -> int:
    """Length as X counts it, roughly: most emoji and CJK count double."""
    return sum(2 if ord(c) > 0x2FFF else 1 for c in text)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def check_post(text: str, max_chars: int, previous: list[str]) -> list[str]:
    """Hard rules every post must pass, independent of the editor's taste."""
    problems = []
    if not text:
        problems.append("the post is empty")
        return problems
    n = x_length(text)
    if n > max_chars:
        problems.append(f"too long: {n} characters, the limit is {max_chars}")
    if URL_RE.search(text):
        problems.append("contains a link or domain; posts must not contain links")
    if MENTION_RE.search(text):
        problems.append("mentions an account; posts must not @-mention anyone")
    if ADDRESS_RE.search(text):
        problems.append("contains an IP address, host or file path; keep internal details out of posts")
    if len(HASHTAG_RE.findall(text)) > 1:
        problems.append("more than one hashtag")
    for old in previous:
        if similarity(text, old) > 0.8:
            problems.append(f"nearly identical to an earlier post: {old[:80]!r}")
            break
    return problems


def mentioned(content: str, names: dict[str, int]) -> set[str]:
    """Persona names (keys of `names`, mapped to Zulip user ids) mentioned in
    raw markdown `content`."""
    by_id = {uid: name for name, uid in names.items()}
    by_name = {name.lower(): name for name in names}
    found = set()
    for full_name, uid in ZULIP_MENTION_RE.findall(content):
        if uid and int(uid) in by_id:
            found.add(by_id[int(uid)])
        elif full_name.strip().lower() in by_name:
            found.add(by_name[full_name.strip().lower()])
    return found
