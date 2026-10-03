import json, tempfile, unittest
from pathlib import Path
from codex_autoresume.config import Config

class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.port, 8765)
        self.assertEqual(cfg.weekly_guard, 90)
        self.assertEqual(cfg.default_message, "continue")

    def test_load_overrides_known_keys_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"port": 9000, "bogus": 1, "weekly_guard": 50}))
            cfg = Config.load(str(p))
            self.assertEqual(cfg.port, 9000)
            self.assertEqual(cfg.weekly_guard, 50)
            self.assertFalse(hasattr(cfg, "bogus"))
