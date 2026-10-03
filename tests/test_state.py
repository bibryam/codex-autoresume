import tempfile, unittest
from pathlib import Path
from codex_resume.state import State

class StateTest(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            st = State(p)
            self.assertFalse(st.thread("a")["skip"])
            st.set_skip("a", True); st.set_message("a", "go on", model="m", effort="high")
            st.record_attempt("a", 100); st.record_attempt("a", 200)
            st.add_history(5, "a", "queued", "x")
            st2 = State(p)
            self.assertTrue(st2.thread("a")["skip"])
            self.assertEqual(st2.thread("a")["message"], "go on")
            self.assertEqual((st2.thread("a")["model"], st2.thread("a")["effort"]), ("m", "high"))
            self.assertEqual(st2.attempts_since("a", 150), 1)
            self.assertEqual(st2.attempts_since("a", 0), 2)
            self.assertEqual(st2.history()[0]["event"], "queued")

    def test_history_cap_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d) / "s.json")
            for i in range(250):
                st.add_history(i, "t", f"e{i}")
            h = st.history(5)
            self.assertEqual([x["event"] for x in h], ["e249", "e248", "e247", "e246", "e245"])
            self.assertEqual(len(st.data["history"]), 200)

    def test_flags(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d) / "s.json")
            st.set_resume_now("t", True); st.set_pending_in_app("t", 9)
            self.assertTrue(st.thread("t")["resume_now"]); self.assertEqual(st.thread("t")["pending_in_app_at"], 9)
            st.set_resume_now("t", False); st.set_pending_in_app("t", None)
            self.assertFalse(st.thread("t")["resume_now"]); self.assertIsNone(st.thread("t")["pending_in_app_at"])

    def test_corrupt_file_starts_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"; p.write_text("{not json")
            self.assertEqual(State(p).history(), [])
