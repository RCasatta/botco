"""Sinks take tasks once every role approved them. They run without a
model; the X publisher is the only component that writes outside botco."""

from __future__ import annotations

from datetime import datetime, timedelta

from . import text as T
from .tracker import Tracker
from .world import World, quote


class XPublisher:
    """Posts the oldest ready task of the kinds with `sink = "x"` at fixed
    times, and reads the account's numbers once a day."""

    def __init__(self, world: World):
        self.w = world
        self.cfg = world.cfg.x

    def kinds(self) -> set[str]:
        return {k.name for k in self.w.cfg.kinds.values() if k.sink == "x"}

    def ready(self) -> list:
        w, kinds = self.w, self.kinds()
        return [t for t in w.store.open_tasks() if t.kind in kinds and w.policy.waits_on(w.store, t).ready]

    def tick(self) -> None:
        self.publish()
        self.record_metrics()

    def publish(self) -> None:
        w, cfg = self.w, self.cfg
        now, today = w.now(), w.today()
        due = sum(1 for t in cfg.times if t <= now.time())
        if w.published_on(today) >= due:
            return
        last = w.store.published(1)
        if last and now - datetime.fromisoformat(last[0].closed_at) < timedelta(minutes=cfg.min_gap_minutes):
            return
        retry_at = w.store.get("publish_retry_at")
        if retry_at and now < datetime.fromisoformat(retry_at):
            return
        ready = self.ready()
        if not ready:
            return
        t = ready[0]
        tracker = Tracker(w)
        problems = T.check_post(t.body, cfg.max_chars, [p.body for p in w.store.published(50)])
        if problems:
            tracker.post(cfg.account, t, f"Not published: {'; '.join(problems)}. Revise it or drop it.")
            w.store.add_review(t.id, t.hash, cfg.account, "revise", "; ".join(problems), cfg.account)
            w.changed(t.id, cfg.account, "the publisher refused it: " + "; ".join(problems))
            return
        try:
            x_id = w.x.post(t.body)
        except Exception as e:
            w.store.put("publish_retry_at", (now + timedelta(minutes=30)).isoformat())
            w.team.notify(f":warning: Publishing #{t.id} failed, retrying in 30 minutes: {e}")
            return
        tracker.close(cfg.account, t, "done", by_sink=True, x_id=x_id)
        where = "dry run, not actually posted" if x_id == "dry-run" else f"https://x.com/i/status/{x_id}"
        w.team.send(cfg.account, w.cfg.streams.published, today,
                    f"Published #{t.id} ({where}):\n{quote(t.body)}", check=False)
        tracker.post(cfg.account, t, f"Published ({where}).")

    def record_metrics(self) -> None:
        w = self.w
        now, today = w.now(), w.today()
        if now.time() < self.cfg.metrics_time or w.store.has_metrics(today):
            return
        if w.store.get("metrics_tried") == today:
            return
        w.store.put("metrics_tried", today)
        try:
            n = w.x.numbers()
        except Exception as e:
            w.team.notify(f":warning: Reading X metrics failed: {e}")
            return
        if n is None:
            return  # dry run
        prev = w.store.metrics(1)
        w.store.add_metrics(today, n.followers, n.following, n.posts)
        delta = f" ({n.followers - prev[0]['followers']:+d})" if prev else ""
        lines = [f"{today}: **{n.followers}** followers{delta}, {n.posts} posts, "
                 f"{w.published_on(today)} published today."]
        lines += self.record_post_metrics()
        w.team.send(self.cfg.account, w.cfg.streams.metrics, "followers", "\n".join(lines), check=False)

    def record_post_metrics(self) -> list[str]:
        """Read the numbers of the posts published in the last
        `metrics_days` days; the best and the worst of them, for #metrics."""
        w = self.w
        since = (w.now() - timedelta(days=self.cfg.metrics_days)).isoformat(timespec="seconds")
        recent = [t for t in w.store.published(200) if t.closed_at and t.closed_at >= since and t.x_id != "dry-run"]
        if not recent:
            return []
        try:
            numbers = w.x.post_numbers([t.x_id for t in recent])
        except Exception as e:
            w.team.notify(f":warning: Reading the posts' numbers failed: {e}")
            return []
        read = [(t, numbers[t.x_id]) for t in recent if t.x_id in numbers]
        for t, n in read:
            w.store.add_post_metrics(t.id, w.today(), n)
        if not read:
            return []
        read.sort(key=lambda tn: tn[1].views, reverse=True)
        shown = read if len(read) <= 2 else [read[0], read[-1]]
        return [f"{'Most' if i == 0 else 'Fewest'} views of the last {self.cfg.metrics_days} days: #{t.id}, "
                f"{metrics_text(n)}: {t.body[:80]}" for i, (t, n) in enumerate(shown)]


def metrics_text(n) -> str:
    """`1234 views, 5 likes, 1 repost, 0 replies, 0 quotes, 2 bookmarks`, from
    PostNumbers or a post_metrics row."""
    get = (lambda k: n[k]) if not hasattr(n, "views") else (lambda k: getattr(n, k))
    return ", ".join(f"{get(k)} {k}" for k in ("views", "likes", "reposts", "replies", "quotes", "bookmarks"))
