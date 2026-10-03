from __future__ import annotations
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .detector import latest_turn, queued_count
from .engine import EngineError
from .planner import Entry

ACTIVE_WRITER = "already has an active writer"


@dataclass
class Outcome:
    mode: str  # engine | desktop | pending-in-app | error
    ok: bool
    detail: str
    turn_status: str | None = None
    error_info: str | None = None


def queue_message(cfg: Config, thread_id: str, message: str, runner=subprocess.run) -> str:
    r = runner([cfg.codex_bin, "queue", "--thread", thread_id, "--message", message],
               capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "codex queue failed").strip()[:300])
    return (r.stdout or "").strip()


def _resume_params(cfg: Config, entry: Entry) -> dict:
    params: dict = {"threadId": entry.thread_id, "excludeTurns": True}
    if entry.model:
        params["model"] = entry.model
    if entry.effort:
        params["config"] = {"model_reasoning_effort": entry.effort}
    if cfg.approvals == "guardian":
        params["approvalsReviewer"] = "guardian_subagent"
    return params


def _turn_params(cfg: Config, entry: Entry) -> dict:
    params: dict = {"threadId": entry.thread_id, "input": [{"type": "text", "text": entry.message}]}
    if entry.model:
        params["model"] = entry.model
    if entry.effort:
        params["effort"] = entry.effort
    if cfg.approvals == "guardian":
        params["approvalsReviewer"] = "guardian_subagent"
    return params


def _outcome_from_turn(mode: str, turn: dict) -> Outcome:
    status = turn.get("status")
    err = turn.get("error") or {}
    detail = err.get("message") or status or "unknown"
    return Outcome(mode, status == "completed", str(detail)[:300], status, err.get("codexErrorInfo"))


def _deliver_via_desktop(cfg: Config, entry: Entry, log, sleep, now, latest, queue, already_queued: int) -> Outcome:
    before = latest(cfg.codex_home, entry.thread_id)
    if already_queued:
        log(f"{already_queued} message(s) already queued for thread {entry.thread_id[:13]}; not queueing again")
    else:
        queue(cfg, entry.thread_id, entry.message)
        log(f"queued {entry.message!r} for the desktop engine (thread {entry.thread_id[:13]})")
    deadline = now() + cfg.desktop_wait_seconds
    while now() < deadline:
        sleep(5)
        cur = latest(cfg.codex_home, entry.thread_id)
        if cur and (before is None or cur[0] != before[0]):
            log(f"desktop started turn {cur[0][:13]}")
            end = now() + cfg.turn_timeout_seconds
            while cur[1] == "inProgress" and now() < end:
                sleep(15)
                cur = latest(cfg.codex_home, entry.thread_id) or cur
            status, info = cur[1], cur[4]
            return Outcome("desktop", status == "completed", info or status or "", status, info)
    return Outcome("pending-in-app", False, "queued; open the thread in the ChatGPT app to start it")


def deliver(cfg: Config, engine, entry: Entry, begin_watch: Callable[[str], object], log: Callable[[str], None],
            sleep=time.sleep, now=time.time, latest=latest_turn, queue=queue_message, queued=queued_count) -> Outcome:
    already_queued = queued(cfg.codex_home, entry.thread_id)
    watch = begin_watch(entry.thread_id)  # registered before resume: a queued message starts a turn on resume
    try:
        engine.call("thread/resume", _resume_params(cfg, entry), timeout=180)
    except EngineError as e:
        if ACTIVE_WRITER in e.message:
            return _deliver_via_desktop(cfg, entry, log, sleep, now, latest, queue, already_queued)
        return Outcome("error", False, f"thread/resume failed: {e.message}")
    try:
        if already_queued:
            log(f"thread {entry.thread_id[:13]} resumed; its {already_queued} queued message(s) start the turn")
        else:
            res = engine.call("turn/start", _turn_params(cfg, entry), timeout=180) or {}
            turn_id = (res.get("turn") or {}).get("id", "?")
            log(f"turn {str(turn_id)[:13]} started in our engine for thread {entry.thread_id[:13]}")
        turn = watch.wait(cfg.turn_timeout_seconds)
        if turn is None:
            return Outcome("engine", False, "timed out waiting for turn/completed")
        return _outcome_from_turn("engine", turn)
    except EngineError as e:
        return Outcome("error", False, f"turn/start failed: {e.message}")
    finally:
        try:
            engine.call("thread/unsubscribe", {"threadId": entry.thread_id}, timeout=30)
        except Exception:  # noqa: BLE001
            pass
