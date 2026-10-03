from __future__ import annotations
import threading
import time
import traceback

from .config import Config
from .deliver import deliver, Outcome
from .detector import find_blocked
from .engine import Engine
from .limits import Limits, parse_limits, read_limits
from .notify import notify
from .planner import Plan, build_plan
from .state import State

LIMITS_EVERY = 300
LIMITS_NEAR_RESET = 55
NEAR_RESET_WINDOW = 900


class TurnWatch:
    def __init__(self):
        self._ev = threading.Event()
        self.turn: dict | None = None

    def set(self, turn: dict) -> None:
        self.turn = turn
        self._ev.set()

    def wait(self, timeout: float | None) -> dict | None:
        self._ev.wait(timeout)
        return self.turn


class Scheduler:
    def __init__(self, cfg: Config, state: State, notifier=notify, engine_factory=None, clock=time.time,
                 finder=find_blocked, deliverer=deliver, dry_run: bool = False):
        self.cfg, self.state, self.notify, self.clock = cfg, state, notifier, clock
        self.finder, self.deliverer = finder, deliverer
        self.dry_run = dry_run
        self.engine_factory = engine_factory or (lambda on_notification, on_server_request: Engine(
            cfg.codex_bin, on_notification=on_notification, on_server_request=on_server_request,
            on_exit=self._on_engine_exit))
        self._engine = None
        self.engine_status = "down: not started"
        self.limits: Limits | None = None
        self.limits_ok = False
        self.limits_at = 0
        self.models: list[dict] = []
        self.blocked = []
        self.plan = Plan([], None, "")
        self.running: str | None = None
        self._worker: threading.Thread | None = None
        self._watches: dict[str, TurnWatch] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.last_tick = 0
        self.log_lines: list[str] = []

    # ---- logging / engine -------------------------------------------------
    def log(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {text}"
        print(line, flush=True)
        self.log_lines = (self.log_lines + [line])[-200:]

    def engine(self):
        with self._lock:
            if self._engine is None or not self._engine.alive():
                self._engine = self.engine_factory(self._on_notification, self._on_server_request)
                self._engine.start()
            return self._engine

    def _reset_engine(self) -> None:
        with self._lock:
            if self._engine is not None:
                try:
                    self._engine.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._engine = None

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "turn/completed":
            w = self._watches.get(params.get("threadId", ""))
            if w:
                w.set(params.get("turn") or {})
        elif method == "account/rateLimits/updated":
            self.limits = parse_limits({"rateLimits": params.get("rateLimits") or {}}, int(self.clock()))
            self.limits_at = int(self.clock())
        elif method == "error":
            err = params.get("error") or {}
            self.log(f"engine error on thread {str(params.get('threadId'))[:13]}: {str(err.get('message', ''))[:120]}")

    def _on_server_request(self, method: str, params: dict):
        if "requestApproval" in method:
            tid = str(params.get("threadId", ""))[:13]
            self.log(f"approval requested in thread {tid} ({method}); declined")
            self.state.add_history(int(self.clock()), params.get("threadId"), "approval-declined", method)
            self.notify("codex-resume", f"Thread {tid} asked for approval; declined. Open it in the app.")
        return Engine.default_server_request(method, params)

    def _on_engine_exit(self) -> None:
        """The app-server process ended: release every waiting delivery so nothing hangs."""
        for tid, w in list(self._watches.items()):
            w.set({"status": "failed", "error": {"message": "engine exited before the turn completed", "codexErrorInfo": "engineExited"}})
        if self._watches:
            self.log("engine exited while a delivery was waiting")

    def begin_watch(self, thread_id: str) -> TurnWatch:
        w = TurnWatch()
        self._watches[thread_id] = w
        return w

    # ---- tick -------------------------------------------------------------
    def _limits_due(self, now: int) -> bool:
        if now - self.limits_at >= LIMITS_EVERY:
            return True
        if self.limits and self.limits.primary and self.limits.primary.resets_at:
            until = self.limits.primary.resets_at + self.cfg.grace_seconds - now
            if -NEAR_RESET_WINDOW <= until <= NEAR_RESET_WINDOW and now - self.limits_at >= LIMITS_NEAR_RESET:
                return True
        return False

    def tick(self) -> None:
        now = int(self.clock())
        if self._limits_due(now):
            try:
                self.limits = read_limits(self.engine(), now)
                self.limits_at = now
                self.limits_ok = True
                self.engine_status = "ok"
                if not self.models:
                    self.models = self._read_models()
            except Exception as e:  # noqa: BLE001
                self.engine_status = f"down: {str(e)[:120]}"
                self.limits_ok = False  # stale limits never drive a delivery
                self.log(self.engine_status)
                if self.running is None:
                    self._reset_engine()  # never kill the engine under a running delivery
        try:
            self.blocked = self.finder(self.cfg.codex_home)
        except Exception as e:  # noqa: BLE001
            self.log(f"detector failed: {e}")
        last_turn = {b.thread_id: b.last_turn_id for b in self.blocked}
        for tid, t in list(self.state.data["threads"].items()):
            if not t.get("pending_in_app_at"):
                continue
            if tid not in last_turn or (t.get("pending_turn_id") and last_turn[tid] != t["pending_turn_id"]):
                self.state.set_pending_in_app(tid, None)  # gone, or a newer turn ran since we queued
        with self._lock:
            self.plan = build_plan(self.blocked, self.limits if self.limits_ok else None, self.state, self.cfg, now, self.running)
            nxt = self.plan.next_action
            if nxt and self.running is None and not self.dry_run:
                self._start_delivery(nxt)
        self.last_tick = now

    def _read_models(self) -> list[dict]:
        """Models the account can use, with their reasoning efforts (for the Edit form)."""
        try:
            res = self.engine().call("model/list", {}, timeout=30) or {}
        except Exception as e:  # noqa: BLE001
            self.log(f"model/list failed: {e}")
            return []
        out = []
        for m in res.get("data") or []:
            if m.get("hidden"):
                continue
            efforts = [e.get("reasoningEffort") if isinstance(e, dict) else e for e in m.get("supportedReasoningEfforts") or []]
            out.append({"id": m.get("id") or m.get("model"), "display": m.get("displayName") or m.get("id"),
                        "efforts": [e for e in efforts if e], "default_effort": m.get("defaultReasoningEffort")})
        return out

    def _start_delivery(self, entry) -> None:
        now = int(self.clock())
        self.running = entry.thread_id
        self.state.record_attempt(entry.thread_id, now)
        self.state.set_resume_now(entry.thread_id, False)
        self.state.add_history(now, entry.thread_id, "resuming", f"{entry.message!r} ({entry.title[:60]})")
        self.log(f"resuming thread {entry.thread_id[:13]} with {entry.message!r}")
        self.notify("codex-resume", f"Resuming: {entry.title[:80]}")
        self._worker = threading.Thread(target=self._deliver, args=(entry,), daemon=True, name="codex-resume-deliver")
        self._worker.start()
        self._replan()

    def _deliver(self, entry) -> None:
        outcome: Outcome
        try:
            outcome = self.deliverer(self.cfg, self.engine(), entry, self.begin_watch, self.log)
        except Exception as e:  # noqa: BLE001
            outcome = Outcome("error", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()
        now = int(self.clock())
        self._watches.pop(entry.thread_id, None)
        with self._lock:
            self.running = None
        if outcome.mode == "pending-in-app":
            last = next((b.last_turn_id for b in self.blocked if b.thread_id == entry.thread_id), None)
            self.state.set_pending_in_app(entry.thread_id, now, last)
        summary = f"{outcome.mode}: {outcome.turn_status or ''} {outcome.detail}".strip()
        self.state.set_last_result(entry.thread_id, summary)
        self.state.add_history(now, entry.thread_id, "done" if outcome.ok else "ended", summary)
        self.log(f"thread {entry.thread_id[:13]} {summary}")
        self.notify("codex-resume", f"{'Finished' if outcome.ok else 'Stopped'}: {entry.title[:60]} ({outcome.mode})")
        if outcome.mode in ("engine", "error"):
            self._reset_engine()  # releases the thread writer lock for the desktop
        self.limits_at = 0
        self._replan()

    def join_delivery(self, timeout: float) -> None:
        if self._worker:
            self._worker.join(timeout)

    # ---- UI actions ---------------------------------------------------------
    def set_skip(self, tid: str, flag: bool) -> None:
        self.state.set_skip(tid, flag)
        self.state.add_history(int(self.clock()), tid, "skip" if flag else "unskip")
        self._replan()

    def schedule(self, tid: str) -> None:
        """Put a parked thread into the normal queue (clears skip, overrides automation/guards)."""
        self.state.set_skip(tid, False)
        self.state.set_force(tid, True)
        self.state.set_pending_in_app(tid, None)
        self.state.add_history(int(self.clock()), tid, "schedule")
        self._replan()

    def set_message(self, tid: str, message: str | None, model: str | None = None, effort: str | None = None) -> None:
        self.state.set_message(tid, message, model, effort)
        self._replan()

    def resume_now(self, tid: str) -> None:
        self.state.set_resume_now(tid, True)
        self.state.set_pending_in_app(tid, None)
        self.state.set_skip(tid, False)
        self.state.add_history(int(self.clock()), tid, "resume-now")
        self.tick()

    def _replan(self) -> None:
        with self._lock:
            self.plan = build_plan(self.blocked, self.limits if self.limits_ok else None, self.state, self.cfg,
                                   int(self.clock()), self.running)

    def known_threads(self) -> set[str]:
        with self._lock:
            return {e.thread_id for e in self.plan.entries}

    # ---- snapshot / loop ----------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            entries = [e.to_dict() for e in self.plan.entries]
            for e in entries:
                e["last_result"] = self.state.thread(e["thread_id"]).get("last_result")
            return {
                "now": int(self.clock()),
                "engine": self.engine_status,
                "limits": self.limits.to_dict() if self.limits else None,
                "limits_stale": bool(self.limits) and not self.limits_ok,
                "note": self.plan.note,
                "entries": entries,
                "running": self.running,
                "models": self.models,
                "history": self.state.history(50),
                "log": self.log_lines[-30:],
                "config": {"port": self.cfg.port, "weekly_guard": self.cfg.weekly_guard, "max_attempts": self.cfg.max_attempts,
                           "default_message": self.cfg.default_message, "approvals": self.cfg.approvals},
            }

    def run_forever(self) -> None:
        self.log(f"codex-resume started; engine {self.cfg.codex_bin}")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.log(f"tick failed: {e}")
                traceback.print_exc()
            self._stop.wait(self.cfg.tick_seconds)

    def stop(self) -> None:
        self._stop.set()
        self._reset_engine()
