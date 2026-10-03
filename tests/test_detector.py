import json, sqlite3, tempfile, unittest
from pathlib import Path
from codex_resume.detector import find_blocked, latest_turn

ERR = json.dumps({"message": "You've hit your usage limit. ... try again at 1:56 PM.", "codexErrorInfo": "usageLimitExceeded"})


def make_home(d: str) -> str:
    s = sqlite3.connect(Path(d) / "state_5.sqlite")
    s.execute("""create table threads (id text primary key, cwd text, title text, first_user_message text, model text,
                 reasoning_effort text, source text, thread_source text, archived int default 0, updated_at int)""")
    h = sqlite3.connect(Path(d) / "thread_history_1.sqlite")
    h.execute("create table thread_turns (thread_id text, turn_id text, rollout_ordinal int, status text, error_json text, started_at int, completed_at int)")
    h.execute("create table thread_items (thread_id text, turn_id text, rollout_ordinal int, item_type text, item_json text)")
    s.commit(); h.commit()
    return d


def add_thread(d, tid, turns, source="vscode", tsrc="user", archived=0, title="T", updated=100):
    s = sqlite3.connect(Path(d) / "state_5.sqlite")
    s.execute("insert into threads values (?,?,?,?,?,?,?,?,?,?)", (tid, "/p/proj", title, "first", "gpt-6-astra", "xhigh", source, tsrc, archived, updated))
    s.commit()
    h = sqlite3.connect(Path(d) / "thread_history_1.sqlite")
    for i, (status, err, user) in enumerate(turns, start=1):
        h.execute("insert into thread_turns values (?,?,?,?,?,?,?)", (tid, f"{tid}-turn{i}", i * 10, status, err, 1000 + i, 2000 + i))
        if user is not None:
            h.execute("insert into thread_items values (?,?,?,?,?)", (tid, f"{tid}-turn{i}", i * 10 + 1, "userMessage",
                      json.dumps({"type": "userMessage", "content": [{"type": "text", "text": user}]})))
    h.commit()


class DetectorTest(unittest.TestCase):
    def test_finds_blocked_main_thread_only(self):
        with tempfile.TemporaryDirectory() as d:
            make_home(d)
            add_thread(d, "main-blocked", [("completed", None, "do X"), ("failed", ERR, "continue")])
            add_thread(d, "main-ok", [("completed", None, "fine")])
            add_thread(d, "sub-blocked", [("failed", ERR, "judge")], source='{"subagent":{"other":"guardian"}}', tsrc="guardian_review")
            add_thread(d, "archived", [("failed", ERR, "x")], archived=1)
            add_thread(d, "other-fail", [("failed", json.dumps({"message": "boom", "codexErrorInfo": "internalServerError"}), "x")])
            got = find_blocked(d)
            self.assertEqual([b.thread_id for b in got], ["main-blocked"])
            b = got[0]
            self.assertEqual(b.last_user_message, "continue")
            self.assertEqual(b.blocked_at, 2002)
            self.assertIn("1:56 PM", b.reset_hint)
            self.assertEqual((b.model, b.effort, b.cwd, b.title), ("gpt-6-astra", "xhigh", "/p/proj", "T"))
            self.assertEqual(b.last_turn_id, "main-blocked-turn2")
            self.assertEqual(b.to_dict()["thread_id"], "main-blocked")

    def test_ignores_thread_with_later_successful_turn(self):
        with tempfile.TemporaryDirectory() as d:
            make_home(d)
            add_thread(d, "t", [("failed", ERR, "a"), ("completed", None, "continue")])
            self.assertEqual(find_blocked(d), [])

    def test_in_progress_turn_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            make_home(d)
            add_thread(d, "t", [("failed", ERR, "a"), ("inProgress", None, "continue")])
            self.assertEqual(find_blocked(d), [])

    def test_latest_turn(self):
        with tempfile.TemporaryDirectory() as d:
            make_home(d)
            add_thread(d, "t", [("completed", None, "a"), ("failed", ERR, "b")])
            self.assertEqual(latest_turn(d, "t"), ("t-turn2", "failed", 1002, 2002, "usageLimitExceeded"))
            self.assertIsNone(latest_turn(d, "nope"))

    def test_missing_databases(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(find_blocked(d), [])
            self.assertIsNone(latest_turn(d, "t"))


class QueuedCountTest(unittest.TestCase):
    def test_counts_queue_rows_for_thread(self):
        from codex_resume.detector import queued_count
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(queued_count(d, "t"), 0)  # no db yet
            q = sqlite3.connect(Path(d) / "queue_1.sqlite")
            q.execute("create table queued_items (id text primary key, thread_id text, payload_json text, queue_order int, created_at_ms int, updated_at_ms int)")
            q.execute("insert into queued_items values ('1','t','{}',0,1,1)")
            q.execute("insert into queued_items values ('2','t','{}',1,1,1)")
            q.execute("insert into queued_items values ('3','u','{}',0,1,1)")
            q.commit()
            self.assertEqual(queued_count(d, "t"), 2)
            self.assertEqual(queued_count(d, "zzz"), 0)
