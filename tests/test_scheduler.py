import tempfile, threading, unittest
from pathlib import Path
from codex_resume.config import Config
from codex_resume.deliver import Outcome
from codex_resume.detector import Blocked
from codex_resume.scheduler import Scheduler
from codex_resume.state import State

NOW = 2_000_000
RL_BLOCKED = {"ordinaryUsageAllowed": False, "rateLimits": {"primary": {"usedPercent": 100, "resetsAt": NOW + 100, "windowDurationMins": 300}, "secondary": {"usedPercent": 20, "resetsAt": NOW + 9999, "windowDurationMins": 10080}}}
RL_OPEN = {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 1, "resetsAt": NOW + 18000, "windowDurationMins": 300}, "secondary": {"usedPercent": 20, "resetsAt": NOW + 9999, "windowDurationMins": 10080}}}


class FakeEngine:
    def __init__(self, limits):
        self.limits = limits; self.started = 0; self.stopped = 0; self.on_notification = None
    def start(self): self.started += 1
    def stop(self): self.stopped += 1
    def alive(self): return True
    def call(self, method, params=None, timeout=60):
        if method == "account/rateLimits/read":
            return self.limits
        return {}


def blocked(tid, at):
    return Blocked(tid, "/p/x", "title", "last", at, "try again at 1:56 PM.", "gpt-6-astra", "xhigh", "vscode", tid + "-t")


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "s.json")
        self.cfg = Config(codex_bin="/bin/echo", codex_home=self.tmp.name, tick_seconds=1)
        self.clock = [float(NOW)]
        self.notes = []
        self.blocked = [blocked("a", 10), blocked("b", 20)]
        self.delivered = []
        self.done = threading.Event()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, engine, outcome=Outcome("engine", True, "completed", "completed", None)):
        def deliverer(cfg, eng, entry, begin_watch, log, **kw):
            self.delivered.append(entry.thread_id); self.done.set(); return outcome
        self.engine = engine
        def factory(on_notification, on_server_request):
            engine.on_notification = on_notification; return engine
        return Scheduler(self.cfg, self.state, notifier=lambda t, x: self.notes.append(x), engine_factory=factory,
                         clock=lambda: self.clock[0], finder=lambda home: self.blocked, deliverer=deliverer)

    def test_tick_blocked_schedules_and_does_not_deliver(self):
        s = self.make(FakeEngine(RL_BLOCKED))
        s.tick()
        snap = s.snapshot()
        self.assertEqual(snap["engine"], "ok")
        self.assertEqual([e["thread_id"] for e in snap["entries"]], ["a", "b"])
        self.assertEqual(snap["entries"][0]["resume_at"], NOW + 100 + 60)
        self.assertEqual(self.delivered, [])
        self.assertEqual(snap["limits"]["primary"]["used_percent"], 100)

    def test_tick_open_delivers_one_then_next(self):
        s = self.make(FakeEngine(RL_OPEN))
        s.tick(); self.assertTrue(self.done.wait(2)); self.done.clear()
        s.join_delivery(2)
        self.assertEqual(self.delivered, ["a"])
        self.assertEqual(self.state.attempts_since("a", 0), 1)
        self.assertTrue(any("a" in h["thread_id"] for h in self.state.history()))
        s.tick(); self.assertTrue(self.done.wait(2)); s.join_delivery(2)
        self.assertEqual(self.delivered, ["a", "b"])
        self.assertGreaterEqual(self.engine.stopped, 1)

    def test_pending_in_app_marks_state(self):
        s = self.make(FakeEngine(RL_OPEN), outcome=Outcome("pending-in-app", False, "queued"))
        s.tick(); self.assertTrue(self.done.wait(2)); s.join_delivery(2)
        self.assertIsNotNone(self.state.thread("a")["pending_in_app_at"])
        self.assertEqual(s.snapshot()["entries"][0]["state"], "pending-in-app")

    def test_engine_failure_marks_down_and_keeps_plan_from_hints(self):
        class Dead(FakeEngine):
            def call(self, method, params=None, timeout=60): raise RuntimeError("gone")
        s = self.make(Dead(RL_OPEN))
        s.tick()
        snap = s.snapshot()
        self.assertTrue(snap["engine"].startswith("down"))
        self.assertEqual(len(snap["entries"]), 2)
        self.assertEqual(self.delivered, [])

    def test_ui_actions(self):
        s = self.make(FakeEngine(RL_BLOCKED))
        s.tick()
        s.set_skip("a", True); s.set_message("b", "carry on", "m", "low")
        snap = s.snapshot()
        by = {e["thread_id"]: e for e in snap["entries"]}
        self.assertEqual(by["a"]["state"], "skipped")
        self.assertEqual((by["b"]["message"], by["b"]["model"], by["b"]["effort"]), ("carry on", "m", "low"))
        s.resume_now("b"); self.assertTrue(self.done.wait(2)); s.join_delivery(2)
        self.assertEqual(self.delivered, ["b"])

    def test_rate_limit_notification_updates_limits(self):
        s = self.make(FakeEngine(RL_BLOCKED))
        s.tick()
        self.engine.on_notification("account/rateLimits/updated", {"rateLimits": {"primary": {"usedPercent": 3, "resetsAt": 1}, "rateLimitReachedType": None}})
        self.assertEqual(s.snapshot()["limits"]["primary"]["used_percent"], 3)
        self.assertTrue(s.snapshot()["limits"]["allowed"])

    def test_turn_watch_receives_completion(self):
        s = self.make(FakeEngine(RL_BLOCKED))
        s.tick()
        w = s.begin_watch("a")
        self.engine.on_notification("turn/completed", {"threadId": "a", "turn": {"id": "x", "status": "completed"}})
        self.assertEqual(w.wait(1)["status"], "completed")
        self.assertIsNone(s.begin_watch("zzz").wait(0.01))
