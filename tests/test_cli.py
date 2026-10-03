import unittest
from codex_autoresume.__main__ import build_plist, format_status

class CliTest(unittest.TestCase):
    def test_plist_contents(self):
        p = build_plist("/usr/bin/python3", "/work", "/data")
        self.assertIn("<string>/usr/bin/python3</string>", p)
        self.assertIn("<string>codex_autoresume</string>", p)
        self.assertIn("<key>WorkingDirectory</key>\n    <string>/work</string>", p)
        self.assertIn("/data/daemon.log", p)
        self.assertIn("<key>KeepAlive</key>", p)

    def test_plist_carries_install_time_path(self):
        p = build_plist("/usr/bin/python3", "/work", "/data", path="/Users/me/.nvm/bin:/opt/homebrew/bin")
        self.assertIn("<string>/Users/me/.nvm/bin:/opt/homebrew/bin</string>", p)

    def test_format_status(self):
        snap = {"now": 100, "engine": "ok", "note": "", "limits": {"primary": {"used_percent": 100, "resets_at": 160, "window_minutes": 300}, "secondary": {"used_percent": 20, "resets_at": 900, "window_minutes": 10080}, "allowed": False, "plan": "plus", "fetched_at": 100},
                "entries": [{"thread_id": "01a0fe2e-7ce2-abc", "cwd": "/p/cleverplayhouse", "title": "T", "last_user_message": "make it so", "blocked_at": 50, "resume_at": 220, "state": "scheduled", "position": 1, "message": "continue", "model": "m", "effort": "e", "attempts": 0, "reason": "after reset"}],
                "running": None, "history": [], "log": [], "config": {}}
        out = format_status(snap)
        self.assertIn("5h window 100%", out)
        self.assertIn("cleverplayhouse", out)
        self.assertIn("scheduled #1", out)


class SignalTest(unittest.TestCase):
    def test_sigterm_raises_keyboard_interrupt(self):
        import os, signal
        from codex_autoresume.__main__ import install_signal_handlers
        install_signal_handlers()
        with self.assertRaises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
            import time; time.sleep(0.2)


class InstallPathTest(unittest.TestCase):
    def test_install_path_keeps_user_dirs_and_drops_temp_shims(self):
        from codex_autoresume.__main__ import install_path
        env = "/var/folders/zz/T/cmux-cli-shims/ABC:/Users/me/.local/bin:/opt/homebrew/bin:/private/tmp/x:/usr/bin:/Users/me/.nvm/versions/node/v22/bin"
        got = install_path(env)
        self.assertEqual(got.split(":")[:2], ["/Users/me/.local/bin", "/Users/me/.nvm/versions/node/v22/bin"])
        self.assertNotIn("/var/folders", got)
        self.assertNotIn("/private/tmp", got)
        self.assertTrue(got.endswith("/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"))
