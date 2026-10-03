from __future__ import annotations
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta


@dataclass
class Window:
    used_percent: int
    resets_at: int | None
    window_minutes: int | None


@dataclass
class Limits:
    primary: Window | None
    secondary: Window | None
    allowed: bool | None
    plan: str | None
    fetched_at: int

    def to_dict(self) -> dict:
        return {
            "primary": asdict(self.primary) if self.primary else None,
            "secondary": asdict(self.secondary) if self.secondary else None,
            "allowed": self.allowed,
            "plan": self.plan,
            "fetched_at": self.fetched_at,
        }


def _window(d: dict | None) -> Window | None:
    if not d:
        return None
    return Window(int(d.get("usedPercent") or 0), d.get("resetsAt"), d.get("windowDurationMins"))


def parse_limits(result: dict, now: int) -> Limits:
    rl = result.get("rateLimits") or {}
    allowed = result.get("ordinaryUsageAllowed")
    if allowed is None and "rateLimitReachedType" in rl:
        allowed = rl["rateLimitReachedType"] is None
    plan = rl.get("planType")
    if not isinstance(plan, str):
        plan = None
    return Limits(_window(rl.get("primary")), _window(rl.get("secondary")), allowed, plan, now)


def read_limits(engine, now: int | None = None) -> Limits:
    result = engine.call("account/rateLimits/read", {})
    return parse_limits(result or {}, now if now is not None else int(time.time()))


_RESET_RE = re.compile(r"try again at (\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)


def parse_reset_hint(message: str, reference_ts: int, tz=None) -> int | None:
    """Turn 'try again at 1:56 PM' into an epoch, relative to the day of reference_ts (local time)."""
    m = _RESET_RE.search(message or "")
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)) % 12, int(m.group(2)), m.group(3).upper()
    if ampm == "PM":
        hour += 12
    ref = datetime.fromtimestamp(reference_ts, tz)
    cand = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand < ref:
        cand += timedelta(days=1)
    return int(cand.timestamp())
