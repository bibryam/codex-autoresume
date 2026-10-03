import tempfile, unittest
from pathlib import Path
from codex_resume.config import Config
from codex_resume.detector import Blocked
from codex_resume.limits import Limits, Window
from codex_resume.planner import build_plan
from codex_resume.state import State

NOW = 1_000_000

def blocked(tid, at, hint="try again at 1:56 PM."):
    return Blocked(tid, "/p/x", "title " + tid, "last", at, hint, "gpt-6-astra", "xhigh", "vscode", tid + "-t")

def limits(allowed, p_reset=NOW + 3600, weekly=20):
    return Limits(Window(100 if not allowed else 10, p_reset, 300), Window(weekly, NOW + 99999, 10080), allowed, "plus", NOW)


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "s.json")
        self.cfg = Config(grace_seconds=60, weekly_guard=90, max_attempts=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_allowed_schedules_after_reset_in_blocked_order(self):
        plan = build_plan([blocked("b", 200), blocked("a", 100)], limits(False), self.state, self.cfg, NOW)
        self.assertEqual([e.thread_id for e in plan.entries], ["a", "b"])
        self.assertEqual([e.position for e in plan.entries], [1, 2])
        self.assertEqual(plan.entries[0].resume_at, NOW + 3600 + 60)
        self.assertEqual(plan.entries[0].state, "scheduled")
        self.assertEqual(plan.entries[0].message, "continue")
        self.assertIsNone(plan.next_action)

    def test_allowed_and_due_picks_first(self):
        plan = build_plan([blocked("a", 100), blocked("b", 200)], limits(True), self.state, self.cfg, NOW)
        self.assertEqual(plan.next_action.thread_id, "a")
        self.assertEqual(plan.entries[0].resume_at, NOW)

    def test_running_blocks_next_action(self):
        plan = build_plan([blocked("a", 100), blocked("b", 200)], limits(True), self.state, self.cfg, NOW, running="a")
        self.assertEqual(plan.entries[0].state, "running")
        self.assertIsNone(plan.next_action)
        self.assertEqual(plan.entries[1].position, 1)

    def test_skip_weekly_guard_and_max_attempts(self):
        self.state.set_skip("s", True)
        for ts in (NOW - 100, NOW - 200, NOW - 300):
            self.state.record_attempt("m", ts)
        plan = build_plan([blocked("s", 1), blocked("m", 2), blocked("w", 3)], limits(True, weekly=95), self.state, self.cfg, NOW)
        by = {e.thread_id: e for e in plan.entries}
        self.assertEqual(by["s"].state, "skipped")
        self.assertEqual(by["m"].state, "max-attempts")
        self.assertEqual(by["w"].state, "weekly-guard")
        self.assertIsNone(plan.next_action)

    def test_resume_now_bypasses_guards_and_jumps_queue(self):
        for ts in (NOW - 100, NOW - 200, NOW - 300):
            self.state.record_attempt("m", ts)
        self.state.set_resume_now("m", True)
        plan = build_plan([blocked("a", 1), blocked("m", 2)], limits(False, weekly=95), self.state, self.cfg, NOW)
        self.assertEqual(plan.next_action.thread_id, "m")
        self.assertEqual(plan.entries[0].thread_id, "m")

    def test_plan_without_limits_uses_hint(self):
        import datetime as dt
        at = int(dt.datetime(2026, 10, 3, 9, 30).timestamp())
        now = at + 60
        plan = build_plan([blocked("a", at)], None, self.state, self.cfg, now)
        self.assertEqual(plan.entries[0].resume_at, int(dt.datetime(2026, 10, 3, 13, 56).timestamp()) + 60)
        self.assertIsNone(plan.next_action)
        self.assertIn("limits unavailable", plan.note)

    def test_reblocked_thread_waits_for_new_reset(self):
        plan = build_plan([blocked("a", NOW - 10)], limits(False, p_reset=NOW + 18000), self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].resume_at, NOW + 18000 + 60)
        self.assertIsNone(plan.next_action)

    def test_pending_in_app_not_rescheduled(self):
        self.state.set_pending_in_app("a", NOW - 5)
        plan = build_plan([blocked("a", 1)], limits(True), self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].state, "pending-in-app")
        self.assertIsNone(plan.next_action)

    def test_custom_message_model_effort(self):
        self.state.set_message("a", "carry on", model="gpt-x", effort="low")
        e = build_plan([blocked("a", 1)], limits(True), self.state, self.cfg, NOW).entries[0]
        self.assertEqual((e.message, e.model, e.effort), ("carry on", "gpt-x", "low"))
        self.assertEqual(e.to_dict()["message"], "carry on")


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "s.json")
        self.cfg = Config(grace_seconds=60, retry_cooldown_seconds=600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recently_attempted_thread_in_cooldown(self):
        # we resumed at NOW-30 but the detector still shows the old failure (blocked_at older than the attempt)
        self.state.record_attempt("a", NOW - 30)
        plan = build_plan([blocked("a", NOW - 100), blocked("b", NOW - 50)], limits(True), self.state, self.cfg, NOW)
        by = {e.thread_id: e for e in plan.entries}
        self.assertEqual(by["a"].state, "cooldown")
        self.assertEqual(plan.next_action.thread_id, "b")

    def test_cooldown_expires(self):
        self.state.record_attempt("a", NOW - 601)
        plan = build_plan([blocked("a", NOW - 1000)], limits(True), self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].state, "scheduled")
        self.assertEqual(plan.next_action.thread_id, "a")



