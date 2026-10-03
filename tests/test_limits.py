import unittest
from datetime import datetime, timezone, timedelta
from codex_resume.limits import parse_limits, parse_reset_hint, Limits

SAMPLE = {
    "ordinaryUsageAllowed": False,
    "rateLimits": {
        "planType": "plus",
        "primary": {"usedPercent": 100, "resetsAt": 1791032178, "windowDurationMins": 300},
        "secondary": {"usedPercent": 20, "resetsAt": 1791581316, "windowDurationMins": 10080},
        "rateLimitReachedType": {"type": "rate_limit_reached"},
    },
}

class LimitsTest(unittest.TestCase):
    def test_parse_limits(self):
        lim = parse_limits(SAMPLE, now=1791018000)
        self.assertFalse(lim.allowed)
        self.assertEqual(lim.primary.used_percent, 100)
        self.assertEqual(lim.primary.resets_at, 1791032178)
        self.assertEqual(lim.secondary.window_minutes, 10080)
        self.assertEqual(lim.plan, "plus")
        self.assertEqual(lim.to_dict()["primary"]["resets_at"], 1791032178)

    def test_parse_limits_from_notification_without_allowed_flag(self):
        res = {"rateLimits": {"primary": {"usedPercent": 10, "resetsAt": 5}, "rateLimitReachedType": None}}
        self.assertTrue(parse_limits(res, now=1).allowed)
        res["rateLimits"]["rateLimitReachedType"] = {"type": "rate_limit_reached"}
        self.assertFalse(parse_limits(res, now=1).allowed)

    def test_parse_limits_missing_windows(self):
        lim = parse_limits({}, now=7)
        self.assertIsNone(lim.primary)
        self.assertIsNone(lim.allowed)
        self.assertEqual(lim.fetched_at, 7)

    def test_reset_hint_same_day(self):
        tz = timezone(timedelta(hours=1))
        ref = int(datetime(2026, 10, 3, 9, 30, tzinfo=tz).timestamp())
        msg = "You've hit your usage limit. ... or try again at 1:56 PM."
        got = parse_reset_hint(msg, ref, tz=tz)
        self.assertEqual(got, int(datetime(2026, 10, 3, 13, 56, tzinfo=tz).timestamp()))

    def test_reset_hint_rolls_to_next_day(self):
        tz = timezone(timedelta(hours=1))
        ref = int(datetime(2026, 10, 3, 22, 30, tzinfo=tz).timestamp())
        got = parse_reset_hint("try again at 3:30 AM.", ref, tz=tz)
        self.assertEqual(got, int(datetime(2026, 10, 4, 3, 30, tzinfo=tz).timestamp()))

    def test_reset_hint_absent(self):
        self.assertIsNone(parse_reset_hint("some other error", 0))
        self.assertIsNone(parse_reset_hint("", 0))
