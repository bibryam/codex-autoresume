import http.client, json, threading, unittest
from codex_resume.web import make_server


class FakeScheduler:
    def __init__(self):
        self.calls = []
    def snapshot(self):
        return {"now": 1, "engine": "ok", "limits": None, "note": "", "entries": [], "running": None, "history": [], "log": [], "config": {}}
    def set_skip(self, tid, flag): self.calls.append(("skip", tid, flag))
    def set_message(self, tid, message, model=None, effort=None): self.calls.append(("message", tid, message, model, effort))
    def resume_now(self, tid): self.calls.append(("now", tid))


class WebTest(unittest.TestCase):
    def setUp(self):
        self.sched = FakeScheduler()
        self.server = make_server(self.sched, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, r.getheader("Content-Type", ""), data

    def test_page_and_state(self):
        st, ct, data = self.req("GET", "/")
        self.assertEqual(st, 200); self.assertIn("text/html", ct); self.assertIn(b"codex-resume", data)
        st, ct, data = self.req("GET", "/api/state")
        self.assertEqual(st, 200); self.assertEqual(json.loads(data)["engine"], "ok")

    def test_actions(self):
        self.assertEqual(self.req("POST", "/api/threads/t1/skip")[0], 200)
        self.assertEqual(self.req("POST", "/api/threads/t1/unskip")[0], 200)
        self.assertEqual(self.req("POST", "/api/threads/t1/resume-now")[0], 200)
        self.assertEqual(self.sched.calls, [("skip", "t1", True), ("skip", "t1", False), ("now", "t1")])

    def test_post_message_roundtrip(self):
        msg = 'say "hi" — ünï'
        st, _, data = self.req("POST", "/api/threads/t1/message", {"message": msg, "model": "gpt-6-astra", "effort": "xhigh"})
        self.assertEqual(st, 200); self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(self.sched.calls[-1], ("message", "t1", msg, "gpt-6-astra", "xhigh"))

    def test_unknown_routes(self):
        self.assertEqual(self.req("GET", "/nope")[0], 404)
        self.assertEqual(self.req("POST", "/api/threads/t1/explode")[0], 404)
        self.assertEqual(self.req("POST", "/api/threads/t1/message", None)[0], 400)
