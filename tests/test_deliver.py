import unittest
from codex_autoresume.config import Config
from codex_autoresume.deliver import deliver, queue_message
from codex_autoresume.engine import EngineError
from codex_autoresume.planner import Entry


def entry(**kw):
    base = dict(thread_id="t1", title="T", cwd="/p", last_user_message="x", blocked_at=1, resume_at=2, state="scheduled",
                position=1, message="continue", model="gpt-6-astra", effort="xhigh", attempts=0, reason="")
    base.update(kw)
    return Entry(**base)


class FakeEngine:
    def __init__(self, resume_error=None):
        self.calls = []
        self.resume_error = resume_error

    def call(self, method, params=None, timeout=60):
        self.calls.append((method, params))
        if method == "thread/resume" and self.resume_error:
            raise EngineError(-32600, self.resume_error)
        if method == "turn/start":
            return {"turn": {"id": "turn-9", "status": "inProgress"}}
        return {}


class Watch:
    def __init__(self, turn): self.turn = turn
    def wait(self, timeout): return self.turn


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(codex_bin="/bin/echo", codex_home="/nonexistent", desktop_wait_seconds=20)
        self.logs = []

    def test_deliver_engine_completed(self):
        eng = FakeEngine()
        watch = Watch({"id": "turn-9", "status": "completed"})
        out = deliver(self.cfg, eng, entry(), lambda tid: watch, self.logs.append)
        self.assertEqual((out.mode, out.ok, out.turn_status), ("engine", True, "completed"))
        methods = [m for m, _ in eng.calls]
        self.assertEqual(methods, ["thread/resume", "turn/start", "thread/unsubscribe"])
        resume = eng.calls[0][1]
        self.assertEqual(resume["model"], "gpt-6-astra")
        self.assertEqual(resume["config"], {"model_reasoning_effort": "xhigh"})
        self.assertEqual(resume["approvalsReviewer"], "guardian_subagent")
        self.assertTrue(resume["excludeTurns"])
        start = eng.calls[1][1]
        self.assertEqual(start["input"], [{"type": "text", "text": "continue"}])
        self.assertEqual((start["model"], start["effort"]), ("gpt-6-astra", "xhigh"))

    def test_deliver_engine_turn_failed_usage_limit(self):
        eng = FakeEngine()
        watch = Watch({"id": "turn-9", "status": "failed", "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"}})
        out = deliver(self.cfg, eng, entry(), lambda tid: watch, self.logs.append)
        self.assertEqual((out.mode, out.ok, out.error_info), ("engine", False, "usageLimitExceeded"))
        self.assertEqual([m for m, _ in eng.calls][-1], "thread/unsubscribe")

    def test_deliver_engine_timeout(self):
        eng = FakeEngine()
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), self.logs.append)
        self.assertEqual((out.mode, out.ok), ("engine", False))
        self.assertIn("timed out", out.detail)

    def test_decline_approvals_config_omits_reviewer(self):
        eng = FakeEngine()
        cfg = Config(codex_bin="/bin/echo", approvals="decline")
        deliver(cfg, eng, entry(model=None, effort=None), lambda tid: Watch({"status": "completed"}), self.logs.append)
        self.assertNotIn("approvalsReviewer", eng.calls[0][1])
        self.assertNotIn("model", eng.calls[0][1])
        self.assertNotIn("config", eng.calls[0][1])

    def test_deliver_via_desktop_completed(self):
        eng = FakeEngine(resume_error="thread t1 already has an active writer")
        turns = iter([("old", "failed", 1, 2, "usageLimitExceeded"), ("new", "inProgress", 3, None, None),
                      ("new", "completed", 3, 4, None)])
        queued = []
        clock = [1000.0]
        def now(): return clock[0]
        def sleep(s): clock[0] += s
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), self.logs.append, sleep=sleep, now=now,
                      latest=lambda home, tid: next(turns), queue=lambda cfg, tid, msg: queued.append((tid, msg)) or "ok")
        self.assertEqual((out.mode, out.ok, out.turn_status), ("desktop", True, "completed"))
        self.assertEqual(queued, [("t1", "continue")])
        self.assertEqual([m for m, _ in eng.calls], ["thread/resume"])

    def test_deliver_pending_in_app(self):
        eng = FakeEngine(resume_error="thread t1 already has an active writer")
        queued = []
        clock = [1000.0]
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), self.logs.append,
                      sleep=lambda s: clock.__setitem__(0, clock[0] + s), now=lambda: clock[0],
                      latest=lambda home, tid: ("old", "failed", 1, 2, "usageLimitExceeded"),
                      queue=lambda cfg, tid, msg: queued.append(msg) or "ok")
        self.assertEqual((out.mode, out.ok), ("pending-in-app", False))
        self.assertEqual(len(queued), 1)

    def test_deliver_other_resume_error(self):
        eng = FakeEngine(resume_error="thread not found")
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), self.logs.append)
        self.assertEqual(out.mode, "error")
        self.assertIn("thread not found", out.detail)

    def test_queue_message_passes_text_verbatim(self):
        seen = []
        def runner(cmd, **kw):
            seen.append(cmd); return type("R", (), {"returncode": 0, "stdout": "Queued message 1 for thread t.\n", "stderr": ""})()
        msg = 'say "hi" — ünï'
        self.assertIn("Queued", queue_message(self.cfg, "t", msg, runner=runner))
        self.assertEqual(seen[0], ["/bin/echo", "queue", "--thread=t", f"--message={msg}"])

    def test_queue_message_failure_raises(self):
        def runner(cmd, **kw):
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "no such thread"})()
        with self.assertRaises(RuntimeError):
            queue_message(self.cfg, "t", "x", runner=runner)


class QueuedThreadTest(unittest.TestCase):
    """When a message is already queued for the thread, resuming drains it; never also turn/start."""

    def setUp(self):
        self.cfg = Config(codex_bin="/bin/echo", codex_home="/nonexistent", desktop_wait_seconds=20)

    def test_resume_only_when_queue_has_message(self):
        eng = FakeEngine()
        watch = Watch({"id": "q-turn", "status": "completed"})
        out = deliver(self.cfg, eng, entry(), lambda tid: watch, lambda m: None, queued=lambda home, tid: 1)
        self.assertEqual((out.mode, out.ok), ("engine", True))
        self.assertEqual([m for m, _ in eng.calls], ["thread/resume", "thread/unsubscribe"])

    def test_desktop_path_does_not_queue_twice(self):
        eng = FakeEngine(resume_error="thread t1 already has an active writer")
        queued = []
        clock = [1000.0]
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), lambda m: None,
                      sleep=lambda s: clock.__setitem__(0, clock[0] + s), now=lambda: clock[0],
                      latest=lambda home, tid: ("old", "failed", 1, 2, "usageLimitExceeded"),
                      queue=lambda cfg, tid, msg: queued.append(msg) or "ok", queued=lambda home, tid: 1)
        self.assertEqual(out.mode, "pending-in-app")
        self.assertEqual(queued, [])
