import os, sys, time, unittest
from codex_autoresume.engine import Engine, EngineError

FAKE = os.path.join(os.path.dirname(__file__), "fake_codex.py")


class FakeBinEngine(Engine):
    def _command(self):
        return [sys.executable, FAKE]


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.notes = []
        self.eng = FakeBinEngine("unused", on_notification=lambda m, p: self.notes.append((m, p)))
        self.eng.start()

    def tearDown(self):
        self.eng.stop()

    def test_call_roundtrip(self):
        self.assertEqual(self.eng.call("echo", {"a": 1}), {"a": 1})
        self.assertTrue(self.eng.alive())

    def test_error_raises(self):
        with self.assertRaises(EngineError) as cm:
            self.eng.call("boom", {})
        self.assertIn("active writer", cm.exception.message)
        self.assertEqual(cm.exception.code, -32600)

    def test_notification_dispatch(self):
        self.eng.call("emit", {})
        self.eng.call("echo", {})  # ordering barrier
        self.assertIn(("turn/completed", {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}}), self.notes)

    def test_server_request_declined_by_default(self):
        res = self.eng.call("ask", {})
        self.assertEqual(res["gotDecision"], "decline")

    def test_server_request_custom_handler(self):
        eng = FakeBinEngine("unused", on_server_request=lambda m, p: ({"decision": "accept"}, None))
        eng.start()
        try:
            self.assertEqual(eng.call("ask", {})["gotDecision"], "accept")
        finally:
            eng.stop()

    def test_call_after_exit_raises(self):
        self.eng.notify("exit", {})
        time.sleep(0.3)
        with self.assertRaises(EngineError):
            self.eng.call("echo", {}, timeout=1)


class EngineExitTest(unittest.TestCase):
    def test_on_exit_called_when_process_ends(self):
        exits = []
        eng = FakeBinEngine("unused", on_exit=lambda: exits.append(True))
        eng.start()
        eng.notify("exit", {})
        time.sleep(0.5)
        self.assertEqual(exits, [True])
        eng.stop()
