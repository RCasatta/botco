"""Five-field cron expressions (minute hour day month weekday), enough for
schedules: `*`, numbers, lists, ranges, steps and day or month names."""

from __future__ import annotations

from datetime import datetime, timedelta

DAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"))}
FIELDS = ((0, 59, {}), (0, 23, {}), (1, 31, {}), (1, 12, MONTHS), (0, 7, DAYS))


def _value(s: str, names: dict[str, int]) -> int:
    return names[s.lower()] if s.lower() in names else int(s)


def _field(spec: str, lo: int, hi: int, names: dict[str, int]) -> set[int]:
    out = set()
    for part in spec.split(","):
        base, _, step = part.partition("/")
        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            x, y = base.split("-", 1)
            a, b = _value(x, names), _value(y, names)
        else:
            a = b = _value(base, names)
            if step:
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi):
            raise ValueError(f"cron: {part!r} is out of range {lo}-{hi}")
        out.update(range(a, b + 1, int(step) if step else 1))
    return out


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron: {expr!r} needs five fields")
        self.minute, self.hour, self.day, self.month, dow = (
            _field(p, lo, hi, names) for p, (lo, hi, names) in zip(parts, FIELDS))
        self.weekday = {d % 7 for d in dow}  # 0 and 7 are both Sunday
        # As in cron: when both day fields are restricted, either may match.
        self.any_day = parts[2] != "*" and parts[4] != "*"

    def matches(self, t: datetime) -> bool:
        if t.minute not in self.minute or t.hour not in self.hour or t.month not in self.month:
            return False
        day, weekday = t.day in self.day, (t.isoweekday() % 7) in self.weekday
        return (day or weekday) if self.any_day else (day and weekday)

    def fired(self, after: datetime, until: datetime) -> datetime | None:
        """The latest matching minute in (after, until], or None. Looks back
        at most eight days, so a long outage fires once, not once per missed
        run."""
        start = max(after, until - timedelta(days=8))
        t = until.replace(second=0, microsecond=0)
        while t > start:
            if self.matches(t):
                return t
            t -= timedelta(minutes=1)
        return None