class AutomationTest(unittest.TestCase):
    def test_heartbeat_automation_thread_is_not_auto_resumed(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(Path(d) / "s.json")
            hb = Blocked("h", "/p/x", "t", "continue", 5, None, None, None, "vscode", "h-t", is_automation=True)
            plan = build_plan([hb, blocked("a", 10)], limits(True), state, Config(), NOW)
            by = {e.thread_id: e for e in plan.entries}
            self.assertEqual(by["h"].state, "automation")
            self.assertEqual(plan.next_action.thread_id, "a")
            state.set_resume_now("h", True)
            plan = build_plan([hb], limits(True), state, Config(), NOW)
            self.assertEqual(plan.next_action.thread_id, "h")


class ReviewFixesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "s.json")
        self.cfg = Config(grace_seconds=60, retry_cooldown_seconds=600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_c2_reset_passed_but_flag_still_false_delivers_when_limits_are_fresh(self):
        lim = Limits(Window(100, NOW - 120, 300), Window(20, NOW + 99999, 10080), False, "plus", fetched_at=NOW - 10)
        plan = build_plan([blocked("a", NOW - 5000)], lim, self.state, self.cfg, NOW)
        self.assertEqual(plan.next_action.thread_id, "a")

    def test_c2_stale_limits_do_not_deliver_and_note_explains(self):
        lim = Limits(Window(100, NOW - 120, 300), Window(20, NOW + 99999, 10080), False, "plus", fetched_at=NOW - 400)
        plan = build_plan([blocked("a", NOW - 5000)], lim, self.state, self.cfg, NOW)
        self.assertIsNone(plan.next_action)
        self.assertIn("not allowed", plan.note)

    def test_i3_failed_again_waits_for_a_fresh_window(self):
        # we attempted at NOW-700, it failed again at NOW-650; limits still say allowed with the same window
        self.state.record_attempt("a", NOW - 700)
        same_window = Limits(Window(100, NOW + 3600, 300), Window(20, NOW + 99999, 10080), True, "plus", NOW)
        plan = build_plan([blocked("a", NOW - 650)], same_window, self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].state, "waiting-reset")
        self.assertIsNone(plan.next_action)
        # a window that started after the failure clears the hold
        fresh = Limits(Window(1, NOW - 600 + 18000, 300), Window(20, NOW + 99999, 10080), True, "plus", NOW)
        plan = build_plan([blocked("a", NOW - 650)], fresh, self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].state, "scheduled")
        self.assertEqual(plan.next_action.thread_id, "a")

    def test_i3_any_recent_attempt_is_cooldown(self):
        self.state.record_attempt("a", NOW - 30)
        plan = build_plan([blocked("a", NOW - 10)], limits(True), self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].state, "cooldown")


class ScheduleAnywayTest(unittest.TestCase):
    """'Schedule' on a parked row: include it in the normal queue despite automation/guards, still waiting for the window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "s.json")
        self.cfg = Config(grace_seconds=60, max_attempts=3, weekly_guard=90)

    def tearDown(self):
        self.tmp.cleanup()

    def test_force_overrides_automation_and_guards_but_not_the_window(self):
        auto = Blocked("h", "/p/x", "t", "continue", 5, None, None, None, "vscode", "h-t", is_automation=True)
        for ts in (NOW - 2000, NOW - 3000, NOW - 4000):  # 3 attempts today, outside the 10-min cooldown
            self.state.record_attempt("m", ts)
        self.state.set_force("h", True); self.state.set_force("m", True)
        plan = build_plan([auto, blocked("m", 6)], limits(False, weekly=95), self.state, self.cfg, NOW)
        self.assertEqual([e.state for e in plan.entries], ["scheduled", "scheduled"])
        self.assertEqual(plan.entries[0].resume_at, NOW + 3600 + 60)
        self.assertIsNone(plan.next_action)  # window still closed

    def test_skip_wins_over_force(self):
        self.state.set_force("a", True); self.state.set_skip("a", True)
        plan = build_plan([blocked("a", 1)], limits(True), self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].state, "skipped")
