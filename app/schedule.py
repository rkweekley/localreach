"""Send windows.

`send_window` is stored on the sequence but was previously decorative — mail
went out whenever the tick ran, including 3am local time, which is exactly the
pattern that gets a new sending domain throttled.

Spec format: `always` | `<days>_<start>_<end>` where days is `weekdays`,
`weekend`, `daily`, or `mon,tue,...`. Hours are 24h, inclusive of start and
exclusive of end, evaluated in the workspace timezone.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_DAY_ALIASES = {
    "daily": set(range(7)),
    "weekdays": {0, 1, 2, 3, 4},
    "weekend": {5, 6},
}
_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/New_York")
    except Exception:  # noqa: BLE001 - bad tz must not stop the worker
        return ZoneInfo("America/New_York")


def parse(spec: str | None) -> tuple[set[int], int, int] | None:
    """Returns (days, start_hour, end_hour) or None for 'always'."""
    spec = (spec or "always").strip().lower()
    if spec in {"", "always", "any", "24x7"}:
        return None
    parts = spec.split("_")
    if len(parts) != 3:
        return None
    days_raw, start_raw, end_raw = parts
    if days_raw in _DAY_ALIASES:
        days = _DAY_ALIASES[days_raw]
    else:
        days = {_NAMES[d] for d in days_raw.split(",") if d in _NAMES}
        if not days:
            return None
    try:
        start, end = int(start_raw), int(end_raw)
    except ValueError:
        return None
    if not (0 <= start < 24 and 0 <= end <= 24) or start >= end:
        return None
    return days, start, end


def is_open(spec: str | None, tz_name: str | None, now: datetime | None = None) -> bool:
    window = parse(spec)
    if window is None:
        return True
    days, start, end = window
    local = (now or datetime.now(ZoneInfo("UTC"))).astimezone(_tz(tz_name))
    return local.weekday() in days and start <= local.hour < end


def next_open(spec: str | None, tz_name: str | None, now: datetime | None = None) -> datetime:
    """Next UTC instant the window opens (or now, if it is already open)."""
    window = parse(spec)
    base = (now or datetime.now(ZoneInfo("UTC"))).astimezone(_tz(tz_name))
    if window is None:
        return base.astimezone(ZoneInfo("UTC"))
    days, start, end = window

    candidate = base.replace(minute=0, second=0, microsecond=0)
    for _ in range(0, 15 * 24):  # two weeks is more than enough
        if candidate.weekday() in days and start <= candidate.hour < end:
            return max(candidate, base).astimezone(ZoneInfo("UTC"))
        candidate += timedelta(hours=1)
    return (base + timedelta(days=1)).astimezone(ZoneInfo("UTC"))
