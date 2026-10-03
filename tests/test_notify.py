import unittest
from codex_autoresume.notify import notify

class NotifyTest(unittest.TestCase):
    def test_builds_osascript_command(self):
        calls = []
        def runner(cmd, **kw):
            calls.append(cmd); return type("R", (), {"returncode": 0})()
        self.assertTrue(notify("codex-autoresume", 'He said "hi" – ünïcode', runner=runner))
        self.assertEqual(calls[0][:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('display notification "He said \\"hi\\" – ünïcode" with title "codex-autoresume"', calls[0][2])

    def test_failure_is_swallowed(self):
        def runner(cmd, **kw):
            raise OSError("no osascript")
        self.assertFalse(notify("t", "x", runner=runner))
