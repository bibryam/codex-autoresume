from __future__ import annotations
from dataclasses import dataclass, asdict

from .config import Config
from .detector import Blocked
from .limits import Limits, parse_reset_hint
from .state import State

DAY = 86400


@dataclass
class Entry:
    thread_id: str
    title: str
    cwd: str
    last_user_message: str
    blocked_at: int
    resume_at: int | None
    state: str
    position: int | None
    message: str
    model: str | None
    effort: str | None
    attempts: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    entries: list[Entry]
    next_action: Entry | None
    note: str


def _earliest(limits: Limits | None, b: Blocked, cfg: Config, now: int) -> tuple[int, str]:
    if limits is not None and limits.allowed:
        return now, "window open"
    if limits is not None and limits.allowed is False and limits.primary and limits.primary.resets_at:
        return int(limits.primary.resets_at) + cfg.grace_seconds, "after 5h window resets"
    hint = parse_reset_hint(b.reset_hint or "", b.blocked_at)
    if hint:
        return hint + cfg.grace_seconds, "from error message (limits unavailable)"
    return now + 300, "no reset time known, retry in 5 min"


def build_plan(blocked: list[Blocked], limits: Limits | None, state: State, cfg: Config,
               now: int, running: str | None = None) -> Plan:
    weekly = limits.secondary.used_percent if (limits and limits.secondary) else 0
    note = "" if limits is not None else "limits unavailable; using reset times from error messages"
    ordered = sorted(blocked, key=lambda b: (not state.thread(b.thread_id)["resume_now"], b.blocked_at))
    entries: list[Entry] = []
    position = 0
    for b in ordered:
        st = state.thread(b.thread_id)
        attempts = state.attempts_since(b.thread_id, now - DAY)
        last_attempt = max(st["attempts"], default=None)
        in_cooldown = (last_attempt is not None and last_attempt >= b.blocked_at
                       and now < last_attempt + cfg.retry_cooldown_seconds)
        resume_at, reason = _earliest(limits, b, cfg, now)
        if st["resume_now"]:
            resume_at, reason = now, "resume now requested"
        if running == b.thread_id:
            kind = "running"
        elif st["skip"]:
            kind = "skipped"
        elif st["pending_in_app_at"]:
            kind = "pending-in-app"
            reason = "queued in the app; open the thread to start it"
        elif not st["resume_now"] and attempts >= cfg.max_attempts:
            kind = "max-attempts"
            reason = f"{attempts} automatic resumes in 24h"
        elif not st["resume_now"] and in_cooldown:
            kind = "cooldown"
            reason = f"resumed {(now - last_attempt) // 60} min ago; waiting before retrying"
        elif not st["resume_now"] and weekly >= cfg.weekly_guard:
            kind = "weekly-guard"
            reason = f"weekly usage {weekly}% >= guard {cfg.weekly_guard}%"
        else:
            kind = "scheduled"
        pos = None
        if kind == "scheduled":
            position += 1
            pos = position
        entries.append(Entry(
            thread_id=b.thread_id, title=b.title, cwd=b.cwd, last_user_message=b.last_user_message,
            blocked_at=b.blocked_at, resume_at=resume_at if kind in ("scheduled", "running") else None,
            state=kind, position=pos, message=st["message"] or cfg.default_message,
            model=st["model"] or b.model, effort=st["effort"] or b.effort, attempts=attempts, reason=reason))
    next_action = None
    if running is None:
        for e in entries:
            if e.state != "scheduled" or e.resume_at is None or e.resume_at > now:
                continue
            forced = state.thread(e.thread_id)["resume_now"]
            if forced or (limits is not None and limits.allowed):
                next_action = e
            break
    return Plan(entries, next_action, note)
