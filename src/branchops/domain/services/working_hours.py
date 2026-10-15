"""Team working hours: ``"Mon-Fri 08:30-17:30"`` (Europe/Istanbul) or ``"Pzt-Cmt 09:00-18:00 UTC"``.

Several ranges may be separated by ``;`` (``"Mon-Fri 08:30-17:30; Sat 09:00-13:00"``). Day names are English
or Turkish three-letter abbreviations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ZONE = "Europe/Istanbul"
_DAYS = {
    **{d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))},
    **{d: i for i, d in enumerate(("pzt", "sal", "çar", "per", "cum", "cmt", "paz"))},
    "car": 2,
}
_RANGE_RE = re.compile(
    r"^\s*(?P<d1>[a-zçğıöşü]{3})(?:-(?P<d2>[a-zçğıöşü]{3}))?\s+(?P<t1>\d{1,2}:\d{2})-(?P<t2>\d{1,2}:\d{2})"
    r"(?:\s+(?P<tz>[A-Za-z_]+(?:/[A-Za-z_]+)*))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WorkingRange:
    days: frozenset[int]
    start: time
    end: time
    zone: str


def _time(value: str) -> time:
    hour, minute = (int(x) for x in value.split(":"))
    return time(hour, minute)


def parse_working_hours(spec: str) -> list[WorkingRange]:
    ranges: list[WorkingRange] = []
    for part in spec.split(";"):
        if not part.strip():
            continue
        m = _RANGE_RE.match(part)
        if m is None:
            raise ValueError(f"invalid working hours {part.strip()!r}; expected e.g. 'Mon-Fri 08:30-17:30'")
        d1, d2 = _DAYS.get(m["d1"].casefold()), _DAYS.get((m["d2"] or m["d1"]).casefold())
        if d1 is None or d2 is None:
            raise ValueError(f"unknown day in {part.strip()!r}")
        days = frozenset(range(d1, d2 + 1)) if d1 <= d2 else frozenset([*range(d1, 7), *range(0, d2 + 1)])
        start, end = _time(m["t1"]), _time(m["t2"])
        if end <= start:
            raise ValueError(f"end before start in {part.strip()!r}")
        zone = DEFAULT_ZONE
        if m["tz"]:
            zone = next((z for z in (m["tz"], m["tz"].upper(), m["tz"].title()) if _zone_ok(z)), "")
            if not zone:
                raise ValueError(f"unknown time zone in {part.strip()!r}")
        ranges.append(WorkingRange(days, start, end, zone))
    if not ranges:
        raise ValueError("empty working hours")
    return ranges


def _zone_ok(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def within_working_hours(spec: str | None, at: datetime) -> bool:
    """``True`` when ``at`` (timezone-aware) is inside the spec; no spec means "always reachable"."""
    if not spec:
        return True
    for r in parse_working_hours(spec):
        local = at.astimezone(ZoneInfo(r.zone))
        if local.weekday() in r.days and r.start <= local.time() < r.end:
            return True
    return False
